"""Read-only smoke tests against a real Proxmox VE cluster.

Skipped unless ``PROXMOX_LIVE_ENV_FILE`` points to an env file with a token (see
``.env.example``). They never change anything on the cluster:

    PROXMOX_LIVE_ENV_FILE=~/.config/proxmox-ops-mcp/lab.env uv run pytest tests/live -v
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from proxmox_ops_mcp.config import Settings
from proxmox_ops_mcp.security import AccessTier
from proxmox_ops_mcp.server import build_server

ENV_FILE = os.environ.get("PROXMOX_LIVE_ENV_FILE")

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(not ENV_FILE, reason="set PROXMOX_LIVE_ENV_FILE to run live tests"),
]


@pytest.fixture(scope="module")
def server() -> MCPServer:
    assert ENV_FILE is not None
    settings = Settings(_env_file=Path(ENV_FILE).expanduser())
    # Whatever the env file says: these tests only ever run read-only.
    return build_server(settings.model_copy(update={"access_tier": AccessTier.READ_ONLY}))


async def _call(server: MCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await server.call_tool(tool, arguments)
    assert isinstance(result, CallToolResult)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return dict(result.structured_content)


async def test_only_read_tools_are_exposed(server: MCPServer) -> None:
    tools = await server.list_tools()
    assert tools
    assert all(t.annotations and t.annotations.read_only_hint for t in tools)


async def test_inventory(server: MCPServer) -> None:
    nodes = (await _call(server, "list_nodes"))["nodes"]
    assert nodes
    assert any(n["status"] == "online" for n in nodes)
    guests = (await _call(server, "list_guests"))["guests"]
    if guests:
        detail = await _call(server, "get_guest", vmid=guests[0]["vmid"])
        assert detail["guest"]["status"] in {"running", "stopped", "paused"}


@pytest.mark.parametrize(
    "tool",
    [
        "cluster_health",
        "compare_nodes",
        "backup_audit",
        "recent_task_failures",
        "storage_health",
        "node_maintenance_status",
    ],
)
async def test_diagnostics_return_reports(server: MCPServer, tool: str) -> None:
    report = await _call(server, tool)
    assert report.get("status", "info") in {"info", "warning", "critical"}


async def test_compare_nodes_has_real_cpu_values(server: MCPServer) -> None:
    # Regression: Proxmox VE 9 reports cpu=0 in /nodes/{node}/status.
    nodes = (await _call(server, "compare_nodes"))["nodes"]
    assert any(n["cpu_pct"] > 0 or n["load_1m"] == 0 for n in nodes)


async def test_ceph_reports_or_explains(server: MCPServer) -> None:
    error: str | None = None
    try:
        report = await _call(server, "ceph_health")
    except ToolError as exc:
        error = str(exc)
    if error is not None:
        assert "Ceph" in error  # not installed / not permitted, explained cleanly
    else:
        assert report["ceph_health"].startswith("HEALTH_")


async def test_planning(server: MCPServer) -> None:
    placement = await _call(server, "recommend_placement", cores=1, memory_gib=1)
    assert placement["candidates"]
    node = placement["candidates"][0]["node"]
    plan = await _call(server, "plan_node_evacuation", node=node)
    assert isinstance(plan["feasible"], bool)
