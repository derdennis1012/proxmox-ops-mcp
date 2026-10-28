"""Diagnostic tools: cluster health, Ceph, node comparison and failed tasks."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import as_int, items
from proxmox_ops_mcp.analysis.ceph import CephHealthReport, analyze_ceph
from proxmox_ops_mcp.analysis.cluster import ClusterHealthReport, analyze_cluster
from proxmox_ops_mcp.analysis.nodes import NodeComparisonReport, compare_nodes, node_metrics
from proxmox_ops_mcp.client import validate_node
from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError
from proxmox_ops_mcp.tools._common import (
    READ_ONLY,
    ToolContext,
    cluster_resources,
    guarded,
    is_failed_task,
)


class FailedTask(BaseModel):
    """A task that did not finish with ``OK``."""

    upid: str
    node: str
    type: str
    target: str | None
    user: str | None
    started: int
    status: str


class FailedTaskReport(BaseModel):
    """Result of ``recent_task_failures``."""

    window_hours: int
    total: int
    by_type: dict[str, int]
    tasks: list[FailedTask]


def _rrd(ctx: ToolContext, node: str) -> Any:
    """Recent per-minute averages; optional, the status call is the fallback."""
    try:
        return ctx.client.get(f"nodes/{node}/rrddata", timeframe="hour", cf="AVERAGE")
    except ProxmoxAPIError:
        return None


_CEPH_PLUGINS = frozenset({"rbd", "cephfs"})


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register diagnostic tools on ``server``."""

    @server.tool(title="Cluster health check", annotations=READ_ONLY)
    @guarded
    def cluster_health() -> ClusterHealthReport:
        """Check quorum, node availability, CPU/memory pressure, storage fill levels and HA.

        Start here for any "is something wrong with the cluster?" question. Findings are
        sorted worst-first and each carries a concrete recommendation.
        """
        status = ctx.client.get("cluster/status")
        resources = cluster_resources(ctx.client)
        try:
            ha = ctx.client.get("cluster/ha/status/current")
        except ProxmoxAPIError:
            ha = None  # HA not configured or not permitted - not an error in itself.
        return analyze_cluster(status, resources, ha, ctx.thresholds)

    @server.tool(title="Ceph health analysis", annotations=READ_ONLY)
    @guarded
    def ceph_health() -> CephHealthReport:
        """Analyse Ceph: health checks, OSD up/in counts, PG states, monitor quorum, capacity.

        Use for "why is Ceph degraded/warning?". Each Ceph health check becomes a finding
        with operator guidance.
        """
        try:
            status = ctx.client.get("cluster/ceph/status")
        except ProxmoxAPIError as exc:
            storages: list[Mapping[str, Any]] = []
            with contextlib.suppress(ProxmoxAPIError):
                storages = cluster_resources(ctx.client, "storage")
            if storages and not any(s.get("plugintype") in _CEPH_PLUGINS for s in storages):
                raise ValidationError(
                    "Ceph is not configured on this cluster (no RBD or CephFS storage)."
                ) from exc
            raise ValidationError(
                "Ceph status is unavailable. Ceph may not be installed on this cluster, or "
                f"the API token lacks Sys.Audit on '/'. ({exc})"
            ) from exc
        return analyze_ceph(status or {}, ctx.thresholds)

    @server.tool(name="compare_nodes", title="Compare nodes", annotations=READ_ONLY)
    @guarded
    def compare_nodes_tool(
        nodes: Annotated[
            list[str] | None,
            Field(description="Nodes to compare. Defaults to all online nodes."),
        ] = None,
    ) -> NodeComparisonReport:
        """Compare live CPU, I/O wait, load, memory, swap and versions across nodes.

        Use for "why is node X slower than node Y?". Flags I/O-bound nodes, swapping,
        overload, version drift and memory imbalance.
        """
        resources = cluster_resources(ctx.client)
        online = sorted(
            validate_node(str(r.get("node")))
            for r in resources
            if r.get("type") == "node" and r.get("status") == "online"
        )
        selected = [validate_node(n) for n in nodes] if nodes else online
        running = {
            n: sum(
                1
                for r in resources
                if r.get("type") in {"qemu", "lxc"}
                and r.get("node") == n
                and r.get("status") == "running"
            )
            for n in selected
        }
        metrics = [
            node_metrics(n, ctx.client.get(f"nodes/{n}/status") or {}, running[n], _rrd(ctx, n))
            for n in selected
        ]
        return compare_nodes(metrics, ctx.thresholds)

    @server.tool(title="Recent failed tasks", annotations=READ_ONLY)
    @guarded
    def recent_task_failures(
        hours: Annotated[
            int, Field(ge=1, le=24 * 30, description="Time window to look back, in hours.")
        ] = 24,
        task_type: Annotated[
            str | None, Field(description="e.g. 'vzdump', 'qmigrate', 'qmstart'.")
        ] = None,
        limit: Annotated[
            int, Field(ge=1, le=500, description="Maximum number of failed tasks to return.")
        ] = 50,
    ) -> FailedTaskReport:
        """List tasks that failed recently, grouped by type. Good first stop after an incident.

        Based on the cluster-wide recent task list, which Proxmox keeps short; very old
        failures may no longer be included regardless of the window.
        """
        since = int(ctx.clock()) - hours * 3600
        tasks = items(ctx.client.get("cluster/tasks"))
        failed = [
            FailedTask(
                upid=str(t.get("upid")),
                node=str(t.get("node")),
                type=str(t.get("type")),
                target=str(t["id"]) if t.get("id") else None,
                user=t.get("user"),
                started=as_int(t.get("starttime")),
                status=str(t.get("status")),
            )
            for t in tasks
            if is_failed_task(t)
            and as_int(t.get("starttime")) >= since
            and (task_type is None or t.get("type") == task_type)
        ]
        failed.sort(key=lambda t: -t.started)
        by_type: dict[str, int] = {}
        for t in failed:
            by_type[t.type] = by_type.get(t.type, 0) + 1
        return FailedTaskReport(
            window_hours=hours, total=len(failed), by_type=by_type, tasks=failed[:limit]
        )
