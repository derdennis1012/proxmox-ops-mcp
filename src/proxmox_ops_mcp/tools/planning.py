"""Planning tools: guest placement and node evacuation. Read-only by construction."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from proxmox_ops_mcp.analysis._util import as_int, items
from proxmox_ops_mcp.analysis.maintenance import EvacuationPlan, plan_evacuation
from proxmox_ops_mcp.analysis.placement import (
    PlacementReport,
    PlacementRequest,
    recommend_placement,
)
from proxmox_ops_mcp.client import validate_guest_type, validate_node, validate_storage
from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError
from proxmox_ops_mcp.tools._common import READ_ONLY, ToolContext, cluster_resources, guarded

_CEPH_PLUGINS = frozenset({"rbd", "cephfs"})


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register planning tools on ``server``."""

    @server.tool(
        name="recommend_placement", title="Recommend guest placement", annotations=READ_ONLY
    )
    @guarded
    def recommend_placement_tool(
        cores: Annotated[int, Field(ge=1, le=1024, description="vCPUs the guest needs.")],
        memory_gib: Annotated[float, Field(gt=0, le=16384, description="Memory in GiB.")],
        disk_gib: Annotated[float, Field(ge=0, description="Disk size in GiB (0 = skip).")] = 0,
        storage: Annotated[str | None, Field(description="Require this storage id.")] = None,
        exclude_nodes: Annotated[list[str] | None, Field(description="Never pick these.")] = None,
    ) -> PlacementReport:
        """Rank nodes for a new VM/container by memory headroom, CPU load and vCPU over-commit.

        Every node gets a score and the reasons it was accepted or rejected.
        """
        if storage:
            validate_storage(storage)
        excluded = frozenset(validate_node(n) for n in exclude_nodes or [])
        request = PlacementRequest(
            cores=cores, memory_gib=memory_gib, disk_gib=disk_gib, storage=storage
        )
        return recommend_placement(request, cluster_resources(ctx.client), excluded)

    @server.tool(title="Plan node evacuation", annotations=READ_ONLY)
    @guarded
    def plan_node_evacuation(
        node: Annotated[str, Field(description="Node to drain for maintenance.")],
        exclude_targets: Annotated[
            list[str] | None, Field(description="Nodes that must not receive guests.")
        ] = None,
        memory_limit: Annotated[
            float, Field(gt=0.1, le=1, description="Max memory use allowed on targets.")
        ] = 0.85,
    ) -> EvacuationPlan:
        """Plan how to empty a node for maintenance without breaking HA or overloading others.

        Detects passthrough devices, bind mounts, local disks, locks and HA-managed guests,
        assigns every guest a target and migration mode, and returns the exact command
        sequence (including Ceph 'noout' and HA maintenance mode). Executes nothing.
        """
        validate_node(node)
        excluded = frozenset(validate_node(n) for n in exclude_targets or [])
        resources = cluster_resources(ctx.client)
        if not any(r.get("type") == "node" and r.get("node") == node for r in resources):
            raise ValidationError(f"node {node!r} is not part of this cluster")

        configs: dict[int, Mapping[str, Any]] = {}
        for g in resources:
            if g.get("type") in {"qemu", "lxc"} and g.get("node") == node:
                vmid = as_int(g.get("vmid"))
                # An unreadable config surfaces as a "could not be read" blocker in the plan.
                kind = validate_guest_type(str(g.get("type")))
                with contextlib.suppress(ProxmoxAPIError):
                    configs[vmid] = ctx.client.get(f"nodes/{node}/{kind}/{vmid}/config")
        try:
            ha_resources = items(ctx.client.get("cluster/ha/resources"))
        except ProxmoxAPIError:
            ha_resources = []
        ceph = any(
            r.get("type") == "storage" and r.get("plugintype") in _CEPH_PLUGINS for r in resources
        )
        return plan_evacuation(
            node=node,
            resources=resources,
            guest_configs=configs,
            ha_resources=ha_resources,
            ceph_in_use=ceph,
            memory_limit=memory_limit,
            exclude_targets=excluded,
        )
