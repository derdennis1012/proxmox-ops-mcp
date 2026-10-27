"""MCP tool definitions. Each module exposes ``register(server, ctx)``."""

from mcp.server.mcpserver import MCPServer

from proxmox_ops_mcp.tools import (
    backups,
    diagnostics,
    inventory,
    operations,
    planning,
    upkeep,
)
from proxmox_ops_mcp.tools._common import ToolContext


def register_all(server: MCPServer, ctx: ToolContext) -> None:
    """Register every tool allowed by ``ctx.tier`` on ``server``."""
    for module in (inventory, diagnostics, upkeep, backups, planning, operations):
        module.register(server, ctx)


__all__ = ["ToolContext", "register_all"]
