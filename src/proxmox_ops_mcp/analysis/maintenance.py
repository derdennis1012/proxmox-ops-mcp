"""Node evacuation planning: can this node be drained for maintenance, and how?

The planner is deliberately conservative. It reserves each running guest's *configured*
maximum memory on the target (not its current usage), refuses targets above a memory
limit, and treats anything it cannot prove migratable as a blocker.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from proxmox_ops_mcp.analysis._util import as_float, as_int, gib, items, pct, ratio

_QEMU_DISK_RE = re.compile(r"^(?:scsi|virtio|sata|ide|efidisk|tpmstate)\d+$")
_LXC_VOLUME_RE = re.compile(r"^(?:rootfs|mp\d+)$")
_PASSTHROUGH_RE = re.compile(r"^(?:hostpci|usb)\d+$")
_LXC_DEVICE_RE = re.compile(r"^dev\d+$")


class MigrationMode(StrEnum):
    """How a guest would be moved."""

    LIVE = "live"
    """Online migration, no downtime."""

    LIVE_WITH_LOCAL_DISKS = "live-with-local-disks"
    """Online, but local disks are copied over the network (slow)."""

    RESTART = "restart"
    """Container restart migration, short downtime."""

    OFFLINE = "offline"
    """Guest is stopped; only its configuration and volumes move."""

    HA_MANAGED = "ha-managed"
    """Moved by the HA stack when the node enters maintenance mode."""


class GuestMovePlan(BaseModel):
    """Plan for one guest on the node being drained."""

    vmid: int
    name: str | None
    type: str
    running: bool
    memory_gib: float
    target: str | None
    mode: MigrationMode | None
    blockers: list[str]
    warnings: list[str]


class TargetLoad(BaseModel):
    """Projected load of a target node after the evacuation."""

    node: str
    memory_pct_before: float
    memory_pct_after: float
    guests_added: int


class EvacuationPlan(BaseModel):
    """Result of :func:`plan_evacuation`."""

    node: str
    feasible: bool
    summary: str
    guests: list[GuestMovePlan]
    targets: list[TargetLoad]
    steps: list[str]


def plan_evacuation(
    *,
    node: str,
    resources: Any,
    guest_configs: Mapping[int, Mapping[str, Any]],
    ha_resources: Any = None,
    ceph_in_use: bool = False,
    memory_limit: float = 0.85,
    exclude_targets: frozenset[str] = frozenset(),
) -> EvacuationPlan:
    """Build a migration plan to empty ``node``.

    Args:
        node: Node to drain.
        resources: ``/cluster/resources`` (all types).
        guest_configs: VM/CT configuration per vmid for every guest on ``node``.
        ha_resources: ``/cluster/ha/resources``.
        ceph_in_use: Whether the cluster runs Ceph (adds the ``noout`` step).
        memory_limit: Max memory utilisation a target may reach.
        exclude_targets: Nodes that must not receive guests.
    """
    entries = items(resources)
    shared = shared_storages(entries)
    available: dict[str, set[str]] = {}
    for s in entries:
        if s.get("type") == "storage" and s.get("status") in (None, "available"):
            available.setdefault(str(s.get("node")), set()).add(str(s.get("storage")))
    ha_managed = {
        str(r.get("sid"))
        for r in items(ha_resources)
        if r.get("state") not in {"ignored", "disabled"}
    }

    targets = {
        str(n.get("node")): n
        for n in entries
        if n.get("type") == "node"
        and n.get("status") == "online"
        and n.get("node") != node
        and str(n.get("node")) not in exclude_targets
    }
    reserved = {name: as_float(t.get("mem")) for name, t in targets.items()}
    added = dict.fromkeys(targets, 0)

    guests = [g for g in entries if g.get("type") in {"qemu", "lxc"} and g.get("node") == node]
    # Biggest first: classic first-fit-decreasing bin packing.
    guests.sort(key=lambda g: (g.get("status") != "running", -as_float(g.get("maxmem"))))

    plans: list[GuestMovePlan] = []
    for g in guests:
        vmid = as_int(g.get("vmid"))
        kind = str(g.get("type"))
        running = g.get("status") == "running"
        mem = as_float(g.get("maxmem"))
        inspection = inspect_guest_config(kind, guest_configs.get(vmid), shared)
        blockers, warnings = list(inspection.blockers), list(inspection.warnings)
        local_disks = inspection.local_volumes
        sid = f"{'ct' if kind == 'lxc' else 'vm'}:{vmid}"

        mode = _migration_mode(
            kind, running, ha=sid in ha_managed, local_disks=local_disks,
            blockers=blockers, warnings=warnings,
        )  # fmt: skip

        target: str | None = None
        if not blockers:
            needed = set(local_disks.values())
            candidates = {n: t for n, t in targets.items() if needed <= available.get(n, set())}
            target = _choose_target(candidates, reserved, mem if running else 0.0, memory_limit)
            if not targets:
                blockers.append("no other online node is available as a target")
            elif not candidates:
                blockers.append(
                    "no target node provides the local storage(s) " + ", ".join(sorted(needed))
                )
            elif target is None:
                blockers.append("no target node has enough memory headroom")
            else:
                reserved[target] += mem if running else 0.0
                added[target] += 1

        plans.append(
            GuestMovePlan(
                vmid=vmid,
                name=str(g["name"]) if g.get("name") else None,
                type=kind,
                running=running,
                memory_gib=gib(mem),
                target=target,
                mode=mode if not blockers else None,
                blockers=blockers,
                warnings=warnings,
            )
        )

    blocked = [p for p in plans if p.blockers]
    feasible = bool(targets) and not blocked
    loads = [
        TargetLoad(
            node=name,
            memory_pct_before=pct(ratio(t.get("mem"), t.get("maxmem"))),
            memory_pct_after=pct(ratio(reserved[name], t.get("maxmem"))),
            guests_added=added[name],
        )
        for name, t in sorted(targets.items())
    ]
    return EvacuationPlan(
        node=node,
        feasible=feasible,
        summary=_summary(node, plans, blocked, targets),
        guests=plans,
        targets=loads,
        steps=_steps(node, plans, ceph_in_use) if feasible else [],
    )


class ConfigInspection(BaseModel):
    """Migration-relevant facts extracted from a guest configuration."""

    blockers: list[str]
    warnings: list[str]
    local_volumes: dict[str, str]
    """Config key -> storage id for every volume on non-shared storage."""


def shared_storages(resources: Any) -> set[str]:
    """Storage ids flagged as shared in ``/cluster/resources``."""
    return {
        str(s.get("storage"))
        for s in items(resources)
        if s.get("type") == "storage" and s.get("shared")
    }


def inspect_guest_config(
    kind: str, config: Mapping[str, Any] | None, shared: set[str]
) -> ConfigInspection:
    """Find what prevents or complicates migrating a guest with this configuration."""
    blockers: list[str] = []
    warnings: list[str] = []
    local: dict[str, str] = {}
    if config is None:
        blockers.append("configuration could not be read; migratability unknown")
        return ConfigInspection(blockers=blockers, warnings=warnings, local_volumes=local)

    for key, raw in config.items():
        value = str(raw)
        if kind == "lxc" and _LXC_DEVICE_RE.match(key):
            blockers.append(f"{key}: device passthrough ({value.split(',', 1)[0]})")
            continue
        if kind == "qemu" and _PASSTHROUGH_RE.match(key) and not value.startswith("spice"):
            blockers.append(f"{key}: host device passthrough ({value.split(',', 1)[0]})")
            continue
        is_volume = (kind == "qemu" and _QEMU_DISK_RE.match(key)) or (
            kind == "lxc" and _LXC_VOLUME_RE.match(key)
        )
        if not is_volume or value.startswith("none"):
            continue
        volume = value.split(",", 1)[0]
        if volume.startswith("/"):
            blockers.append(f"{key}: bind mount or raw device '{volume}'")
            continue
        storage = volume.split(":", 1)[0]
        # Cloud-init drives are attached as cdrom but are real volumes that must move.
        if "media=cdrom" in value and "cloudinit" not in volume:
            if storage not in shared:
                warnings.append(f"{key}: ISO on local storage '{storage}'; eject before migrating")
            continue
        if storage not in shared:
            local[key] = storage
    if config.get("lock"):
        blockers.append(f"guest is locked ({config.get('lock')})")
    return ConfigInspection(blockers=blockers, warnings=warnings, local_volumes=local)


def _migration_mode(
    kind: str,
    running: bool,
    *,
    ha: bool,
    local_disks: Mapping[str, str],
    blockers: list[str],
    warnings: list[str],
) -> MigrationMode:
    """Pick how a guest moves; appends any blockers and warnings that come with it."""
    restart = "containers cannot live-migrate; expect a short restart"
    if ha:
        if local_disks:
            blockers.append("HA-managed guest with local disks cannot be recovered or moved")
        warnings.append(
            "HA chooses the actual target from its group rules; capacity is reserved on the "
            "planned node as an estimate"
        )
        if running and kind == "lxc":
            warnings.append(restart)
        return MigrationMode.HA_MANAGED
    if not running:
        return MigrationMode.OFFLINE
    if kind == "lxc":
        warnings.append(restart)
        return MigrationMode.RESTART
    if local_disks:
        warnings.append(f"local disks will be copied: {', '.join(_describe(local_disks))}")
        return MigrationMode.LIVE_WITH_LOCAL_DISKS
    return MigrationMode.LIVE


def _describe(local_volumes: Mapping[str, str]) -> list[str]:
    return [f"{key}@{storage}" for key, storage in sorted(local_volumes.items())]


def _choose_target(
    targets: Mapping[str, Mapping[str, Any]],
    reserved: Mapping[str, float],
    mem: float,
    limit: float,
) -> str | None:
    best, best_after = None, float("inf")
    for name, t in sorted(targets.items()):
        after = ratio(reserved[name] + mem, t.get("maxmem"))
        # A stopped guest reserves no memory, so it may go to any otherwise suitable node.
        if (mem == 0 or after <= limit) and after < best_after:
            best, best_after = name, after
    return best


def _summary(
    node: str,
    plans: list[GuestMovePlan],
    blocked: list[GuestMovePlan],
    targets: Mapping[str, Any],
) -> str:
    if not targets:
        return f"Cannot drain {node}: no other online node is available as a target."
    if not plans:
        return f"{node} has no guests; it can enter maintenance immediately."
    if blocked:
        ids = ", ".join(str(p.vmid) for p in blocked)
        return (
            f"{node} cannot be fully drained: {len(blocked)} of {len(plans)} guests are "
            f"blocked ({ids}). Resolve the blockers or accept downtime for them."
        )
    downtime = sum(p.running and p.type == "lxc" for p in plans)
    return f"{node} can be drained: {len(plans)} guests to {len(targets)} target nodes" + (
        f", {downtime} container(s) with a brief restart." if downtime else ", no downtime."
    )


def _steps(node: str, plans: list[GuestMovePlan], ceph_in_use: bool) -> list[str]:
    steps = []
    if ceph_in_use:
        steps.append("ceph osd set noout   # prevent rebalancing while the node is down")
    if any(p.mode == MigrationMode.HA_MANAGED for p in plans):
        steps.append(
            f"ha-manager crm-command node-maintenance enable {node}   "
            "# HA moves its guests automatically"
        )
    for p in plans:
        if p.mode in {None, MigrationMode.HA_MANAGED}:
            continue
        cmd = "qm" if p.type == "qemu" else "pct"
        flags = {
            MigrationMode.LIVE: " --online",
            MigrationMode.LIVE_WITH_LOCAL_DISKS: " --online --with-local-disks",
            MigrationMode.RESTART: " --restart",
            MigrationMode.OFFLINE: "",
        }[p.mode]
        steps.append(f"{cmd} migrate {p.vmid} {p.target}{flags}")
    steps.append(f"# perform maintenance on {node}, reboot, verify it rejoined the cluster")
    if any(p.mode == MigrationMode.HA_MANAGED for p in plans):
        steps.append(f"ha-manager crm-command node-maintenance disable {node}")
    if ceph_in_use:
        steps.append("ceph osd unset noout")
    return steps
