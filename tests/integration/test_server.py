"""End-to-end tests through the MCP server object, with a fake Proxmox API."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import GetPromptResult

from proxmox_ops_mcp.errors import ProxmoxAPIError
from proxmox_ops_mcp.security import AccessTier
from tests.conftest import ServerHarness
from tests.fakes import FakeProxmoxClient

pytestmark = pytest.mark.anyio

Harness = Callable[[AccessTier], ServerHarness]

READ_TOOLS = {
    "list_nodes", "list_guests", "get_guest", "get_task_status", "cluster_health",
    "ceph_health", "compare_nodes", "recent_task_failures", "backup_audit",
    "recommend_placement", "plan_node_evacuation", "storage_health", "node_maintenance_status",
}  # fmt: skip


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        (AccessTier.READ_ONLY, READ_TOOLS),
        (AccessTier.OPERATOR, READ_TOOLS | {"guest_power", "create_snapshot"}),
        (AccessTier.ADMIN, READ_TOOLS | {"guest_power", "create_snapshot", "migrate_guest"}),
    ],
)
async def test_tools_are_registered_per_tier(
    harness: Harness, tier: AccessTier, expected: set[str]
) -> None:
    assert await harness(tier).tool_names() == expected


async def test_tool_annotations(harness: Harness) -> None:
    tools = {t.name: t for t in await harness(AccessTier.ADMIN).server.list_tools()}
    for name in READ_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True, name
    for name in ("guest_power", "migrate_guest"):
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.destructive_hint is True
    assert all(t.output_schema for t in tools.values())


async def test_diagnostics_round_trip(harness: Harness) -> None:
    h = harness(AccessTier.READ_ONLY)
    health = await h.call("cluster_health")
    assert health["status"] == "info"
    ceph = await h.call("ceph_health")
    assert ceph["ceph_health"] == "HEALTH_OK"
    nodes = await h.call("compare_nodes", nodes=["pve1", "pve2"])
    assert [n["node"] for n in nodes["nodes"]] == ["pve1", "pve2"]
    failures = await h.call("recent_task_failures")
    assert failures["by_type"] == {"vzdump": 1}  # WARNINGS and OK tasks are not failures
    assert (await h.call("recent_task_failures", task_type="qmigrate"))["total"] == 0


async def test_cluster_health_without_ha(fake_client: FakeProxmoxClient, harness: Harness) -> None:
    del fake_client.routes["cluster/ha/status/current"]
    assert (await harness(AccessTier.READ_ONLY).call("cluster_health"))["status"] == "info"


async def test_ceph_unavailable_is_a_clean_error(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    del fake_client.routes["cluster/ceph/status"]
    with pytest.raises(ToolError, match="Ceph may not be installed"):
        await harness(AccessTier.READ_ONLY).server.call_tool("ceph_health", {})


async def test_inventory(harness: Harness) -> None:
    h = harness(AccessTier.READ_ONLY)
    nodes = await h.call("list_nodes")
    assert [n["node"] for n in nodes["nodes"]] == ["pve1", "pve2", "pve3"]
    assert (await h.call("list_guests"))["total"] == 5
    assert (await h.call("list_guests", include_templates=True))["total"] == 6
    running_vms_on_pve1 = await h.call(
        "list_guests", node="pve1", status="running", guest_type="qemu"
    )
    assert [g["vmid"] for g in running_vms_on_pve1["guests"]] == [100, 101]
    assert [g["vmid"] for g in (await h.call("list_guests", tag="db"))["guests"]] == [100]
    detail = await h.call("get_guest", vmid=100)
    assert detail["config"]["cipassword"] == "***"
    assert detail["guest"]["tags"] == ["prod", "db"]


async def test_task_status(fake_client: FakeProxmoxClient, harness: Harness) -> None:
    upid = "UPID:pve1:0000ABCD:00112233:6955B600:qmstart:101:ops@pve!mcp:"
    fake_client.routes[f"nodes/pve1/tasks/{upid}/status"] = {
        "status": "stopped", "exitstatus": "OK", "type": "qmstart",
    }  # fmt: skip
    fake_client.routes[f"nodes/pve1/tasks/{upid}/log"] = [
        {"n": i, "t": f"line {i}"} for i in range(10)
    ]
    status = await harness(AccessTier.READ_ONLY).call(
        "get_task_status", node="pve1", upid=upid, log_lines=3
    )
    assert status["succeeded"] is True
    assert status["log_tail"] == ["line 7", "line 8", "line 9"]


async def test_task_log_is_paged_to_the_real_tail(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    upid = "UPID:pve1:0000ABCD:00112233:6955B600:vzdump::root@pam:"
    lines = [{"n": i, "t": f"line {i}"} for i in range(2500)]
    fake_client.routes[f"nodes/pve1/tasks/{upid}/status"] = {"status": "running"}

    def get(path: str, **params: Any) -> Any:
        if path.endswith("/log"):
            return lines[params["start"] : params["start"] + params["limit"]]
        return FakeProxmoxClient.get(fake_client, path, **params)

    fake_client.get = get  # type: ignore[method-assign]
    status = await harness(AccessTier.READ_ONLY).call(
        "get_task_status", node="pve1", upid=upid, log_lines=2
    )
    assert status["log_tail"] == ["line 2498", "line 2499"]
    assert status["succeeded"] is None  # still running


async def test_unmanaged_ha_state_is_not_reported(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    next(r for r in fake_client.routes["cluster/resources"] if r.get("vmid") == 101)["hastate"] = (
        "unmanaged"
    )
    guests = (await harness(AccessTier.READ_ONLY).call("list_guests"))["guests"]
    assert {g["vmid"]: g["ha_state"] for g in guests}[101] is None


async def test_backup_audit_tool(fake_client: FakeProxmoxClient, harness: Harness) -> None:
    fake_client.routes["nodes/pve2/storage/nfs-backup/content"] = []  # must not be queried
    report = await harness(AccessTier.READ_ONLY).call("backup_audit")
    assert report["guests_missing"] == 2
    content_gets = [p for p, _ in fake_client.gets if p.endswith("/content")]
    assert content_gets == ["nodes/pve1/storage/nfs-backup/content"]  # shared storage once

    report = await harness(AccessTier.READ_ONLY).call("backup_audit", node="pve3")
    assert {g["vmid"] for g in report["guests"]} == {103}


async def test_backup_audit_reports_unreadable_storage(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    fake_client.routes["nodes/pve1/storage/nfs-backup/content"] = ProxmoxAPIError("403")
    report = await harness(AccessTier.READ_ONLY).call("backup_audit")
    assert any(f["code"] == "BACKUP_STORAGE_UNREADABLE" for f in report["findings"])


async def test_odd_storage_name_does_not_abort_backup_audit(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    fake_client.routes["cluster/resources"].append(
        {"type": "storage", "storage": "../evil", "node": "pve1", "content": "backup",
         "status": "available"}
    )  # fmt: skip
    report = await harness(AccessTier.READ_ONLY).call("backup_audit")
    unreadable = [f for f in report["findings"] if f["code"] == "BACKUP_STORAGE_UNREADABLE"]
    assert "pve1/../evil" in unreadable[0]["message"]


async def test_backup_audit_respects_backup_visibility_rules(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    # PVEAuditor alone: Proxmox returns an *empty* listing, not an error.
    volumes = fake_client.routes["nodes/pve1/storage/nfs-backup/content"]
    fake_client.routes["nodes/pve1/storage/nfs-backup/content"] = []
    auditor = dict.fromkeys(["Sys.Audit", "VM.Audit", "Datastore.Audit"], 1)
    fake_client.routes["access/permissions"] = {"/": auditor}
    report = await harness(AccessTier.READ_ONLY).call("backup_audit")
    assert report["guests_missing"] == 0
    assert report["guests_unknown"] == 5
    assert "may not see backup volumes" in report["unreadable_storages"]["nfs-backup"]
    fake_client.routes["nodes/pve1/storage/nfs-backup/content"] = volumes

    # AllocateSpace on the storage plus VM.Backup on the guests is enough.
    fake_client.routes["access/permissions"] = {
        "/": auditor,
        "/storage/nfs-backup": auditor | {"Datastore.AllocateSpace": 1},
        "/vms": auditor | {"VM.Backup": 1},
    }
    report = await harness(AccessTier.READ_ONLY).call("backup_audit")
    assert report["unreadable_storages"] == {}
    assert report["guests_ok"] == 2

    # VM.Backup granted per pool, AllocateSpace on the exact path without propagation.
    fake_client.routes["access/permissions"] = {
        "/": auditor,
        "/storage/nfs-backup": auditor | {"Datastore.AllocateSpace": 0},
        "/pool/web": {"VM.Backup": 1},
    }
    assert (await harness(AccessTier.READ_ONLY).call("backup_audit"))["unreadable_storages"] == {}

    # Datastore.Allocate alone is enough as well.
    fake_client.routes["access/permissions"] = {"/": auditor | {"Datastore.Allocate": 1}}
    assert (await harness(AccessTier.READ_ONLY).call("backup_audit"))["guests_ok"] == 2


async def test_ceph_not_configured_message(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    del fake_client.routes["cluster/ceph/status"]
    fake_client.routes["cluster/resources"] = [
        r for r in fake_client.routes["cluster/resources"] if r.get("plugintype") != "rbd"
    ]
    with pytest.raises(ToolError, match="Ceph is not configured"):
        await harness(AccessTier.READ_ONLY).server.call_tool("ceph_health", {})


async def test_compare_nodes_uses_rrd(fake_client: FakeProxmoxClient, harness: Harness) -> None:
    fake_client.routes["nodes/pve1/rrddata"] = [{"cpu": 0.5, "iowait": 0.01}]
    report = await harness(AccessTier.READ_ONLY).call("compare_nodes", nodes=["pve1"])
    assert report["nodes"][0]["cpu_pct"] == 50.0


async def test_storage_and_maintenance_tools(
    fake_client: FakeProxmoxClient, harness: Harness
) -> None:
    import json
    from pathlib import Path

    fixtures = Path(__file__).parent.parent / "fixtures" / "pve9"

    def load(name: str) -> Any:
        return json.loads((fixtures / name).read_text())

    for node in ("pve1", "pve2"):
        fake_client.routes[f"nodes/{node}/disks/zfs"] = load("disks_zfs.json")
        fake_client.routes[f"nodes/{node}/disks/zfs/tank"] = load("disks_zfs_tank.json")
        fake_client.routes[f"nodes/{node}/disks/zfs/backup"] = load("disks_zfs_backup.json")
        fake_client.routes[f"nodes/{node}/disks/list"] = load("disks_list.json")
        fake_client.routes[f"nodes/{node}/disks/smart"] = load("disks_smart_sda.json")
        fake_client.routes[f"nodes/{node}/apt/versions"] = load("apt_versions.json")
        fake_client.routes[f"nodes/{node}/certificates/info"] = load("certificates_info.json")
    # pve3 answers nothing useful: the reports must still come back for the others.
    h = harness(AccessTier.READ_ONLY)
    storage = await h.call("storage_health")
    assert {p["node"] for p in storage["pools"]} == {"pve1", "pve2"}
    assert any(f["code"] == "DATA_UNAVAILABLE" for f in storage["findings"])
    assert len(storage["disks"]) == 6
    smart_calls = [p for p in fake_client.gets if p[0].endswith("/disks/smart")]
    assert ("nodes/pve1/disks/smart", {"disk": "/dev/sda"}) in smart_calls

    upkeep = await h.call("node_maintenance_status", node="pve1")
    assert [n["node"] for n in upkeep["nodes"]] == ["pve1"]
    assert upkeep["findings"][0]["code"] == "NODE_UPDATES_PENDING"
    with pytest.raises(ToolError, match="not an online cluster member"):
        await h.server.call_tool("storage_health", {"node": "pve9"})

    # pve3 serves none of these endpoints: that must surface, not read as healthy.
    storage = await h.call("storage_health", node="pve3")
    assert storage["status"] == "warning"
    assert {f["code"] for f in storage["findings"]} == {"DATA_UNAVAILABLE"}
    upkeep = await h.call("node_maintenance_status", node="pve3")
    assert [f["resource"] for f in upkeep["findings"]] == [
        "nodes/pve3/apt/versions",
        "nodes/pve3/certificates/info",
    ]


async def test_planning_tools(fake_client: FakeProxmoxClient, harness: Harness) -> None:
    h = harness(AccessTier.READ_ONLY)
    placement = await h.call("recommend_placement", cores=2, memory_gib=4, exclude_nodes=["pve3"])
    assert placement["recommended_node"] == "pve2"
    plan = await h.call("plan_node_evacuation", node="pve1")
    assert plan["feasible"] is True
    assert any("noout" in s for s in plan["steps"])  # Ceph detected from storage types
    assert fake_client.posts == []  # planning never writes
    with pytest.raises(ToolError, match="not part of this cluster"):
        await h.server.call_tool("plan_node_evacuation", {"node": "pve9"})


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_guest", {"vmid": 4242}),
        ("list_guests", {"node": "../../etc"}),
        ("get_task_status", {"node": "pve1", "upid": "not-a-upid"}),
        ("recommend_placement", {"cores": 1, "memory_gib": 1, "storage": "a/b"}),
    ],
)
async def test_invalid_input_becomes_tool_error(
    harness: Harness, tool: str, args: dict[str, Any]
) -> None:
    with pytest.raises(ToolError):
        await harness(AccessTier.READ_ONLY).server.call_tool(tool, args)


class TestTwoPhaseWrites:
    async def test_preview_then_execute(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        preview = await h.call("guest_power", vmid=101, action="shutdown")
        assert preview["phase"] == "preview"
        assert preview["node"] == "pve1"
        assert fake_client.posts == []

        done = await h.call(
            "guest_power", vmid=101, action="shutdown",
            confirmation_token=preview["confirmation_token"],
        )  # fmt: skip
        assert done["phase"] == "executed"
        assert done["task_upid"].startswith("UPID:")
        assert fake_client.posts == [("nodes/pve1/qemu/101/status/shutdown", {})]
        assert [e["outcome"] for e in h.audit_events()] == ["previewed", "executed"]
        assert all(e["params"].get("confirmation_token") is None for e in h.audit_events())

    async def test_token_cannot_be_reused_or_redirected(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        token = (await h.call("guest_power", vmid=101, action="reboot"))["confirmation_token"]
        with pytest.raises(ToolError, match="does not match"):
            await h.server.call_tool(
                "guest_power", {"vmid": 101, "action": "shutdown", "confirmation_token": token}
            )
        with pytest.raises(ToolError, match="already used"):
            await h.server.call_tool(
                "guest_power", {"vmid": 101, "action": "reboot", "confirmation_token": token}
            )
        assert fake_client.posts == []
        assert [e["outcome"] for e in h.audit_events()] == ["previewed", "rejected", "rejected"]

    async def test_guest_moved_between_preview_and_execute(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        token = (await h.call("guest_power", vmid=101, action="reboot"))["confirmation_token"]
        next(r for r in fake_client.routes["cluster/resources"] if r.get("vmid") == 101)["node"] = (
            "pve2"
        )
        with pytest.raises(ToolError, match="does not match"):
            await h.server.call_tool(
                "guest_power", {"vmid": 101, "action": "reboot", "confirmation_token": token}
            )
        assert fake_client.posts == []

    async def test_hard_stop_requires_admin(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        with pytest.raises(ToolError, match="requires access tier 'admin'"):
            await h.server.call_tool("guest_power", {"vmid": 101, "action": "stop"})
        assert h.audit_events()[0]["outcome"] == "rejected"

        admin = harness(AccessTier.ADMIN)
        preview = await admin.call("guest_power", vmid=100, action="stop")
        assert any("pulling the plug" in e for e in preview["effects"])
        assert any("HA-managed" in w for w in preview["warnings"])

    @pytest.mark.parametrize(
        ("vmid", "action", "message"),
        [(101, "start", "cannot be started"), (103, "shutdown", "not running")],
    )
    async def test_nonsensical_power_actions(
        self, harness: Harness, vmid: int, action: str, message: str
    ) -> None:
        with pytest.raises(ToolError, match=message):
            await harness(AccessTier.OPERATOR).server.call_tool(
                "guest_power", {"vmid": vmid, "action": action}
            )

    async def test_snapshot(self, fake_client: FakeProxmoxClient, harness: Harness) -> None:
        h = harness(AccessTier.OPERATOR)
        args = {
            "vmid": 101,
            "name": "pre_upgrade",
            "description": "before apt upgrade",
            "include_ram": True,
        }
        preview = await h.call("create_snapshot", **args)
        assert "including RAM" in preview["summary"]
        await h.call("create_snapshot", **args, confirmation_token=preview["confirmation_token"])
        assert fake_client.posts == [
            (
                "nodes/pve1/qemu/101/snapshot",
                {"snapname": "pre_upgrade", "description": "before apt upgrade", "vmstate": 1},
            )
        ]

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ({"vmid": 100, "name": "pre-upgrade"}, "already exists"),
            ({"vmid": 200, "name": "snap", "include_ram": True}, "only supported for VMs"),
            ({"vmid": 100, "name": "current"}, "snapshot name"),
        ],
    )
    async def test_snapshot_validation(
        self, harness: Harness, args: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ToolError, match=message):
            await harness(AccessTier.OPERATOR).server.call_tool("create_snapshot", args)

    async def test_snapshot_chain_warning(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        fake_client.routes["nodes/pve1/qemu/101/snapshot"] = [{"name": f"s{i}"} for i in range(6)]
        preview = await harness(AccessTier.OPERATOR).call("create_snapshot", vmid=101, name="s99")
        assert any("6 snapshots" in w for w in preview["warnings"])

    async def test_migrate(self, fake_client: FakeProxmoxClient, harness: Harness) -> None:
        h = harness(AccessTier.ADMIN)
        preview = await h.call("migrate_guest", vmid=101, target_node="pve3", with_local_disks=True)
        await h.call(
            "migrate_guest", vmid=101, target_node="pve3", with_local_disks=True,
            confirmation_token=preview["confirmation_token"],
        )  # fmt: skip
        assert fake_client.posts == [
            ("nodes/pve1/qemu/101/migrate", {"target": "pve3", "online": 1, "with-local-disks": 1})
        ]
        preview = await h.call("migrate_guest", vmid=200, target_node="pve2")
        assert any("Restart migration" in e for e in preview["effects"])
        preview = await h.call("migrate_guest", vmid=103, target_node="pve2")
        assert preview["effects"] == ["Moves qemu 103 from pve3 to pve2."]
        preview = await h.call("migrate_guest", vmid=100, target_node="pve2")
        assert any("HA-managed" in w for w in preview["warnings"])

    async def test_ha_routed_request_has_no_upid(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        fake_client.post_result = None
        token = (await h.call("guest_power", vmid=100, action="reboot"))["confirmation_token"]
        done = await h.call("guest_power", vmid=100, action="reboot", confirmation_token=token)
        assert done["task_upid"] is None
        assert "HA stack" in done["next_step"]

    async def test_guest_agent_warning_reflects_config(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        preview = await h.call("guest_power", vmid=101, action="reboot")
        assert any("ACPI" in w for w in preview["warnings"])
        fake_client.routes["nodes/pve1/qemu/101/config"]["agent"] = "1,fstrim_cloned_disks=1"
        preview = await h.call("guest_power", vmid=101, action="shutdown")
        assert any("fails after a timeout" in w for w in preview["warnings"])

    async def test_preconditions_use_live_state_not_the_resource_cache(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        # The cache still says "running" right after a shutdown; the node knows better.
        fake_client.routes["nodes/pve1/qemu/101/status/current"] = {"status": "stopped"}
        h = harness(AccessTier.OPERATOR)
        preview = await h.call("guest_power", vmid=101, action="start")
        assert preview["phase"] == "preview"
        assert (await h.call("get_guest", vmid=101))["guest"]["status"] == "stopped"

    async def test_locked_guest_is_rejected(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        fake_client.routes["nodes/pve1/qemu/101/status/current"] = {
            "status": "running", "lock": "backup",
        }  # fmt: skip
        for tool, args in [
            ("guest_power", {"vmid": 101, "action": "reboot"}),
            ("create_snapshot", {"vmid": 101, "name": "snap"}),
        ]:
            with pytest.raises(ToolError, match=r"locked \(backup\)"):
                await harness(AccessTier.ADMIN).server.call_tool(tool, args)
        with pytest.raises(ToolError, match="locked"):
            await harness(AccessTier.ADMIN).server.call_tool(
                "migrate_guest", {"vmid": 101, "target_node": "pve2"}
            )
        assert (await harness(AccessTier.READ_ONLY).call("get_guest", vmid=101))["guest"][
            "lock"
        ] == "backup"

    async def test_paused_vm_detected_from_qmp(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        fake_client.routes["nodes/pve1/qemu/101/status/current"] = {
            "status": "running", "qmpstatus": "paused",
        }  # fmt: skip
        with pytest.raises(ToolError, match="paused"):
            await harness(AccessTier.OPERATOR).server.call_tool(
                "guest_power", {"vmid": 101, "action": "start"}
            )

    async def test_paused_guest_can_be_stopped_and_hibernated_vm_resumed(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        fake_client.routes["nodes/pve1/qemu/101/status/current"] = {
            "status": "running", "qmpstatus": "paused",
        }  # fmt: skip
        preview = await harness(AccessTier.ADMIN).call("guest_power", vmid=101, action="stop")
        assert preview["phase"] == "preview"
        fake_client.routes["nodes/pve3/qemu/103/status/current"] = {
            "status": "stopped", "lock": "suspended",
        }  # fmt: skip
        preview = await harness(AccessTier.OPERATOR).call("guest_power", vmid=103, action="start")
        assert preview["phase"] == "preview"

    async def test_paused_guest_cannot_be_started(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        next(r for r in fake_client.routes["cluster/resources"] if r.get("vmid") == 103)[
            "status"
        ] = "paused"
        with pytest.raises(ToolError, match="only 'stop'"):
            await harness(AccessTier.OPERATOR).server.call_tool(
                "guest_power", {"vmid": 103, "action": "start"}
            )

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ({"vmid": 101, "target_node": "pve3"}, "has local disks"),
            ({"vmid": 102, "target_node": "pve3"}, "host device passthrough"),
            ({"vmid": 101, "target_node": "pve1"}, "already runs on"),
            ({"vmid": 101, "target_node": "pve9"}, "not an online cluster member"),
            ({"vmid": 200, "target_node": "pve2", "with_local_disks": True}, "VMs only"),
        ],
    )
    async def test_migrate_validation(
        self, harness: Harness, args: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ToolError, match=message):
            await harness(AccessTier.ADMIN).server.call_tool("migrate_guest", args)

    async def test_api_failure_is_audited(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        token = (await h.call("guest_power", vmid=103, action="start"))["confirmation_token"]

        def boom(path: str, **data: Any) -> Any:
            raise ProxmoxAPIError("500 storage offline")

        fake_client.post = boom  # type: ignore[method-assign]
        with pytest.raises(ToolError, match="storage offline"):
            await h.server.call_tool(
                "guest_power", {"vmid": 103, "action": "start", "confirmation_token": token}
            )
        assert h.audit_events()[-1]["outcome"] == "failed"

    async def test_unexpected_failure_is_audited_without_details(
        self, fake_client: FakeProxmoxClient, harness: Harness
    ) -> None:
        h = harness(AccessTier.OPERATOR)
        token = (await h.call("guest_power", vmid=103, action="start"))["confirmation_token"]

        def boom(path: str, **data: Any) -> Any:
            raise RuntimeError("internal detail")

        fake_client.post = boom  # type: ignore[method-assign]
        with pytest.raises(ToolError):
            await h.server.call_tool(
                "guest_power", {"vmid": 103, "action": "start", "confirmation_token": token}
            )
        assert h.audit_events()[-1]["detail"] == {"error": "RuntimeError"}


async def _prompt_text(server: Any, name: str, args: dict[str, str]) -> str:
    result = await server.get_prompt(name, args)
    assert isinstance(result, GetPromptResult)
    return str(result.messages[0].content)


async def test_write_scope_and_rate_limit(fake_client: FakeProxmoxClient) -> None:
    h = ServerHarness(AccessTier.ADMIN, fake_client, write_pools="web", max_writes_per_hour=1)
    with pytest.raises(ToolError, match="outside the write scope"):
        await h.server.call_tool("guest_power", {"vmid": 100, "action": "reboot"})
    assert h.audit_events()[-1]["outcome"] == "rejected"

    token = (await h.call("guest_power", vmid=101, action="reboot"))["confirmation_token"]
    await h.call("guest_power", vmid=101, action="reboot", confirmation_token=token)
    token = (await h.call("guest_power", vmid=101, action="shutdown"))["confirmation_token"]
    with pytest.raises(ToolError, match="1 per hour"):
        await h.server.call_tool(
            "guest_power", {"vmid": 101, "action": "shutdown", "confirmation_token": token}
        )
    assert len(fake_client.posts) == 1


async def test_prompts(harness: Harness) -> None:
    ro = harness(AccessTier.READ_ONLY).server
    assert {p.name for p in await ro.list_prompts()} == {"triage_cluster", "prepare_maintenance"}
    text = await _prompt_text(ro, "prepare_maintenance", {"node": "pve2"})
    assert "pve2" in text
    assert "Do not execute anything" in text
    admin = harness(AccessTier.ADMIN).server
    assert "migrate_guest" in await _prompt_text(admin, "prepare_maintenance", {"node": "pve2"})
    assert "cluster_health" in await _prompt_text(ro, "triage_cluster", {})
