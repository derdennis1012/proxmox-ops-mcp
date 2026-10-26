"""Ceph health analysis based on ``/cluster/ceph/status`` (``ceph status`` as JSON)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from proxmox_ops_mcp.analysis._util import as_float, as_int, gib, items, pct, ratio
from proxmox_ops_mcp.analysis.findings import (
    Finding,
    Severity,
    overall_status,
    sort_findings,
    threshold_finding,
)
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds

_HEALTH_SEVERITY = {
    "HEALTH_OK": Severity.INFO,
    "HEALTH_WARN": Severity.WARNING,
    "HEALTH_ERR": Severity.CRITICAL,
}

# Operator guidance for the health checks that show up most in practice.
# Reference: https://docs.ceph.com/en/latest/rados/operations/health-checks/
_RECOMMENDATIONS = {
    "OSD_DOWN": "Check the OSD service ('systemctl status ceph-osd@<id>') and the "
    "underlying disk (SMART, dmesg). Do not mark OSDs out manually unless the disk is dead.",
    "OSD_HOST_DOWN": "A whole host's OSDs are down: check node power and the Ceph "
    "public/cluster networks.",
    "OSD_NEARFULL": "Rebalance or add capacity now; writes stop at the full ratio.",
    "OSD_BACKFILLFULL": "Backfill is blocked on these OSDs; add capacity or reweight.",
    "OSD_FULL": "The cluster is refusing writes. Free space or add OSDs immediately.",
    "POOL_NEARFULL": "Review pool quotas and usage; plan capacity expansion.",
    "POOL_FULL": "Pool is full and refusing writes. Free space or raise the quota.",
    "PG_DEGRADED": "Data has fewer replicas than configured. Usually recovers by itself "
    "once OSDs are back; watch recovery progress.",
    "PG_AVAILABILITY": "Some data is inaccessible. Bring the affected OSDs back up as top "
    "priority; guest I/O on these PGs is blocked.",
    "PG_DAMAGED": "Scrub found inconsistencies. Inspect with 'ceph health detail' and "
    "'rados list-inconsistent-obj' before running 'ceph pg repair'.",
    "MON_DOWN": "A monitor is down; quorum is at risk if another fails.",
    "MON_CLOCK_SKEW": "Fix time synchronisation (chrony) between monitor hosts.",
    "SLOW_OPS": "Requests are blocked. Look for a failing disk or saturated network.",
    "RECENT_CRASH": "Inspect with 'ceph crash ls' / 'ceph crash info', then archive.",
    "OSDMAP_FLAGS": "Cluster flags such as noout/norebalance are set. Unset them once "
    "maintenance is finished.",
    "PG_NOT_SCRUBBED": "Scrubbing is behind schedule; check OSD load and scrub settings.",
    "PG_NOT_DEEP_SCRUBBED": "Deep scrubbing is behind schedule; check OSD load and "
    "osd_deep_scrub_interval.",
}


class OsdSummary(BaseModel):
    """OSD counts."""

    total: int
    up: int
    in_cluster: int
    down: int
    out: int


class PgSummary(BaseModel):
    """Placement group state distribution."""

    total: int
    active_clean: int
    active_clean_pct: float
    by_state: dict[str, int]


class MonSummary(BaseModel):
    """Monitor quorum."""

    total: int
    in_quorum: list[str]
    down: int


class CapacitySummary(BaseModel):
    """Raw cluster capacity."""

    used_gib: float
    total_gib: float
    used_pct: float


class CephHealthReport(BaseModel):
    """Result of :func:`analyze_ceph`."""

    status: Severity
    ceph_health: str
    fsid: str | None
    osds: OsdSummary
    pgs: PgSummary
    mons: MonSummary
    capacity: CapacitySummary
    findings: list[Finding]


def analyze_ceph(
    status: Mapping[str, Any], thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> CephHealthReport:
    """Turn a ``ceph status`` JSON document into a structured health report."""
    findings: list[Finding] = []
    health = status.get("health") or {}
    health_status = str(health.get("status", "HEALTH_UNKNOWN"))

    checks = health.get("checks") or {}
    for name, check in sorted(checks.items()):
        severity = _HEALTH_SEVERITY.get(str(check.get("severity")), Severity.WARNING)
        if check.get("muted"):
            severity = Severity.INFO
        summary = check.get("summary") or {}
        findings.append(
            Finding(
                severity=severity,
                code=f"CEPH_{name}",
                message=str(summary.get("message") or name),
                resource="ceph",
                recommendation=_RECOMMENDATIONS.get(
                    name, "Run 'ceph health detail' for the affected objects."
                ),
            )
        )
    if health_status == "HEALTH_UNKNOWN":
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="CEPH_HEALTH_UNKNOWN",
                message="Ceph did not report a health status.",
                resource="ceph",
                recommendation="Check that the monitors are reachable from the queried node.",
            )
        )

    osds = _osd_summary(status)
    pgs = _pg_summary(status)
    mons = _mon_summary(status)
    capacity = _capacity(status)

    if mons.down and not any(f.code == "CEPH_MON_DOWN" for f in findings):
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="CEPH_MON_OUT_OF_QUORUM",
                message=f"{mons.down} of {mons.total} monitors are out of quorum "
                f"(in quorum: {', '.join(mons.in_quorum) or 'none'}).",
                resource="ceph",
                recommendation=_RECOMMENDATIONS["MON_DOWN"],
            )
        )
    findings += threshold_finding(
        capacity.used_pct / 100,
        thresholds.storage_warning,
        thresholds.storage_critical,
        code="CEPH_CAPACITY_HIGH",
        message=f"Ceph raw capacity is {capacity.used_pct}% used. Remember that losing a host "
        "requires free space to re-replicate its data.",
        resource="ceph",
        recommendation="Keep enough headroom to survive the loss of your largest failure domain.",
    )

    return CephHealthReport(
        status=overall_status(findings),
        ceph_health=health_status,
        fsid=str(status["fsid"]) if status.get("fsid") else None,
        osds=osds,
        pgs=pgs,
        mons=mons,
        capacity=capacity,
        findings=sort_findings(findings),
    )


def _osd_summary(status: Mapping[str, Any]) -> OsdSummary:
    osdmap = status.get("osdmap") or {}
    # Ceph < 17 nests the counters one level deeper.
    if isinstance(osdmap.get("osdmap"), Mapping):
        osdmap = osdmap["osdmap"]
    total = as_int(osdmap.get("num_osds"))
    up = as_int(osdmap.get("num_up_osds"))
    in_cluster = as_int(osdmap.get("num_in_osds"))
    return OsdSummary(
        total=total,
        up=up,
        in_cluster=in_cluster,
        down=max(total - up, 0),
        out=max(total - in_cluster, 0),
    )


def _pg_summary(status: Mapping[str, Any]) -> PgSummary:
    pgmap = status.get("pgmap") or {}
    by_state = {
        str(s.get("state_name")): as_int(s.get("count")) for s in items(pgmap.get("pgs_by_state"))
    }
    total = as_int(pgmap.get("num_pgs")) or sum(by_state.values())
    # "active+clean+scrubbing+deep" and friends are healthy too.
    clean = sum(n for state, n in by_state.items() if {"active", "clean"} <= set(state.split("+")))
    return PgSummary(
        total=total,
        active_clean=clean,
        active_clean_pct=pct(ratio(clean, total)) if total else 100.0,
        by_state=dict(sorted(by_state.items(), key=lambda kv: -kv[1])),
    )


def _mon_summary(status: Mapping[str, Any]) -> MonSummary:
    # `ceph status` reports only `monmap.num_mons`; older releases embed the full list.
    monmap = status.get("monmap") or {}
    quorum = [str(n) for n in status.get("quorum_names") or []]
    total = as_int(monmap.get("num_mons")) or len(items(monmap.get("mons"))) or len(quorum)
    return MonSummary(total=total, in_quorum=quorum, down=max(total - len(quorum), 0))


def _capacity(status: Mapping[str, Any]) -> CapacitySummary:
    pgmap = status.get("pgmap") or {}
    used, total = as_float(pgmap.get("bytes_used")), as_float(pgmap.get("bytes_total"))
    return CapacitySummary(
        used_gib=gib(used), total_gib=gib(total), used_pct=pct(ratio(used, total))
    )
