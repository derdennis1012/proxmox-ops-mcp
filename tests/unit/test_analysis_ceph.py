from __future__ import annotations

from typing import Any

from proxmox_ops_mcp.analysis.ceph import analyze_ceph
from proxmox_ops_mcp.analysis.findings import Severity
from tests.fakes import GIB, build_cluster


def _healthy() -> dict[str, Any]:
    status: dict[str, Any] = build_cluster()["cluster/ceph/status"]
    return status


def test_healthy() -> None:
    report = analyze_ceph(_healthy())
    assert report.status is Severity.INFO
    assert report.ceph_health == "HEALTH_OK"
    assert report.osds.total == report.osds.up == 9
    assert report.pgs.active_clean_pct == 100.0
    assert (report.mons.total, report.mons.down) == (3, 0)
    assert report.capacity.used_pct == 40.0


def test_degraded_cluster() -> None:
    status = _healthy()
    status["health"] = {
        "status": "HEALTH_ERR",
        "checks": {
            "OSD_DOWN": {"severity": "HEALTH_WARN", "summary": {"message": "1 osds down"}},
            "PG_AVAILABILITY": {
                "severity": "HEALTH_ERR",
                "summary": {"message": "Reduced data availability: 2 pgs inactive"},
            },
            "MON_CLOCK_SKEW": {"severity": "HEALTH_WARN", "summary": {}, "muted": True},
            "SOMETHING_NEW": {"severity": "HEALTH_WARN", "summary": {"message": "new"}},
        },
    }
    status["osdmap"] = {"num_osds": 9, "num_up_osds": 8, "num_in_osds": 9}
    status["pgmap"]["pgs_by_state"] = [
        {"state_name": "active+clean", "count": 240},
        {"state_name": "active+clean+scrubbing+deep", "count": 10},
        {"state_name": "active+undersized+degraded", "count": 4},
        {"state_name": "undersized+degraded+peered", "count": 2},
    ]
    status["quorum_names"] = ["pve1", "pve2"]
    report = analyze_ceph(status)

    assert report.status is Severity.CRITICAL
    assert report.osds.down == 1
    assert report.pgs.active_clean == 250
    assert report.pgs.active_clean_pct == 97.7
    assert next(iter(report.pgs.by_state)) == "active+clean"  # sorted by count
    assert report.mons.down == 1

    by_code = {f.code: f for f in report.findings}
    assert by_code["CEPH_PG_AVAILABILITY"].severity is Severity.CRITICAL
    assert by_code["CEPH_OSD_DOWN"].message == "1 osds down"
    assert "systemctl" in (by_code["CEPH_OSD_DOWN"].recommendation or "")
    assert by_code["CEPH_MON_CLOCK_SKEW"].severity is Severity.INFO  # muted
    assert "ceph health detail" in (by_code["CEPH_SOMETHING_NEW"].recommendation or "")
    assert "CEPH_MON_OUT_OF_QUORUM" in by_code
    assert report.findings[0].severity is Severity.CRITICAL


def test_legacy_nested_osdmap_and_capacity() -> None:
    status = _healthy()
    status["osdmap"] = {"osdmap": {"num_osds": 3, "num_up_osds": 3, "num_in_osds": 2}}
    status["pgmap"]["bytes_used"] = 9200 * GIB
    report = analyze_ceph(status)
    assert report.osds.out == 1
    capacity = [f for f in report.findings if f.code == "CEPH_CAPACITY_HIGH"]
    assert capacity
    assert capacity[0].severity is Severity.CRITICAL


def test_legacy_monmap_with_member_list() -> None:
    status = _healthy()
    status["monmap"] = {"mons": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
    status["quorum_names"] = ["a", "b"]
    report = analyze_ceph(status)
    assert report.mons.down == 1
    assert "CEPH_MON_OUT_OF_QUORUM" in {f.code for f in report.findings}


def test_missing_health_is_flagged() -> None:
    report = analyze_ceph({})
    assert report.ceph_health == "HEALTH_UNKNOWN"
    assert {f.code for f in report.findings} == {"CEPH_HEALTH_UNKNOWN"}
    assert report.pgs.active_clean_pct == 100.0
