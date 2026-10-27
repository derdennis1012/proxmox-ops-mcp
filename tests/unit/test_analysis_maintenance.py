from __future__ import annotations

from typing import Any

from proxmox_ops_mcp.analysis.maintenance import MigrationMode, plan_evacuation
from tests.fakes import GIB, build_cluster


def _configs(c: dict[str, Any], node: str) -> dict[int, Any]:
    return {
        r["vmid"]: c[f"nodes/{node}/{r['type']}/{r['vmid']}/config"]
        for r in c["cluster/resources"]
        if r.get("node") == node
        and r["type"] in {"qemu", "lxc"}
        and f"nodes/{node}/{r['type']}/{r['vmid']}/config" in c
    }


def test_drain_pve1() -> None:
    c = build_cluster()
    plan = plan_evacuation(
        node="pve1",
        resources=c["cluster/resources"],
        guest_configs=_configs(c, "pve1"),
        ha_resources=c["cluster/ha/resources"],
        ceph_in_use=True,
    )
    assert plan.feasible
    modes = {g.vmid: g.mode for g in plan.guests}
    assert modes == {
        100: MigrationMode.HA_MANAGED,
        101: MigrationMode.LIVE_WITH_LOCAL_DISKS,
        200: MigrationMode.RESTART,
    }
    assert all(g.target in {"pve2", "pve3"} for g in plan.guests)
    assert plan.steps[0].startswith("ceph osd set noout")
    assert any("node-maintenance enable pve1" in s for s in plan.steps)
    assert any(s.startswith("qm migrate 101 ") and "--with-local-disks" in s for s in plan.steps)
    assert any(s.startswith("pct migrate 200 ") and s.endswith("--restart") for s in plan.steps)
    assert plan.steps[-1] == "ceph osd unset noout"
    assert "brief restart" in plan.summary
    assert sum(t.guests_added for t in plan.targets) == 3


def test_blockers() -> None:
    c = build_cluster()
    configs = {
        102: {"scsi0": "ceph-vm:vm-102-disk-0", "hostpci0": "0000:01:00,pcie=1", "lock": "backup"},
    }
    plan = plan_evacuation(node="pve2", resources=c["cluster/resources"], guest_configs=configs)
    assert not plan.feasible
    assert plan.steps == []
    gpu = plan.guests[0]
    assert gpu.mode is None
    assert gpu.target is None
    assert any("passthrough" in b for b in gpu.blockers)
    assert any("locked" in b for b in gpu.blockers)
    assert "cannot be fully drained" in plan.summary


def test_container_specific_blockers_and_unknown_config() -> None:
    c = build_cluster()
    configs = {
        200: {
            "rootfs": "local-lvm:subvol-200",
            "mp0": "/mnt/data,mp=/data",
            "dev0": "/dev/ttyUSB0",
        },
    }
    plan = plan_evacuation(node="pve1", resources=c["cluster/resources"], guest_configs=configs)
    by_id = {g.vmid: g for g in plan.guests}
    assert any("bind mount" in b for b in by_id[200].blockers)
    assert any("device passthrough" in b for b in by_id[200].blockers)
    assert by_id[100].blockers == ["configuration could not be read; migratability unknown"]


def test_local_iso_and_offline_guest() -> None:
    c = build_cluster()
    configs = {103: {"scsi0": "ceph-vm:vm-103-disk-0", "ide2": "local:iso/debian.iso,media=cdrom"}}
    plan = plan_evacuation(node="pve3", resources=c["cluster/resources"], guest_configs=configs)
    old = next(g for g in plan.guests if g.vmid == 103)
    assert old.mode is MigrationMode.OFFLINE
    assert any("ISO" in w for w in old.warnings)


def test_ha_guest_with_local_disk_is_blocked() -> None:
    c = build_cluster()
    configs = _configs(c, "pve1")
    configs[100] = {"scsi0": "local-lvm:vm-100-disk-0"}
    plan = plan_evacuation(
        node="pve1",
        resources=c["cluster/resources"],
        guest_configs=configs,
        ha_resources=c["cluster/ha/resources"],
    )
    db = next(g for g in plan.guests if g.vmid == 100)
    assert db.blockers


def test_capacity_exhaustion() -> None:
    c = build_cluster()
    for r in c["cluster/resources"]:
        if r.get("type") == "node" and r["node"] != "pve1":
            r["mem"] = 215 * GIB
    plan = plan_evacuation(
        node="pve1", resources=c["cluster/resources"], guest_configs=_configs(c, "pve1")
    )
    db = next(g for g in plan.guests if g.vmid == 100)
    assert "no target node has enough memory headroom" in db.blockers


def test_no_targets_and_empty_node() -> None:
    c = build_cluster()
    plan = plan_evacuation(
        node="pve1",
        resources=c["cluster/resources"],
        guest_configs={},
        exclude_targets=frozenset({"pve2", "pve3"}),
    )
    assert not plan.feasible
    assert "no other online node" in plan.summary

    plan = plan_evacuation(node="pve9", resources=c["cluster/resources"], guest_configs={})
    assert plan.feasible
    assert "no guests" in plan.summary


def test_live_migration_without_local_disks() -> None:
    c = build_cluster()
    configs = {102: {"scsi0": "ceph-vm:vm-102-disk-0", "usb0": "spice"}}
    plan = plan_evacuation(node="pve2", resources=c["cluster/resources"], guest_configs=configs)
    assert plan.guests[0].mode is MigrationMode.LIVE
    assert "no downtime" in plan.summary
    assert any(s.endswith("--online") for s in plan.steps)


def test_cloud_init_drive_is_a_local_volume() -> None:
    c = build_cluster()
    configs = {
        102: {"scsi0": "ceph-vm:vm-102-disk-0", "ide2": "local-lvm:vm-102-cloudinit,media=cdrom"}
    }
    plan = plan_evacuation(node="pve2", resources=c["cluster/resources"], guest_configs=configs)
    vm = plan.guests[0]
    assert vm.mode is MigrationMode.LIVE_WITH_LOCAL_DISKS
    assert not any("ISO" in w for w in vm.warnings)


def test_target_must_provide_local_storage() -> None:
    c = build_cluster()
    c["cluster/resources"] = [
        r
        for r in c["cluster/resources"]
        if not (r.get("storage") == "local-lvm" and r["node"] in {"pve1", "pve3"})
    ]
    c["cluster/resources"].append(
        {"type": "storage", "storage": "local-zfs", "node": "pve2", "shared": 0}
    )
    configs = {103: {"scsi0": "local-zfs:vm-103-disk-0"}}
    plan = plan_evacuation(node="pve3", resources=c["cluster/resources"], guest_configs=configs)
    vm = next(g for g in plan.guests if g.vmid == 103)
    assert vm.target == "pve2"  # the only node with local-zfs

    c["cluster/resources"] = [r for r in c["cluster/resources"] if r.get("storage") != "local-zfs"]
    plan = plan_evacuation(node="pve3", resources=c["cluster/resources"], guest_configs=configs)
    vm = next(g for g in plan.guests if g.vmid == 103)
    assert vm.blockers == ["no target node provides the local storage(s) local-zfs"]


def test_stopped_guest_fits_even_on_busy_targets() -> None:
    c = build_cluster()
    for r in c["cluster/resources"]:
        if r.get("type") == "node" and r["node"] != "pve3":
            r["mem"] = 240 * GIB
    configs = {103: {"scsi0": "ceph-vm:vm-103-disk-0"}, 9000: {"scsi0": "ceph-vm:base-9000"}}
    plan = plan_evacuation(node="pve3", resources=c["cluster/resources"], guest_configs=configs)
    assert plan.feasible
    assert {g.mode for g in plan.guests} == {MigrationMode.OFFLINE}


def test_ha_container_counts_as_downtime() -> None:
    c = build_cluster()
    configs = _configs(c, "pve1")
    plan = plan_evacuation(
        node="pve1",
        resources=c["cluster/resources"],
        guest_configs=configs,
        ha_resources=[*c["cluster/ha/resources"], {"sid": "ct:200", "state": "started"}],
    )
    ct = next(g for g in plan.guests if g.vmid == 200)
    assert ct.mode is MigrationMode.HA_MANAGED
    assert any("restart" in w for w in ct.warnings)
    assert "1 container(s) with a brief restart" in plan.summary


def test_no_targets_blocker_message() -> None:
    c = build_cluster()
    plan = plan_evacuation(
        node="pve1",
        resources=c["cluster/resources"],
        guest_configs=_configs(c, "pve1"),
        exclude_targets=frozenset({"pve2", "pve3"}),
    )
    assert plan.guests[0].blockers == ["no other online node is available as a target"]
