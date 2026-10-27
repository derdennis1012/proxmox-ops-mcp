"""Server assembly: wires settings, client, safety primitives and tools together."""

from __future__ import annotations

import time
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

from proxmox_ops_mcp import __version__
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds
from proxmox_ops_mcp.client import ProxmoxClient, ProxmoxerClient
from proxmox_ops_mcp.config import Settings
from proxmox_ops_mcp.security import AccessTier, AuditLogger, ConfirmationManager, WritePolicy
from proxmox_ops_mcp.tools import ToolContext, register_all

SERVER_NAME = "proxmox-ops"

_INSTRUCTIONS = """\
Operations server for a Proxmox VE cluster ({tier} access).

How to work with it:
- Diagnose before acting. Start with cluster_health; drill down with ceph_health,
  compare_nodes, storage_health, node_maintenance_status, backup_audit and
  recent_task_failures.
- Planning tools (recommend_placement, plan_node_evacuation) never change anything.
- Write tools are two-phase. The first call returns a preview and a confirmation_token.
  Present the preview to the user and wait for explicit approval before calling again
  with the token. Never chain preview and execution without the user's approval.
- Findings carry stable codes and recommendations; quote them instead of guessing.
"""


def build_server(
    settings: Settings,
    *,
    client: ProxmoxClient | None = None,
    audit: AuditLogger | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    clock: Callable[[], float] = time.time,
) -> MCPServer:
    """Create a fully configured server.

    ``client``, ``audit`` and ``clock`` can be injected for tests; by default they are
    derived from ``settings``.
    """
    if client is None:
        client = ProxmoxerClient.from_settings(settings)
    ctx = ToolContext(
        client=client,
        tier=settings.access_tier,
        audit=audit or AuditLogger(settings.audit_log),
        confirmations=ConfirmationManager(settings.confirmation_ttl),
        thresholds=thresholds,
        policy=WritePolicy(
            pools=settings.write_pool_set,
            vmids=settings.write_vmid_set,
            max_per_hour=settings.max_writes_per_hour,
        ),
        clock=clock,
    )
    server = MCPServer(
        name=SERVER_NAME,
        title="Proxmox Ops",
        version=__version__,
        instructions=_INSTRUCTIONS.format(tier=settings.access_tier),
        website_url="https://github.com/derdennis1012/proxmox-ops-mcp",
    )
    register_all(server, ctx)
    _register_prompts(server, settings.access_tier)
    return server


def _register_prompts(server: MCPServer, tier: AccessTier) -> None:
    @server.prompt(title="Triage the cluster")
    def triage_cluster() -> str:
        """Full health triage across cluster, Ceph, backups and recent failures."""
        return (
            "Triage this Proxmox cluster. Call cluster_health, ceph_health (skip if Ceph is "
            "not configured), storage_health, node_maintenance_status, backup_audit and "
            "recent_task_failures. Then give me: 1) a "
            "one-line overall verdict, 2) critical issues with the exact finding codes, "
            "3) warnings worth scheduling, 4) the next three concrete actions. Do not run "
            "any write operation."
        )

    @server.prompt(title="Prepare node maintenance")
    def prepare_maintenance(node: str) -> str:
        """Check whether a node can be drained and produce a step-by-step runbook."""
        follow_up = (
            "Do not execute anything."
            if not tier.allows(AccessTier.ADMIN)
            else "If I approve, execute the migrations one at a time using migrate_guest, "
            "showing me each preview first."
        )
        return (
            f"I want to take node '{node}' down for maintenance. Run cluster_health and "
            f"plan_node_evacuation for '{node}'. Explain blockers, expected downtime and the "
            f"load on target nodes, then give me the runbook. {follow_up}"
        )
