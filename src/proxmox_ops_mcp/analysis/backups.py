"""Backup audit: which guests are unprotected, stale or not scheduled at all."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import as_int, items
from proxmox_ops_mcp.analysis.findings import Finding, Severity, overall_status, sort_findings


class BackupVerdict(StrEnum):
    """Backup state of a single guest."""

    OK = "ok"
    STALE = "stale"
    MISSING = "missing"
    UNKNOWN = "unknown"
    """No backup visible, but at least one backup storage could not be read."""


class GuestBackupStatus(BaseModel):
    """Backup state of one guest."""

    vmid: int
    name: str | None
    node: str
    type: str
    running: bool
    verdict: BackupVerdict
    last_backup: datetime | None
    age_hours: float | None
    backup_count: int
    latest_verification: str | None = Field(
        default=None,
        description="Proxmox Backup Server verify state of the newest backup: ok, failed, "
        "or 'none' if it was never verified. Null for non-PBS backups.",
    )
    scheduled_by: list[str]


class BackupAuditReport(BaseModel):
    """Result of :func:`audit_backups`."""

    status: Severity
    max_age_hours: int
    guests_total: int
    guests_ok: int
    guests_stale: int
    guests_missing: int
    guests_unknown: int
    guests_unscheduled: int
    unreadable_storages: dict[str, str]
    guests: list[GuestBackupStatus]
    findings: list[Finding]


def audit_backups(
    *,
    guests: Any,
    backup_volumes: Iterable[Mapping[str, Any]],
    jobs: Any,
    failed_tasks: Any = None,
    unreadable_storages: Mapping[str, str] | None = None,
    max_age_hours: int = 26,
    now: datetime | None = None,
) -> BackupAuditReport:
    """Cross-reference guests, existing backup volumes and scheduled backup jobs.

    Args:
        guests: ``/cluster/resources?type=vm`` entries.
        backup_volumes: Storage content entries with ``content == 'backup'``.
        jobs: ``/cluster/backup`` job definitions.
        failed_tasks: ``vzdump`` tasks from ``/cluster/tasks`` whose status is not OK.
        unreadable_storages: Backup storages that could not be listed, mapped to the
            reason. Guests without a visible backup are then reported as ``unknown``
            instead of ``missing``, because the backup may sit on such a storage.
        max_age_hours: A backup older than this is reported as stale. The default of 26
            hours tolerates a daily schedule with some runtime jitter.
        now: Reference time (injected for deterministic tests).
    """
    now = now or datetime.now(UTC)
    unreadable = dict(sorted((unreadable_storages or {}).items()))
    latest: dict[int, float] = {}
    latest_volume: dict[int, Mapping[str, Any]] = {}
    counts: dict[int, int] = {}
    for vol in backup_volumes:
        vmid, ctime = as_int(vol.get("vmid")), as_int(vol.get("ctime"))
        if not vmid:
            continue
        counts[vmid] = counts.get(vmid, 0) + 1
        if ctime >= latest.get(vmid, 0):
            latest[vmid] = ctime
            latest_volume[vmid] = vol

    job_list = items(jobs)
    findings: list[Finding] = []
    statuses: list[GuestBackupStatus] = []
    for g in items(guests):
        if g.get("template"):
            continue
        vmid = as_int(g.get("vmid"))
        running = g.get("status") == "running"
        ts = latest.get(vmid)
        last = datetime.fromtimestamp(ts, UTC) if ts else None
        age = round((now - last).total_seconds() / 3600, 1) if last else None
        if last is None:
            verdict = BackupVerdict.UNKNOWN if unreadable else BackupVerdict.MISSING
        elif age is not None and age > max_age_hours:
            verdict = BackupVerdict.STALE
        else:
            verdict = BackupVerdict.OK
        status = GuestBackupStatus(
            vmid=vmid,
            name=str(g["name"]) if g.get("name") else None,
            node=str(g.get("node")),
            type=str(g.get("type")),
            running=running,
            verdict=verdict,
            last_backup=last,
            age_hours=age,
            backup_count=counts.get(vmid, 0),
            latest_verification=_verification(latest_volume.get(vmid)),
            scheduled_by=_jobs_covering(g, job_list),
        )
        statuses.append(status)
        findings += _guest_findings(status, max_age_hours)

    for job in job_list:
        if not _enabled(job):
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="BACKUP_JOB_DISABLED",
                    message=f"Backup job {job.get('id')} is disabled.",
                    resource=str(job.get("id")),
                    recommendation="Remove it or re-enable it so intent and reality match.",
                )
            )
    for task in items(failed_tasks):
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="BACKUP_TASK_FAILED",
                message=f"Backup task on {task.get('node')} for "
                f"{task.get('id') or 'multiple guests'} ended with: {task.get('status')}",
                resource=str(task.get("upid")),
                recommendation="Open the task log via get_task_status for the exact error.",
            )
        )

    for storage, reason in unreadable.items():
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="BACKUP_STORAGE_UNREADABLE",
                message=f"Backups on {storage} cannot be listed: {reason}",
                resource=storage,
                recommendation="Grant the token 'Datastore.AllocateSpace' on the storage and "
                "'VM.Backup' on the guests (see docs/proxmox-setup.md). Until then, guests "
                "without a visible backup are reported as 'unknown'.",
            )
        )

    statuses.sort(key=lambda s: (_VERDICT_ORDER[s.verdict], s.vmid))
    return BackupAuditReport(
        status=overall_status(findings),
        max_age_hours=max_age_hours,
        guests_total=len(statuses),
        guests_ok=sum(s.verdict == BackupVerdict.OK for s in statuses),
        guests_stale=sum(s.verdict == BackupVerdict.STALE for s in statuses),
        guests_missing=sum(s.verdict == BackupVerdict.MISSING for s in statuses),
        guests_unknown=sum(s.verdict == BackupVerdict.UNKNOWN for s in statuses),
        guests_unscheduled=sum(not s.scheduled_by for s in statuses),
        unreadable_storages=unreadable,
        guests=statuses,
        findings=sort_findings(findings),
    )


_VERDICT_ORDER = {
    BackupVerdict.MISSING: 0,
    BackupVerdict.UNKNOWN: 1,
    BackupVerdict.STALE: 2,
    BackupVerdict.OK: 3,
}


def _verification(volume: Mapping[str, Any] | None) -> str | None:
    """PBS verify state of a backup volume (``ok``/``failed``/``none``), else ``None``."""
    if volume is None or not str(volume.get("format", "")).startswith("pbs-"):
        return None
    state = (volume.get("verification") or {}).get("state")
    return str(state) if state else "none"


def _guest_findings(s: GuestBackupStatus, max_age_hours: int) -> list[Finding]:
    label = f"{s.vmid}" + (f" ({s.name})" if s.name else "")
    findings = []
    if s.latest_verification == "failed":
        findings.append(
            Finding(
                severity=Severity.CRITICAL,
                code="BACKUP_VERIFY_FAILED",
                message=f"The newest backup of guest {label} failed Proxmox Backup Server "
                "verification.",
                resource=str(s.vmid),
                recommendation="Treat it as unusable: check the PBS verify task log, run a new "
                "backup, and check the datastore disks.",
            )
        )
    elif s.latest_verification == "none":
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="BACKUP_NOT_VERIFIED",
                message=f"The newest backup of guest {label} has not been verified yet.",
                resource=str(s.vmid),
                recommendation="Schedule a verify job on the PBS datastore (or enable "
                "'verify new snapshots').",
            )
        )
    if s.verdict == BackupVerdict.MISSING:
        findings.append(
            Finding(
                severity=Severity.CRITICAL if s.running else Severity.WARNING,
                code="BACKUP_MISSING",
                message=f"Guest {label} has no backup on any backup storage.",
                resource=str(s.vmid),
                recommendation="Add the guest to a backup job and run a backup now.",
            )
        )
    elif s.verdict == BackupVerdict.STALE:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="BACKUP_STALE",
                message=f"Last backup of guest {label} is {s.age_hours} h old "
                f"(limit {max_age_hours} h).",
                resource=str(s.vmid),
                recommendation="Check recent vzdump task results and the job schedule.",
            )
        )
    if not s.scheduled_by:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="BACKUP_NOT_SCHEDULED",
                message=f"Guest {label} is not included in any enabled backup job.",
                resource=str(s.vmid),
                recommendation="Include it explicitly, via a pool, or via an 'all' job.",
            )
        )
    return findings


def _enabled(job: Mapping[str, Any]) -> bool:
    return str(job.get("enabled", 1)) not in {"0", "false", "False"}


def _id_set(value: Any) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, int):
        return {value}
    return {as_int(v) for v in str(value).replace(" ", "").split(",") if v}


def _jobs_covering(guest: Mapping[str, Any], jobs: list[Mapping[str, Any]]) -> list[str]:
    """Replicate vzdump's guest selection: explicit list, pool, or 'all' minus excludes."""
    vmid = as_int(guest.get("vmid"))
    covering = []
    for job in jobs:
        if not _enabled(job):
            continue
        if job.get("node") and job.get("node") != guest.get("node"):
            continue
        selected = (
            vmid in _id_set(job.get("vmid"))
            or (bool(job.get("pool")) and job.get("pool") == guest.get("pool"))
            or (
                str(job.get("all", 0)) in {"1", "true", "True"}
                and vmid not in _id_set(job.get("exclude"))
            )
        )
        if selected:
            covering.append(str(job.get("id")))
    return sorted(covering)
