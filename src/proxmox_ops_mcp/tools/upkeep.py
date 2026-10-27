"""Hardware and upkeep tools: ZFS/disk health, updates, reboots, certificates."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from proxmox_ops_mcp.analysis._util import items
from proxmox_ops_mcp.analysis.storage import StorageHealthReport, analyze_storage
from proxmox_ops_mcp.analysis.upkeep import UpkeepReport, analyze_upkeep
from proxmox_ops_mcp.client import validate_node
from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError
from proxmox_ops_mcp.tools._common import READ_ONLY, ToolContext, cluster_resources, guarded


def _online_nodes(ctx: ToolContext, node: str | None) -> list[str]:
    online = sorted(
        validate_node(str(r.get("node")))
        for r in cluster_resources(ctx.client, "node")
        if r.get("status") == "online"
    )
    if node is None:
        return online
    validate_node(node)
    if node not in online:
        raise ValidationError(f"node {node!r} is not an online cluster member")
    return [node]


def _get(ctx: ToolContext, unavailable: dict[str, str], path: str, **params: Any) -> Any:
    """GET that records failures in ``unavailable`` instead of raising.

    One bad endpoint must not sink the report, but it must show up in it.
    """
    try:
        return ctx.client.get(path, **params)
    except (ProxmoxAPIError, ValidationError) as exc:
        label = path + (f" ({', '.join(str(v) for v in params.values())})" if params else "")
        unavailable[label] = str(exc)
        return None


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register hardware and upkeep tools on ``server``."""

    @server.tool(title="Storage hardware health", annotations=READ_ONLY)
    @guarded
    def storage_health(
        node: Annotated[str | None, Field(description="Only this node. Default: all.")] = None,
    ) -> StorageHealthReport:
        """Check ZFS pools and physical disks for anything that threatens data.

        Covers pool state, device errors, data errors, fill level, scrub age, SMART
        health, SSD wear and bad sectors.

        Use when asking whether the storage underneath the guests is healthy, or before
        maintenance on a node.
        """
        pools: dict[str, Any] = {}
        details: dict[tuple[str, str], Mapping[str, Any]] = {}
        disks: dict[str, Any] = {}
        smart: dict[tuple[str, str], Mapping[str, Any]] = {}
        unavailable: dict[str, str] = {}
        for n in _online_nodes(ctx, node):
            pools[n] = _get(ctx, unavailable, f"nodes/{n}/disks/zfs") or []
            for p in items(pools[n]):
                name = str(p.get("name"))
                path = f"nodes/{n}/disks/zfs/{name}"
                details[(n, name)] = _get(ctx, unavailable, path) or {}
            disks[n] = _get(ctx, unavailable, f"nodes/{n}/disks/list") or []
            for d in items(disks[n]):
                dev = str(d.get("devpath") or "")
                if dev.startswith("/dev/"):
                    smart[(n, dev)] = (
                        _get(ctx, unavailable, f"nodes/{n}/disks/smart", disk=dev) or {}
                    )
        return analyze_storage(
            pools=pools,
            pool_details=details,
            disks=disks,
            smart=smart,
            now=datetime.fromtimestamp(ctx.clock(), UTC),
            thresholds=ctx.thresholds,
            unavailable=unavailable,
        )

    @server.tool(title="Updates, reboots and certificates", annotations=READ_ONLY)
    @guarded
    def node_maintenance_status(
        node: Annotated[str | None, Field(description="Only this node. Default: all.")] = None,
    ) -> UpkeepReport:
        """Show pending Proxmox updates, needed reboots and expiring node certificates.

        A reboot is needed when a newer kernel is installed than the one running.

        Covers the Proxmox package set (kernel, QEMU, ZFS, pve-*). Reflects apt's last
        package list refresh, which Proxmox runs daily.
        """
        nodes = _online_nodes(ctx, node)
        unavailable: dict[str, str] = {}
        versions = {n: _get(ctx, unavailable, f"nodes/{n}/apt/versions") or [] for n in nodes}
        certificates = {
            n: _get(ctx, unavailable, f"nodes/{n}/certificates/info") or [] for n in nodes
        }
        return analyze_upkeep(
            versions=versions,
            certificates=certificates,
            now=datetime.fromtimestamp(ctx.clock(), UTC),
            thresholds=ctx.thresholds,
            unavailable=unavailable,
        )
