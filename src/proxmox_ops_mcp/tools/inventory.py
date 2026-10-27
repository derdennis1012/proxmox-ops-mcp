"""Inventory tools: nodes, guests and tasks."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import as_float, as_int, gib, items, pct, ratio
from proxmox_ops_mcp.client import validate_guest_type, validate_node, validate_upid
from proxmox_ops_mcp.security.audit import redact
from proxmox_ops_mcp.tools._common import (
    READ_ONLY,
    ToolContext,
    cluster_resources,
    find_guest,
    guarded,
    is_ha_managed,
    live_state,
)


class NodeInfo(BaseModel):
    """One cluster node."""

    node: str
    status: str
    cpus: int
    cpu_pct: float
    memory_total_gib: float
    memory_pct: float
    uptime_hours: float


class NodeList(BaseModel):
    """Result of ``list_nodes``."""

    nodes: list[NodeInfo]


class GuestInfo(BaseModel):
    """One VM or container."""

    vmid: int
    name: str | None
    type: Literal["qemu", "lxc"]
    node: str
    status: str
    cpus: float
    cpu_pct: float
    memory_max_gib: float
    memory_pct: float
    disk_max_gib: float
    uptime_hours: float
    tags: list[str]
    pool: str | None
    ha_state: str | None
    template: bool
    lock: str | None = Field(default=None, description="Active lock (backup, migrate, ...).")


class GuestList(BaseModel):
    """Result of ``list_guests``."""

    total: int
    guests: list[GuestInfo]


class GuestDetail(BaseModel):
    """Result of ``get_guest``."""

    guest: GuestInfo
    config: dict[str, Any] = Field(description="Guest configuration; secrets are redacted.")


class TaskStatus(BaseModel):
    """Result of ``get_task_status``."""

    upid: str
    node: str
    type: str | None
    status: str
    exit_status: str | None
    succeeded: bool | None
    log_tail: list[str]


def _guest(entry: Any) -> GuestInfo:
    tags = str(entry.get("tags") or "")
    return GuestInfo(
        vmid=as_int(entry.get("vmid")),
        name=entry.get("name"),
        type="lxc" if entry.get("type") == "lxc" else "qemu",
        node=str(entry.get("node")),
        status=str(entry.get("status", "unknown")),
        cpus=as_float(entry.get("maxcpu")),
        cpu_pct=pct(as_float(entry.get("cpu"))),
        memory_max_gib=gib(entry.get("maxmem")),
        memory_pct=pct(ratio(entry.get("mem"), entry.get("maxmem"))),
        disk_max_gib=gib(entry.get("maxdisk")),
        uptime_hours=round(as_float(entry.get("uptime")) / 3600, 1),
        tags=[t for t in tags.replace(",", ";").split(";") if t],
        pool=entry.get("pool"),
        ha_state=entry.get("hastate") if is_ha_managed(entry) else None,
        template=bool(entry.get("template")),
    )


_LOG_PAGE = 1000
_LOG_MAX_LINES = 50_000


def _log_tail(ctx: ToolContext, node: str, upid: str, lines: int) -> list[str]:
    """Return the last ``lines`` log lines, paging through long logs.

    Paging stops after ``_LOG_MAX_LINES`` to bound API load; for even longer logs the
    returned lines are the end of that window, not of the whole log.
    """
    tail: list[str] = []
    start = 0
    while start < _LOG_MAX_LINES:
        page = items(ctx.client.get(f"nodes/{node}/tasks/{upid}/log", start=start, limit=_LOG_PAGE))
        tail = (tail + [str(e.get("t", "")) for e in page])[-lines:]
        if len(page) < _LOG_PAGE:
            break
        start += _LOG_PAGE
    return tail


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register inventory tools on ``server``."""

    @server.tool(title="List cluster nodes", annotations=READ_ONLY)
    @guarded
    def list_nodes() -> NodeList:
        """List all cluster nodes with status, CPU and memory utilisation."""
        nodes = [
            NodeInfo(
                node=str(n.get("node")),
                status=str(n.get("status", "unknown")),
                cpus=as_int(n.get("maxcpu")),
                cpu_pct=pct(as_float(n.get("cpu"))),
                memory_total_gib=gib(n.get("maxmem")),
                memory_pct=pct(ratio(n.get("mem"), n.get("maxmem"))),
                uptime_hours=round(as_float(n.get("uptime")) / 3600, 1),
            )
            for n in cluster_resources(ctx.client, "node")
        ]
        return NodeList(nodes=sorted(nodes, key=lambda n: n.node))

    @server.tool(title="List VMs and containers", annotations=READ_ONLY)
    @guarded
    def list_guests(
        node: Annotated[str | None, Field(description="Only guests on this node.")] = None,
        status: Annotated[
            Literal["running", "stopped", "paused"] | None, Field(description="Filter by state.")
        ] = None,
        guest_type: Annotated[
            Literal["qemu", "lxc"] | None, Field(description="'qemu' (VM) or 'lxc' (container).")
        ] = None,
        tag: Annotated[str | None, Field(description="Only guests carrying this tag.")] = None,
        include_templates: bool = False,
    ) -> GuestList:
        """List VMs and containers across the cluster with resource usage, tags and HA state."""
        if node:
            validate_node(node)
        guests = [_guest(g) for g in cluster_resources(ctx.client, "vm")]
        guests = [
            g
            for g in guests
            if (include_templates or not g.template)
            and (node is None or g.node == node)
            and (status is None or g.status == status)
            and (guest_type is None or g.type == guest_type)
            and (tag is None or tag in g.tags)
        ]
        guests.sort(key=lambda g: g.vmid)
        return GuestList(total=len(guests), guests=guests)

    @server.tool(title="Show guest details", annotations=READ_ONLY)
    @guarded
    def get_guest(vmid: Annotated[int, Field(description="VM or container id.")]) -> GuestDetail:
        """Show one guest's live status and full configuration (secrets redacted)."""
        entry = find_guest(ctx.client, vmid)
        status, lock = live_state(ctx.client, entry)
        info = _guest(entry).model_copy(update={"status": status, "lock": lock})
        node = validate_node(info.node)
        config = ctx.client.get(f"nodes/{node}/{validate_guest_type(info.type)}/{vmid}/config")
        return GuestDetail(guest=info, config=redact(config or {}))

    @server.tool(title="Get task status and log", annotations=READ_ONLY)
    @guarded
    def get_task_status(
        node: Annotated[str, Field(description="Node the task ran on.")],
        upid: Annotated[str, Field(description="Task id (UPID) as returned by write tools.")],
        log_lines: Annotated[int, Field(ge=0, le=500)] = 50,
    ) -> TaskStatus:
        """Return the state of a Proxmox task and the tail of its log."""
        validate_node(node)
        validate_upid(upid)
        status = ctx.client.get(f"nodes/{node}/tasks/{upid}/status") or {}
        log: list[str] = []
        if log_lines:
            log = _log_tail(ctx, node, upid, log_lines)
        exit_status = status.get("exitstatus")
        finished = status.get("status") == "stopped"
        return TaskStatus(
            upid=upid,
            node=node,
            type=status.get("type"),
            status=str(status.get("status", "unknown")),
            exit_status=exit_status,
            succeeded=(exit_status == "OK") if finished else None,
            log_tail=log,
        )
