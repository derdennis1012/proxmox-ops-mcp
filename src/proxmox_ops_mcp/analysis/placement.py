"""Where should a new guest go? Score every node against a resource request."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import as_float, gib, items, pct, ratio

GIB = 1024**3


class PlacementRequest(BaseModel):
    """Resources the new guest needs."""

    cores: int = Field(ge=1, le=1024)
    memory_gib: float = Field(gt=0, le=16384)
    disk_gib: float = Field(default=0, ge=0, le=1_000_000)
    storage: str | None = Field(default=None, description="Require this storage id.")
    memory_limit: float = Field(
        default=0.85, gt=0, le=1, description="Max memory utilisation allowed after placement."
    )
    max_vcpu_ratio: float = Field(
        default=4.0, gt=0, description="Max allocated vCPUs per physical core."
    )


class NodeCandidate(BaseModel):
    """Evaluation of one node."""

    node: str
    eligible: bool
    score: float = Field(description="0-100, higher is better. 0 when not eligible.")
    memory_pct_after: float
    vcpu_ratio_after: float
    cpu_pct_now: float
    storage: str | None
    storage_free_gib: float | None
    reasons: list[str]


class PlacementReport(BaseModel):
    """Result of :func:`recommend_placement`."""

    request: PlacementRequest
    recommended_node: str | None
    recommended_storage: str | None
    candidates: list[NodeCandidate]


def recommend_placement(
    request: PlacementRequest, resources: Any, exclude_nodes: frozenset[str] = frozenset()
) -> PlacementReport:
    """Rank nodes for ``request`` using ``/cluster/resources`` (all types)."""
    entries = items(resources)
    candidates = [
        _evaluate(r, entries, request)
        for r in entries
        if r.get("type") == "node" and str(r.get("node")) not in exclude_nodes
    ]
    candidates.sort(key=lambda c: (not c.eligible, -c.score, c.node))
    best = candidates[0] if candidates and candidates[0].eligible else None
    return PlacementReport(
        request=request,
        recommended_node=best.node if best else None,
        recommended_storage=best.storage if best else None,
        candidates=candidates,
    )


def _evaluate(
    node: Mapping[str, Any], entries: list[Mapping[str, Any]], req: PlacementRequest
) -> NodeCandidate:
    name = str(node.get("node"))
    reasons: list[str] = []
    eligible = True
    if node.get("status") != "online":
        eligible = False
        reasons.append("node is offline")

    maxmem = as_float(node.get("maxmem"))
    mem_after = ratio(as_float(node.get("mem")) + req.memory_gib * GIB, maxmem)
    if mem_after > req.memory_limit:
        eligible = False
        reasons.append(f"memory would reach {pct(mem_after)}% (limit {pct(req.memory_limit)}%)")

    cores = as_float(node.get("maxcpu"), 1) or 1
    allocated = sum(
        as_float(g.get("maxcpu"))
        for g in entries
        if g.get("type") in {"qemu", "lxc"}
        and g.get("node") == name
        and g.get("status") == "running"
    )
    vcpu_after = (allocated + req.cores) / cores
    if vcpu_after > req.max_vcpu_ratio:
        eligible = False
        reasons.append(
            f"vCPU over-commit would be {vcpu_after:.2f}:1 (limit {req.max_vcpu_ratio}:1)"
        )

    storage, free = _pick_storage(name, entries, req)
    if (req.disk_gib > 0 or req.storage) and storage is None:
        eligible = False
        wanted = f"'{req.storage}'" if req.storage else "an images-capable storage"
        reasons.append(f"no {wanted} with {req.disk_gib} GiB free (+10% headroom)")

    cpu_now = as_float(node.get("cpu"))
    score = 0.0
    if eligible:
        # Memory headroom dominates: it is the resource that cannot be over-committed safely.
        score = round(
            100
            * (
                0.55 * (1 - mem_after)
                + 0.25 * (1 - min(cpu_now, 1.0))
                + 0.20 * (1 - min(vcpu_after / req.max_vcpu_ratio, 1.0))
            ),
            1,
        )
        reasons.append("fits all constraints")

    return NodeCandidate(
        node=name,
        eligible=eligible,
        score=score,
        memory_pct_after=pct(mem_after),
        vcpu_ratio_after=round(vcpu_after, 2),
        cpu_pct_now=pct(cpu_now),
        storage=storage,
        storage_free_gib=free,
        reasons=reasons,
    )


def _pick_storage(
    node: str, entries: list[Mapping[str, Any]], req: PlacementRequest
) -> tuple[str | None, float | None]:
    if req.disk_gib <= 0 and req.storage is None:
        return None, None
    needed = req.disk_gib * GIB * 1.1
    best: tuple[str | None, float | None] = (None, None)
    best_free = -1.0
    for s in entries:
        if s.get("type") != "storage" or s.get("node") != node:
            continue
        if s.get("status") not in (None, "available"):
            continue
        if not set(str(s.get("content", "")).split(",")) & {"images", "rootdir"}:
            continue
        if req.storage and s.get("storage") != req.storage:
            continue
        free = as_float(s.get("maxdisk")) - as_float(s.get("disk"))
        if free >= needed and free > best_free:
            best, best_free = (str(s.get("storage")), gib(free)), free
    return best
