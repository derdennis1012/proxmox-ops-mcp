"""Backup audit tool."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from proxmox_ops_mcp.analysis._util import as_int, items
from proxmox_ops_mcp.analysis.backups import BackupAuditReport, audit_backups
from proxmox_ops_mcp.client import ProxmoxClient, validate_node, validate_storage
from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError
from proxmox_ops_mcp.tools._common import (
    READ_ONLY,
    ToolContext,
    cluster_resources,
    guarded,
    is_failed_task,
)


def _has_privilege(permissions: Mapping[str, Any], path: str, privilege: str) -> bool:
    """Evaluate ``/access/permissions`` output: the most specific listed path wins.

    The value next to a privilege is its *propagate* flag, so a grant on the exact path
    counts regardless of it, while one inherited from a parent must propagate.
    """
    parts = [p for p in path.split("/") if p]
    for depth in range(len(parts), -1, -1):
        candidate = "/" + "/".join(parts[:depth])
        if candidate in permissions:
            granted = permissions[candidate] or {}
            if depth == len(parts):
                return privilege in granted
            return bool(granted.get(privilege))
    return False


def _can_list_backups(permissions: Mapping[str, Any] | None, storage: str) -> bool:
    """Mirror ``PVE::Storage::check_volume_access`` for backup volumes.

    Proxmox silently filters backups out of storage listings unless the caller has
    ``Datastore.Allocate`` on the storage, or ``Datastore.AllocateSpace`` on the storage
    together with ``VM.Backup`` on the guest. ``VM.Backup`` may be granted per guest or
    per pool, so any path carrying it counts.
    """
    if permissions is None:
        return True  # permissions unknown: trust the listing
    store = f"/storage/{storage}"
    any_backup = any("VM.Backup" in (granted or {}) for granted in permissions.values())
    return _has_privilege(permissions, store, "Datastore.Allocate") or (
        _has_privilege(permissions, store, "Datastore.AllocateSpace") and any_backup
    )


def collect_backup_volumes(
    client: ProxmoxClient,
    resources: list[Mapping[str, Any]],
    permissions: Mapping[str, Any] | None = None,
) -> tuple[list[Mapping[str, Any]], dict[str, str]]:
    """Read backup volumes from every backup-capable storage.

    Shared storages (NFS, CIFS, PBS, CephFS) are queried once; local ones per node.
    Storages that cannot be read are returned with the reason instead of failing the audit.
    """
    volumes: list[Mapping[str, Any]] = []
    unreadable: dict[str, str] = {}
    seen_shared: set[str] = set()
    for s in resources:
        if s.get("type") != "storage" or s.get("status") not in (None, "available"):
            continue
        if "backup" not in str(s.get("content", "")).split(","):
            continue
        storage, node = str(s.get("storage")), str(s.get("node"))
        if s.get("shared"):
            if storage in seen_shared:
                continue
            seen_shared.add(storage)
        label = storage if s.get("shared") else f"{node}/{storage}"
        try:
            path = f"nodes/{validate_node(node)}/storage/{validate_storage(storage)}/content"
            listed = items(client.get(path, content="backup"))
            volumes += listed
        except ValidationError:
            unreadable[label] = "unexpected storage or node name"
        except ProxmoxAPIError as exc:
            unreadable[label] = str(exc)
            continue
        if not listed and not _can_list_backups(permissions, storage):
            unreadable[label] = (
                "the API token may not see backup volumes here, so Proxmox returns an empty list"
            )
    return volumes, unreadable


def register(server: MCPServer, ctx: ToolContext) -> None:
    """Register backup tools on ``server``."""

    @server.tool(title="Backup audit", annotations=READ_ONLY)
    @guarded
    def backup_audit(
        max_age_hours: Annotated[
            int, Field(ge=1, le=24 * 90, description="Backups older than this are stale.")
        ] = 26,
        node: Annotated[str | None, Field(description="Only audit guests on this node.")] = None,
    ) -> BackupAuditReport:
        """Find guests with missing or stale backups, unscheduled guests and failed jobs.

        Reads backup volumes directly from every backup storage (including Proxmox Backup
        Server), so it reports what actually exists - not what the schedule promises.
        Set max_age_hours to match your schedule (e.g. ~170 for weekly backups). Failed
        tasks come from the cluster's recent task list, which only covers recent history.
        """
        if node:
            validate_node(node)
        resources = cluster_resources(ctx.client)
        guests = [
            r
            for r in resources
            if r.get("type") in {"qemu", "lxc"} and (node is None or r.get("node") == node)
        ]
        try:
            permissions = ctx.client.get("access/permissions")
        except ProxmoxAPIError:
            permissions = None
        volumes, unreadable = collect_backup_volumes(
            ctx.client, resources, permissions if isinstance(permissions, Mapping) else None
        )
        jobs = ctx.client.get("cluster/backup")
        since = int(ctx.clock()) - max_age_hours * 3600
        failed = [
            t
            for t in items(ctx.client.get("cluster/tasks"))
            if t.get("type") == "vzdump"
            and (node is None or t.get("node") == node)
            and is_failed_task(t)
            and as_int(t.get("starttime")) >= since
        ]
        return audit_backups(
            guests=guests,
            backup_volumes=volumes,
            jobs=jobs,
            failed_tasks=failed,
            unreadable_storages=unreadable,
            max_age_hours=max_age_hours,
            now=datetime.fromtimestamp(ctx.clock(), UTC),
        )
