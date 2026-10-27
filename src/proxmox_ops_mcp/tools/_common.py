"""Shared plumbing for the MCP tool modules."""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ParamSpec, TypeVar

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from proxmox_ops_mcp.analysis._util import as_int, items
from proxmox_ops_mcp.analysis.thresholds import Thresholds
from proxmox_ops_mcp.client import (
    ProxmoxClient,
    validate_guest_type,
    validate_node,
    validate_vmid,
)
from proxmox_ops_mcp.errors import ProxmoxOpsError, ValidationError
from proxmox_ops_mcp.security import AccessTier, AuditLogger, ConfirmationManager, WritePolicy

P = ParamSpec("P")
R = TypeVar("R")

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Dependencies shared by all tools of one server instance."""

    client: ProxmoxClient
    tier: AccessTier
    audit: AuditLogger
    confirmations: ConfirmationManager
    thresholds: Thresholds
    policy: WritePolicy = field(default_factory=WritePolicy)
    clock: Callable[[], float] = time.time
    """Current UNIX time; injectable so time-window logic is testable."""


def guarded(fn: Callable[P, R]) -> Callable[P, R]:
    """Turn anticipated package errors into clean MCP tool errors.

    Unexpected exceptions are left alone so the SDK logs them with a traceback.
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except ProxmoxOpsError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def cluster_resources(client: ProxmoxClient, kind: str | None = None) -> list[Mapping[str, Any]]:
    """Fetch ``/cluster/resources``, optionally filtered by type (``vm``, ``node``, ...)."""
    return items(
        client.get("cluster/resources", type=kind) if kind else client.get("cluster/resources")
    )


def find_guest(client: ProxmoxClient, vmid: int) -> Mapping[str, Any]:
    """Locate a guest by vmid. The node is always resolved server-side, never trusted."""
    validate_vmid(vmid)
    for guest in cluster_resources(client, "vm"):
        if as_int(guest.get("vmid")) == vmid:
            return guest
    raise ValidationError(f"no VM or container with vmid {vmid} exists in this cluster")


def live_state(client: ProxmoxClient, guest: Mapping[str, Any]) -> tuple[str, str | None]:
    """Return ``(status, lock)`` straight from the node, bypassing the resource cache.

    ``/cluster/resources`` is refreshed by pvestatd only every few seconds, so right
    after a start or shutdown it still reports the old state. Write preconditions must
    not rely on it. A running-but-paused VM is reported as ``paused``.
    """
    node = validate_node(str(guest.get("node")))
    kind = validate_guest_type(str(guest.get("type")))
    vmid = validate_vmid(as_int(guest.get("vmid")))
    current = client.get(f"nodes/{node}/{kind}/{vmid}/status/current") or {}
    status = str(current.get("status", "unknown"))
    if status == "running" and current.get("qmpstatus") in {"paused", "suspended"}:
        status = "paused"
    lock = current.get("lock")
    return status, str(lock) if lock else None


def is_ha_managed(guest: Mapping[str, Any]) -> bool:
    """Return whether ``/cluster/resources`` marks the guest as managed by HA."""
    return str(guest.get("hastate") or "") not in {"", "unmanaged", "ignored"}


def is_failed_task(task: Mapping[str, Any]) -> bool:
    """Return whether a finished task ended in an error (warnings do not count)."""
    status = str(task.get("status") or "")
    return (
        bool(task.get("endtime")) and status not in {"", "OK"} and not status.startswith("WARNINGS")
    )
