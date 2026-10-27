"""Local storage health: ZFS pools and physical disks (SMART)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from proxmox_ops_mcp.analysis._util import as_float, as_int, gib, items, pct, ratio
from proxmox_ops_mcp.analysis.findings import (
    Finding,
    Severity,
    overall_status,
    sort_findings,
    threshold_finding,
    unavailable_findings,
)
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds

_SCRUB_DATE_RE = re.compile(r" on (\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})")
_ZFS_CRITICAL_STATES = frozenset({"FAULTED", "UNAVAIL", "SUSPENDED", "REMOVED"})
_HEALTHY_LEAF_STATES = frozenset({"ONLINE", "AVAIL"})  # AVAIL = idle hot spare
# ATA attributes that count sectors the drive had to give up on.
_BAD_SECTOR_ATTRIBUTES = {
    5: "reallocated sectors",
    197: "pending sectors",
    198: "offline-uncorrectable sectors",
}
_NVME_MEDIA_ERRORS_RE = re.compile(r"Media and Data Integrity Errors:\s+([\d,]+)")
_NVME_CRITICAL_WARNING_RE = re.compile(r"Critical Warning:\s+0x([0-9a-fA-F]+)")


class ZfsPool(BaseModel):
    """One ZFS pool on one node."""

    node: str
    name: str
    health: str
    used_pct: float
    size_gib: float
    fragmentation_pct: float
    last_scrub: datetime | None
    scrub_age_days: float | None
    scan: str | None
    errors: str | None
    degraded_devices: list[str]


class DiskHealth(BaseModel):
    """One physical disk on one node."""

    node: str
    device: str
    model: str | None
    type: str
    size_gib: float
    smart_health: str
    life_remaining_pct: float | None
    bad_sectors: dict[str, int]
    used_for: str | None


class StorageHealthReport(BaseModel):
    """Result of :func:`analyze_storage`."""

    status: Severity
    pools: list[ZfsPool]
    disks: list[DiskHealth]
    findings: list[Finding]


def parse_scrub_date(scan: str | None) -> datetime | None:
    """Extract the completion time from a ``zpool status`` scan line, if any."""
    # Only a finished *scrub* counts; "resilvered ... on <date>" must not reset the clock.
    match = _SCRUB_DATE_RE.search(scan or "") if (scan or "").startswith("scrub ") else None
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.group(1).split()), "%a %b %d %H:%M:%S %Y").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def analyze_storage(
    *,
    pools: Mapping[str, Any],
    pool_details: Mapping[tuple[str, str], Mapping[str, Any]],
    disks: Mapping[str, Any],
    smart: Mapping[tuple[str, str], Mapping[str, Any]],
    now: datetime | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    unavailable: Mapping[str, str] | None = None,
) -> StorageHealthReport:
    """Evaluate ZFS pools and disks.

    Args:
        pools: node -> ``/nodes/{node}/disks/zfs``.
        pool_details: (node, pool) -> ``/nodes/{node}/disks/zfs/{pool}``.
        disks: node -> ``/nodes/{node}/disks/list``.
        smart: (node, devpath) -> ``/nodes/{node}/disks/smart`` for that disk.
        unavailable: Data sources that could not be read, mapped to the reason.
        now: Reference time for scrub age.
        thresholds: Fill-level thresholds (shared with other storage checks).
    """
    now = now or datetime.now(UTC)
    findings: list[Finding] = unavailable_findings(unavailable or {})
    pool_reports: list[ZfsPool] = []
    for node, node_pools in sorted(pools.items()):
        for p in items(node_pools):
            detail = pool_details.get((node, str(p.get("name")))) or {}
            report = _pool(node, p, detail, now)
            pool_reports.append(report)
            findings += _pool_findings(report, thresholds)

    disk_reports: list[DiskHealth] = []
    for node, node_disks in sorted(disks.items()):
        for d in items(node_disks):
            disk = _disk(node, d, smart.get((node, str(d.get("devpath")))) or {})
            disk_reports.append(disk)
            findings += _disk_findings(disk, thresholds)

    return StorageHealthReport(
        status=overall_status(findings),
        pools=pool_reports,
        disks=disk_reports,
        findings=sort_findings(findings),
    )


def _degraded_leaves(children: Any, path: str = "") -> list[str]:
    out: list[str] = []
    for child in items(children):
        name = str(child.get("name"))
        state = str(child.get("state", "ONLINE"))
        errors = sum(as_int(child.get(k)) for k in ("read", "write", "cksum"))
        if child.get("leaf") and (state not in _HEALTHY_LEAF_STATES or errors):
            detail = f"{state}" + (f", {errors} I/O/checksum errors" if errors else "")
            out.append(f"{path}{name} ({detail})")
        out += _degraded_leaves(
            child.get("children"), f"{path}{name} > " if not child.get("leaf") else path
        )
    return out


def _pool(node: str, p: Mapping[str, Any], detail: Mapping[str, Any], now: datetime) -> ZfsPool:
    scan = detail.get("scan")
    last = parse_scrub_date(str(scan) if scan else None)
    return ZfsPool(
        node=node,
        name=str(p.get("name")),
        health=str(p.get("health") or detail.get("state") or "UNKNOWN"),
        used_pct=pct(ratio(p.get("alloc"), p.get("size"))),
        size_gib=gib(p.get("size")),
        fragmentation_pct=as_float(p.get("frag")),
        last_scrub=last,
        scrub_age_days=round((now - last).total_seconds() / 86400, 1) if last else None,
        scan=str(scan) if scan else None,
        errors=str(detail["errors"]) if detail.get("errors") else None,
        degraded_devices=_degraded_leaves(detail.get("children")),
    )


def _pool_findings(p: ZfsPool, t: Thresholds) -> list[Finding]:
    label = f"{p.node}/{p.name}"
    findings: list[Finding] = []
    if p.health != "ONLINE":
        findings.append(
            Finding(
                severity=Severity.CRITICAL
                if p.health in _ZFS_CRITICAL_STATES
                else Severity.WARNING,
                code="ZFS_POOL_UNHEALTHY",
                message=f"ZFS pool {label} is {p.health}."
                + (f" Affected: {'; '.join(p.degraded_devices)}." if p.degraded_devices else ""),
                resource=label,
                recommendation="Run 'zpool status -x' on the node. Replace failed devices with "
                "'zpool replace' and let the resilver finish before further maintenance.",
            )
        )
    elif p.degraded_devices:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="ZFS_DEVICE_ERRORS",
                message=f"Devices in {label} report errors: {'; '.join(p.degraded_devices)}.",
                resource=label,
                recommendation="Check the disk's SMART data and cabling. If the errors were "
                "transient, clear them with 'zpool clear' and watch whether they come back.",
            )
        )
    if p.errors and p.errors != "No known data errors":
        findings.append(
            Finding(
                severity=Severity.CRITICAL,
                code="ZFS_DATA_ERRORS",
                message=f"ZFS pool {label} reports: {p.errors}.",
                resource=label,
                recommendation="Run 'zpool status -v' to list the affected files and restore "
                "them from backup.",
            )
        )
    findings += threshold_finding(
        p.used_pct / 100,
        t.storage_warning,
        t.storage_critical,
        code="ZFS_POOL_FULL",
        message=f"ZFS pool {label} is {p.used_pct}% full.",
        resource=label,
        recommendation="ZFS slows down sharply above ~80 %. Free space or add vdevs.",
    )
    scrubbing = bool(p.scan and "in progress" in p.scan)  # running scrub/resilver is fine
    if scrubbing:
        return findings
    if p.scrub_age_days is None:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="ZFS_SCRUB_OVERDUE",
                message=f"ZFS pool {label} has no completed scrub on record.",
                resource=label,
                recommendation="Run 'zpool scrub' and check that the monthly scrub timer "
                "(zfsutils-linux) is enabled.",
            )
        )
    elif p.scrub_age_days > t.scrub_max_age_days:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="ZFS_SCRUB_OVERDUE",
                message=f"Last scrub of {label} finished {p.scrub_age_days} days ago.",
                resource=label,
                recommendation="Scrubs find silent corruption while redundancy can still "
                "repair it. Check the scrub schedule.",
            )
        )
    return findings


def _disk(node: str, d: Mapping[str, Any], smart: Mapping[str, Any]) -> DiskHealth:
    wearout = d.get("wearout")
    remaining = as_float(wearout, -1) if wearout not in (None, "N/A", "") else -1
    return DiskHealth(
        node=node,
        device=str(d.get("devpath")),
        model=str(d["model"]).strip() if d.get("model") else None,
        type=str(d.get("type", "unknown")),
        size_gib=gib(d.get("size")),
        smart_health=str(d.get("health") or smart.get("health") or "UNKNOWN"),
        life_remaining_pct=remaining if remaining >= 0 else None,
        bad_sectors=_bad_sectors(smart),
        used_for=str(d["used"]) if d.get("used") else None,
    )


def _bad_sectors(smart: Mapping[str, Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for attr in items(smart.get("attributes")):
        label = _BAD_SECTOR_ATTRIBUTES.get(as_int(str(attr.get("id", "")).strip()))
        raw = as_int(str(attr.get("raw", "0")).split()[0] if attr.get("raw") else 0)
        if label and raw > 0:
            found[label] = raw
    text = str(smart.get("text") or "")
    media = _NVME_MEDIA_ERRORS_RE.search(text)
    if media and as_int(media.group(1).replace(",", "")) > 0:
        found["media and data integrity errors"] = as_int(media.group(1).replace(",", ""))
    warning = _NVME_CRITICAL_WARNING_RE.search(text)
    if warning and int(warning.group(1), 16):
        found["NVMe critical warning flags"] = int(warning.group(1), 16)
    return found


def _disk_findings(d: DiskHealth, t: Thresholds) -> list[Finding]:
    label = f"{d.node}:{d.device}"
    what = f"{d.model or 'disk'} {d.device} on {d.node}"
    findings: list[Finding] = []
    if d.smart_health.upper() not in {"PASSED", "OK", "UNKNOWN"}:
        findings.append(
            Finding(
                severity=Severity.CRITICAL,
                code="DISK_SMART_FAILED",
                message=f"SMART health of {what} is {d.smart_health}.",
                resource=label,
                recommendation="Treat the disk as failing: make sure redundancy and backups are "
                "intact and replace it.",
            )
        )
    if d.life_remaining_pct is not None:
        used = 100 - d.life_remaining_pct
        findings += threshold_finding(
            used / 100,
            1 - t.disk_life_warning,
            1 - t.disk_life_critical,
            code="DISK_WEAROUT",
            message=f"{what} has {d.life_remaining_pct:.0f}% of its rated write endurance left.",
            resource=label,
            recommendation="Plan a replacement. Mirror partners of the same age wear out at "
            "the same rate.",
        )
    if d.bad_sectors:
        detail = ", ".join(f"{n} {k}" for k, n in d.bad_sectors.items())
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="DISK_MEDIA_ERRORS",
                message=f"{what} reports {detail}.",
                resource=label,
                recommendation="A rising count means the disk is failing. Compare with earlier "
                "values and replace it before redundancy is needed.",
            )
        )
    return findings
