"""ZFS and disk health, tested against payloads recorded on Proxmox VE 9.2."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proxmox_ops_mcp.analysis.findings import Severity
from proxmox_ops_mcp.analysis.storage import analyze_storage, parse_scrub_date

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pve9"
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _analyze(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "pools": {"pve1": _load("disks_zfs.json")},
        "pool_details": {
            ("pve1", "tank"): _load("disks_zfs_tank.json"),
            ("pve1", "backup"): _load("disks_zfs_backup.json"),
        },
        "disks": {"pve1": _load("disks_list.json")},
        "smart": {
            ("pve1", "/dev/sda"): _load("disks_smart_sda.json"),
            ("pve1", "/dev/nvme0n1"): _load("disks_smart_nvme0n1.json"),
        },
        "now": NOW,
    }
    kwargs.update(overrides)
    return analyze_storage(**kwargs)


def test_recorded_healthy_host() -> None:
    report = _analyze()
    assert report.status is Severity.INFO
    assert report.findings == []
    tank = next(p for p in report.pools if p.name == "tank")
    assert tank.health == "ONLINE"
    assert tank.scrub_age_days == 16.0
    nvme = next(d for d in report.disks if d.device == "/dev/nvme0n1")
    assert nvme.life_remaining_pct == 96  # PVE reports remaining life ("Percentage Used: 4%")
    hdd = next(d for d in report.disks if d.device == "/dev/sda")
    assert hdd.life_remaining_pct is None  # HDDs report "N/A"


def test_scrub_date_parsing() -> None:
    assert parse_scrub_date(
        "scrub repaired 0B in 00:05:43 with 0 errors on Sun Oct  4 00:29:47 2026"
    ) == datetime(2026, 10, 4, 0, 29, 47, tzinfo=UTC)
    assert parse_scrub_date("none requested") is None
    assert parse_scrub_date(None) is None


def test_degraded_pool_with_errors() -> None:
    tank = _load("disks_zfs_tank.json")
    leaf = tank["children"][0]["children"][0]["children"][0]  # pool > mirror-0 > device
    leaf.update(state="FAULTED", read=3, cksum=12)
    tank.update(state="DEGRADED", errors="2 data errors, use '-v' for a list")
    pools = _load("disks_zfs.json")
    next(p for p in pools if p["name"] == "tank")["health"] = "DEGRADED"
    report = _analyze(pools={"pve1": pools}, pool_details={("pve1", "tank"): tank})
    by_code = {f.code: f for f in report.findings}
    assert by_code["ZFS_POOL_UNHEALTHY"].severity is Severity.WARNING
    assert "FAULTED, 15 I/O/checksum errors" in by_code["ZFS_POOL_UNHEALTHY"].message
    assert by_code["ZFS_DATA_ERRORS"].severity is Severity.CRITICAL
    assert report.status is Severity.CRITICAL


def test_device_errors_on_online_pool_and_faulted_pool() -> None:
    tank = _load("disks_zfs_tank.json")
    tank["children"][0]["children"][0]["children"][1]["cksum"] = 4
    report = _analyze(pool_details={("pve1", "tank"): tank})
    assert "ZFS_DEVICE_ERRORS" in {f.code for f in report.findings}

    pools = _load("disks_zfs.json")
    pools[0]["health"] = "SUSPENDED"
    report = _analyze(pools={"pve1": pools})
    assert report.findings[0].severity is Severity.CRITICAL


def test_full_pool_and_scrub_state() -> None:
    pools = _load("disks_zfs.json")
    tank = next(p for p in pools if p["name"] == "tank")
    tank["alloc"] = int(tank["size"] * 0.93)
    report = _analyze(pools={"pve1": pools}, now=datetime(2026, 12, 31, tzinfo=UTC))
    codes = [(f.code, f.severity) for f in report.findings]
    assert ("ZFS_POOL_FULL", Severity.CRITICAL) in codes
    assert sum(c == "ZFS_SCRUB_OVERDUE" for c, _ in codes) == 2

    never = copy.deepcopy(_load("disks_zfs_tank.json")) | {"scan": "none requested"}
    running = copy.deepcopy(_load("disks_zfs_backup.json")) | {
        "scan": "scrub in progress since Tue Oct 27 01:00:00 2026"
    }
    report = _analyze(
        pool_details={("pve1", "tank"): never, ("pve1", "backup"): running},
        now=datetime(2026, 12, 31, tzinfo=UTC),
    )
    overdue = [f for f in report.findings if f.code == "ZFS_SCRUB_OVERDUE"]
    assert [f.resource for f in overdue] == ["pve1/tank"]
    assert "no completed scrub" in overdue[0].message


def test_failing_disks() -> None:
    disks = _load("disks_list.json")
    disks[0]["wearout"] = 8
    disks[1]["wearout"] = 15
    disks[2]["health"] = "FAILED"
    sda = _load("disks_smart_sda.json")
    for attr in sda["attributes"]:
        if attr["id"].strip() == "5":
            attr["raw"] = "24"
    nvme = _load("disks_smart_nvme0n1.json")
    nvme["text"] = nvme["text"].replace(
        "Critical Warning:                   0x00", "Critical Warning:                   0x04"
    )
    nvme["text"] = nvme["text"].replace(
        "Media and Data Integrity Errors:    0", "Media and Data Integrity Errors:    7"
    )
    report = _analyze(
        disks={"pve1": disks},
        smart={("pve1", "/dev/sda"): sda, ("pve1", "/dev/nvme0n1"): nvme},
    )
    by = {(f.code, f.resource): f.severity for f in report.findings}
    assert by[("DISK_WEAROUT", "pve1:/dev/nvme0n1")] is Severity.CRITICAL
    assert by[("DISK_WEAROUT", "pve1:/dev/nvme1n1")] is Severity.WARNING
    assert by[("DISK_SMART_FAILED", "pve1:/dev/sda")] is Severity.CRITICAL
    assert by[("DISK_MEDIA_ERRORS", "pve1:/dev/sda")] is Severity.WARNING
    nvme_disk = next(d for d in report.disks if d.device == "/dev/nvme0n1")
    assert nvme_disk.bad_sectors == {
        "media and data integrity errors": 7,
        "NVMe critical warning flags": 4,
    }


def test_empty_inputs() -> None:
    report = analyze_storage(pools={}, pool_details={}, disks={}, smart={})
    assert report.pools == []
    assert report.status is Severity.INFO


def test_hot_spare_is_not_an_error_and_resilver_does_not_count_as_scrub() -> None:
    tank = _load("disks_zfs_tank.json")
    tank["children"].append(
        {"name": "spares", "leaf": 0, "children": [
            {"name": "/dev/sdz", "leaf": 1, "state": "AVAIL", "read": 0, "write": 0, "cksum": 0}
        ]}
    )  # fmt: skip
    tank["scan"] = "resilvered 1.2G in 00:01:02 with 0 errors on Mon Oct 26 10:00:00 2026"
    report = _analyze(pool_details={("pve1", "tank"): tank})
    codes = {(f.code, f.resource) for f in report.findings}
    assert ("ZFS_DEVICE_ERRORS", "pve1/tank") not in codes
    assert ("ZFS_SCRUB_OVERDUE", "pve1/tank") in codes  # a resilver is not a scrub


def test_unreadable_sources_never_look_healthy() -> None:
    report = analyze_storage(
        pools={"pve1": []},
        pool_details={},
        disks={"pve1": []},
        smart={},
        unavailable={"nodes/pve1/disks/zfs": "403 Forbidden"},
    )
    assert report.status is Severity.WARNING
    assert report.findings[0].code == "DATA_UNAVAILABLE"
