from __future__ import annotations

from typing import Any

from proxmox_ops_mcp.analysis.cluster import analyze_cluster
from proxmox_ops_mcp.analysis.findings import Finding, Severity, overall_status, sort_findings
from tests.fakes import GIB, build_cluster


def _codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


def test_healthy_cluster_has_no_findings() -> None:
    c = build_cluster()
    report = analyze_cluster(
        c["cluster/status"], c["cluster/resources"], c["cluster/ha/status/current"]
    )
    assert report.status is Severity.INFO
    assert report.findings == []
    assert report.quorate is True
    assert report.cluster_name == "prod"
    assert (report.nodes_online, report.nodes_total) == (3, 3)
    assert report.guests_total == 5  # template excluded
    assert report.guests_running == 4


def test_lost_quorum_and_offline_node_are_critical() -> None:
    c = build_cluster()
    c["cluster/status"][0]["quorate"] = 0
    c["cluster/resources"][2]["status"] = "offline"
    report = analyze_cluster(c["cluster/status"], c["cluster/resources"])
    assert report.status is Severity.CRITICAL
    assert {"CLUSTER_NOT_QUORATE", "NODE_OFFLINE"} <= _codes(report.findings)
    assert report.findings[0].severity is Severity.CRITICAL


def test_standalone_node_reports_unknown_quorum() -> None:
    c = build_cluster()
    report = analyze_cluster([{"type": "node", "name": "pve1"}], c["cluster/resources"])
    assert report.quorate is None
    assert report.cluster_name is None


def test_resource_pressure_thresholds() -> None:
    c = build_cluster()
    node = c["cluster/resources"][0]
    node["cpu"] = 0.97
    node["mem"] = int(0.9 * node["maxmem"])
    report = analyze_cluster(c["cluster/status"], c["cluster/resources"])
    by_code = {f.code: f for f in report.findings}
    assert by_code["NODE_CPU_HIGH"].severity is Severity.CRITICAL
    assert by_code["NODE_MEMORY_HIGH"].severity is Severity.WARNING


def test_shared_storage_is_reported_once() -> None:
    c = build_cluster()
    for r in c["cluster/resources"]:
        if r.get("storage") == "ceph-vm":
            r["disk"] = 9500 * GIB
        if r.get("storage") == "local-lvm" and r["node"] == "pve2":
            r["status"] = "unknown"
    report = analyze_cluster(c["cluster/status"], c["cluster/resources"])
    full = [f for f in report.findings if f.code == "STORAGE_USAGE_HIGH"]
    assert len(full) == 1
    assert full[0].resource == "ceph-vm"
    assert full[0].severity is Severity.CRITICAL
    unavailable = [f for f in report.findings if f.code == "STORAGE_UNAVAILABLE"]
    assert [f.resource for f in unavailable] == ["pve2/local-lvm"]


def test_ha_problems() -> None:
    ha: list[dict[str, Any]] = [
        {"type": "service", "sid": "vm:100", "state": "error"},
        {"type": "service", "sid": "vm:101", "state": "recovery"},
        {"type": "service", "sid": "vm:102", "state": "started"},
        {"type": "lrm", "node": "pve2", "status": "old timestamp - dead?"},
    ]
    c = build_cluster()
    report = analyze_cluster(c["cluster/status"], c["cluster/resources"], ha)
    degraded = {f.resource: f.severity for f in report.findings if f.code == "HA_SERVICE_DEGRADED"}
    assert degraded == {"vm:100": Severity.CRITICAL, "vm:101": Severity.WARNING}
    assert "HA_LRM_STALE" in _codes(report.findings)


def test_garbage_input_is_tolerated() -> None:
    report = analyze_cluster(None, "nonsense", 42)
    assert report.nodes == []
    assert report.status is Severity.INFO


def test_finding_helpers() -> None:
    a = Finding(severity=Severity.WARNING, code="B", message="m")
    b = Finding(severity=Severity.CRITICAL, code="A", message="m")
    c = Finding(severity=Severity.WARNING, code="A", message="m")
    assert overall_status([]) is Severity.INFO
    assert overall_status([a, b]) is Severity.CRITICAL
    assert sort_findings([a, b, c]) == [b, c, a]
