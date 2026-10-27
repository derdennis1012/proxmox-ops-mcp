"""Guarded write operations.

Every tool here is two-phase. Called without ``confirmation_token`` it only *previews*:
it validates the request against live cluster state, explains the effects and returns a
token. Called again with that token and identical arguments, it executes. Each phase is
audit-logged. There are intentionally no tools that delete guests, disks or snapshots.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import items
from proxmox_ops_mcp.analysis.maintenance import inspect_guest_config, shared_storages
from proxmox_ops_mcp.client import validate_guest_type, validate_node, validate_snapshot_name
from proxmox_ops_mcp.errors import (
    ConfirmationError,
    ProxmoxAPIError,
    ProxmoxOpsError,
    ValidationError,
)
from proxmox_ops_mcp.security import AccessTier
from proxmox_ops_mcp.tools._common import (
    ToolContext,
    cluster_resources,
    find_guest,
    guarded,
    is_ha_managed,
    live_state,
)

PowerAction = Literal["start", "shutdown", "reboot", "stop"]

_TOKEN_FIELD = Field(
    description="Omit to get a preview. To execute, pass the token from the preview "
    "unchanged together with the exact same arguments."
)


class OperationResult(BaseModel):
    """Outcome of a preview or an execution."""

    phase: Literal["preview", "executed"]
    action: str
    vmid: int
    node: str
    guest_name: str | None
    summary: str
    effects: list[str]
    warnings: list[str]
    confirmation_token: str | None = None
    token_expires_in_seconds: int | None = None
    task_upid: str | None = None
    next_step: str


def _run_two_phase(
    ctx: ToolContext,
    *,
    action: str,
    required: AccessTier,
    params: Mapping[str, Any],
    token: str | None,
    preview: OperationResult,
    execute: Callable[[], Any],
    guest: Mapping[str, Any],
) -> OperationResult:
    tier = str(ctx.tier)
    try:
        ctx.tier.require(required, action)
        ctx.policy.check_scope(guest)
    except ProxmoxOpsError as exc:
        ctx.audit.record(
            action=action, outcome="rejected", tier=tier, params=params, detail={"reason": str(exc)}
        )
        raise

    if token is None:
        issued = ctx.confirmations.issue(action, params)
        ctx.audit.record(action=action, outcome="previewed", tier=tier, params=params)
        return preview.model_copy(
            update={
                "confirmation_token": issued,
                "token_expires_in_seconds": ctx.confirmations.ttl_seconds,
                "next_step": "Show this preview to the user. Only if they approve, call the "
                "same tool again with identical arguments and this confirmation_token.",
            }
        )

    try:
        ctx.confirmations.redeem(token, action, params)
    except ConfirmationError as exc:
        ctx.audit.record(
            action=action, outcome="rejected", tier=tier, params=params, detail={"reason": str(exc)}
        )
        raise

    try:
        ctx.policy.acquire()
    except ProxmoxOpsError as exc:
        ctx.audit.record(
            action=action, outcome="rejected", tier=tier, params=params, detail={"reason": str(exc)}
        )
        raise

    try:
        upid = execute()
    except Exception as exc:
        # Record every failure, including unexpected ones, so no attempted change goes
        # unaccounted for. Unexpected errors are logged by type only.
        error = str(exc) if isinstance(exc, ProxmoxOpsError) else type(exc).__name__
        ctx.audit.record(
            action=action, outcome="failed", tier=tier, params=params, detail={"error": error}
        )
        raise
    ctx.audit.record(
        action=action, outcome="executed", tier=tier, params=params, detail={"upid": upid}
    )
    return preview.model_copy(
        update={
            "phase": "executed",
            "task_upid": str(upid) if upid else None,
            "next_step": "Track progress with get_task_status using this node and task_upid."
            if upid
            else "The request was handed to the HA stack, which runs it asynchronously. "
            "Verify the result with get_guest or cluster_health.",
        }
    )


def _check_power_preconditions(vmid: int, action: str, status: str, lock: str | None) -> None:
    """Reject power actions that cannot succeed in the guest's current state."""
    # Starting a hibernated VM is how it resumes; that lock is expected.
    if lock and not (action == "start" and lock == "suspended"):
        raise ValidationError(f"guest {vmid} is locked ({lock}); retry when that finishes")
    if status == "paused" and action != "stop":
        raise ValidationError(f"guest {vmid} is paused; only 'stop' is offered for paused guests")
    if action == "start" and status != "stopped":
        raise ValidationError(f"guest {vmid} cannot be started (status: {status})")
    if action in {"shutdown", "reboot"} and status != "running":
        raise ValidationError(f"guest {vmid} is not running (status: {status})")
    if action == "stop" and status not in {"running", "paused"}:
        raise ValidationError(f"guest {vmid} is not running (status: {status})")


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register write tools permitted by the configured tier."""
    if ctx.tier.allows(AccessTier.OPERATOR):
        _register_operator_tools(server, ctx)
    if ctx.tier.allows(AccessTier.ADMIN):
        _register_admin_tools(server, ctx)


def _register_operator_tools(server: MCPServer, ctx: ToolContext) -> None:
    @server.tool(
        title="Power action on a guest",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @guarded
    def guest_power(
        vmid: Annotated[int, Field(description="VM or container id.")],
        action: Annotated[
            PowerAction,
            Field(
                description="start | shutdown (graceful) | reboot (graceful) | "
                "stop (hard power-off, requires admin tier)."
            ),
        ],
        confirmation_token: Annotated[str | None, _TOKEN_FIELD] = None,
    ) -> OperationResult:
        """Start, gracefully shut down, reboot or hard-stop a VM/container. Two-phase."""
        guest = find_guest(ctx.client, vmid)
        node = validate_node(str(guest.get("node")))
        kind = validate_guest_type(str(guest.get("type")))
        status, lock = live_state(ctx.client, guest)
        _check_power_preconditions(vmid, action, status, lock)

        effects = {
            "start": ["Boots the guest."],
            "shutdown": [
                "Sends an ACPI/OS shutdown request and waits for the guest to power off cleanly."
            ],
            "reboot": ["Requests a clean OS reboot; services will be briefly unavailable."],
            "stop": [
                "Immediately powers off the guest, like pulling the plug.",
                "Unsaved data inside the guest is lost; filesystems may need a check.",
            ],
        }[action]
        warnings = []
        if is_ha_managed(guest):
            warnings.append(
                "Guest is HA-managed: the request is routed through the HA stack "
                "and HA will keep enforcing its requested state."
            )
        if action in {"shutdown", "reboot"} and kind == "qemu":
            config: Mapping[str, Any] = {}
            with contextlib.suppress(ProxmoxAPIError):  # advisory only
                config = ctx.client.get(f"nodes/{node}/qemu/{vmid}/config") or {}
            if str(config.get("agent", "0")).split(",", 1)[0] in {"1", "enabled=1"}:
                warnings.append(
                    "The QEMU guest agent is enabled in the config, so Proxmox will ask the "
                    "agent. If qemu-guest-agent is not running inside the guest, the task "
                    "fails after a timeout; fall back to stop (admin) in that case."
                )
            else:
                warnings.append(
                    "No guest agent configured: an ACPI request is sent, which the guest OS "
                    "may ignore."
                )

        params = {"vmid": vmid, "node": node, "action": action}
        preview = OperationResult(
            phase="preview",
            action=f"guest_power:{action}",
            vmid=vmid,
            node=node,
            guest_name=guest.get("name"),
            summary=f"{action} {kind} {vmid} ({guest.get('name') or 'unnamed'}) on {node}",
            effects=effects,
            warnings=warnings,
            next_step="",
        )
        return _run_two_phase(
            ctx,
            action=f"guest_power:{action}",
            required=AccessTier.ADMIN if action == "stop" else AccessTier.OPERATOR,
            params=params,
            token=confirmation_token,
            preview=preview,
            guest=guest,
            execute=lambda: ctx.client.post(f"nodes/{node}/{kind}/{vmid}/status/{action}"),
        )

    @server.tool(
        title="Create a snapshot",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @guarded
    def create_snapshot(
        vmid: Annotated[int, Field(description="VM or container id.")],
        name: Annotated[str, Field(description="Snapshot name: letter first, [A-Za-z0-9_-].")],
        description: Annotated[str, Field(max_length=1024)] = "",
        include_ram: Annotated[
            bool, Field(description="VMs only: also save RAM state (larger, slower).")
        ] = False,
        confirmation_token: Annotated[str | None, _TOKEN_FIELD] = None,
    ) -> OperationResult:
        """Create a snapshot of a VM/container, e.g. before an upgrade. Two-phase."""
        validate_snapshot_name(name)
        guest = find_guest(ctx.client, vmid)
        node = validate_node(str(guest.get("node")))
        kind = validate_guest_type(str(guest.get("type")))
        _, lock = live_state(ctx.client, guest)
        if lock:
            raise ValidationError(f"guest {vmid} is locked ({lock}); retry when that finishes")
        if include_ram and kind != "qemu":
            raise ValidationError("include_ram is only supported for VMs (qemu)")
        existing = {
            str(s.get("name"))
            for s in items(ctx.client.get(f"nodes/{node}/{kind}/{vmid}/snapshot"))
        }
        if name in existing:
            raise ValidationError(f"snapshot {name!r} already exists on guest {vmid}")

        snapshot_count = len(existing - {"current"})
        warnings = []
        if snapshot_count >= 5:
            warnings.append(
                f"Guest already has {snapshot_count} snapshots. Long snapshot "
                "chains cost performance and space; remove old ones."
            )
        warnings.append("Snapshots are not backups: they live on the same storage as the disks.")

        params = {
            "vmid": vmid,
            "node": node,
            "name": name,
            "description": description,
            "include_ram": include_ram,
        }
        payload: dict[str, Any] = {"snapname": name, "description": description}
        if include_ram:
            payload["vmstate"] = 1
        preview = OperationResult(
            phase="preview",
            action="create_snapshot",
            vmid=vmid,
            node=node,
            guest_name=guest.get("name"),
            summary=f"snapshot '{name}' of {kind} {vmid} on {node}"
            + (" including RAM" if include_ram else ""),
            effects=["Creates a point-in-time snapshot; the guest keeps running."],
            warnings=warnings,
            next_step="",
        )
        return _run_two_phase(
            ctx,
            action="create_snapshot",
            required=AccessTier.OPERATOR,
            params=params,
            token=confirmation_token,
            preview=preview,
            guest=guest,
            execute=lambda: ctx.client.post(f"nodes/{node}/{kind}/{vmid}/snapshot", **payload),
        )


def _register_admin_tools(server: MCPServer, ctx: ToolContext) -> None:
    @server.tool(
        title="Migrate a guest",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    @guarded
    def migrate_guest(
        vmid: Annotated[int, Field(description="VM or container id.")],
        target_node: Annotated[str, Field(description="Destination node.")],
        with_local_disks: Annotated[
            bool, Field(description="Copy local disks to the target (VMs, slow).")
        ] = False,
        confirmation_token: Annotated[str | None, _TOKEN_FIELD] = None,
    ) -> OperationResult:
        """Move a VM (live if running) or container (restart if running) to another node.

        Two-phase. Use plan_node_evacuation first to check migratability.
        """
        validate_node(target_node)
        guest = find_guest(ctx.client, vmid)
        node = validate_node(str(guest.get("node")))
        kind = validate_guest_type(str(guest.get("type")))
        status, lock = live_state(ctx.client, guest)
        if lock:
            raise ValidationError(f"guest {vmid} is locked ({lock}); retry when that finishes")
        running = status == "running"
        if target_node == node:
            raise ValidationError(f"guest {vmid} already runs on {node}")
        target = next(
            (n for n in cluster_resources(ctx.client, "node") if n.get("node") == target_node),
            None,
        )
        if target is None or target.get("status") != "online":
            raise ValidationError(f"target node {target_node!r} is not an online cluster member")
        if with_local_disks and kind != "qemu":
            raise ValidationError("with_local_disks applies to VMs only")

        payload: dict[str, Any] = {"target": target_node}
        effects = [f"Moves {kind} {vmid} from {node} to {target_node}."]
        if running and kind == "qemu":
            payload["online"] = 1
            effects.append("Live migration: memory is copied while the VM keeps running.")
        elif running:
            payload["restart"] = 1
            effects.append("Restart migration: the container is stopped, moved and started.")
        if with_local_disks:
            payload["with-local-disks"] = 1
            effects.append("Local disks are copied over the network; this can take long.")
        warnings = []
        if is_ha_managed(guest):
            warnings.append("Guest is HA-managed: the migration is executed by the HA stack.")
        config = ctx.client.get(f"nodes/{node}/{kind}/{vmid}/config") or {}
        inspection = inspect_guest_config(
            kind, config, shared_storages(cluster_resources(ctx.client, "storage"))
        )
        if inspection.blockers:
            raise ValidationError(
                f"guest {vmid} cannot be migrated: " + "; ".join(inspection.blockers)
            )
        warnings += inspection.warnings
        if inspection.local_volumes and kind == "qemu" and not with_local_disks:
            raise ValidationError(
                f"guest {vmid} has local disks ({', '.join(sorted(inspection.local_volumes))}); "
                "set with_local_disks=true to copy them, or move them to shared storage first"
            )

        params = {
            "vmid": vmid,
            "node": node,
            "target_node": target_node,
            "with_local_disks": with_local_disks,
        }
        preview = OperationResult(
            phase="preview",
            action="migrate_guest",
            vmid=vmid,
            node=node,
            guest_name=guest.get("name"),
            summary=f"migrate {kind} {vmid} {node} -> {target_node}",
            effects=effects,
            warnings=warnings,
            next_step="",
        )
        return _run_two_phase(
            ctx,
            action="migrate_guest",
            required=AccessTier.ADMIN,
            params=params,
            token=confirmation_token,
            preview=preview,
            guest=guest,
            execute=lambda: ctx.client.post(f"nodes/{node}/{kind}/{vmid}/migrate", **payload),
        )
