from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from proxmox_ops_mcp.analysis.backups import BackupVerdict, audit_backups
from proxmox_ops_mcp.analysis.findings import Severity
from tests.fakes import NOW, build_cluster


def _audit(**overrides: Any) -> Any:
    c = build_cluster()
    kwargs: dict[str, Any] = {
        "guests": [r for r in c["cluster/resources"] if r["type"] in {"qemu", "lxc"}],
        "backup_volumes": c["nodes/pve1/storage/nfs-backup/content"],
        "jobs": c["cluster/backup"],
        "max_age_hours": 26,
        "now": datetime.fromtimestamp(NOW, UTC),
    }
    kwargs.update(overrides)
    return audit_backups(**kwargs)


def test_verdicts_per_guest() -> None:
    report = _audit()
    verdicts = {g.vmid: g.verdict for g in report.guests}
    assert verdicts == {
        100: BackupVerdict.OK,
        101: BackupVerdict.STALE,
        102: BackupVerdict.MISSING,
        103: BackupVerdict.MISSING,
        200: BackupVerdict.OK,
    }
    assert 9000 not in verdicts  # templates are not audited
    db = next(g for g in report.guests if g.vmid == 100)
    assert db.backup_count == 2
    assert db.age_hours == 6.0
    assert report.guests[0].verdict is BackupVerdict.MISSING  # worst first
    assert (report.guests_ok, report.guests_stale, report.guests_missing) == (2, 1, 2)


def test_job_coverage_follows_vzdump_selection() -> None:
    report = _audit()
    scheduled = {g.vmid: g.scheduled_by for g in report.guests}
    assert scheduled[100] == ["backup-daily"]
    assert scheduled[101] == ["backup-web"]  # via pool
    assert scheduled[102] == []  # only an *disabled* all-job would cover it
    assert report.guests_unscheduled == 2


def test_all_job_with_exclude_and_node_filter() -> None:
    jobs = [
        {"id": "all-but-103", "enabled": "1", "all": 1, "exclude": "103, 200"},
        {"id": "pve3-only", "all": 1, "node": "pve3"},
    ]
    report = _audit(jobs=jobs)
    scheduled = {g.vmid: g.scheduled_by for g in report.guests}
    assert scheduled[100] == ["all-but-103"]
    assert scheduled[103] == ["pve3-only"]
    assert scheduled[200] == []


def test_findings_and_severity() -> None:
    report = _audit(
        failed_tasks=[{"upid": "UPID:x", "node": "pve1", "id": "", "status": "job errors"}]
    )
    codes = [f.code for f in report.findings]
    assert report.status is Severity.CRITICAL
    missing = {f.resource: f.severity for f in report.findings if f.code == "BACKUP_MISSING"}
    assert missing == {"102": Severity.CRITICAL, "103": Severity.WARNING}  # running vs stopped
    assert "BACKUP_STALE" in codes
    assert "BACKUP_JOB_DISABLED" in codes
    assert "BACKUP_TASK_FAILED" in codes


def test_unreadable_storage_turns_missing_into_unknown() -> None:
    report = _audit(unreadable_storages={"pve2/local": "403 Forbidden"})
    assert report.guests_missing == 0
    assert report.guests_unknown == 2
    assert {g.vmid for g in report.guests if g.verdict is BackupVerdict.UNKNOWN} == {102, 103}
    assert report.unreadable_storages == {"pve2/local": "403 Forbidden"}
    unreadable = [f for f in report.findings if f.code == "BACKUP_STORAGE_UNREADABLE"]
    assert unreadable[0].resource == "pve2/local"
    assert "BACKUP_MISSING" not in {f.code for f in report.findings}
    assert report.guests[0].verdict is BackupVerdict.UNKNOWN  # sorted before stale/ok


def test_custom_age_limit() -> None:
    report = _audit(max_age_hours=100)
    assert report.guests_stale == 0


def test_volumes_without_vmid_are_ignored() -> None:
    report = _audit(backup_volumes=[{"volid": "x", "ctime": NOW}], jobs=[])
    assert report.guests_missing == 5


def test_pbs_verification_state() -> None:
    volumes = [
        {"vmid": 100, "ctime": NOW - 3600, "format": "pbs-vm", "verification": {"state": "failed"}},
        {"vmid": 100, "ctime": NOW - 90000, "format": "pbs-vm", "verification": {"state": "ok"}},
        {"vmid": 101, "ctime": NOW - 3600, "format": "pbs-vm"},
        {"vmid": 200, "ctime": NOW - 3600, "format": "pbs-ct", "verification": {"state": "ok"}},
        {"vmid": 102, "ctime": NOW - 3600, "format": "vma.zst"},
    ]
    report = _audit(backup_volumes=volumes)
    states = {g.vmid: g.latest_verification for g in report.guests}
    assert states == {100: "failed", 101: "none", 200: "ok", 102: None, 103: None}
    by = {(f.code, f.resource): f.severity for f in report.findings}
    assert by[("BACKUP_VERIFY_FAILED", "100")] is Severity.CRITICAL
    assert by[("BACKUP_NOT_VERIFIED", "101")] is Severity.INFO
