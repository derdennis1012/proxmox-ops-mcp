"""The common output vocabulary of every diagnostic."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    """How urgently a finding needs attention."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Numeric rank, higher is worse."""
        return {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}[self]


class Finding(BaseModel):
    """A single, actionable observation.

    ``code`` is stable and machine-readable so clients can filter or suppress findings;
    ``message`` and ``recommendation`` are written for humans (and models).
    """

    severity: Severity
    code: str = Field(description="Stable identifier, e.g. 'CEPH_OSD_DOWN'.")
    message: str
    resource: str | None = Field(default=None, description="Affected node, guest, OSD, ...")
    recommendation: str | None = None


def overall_status(findings: list[Finding]) -> Severity:
    """Return the worst severity in ``findings`` (``info`` when empty)."""
    return max((f.severity for f in findings), key=lambda s: s.rank, default=Severity.INFO)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """Order findings worst-first, then by code and resource for stable output."""
    return sorted(findings, key=lambda f: (-f.severity.rank, f.code, f.resource or ""))


def unavailable_findings(unavailable: Mapping[str, str]) -> list[Finding]:
    """One warning per data source that could not be read.

    A health check must never report "all good" for data it could not look at.
    """
    return [
        Finding(
            severity=Severity.WARNING,
            code="DATA_UNAVAILABLE",
            message=f"Could not read {source}: {reason}",
            resource=source,
            recommendation="Check the token's privileges (Sys.Audit on the node) and the "
            "node's API; the report is incomplete until this succeeds.",
        )
        for source, reason in sorted(unavailable.items())
    ]


def threshold_finding(
    value: float,
    warning: float,
    critical: float,
    *,
    code: str,
    message: str,
    resource: str,
    recommendation: str,
) -> list[Finding]:
    """Return a one-element list if ``value`` crosses a threshold, else an empty list."""
    if value >= critical:
        severity = Severity.CRITICAL
    elif value >= warning:
        severity = Severity.WARNING
    else:
        return []
    return [
        Finding(
            severity=severity,
            code=code,
            message=message,
            resource=resource,
            recommendation=recommendation,
        )
    ]
