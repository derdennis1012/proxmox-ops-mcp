"""Cluster-wide health: quorum, node availability, utilisation, storage and HA."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from proxmox_ops_mcp.analysis._util import as_float, items, pct, ratio
from proxmox_ops_mcp.analysis.findings import (
    Finding,
    Severity,
    overall_status,
    sort_findings,
    threshold_finding,
)
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds

_HA_CRITICAL_STATES = frozenset({"error", "fence"})
_HA_WARNING_STATES = frozenset({"recovery"})


class NodeSummary(BaseModel):
    """Per-node snapshot taken from ``/cluster/resources``."""

    node: str
    online: bool
    cpu_pct: float
    memory_pct: float
    guests_running: int
    guests_total: int


class ClusterHealthReport(BaseModel):
    """Result of :func:`analyze_cluster`."""

    status: Severity
    cluster_name: str | None
    quorate: bool | None
    nodes_online: int
    nodes_total: int
    guests_running: int
    guests_total: int
    nodes: list[NodeSummary]
    findings: list[Finding]


def analyze_cluster(
    cluster_status: Any,
    resources: Any,
    ha_status: Any = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> ClusterHealthReport:
    """Correlate ``/cluster/status``, ``/cluster/resources`` and HA state into findings."""
    findings: list[Finding] = []
    status_entries = items(cluster_status)
    resource_entries = items(resources)

    cluster = next((e for e in status_entries if e.get("type") == "cluster"), None)
    quorate = bool(cluster.get("quorate")) if cluster else None
    if quorate is False:
        findings.append(
            Finding(
                severity=Severity.CRITICAL,
                code="CLUSTER_NOT_QUORATE",
                message="The cluster has lost quorum; configuration changes and HA are blocked.",
                resource=str(cluster.get("name")) if cluster else None,
                recommendation="Restore connectivity to a majority of nodes (corosync). "
                "Check 'pvecm status' and the corosync links before anything else.",
            )
        )

    guests = [r for r in resource_entries if r.get("type") in {"qemu", "lxc"}]
    guests = [g for g in guests if not g.get("template")]
    nodes = _summarise_nodes(resource_entries, guests)
    findings += _node_findings(nodes, resource_entries, thresholds)
    offline = {n.node for n in nodes if not n.online}
    findings += _storage_findings(resource_entries, thresholds, offline)
    findings += _ha_findings(ha_status)

    return ClusterHealthReport(
        status=overall_status(findings),
        cluster_name=str(cluster["name"]) if cluster and cluster.get("name") else None,
        quorate=quorate,
        nodes_online=sum(n.online for n in nodes),
        nodes_total=len(nodes),
        guests_running=sum(g.get("status") == "running" for g in guests),
        guests_total=len(guests),
        nodes=nodes,
        findings=sort_findings(findings),
    )


def _summarise_nodes(
    resources: list[Mapping[str, Any]], guests: list[Mapping[str, Any]]
) -> list[NodeSummary]:
    summaries = []
    for r in resources:
        if r.get("type") != "node":
            continue
        name = str(r.get("node"))
        on_node = [g for g in guests if g.get("node") == name]
        summaries.append(
            NodeSummary(
                node=name,
                online=r.get("status") == "online",
                cpu_pct=pct(as_float(r.get("cpu"))),
                memory_pct=pct(ratio(r.get("mem"), r.get("maxmem"))),
                guests_running=sum(g.get("status") == "running" for g in on_node),
                guests_total=len(on_node),
            )
        )
    return sorted(summaries, key=lambda n: n.node)


def _node_findings(
    nodes: list[NodeSummary], resources: list[Mapping[str, Any]], t: Thresholds
) -> list[Finding]:
    findings = []
    for n in nodes:
        if not n.online:
            findings.append(
                Finding(
                    severity=Severity.CRITICAL,
                    code="NODE_OFFLINE",
                    message=f"Node {n.node} is offline ({n.guests_total} guests assigned).",
                    resource=n.node,
                    recommendation="Check power, network and 'systemctl status pve-cluster "
                    "corosync' on the node. HA-managed guests should be recovered elsewhere.",
                )
            )
            continue
        findings += threshold_finding(
            n.cpu_pct / 100,
            t.cpu_warning,
            t.cpu_critical,
            code="NODE_CPU_HIGH",
            message=f"Node {n.node} CPU usage is {n.cpu_pct}%.",
            resource=n.node,
            recommendation="Identify the busiest guests with compare_nodes; move load with "
            "recommend_placement.",
        )
        findings += threshold_finding(
            n.memory_pct / 100,
            t.memory_warning,
            t.memory_critical,
            code="NODE_MEMORY_HIGH",
            message=f"Node {n.node} memory usage is {n.memory_pct}%.",
            resource=n.node,
            recommendation="Check for ballooning limits, ZFS ARC size and over-committed guests.",
        )
    return findings


def _storage_findings(
    resources: list[Mapping[str, Any]], t: Thresholds, offline_nodes: set[str]
) -> list[Finding]:
    findings: list[Finding] = []
    seen_shared: set[str] = set()
    for r in resources:
        if r.get("type") != "storage":
            continue
        storage = str(r.get("storage"))
        node = str(r.get("node"))
        if node in offline_nodes:
            continue  # already covered by NODE_OFFLINE
        if r.get("shared"):
            if storage in seen_shared:
                continue
            seen_shared.add(storage)
            label = storage
        else:
            label = f"{node}/{storage}"
        if r.get("status") not in (None, "available"):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code="STORAGE_UNAVAILABLE",
                    message=f"Storage {label} reports status '{r.get('status')}'.",
                    resource=label,
                    recommendation="Verify the backing device or network share is reachable.",
                )
            )
            continue
        used = ratio(r.get("disk"), r.get("maxdisk"))
        findings += threshold_finding(
            used,
            t.storage_warning,
            t.storage_critical,
            code="STORAGE_USAGE_HIGH",
            message=f"Storage {label} is {pct(used)}% full.",
            resource=label,
            recommendation="Prune old backups/snapshots or extend the storage. "
            "Thin-provisioned pools fail hard when they run out of space.",
        )
    return findings


def _ha_findings(ha_status: Any) -> list[Finding]:
    findings = []
    for entry in items(ha_status):
        kind = entry.get("type")
        if kind == "service":
            state = str(entry.get("state", "")).lower()
            sid = str(entry.get("sid") or entry.get("id"))
            if state in _HA_CRITICAL_STATES or state in _HA_WARNING_STATES:
                findings.append(
                    Finding(
                        severity=Severity.CRITICAL
                        if state in _HA_CRITICAL_STATES
                        else Severity.WARNING,
                        code="HA_SERVICE_DEGRADED",
                        message=f"HA resource {sid} is in state '{state}'.",
                        resource=sid,
                        recommendation="Inspect 'ha-manager status' and the task log. A "
                        "resource in 'error' must be fixed and set to 'disabled' before "
                        "re-enabling.",
                    )
                )
        elif kind == "lrm":
            status = str(entry.get("status", "")).lower()
            if "old timestamp" in status or "dead" in status:
                node = str(entry.get("node"))
                findings.append(
                    Finding(
                        severity=Severity.WARNING,
                        code="HA_LRM_STALE",
                        message=f"HA local resource manager on {node} reports '{status}'.",
                        resource=node,
                        recommendation="Check 'systemctl status pve-ha-lrm' on the node.",
                    )
                )
    return findings


__all__ = ["ClusterHealthReport", "NodeSummary", "analyze_cluster"]
