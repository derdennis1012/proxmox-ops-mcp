"""Node upkeep: pending Proxmox updates, pending reboots and node certificates.

Updates are read from ``/nodes/{node}/apt/versions``, which needs only ``Sys.Audit``.
(``/apt/update`` would list every package, but requires ``Sys.Modify``.) It covers the
Proxmox package set (kernel, QEMU, ZFS, pve-*, Ceph client), which is where updates
matter most for a hypervisor. In that payload ``OldVersion`` is the installed version
and ``Version`` the newest one apt knows about.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from proxmox_ops_mcp.analysis._util import as_float, items
from proxmox_ops_mcp.analysis.findings import (
    Finding,
    Severity,
    overall_status,
    sort_findings,
    unavailable_findings,
)
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds

_KERNEL_PACKAGE_RE = re.compile(r"^(?:proxmox|pve)-kernel-(\d+\.\d+\.\d+-\d+)-pve(?:-signed)?$")
_SECURITY_SENSITIVE = ("pve-qemu-kvm", "qemu-server", "pve-manager", "proxmox-kernel", "zfsutils")


class PendingUpdate(BaseModel):
    """A package with a newer version available."""

    package: str
    installed: str
    available: str


class NodeUpkeep(BaseModel):
    """Upkeep state of one node."""

    node: str
    running_kernel: str | None
    newest_installed_kernel: str | None
    reboot_required: bool
    pending_updates: list[PendingUpdate]


class CertificateInfo(BaseModel):
    """One certificate of a node."""

    node: str
    file: str
    subject: str | None
    issuer: str | None
    expires: datetime | None
    days_left: float | None


class UpkeepReport(BaseModel):
    """Result of :func:`analyze_upkeep`."""

    status: Severity
    nodes: list[NodeUpkeep]
    certificates: list[CertificateInfo]
    findings: list[Finding]


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version))


def analyze_upkeep(
    *,
    versions: Mapping[str, Any],
    certificates: Mapping[str, Any],
    now: datetime | None = None,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    unavailable: Mapping[str, str] | None = None,
) -> UpkeepReport:
    """Evaluate updates, reboot need and certificates.

    Args:
        versions: node -> ``/nodes/{node}/apt/versions``.
        certificates: node -> ``/nodes/{node}/certificates/info``.
        unavailable: Data sources that could not be read, mapped to the reason.
        now: Reference time for certificate expiry.
        thresholds: Certificate expiry thresholds.
    """
    now = now or datetime.now(UTC)
    findings: list[Finding] = unavailable_findings(unavailable or {})
    nodes: list[NodeUpkeep] = []
    for node, packages in sorted(versions.items()):
        upkeep = _node_upkeep(node, items(packages))
        nodes.append(upkeep)
        findings += _upkeep_findings(upkeep)

    certs: list[CertificateInfo] = []
    for node, node_certs in sorted(certificates.items()):
        for c in items(node_certs):
            cert = _certificate(node, c, now)
            certs.append(cert)
            findings += _certificate_findings(cert, thresholds)

    return UpkeepReport(
        status=overall_status(findings),
        nodes=nodes,
        certificates=certs,
        findings=sort_findings(findings),
    )


def _node_upkeep(node: str, packages: list[Mapping[str, Any]]) -> NodeUpkeep:
    running = next((str(p["RunningKernel"]) for p in packages if p.get("RunningKernel")), None)
    installed_kernels = []
    pending: dict[str, PendingUpdate] = {}
    for p in packages:
        name = str(p.get("Package"))
        installed, available = p.get("OldVersion"), p.get("Version")
        if p.get("CurrentState") != "Installed" or not installed:
            continue
        match = _KERNEL_PACKAGE_RE.match(name)
        if match:
            installed_kernels.append(match.group(1))
        if available and available != installed:
            pending[name] = PendingUpdate(
                package=name, installed=str(installed), available=str(available)
            )
    newest = max(installed_kernels, key=_version_key, default=None)
    running_version = running.removesuffix("-pve") if running else None
    reboot = bool(
        newest and running_version and _version_key(newest) > _version_key(running_version)
    )
    return NodeUpkeep(
        node=node,
        running_kernel=running,
        newest_installed_kernel=f"{newest}-pve" if newest else None,
        reboot_required=reboot,
        pending_updates=sorted(pending.values(), key=lambda u: u.package),
    )


def _upkeep_findings(n: NodeUpkeep) -> list[Finding]:
    findings: list[Finding] = []
    if n.reboot_required:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="NODE_REBOOT_REQUIRED",
                message=f"{n.node} runs kernel {n.running_kernel}, but "
                f"{n.newest_installed_kernel} is installed.",
                resource=n.node,
                recommendation="Schedule a reboot. Use plan_node_evacuation first to move "
                "guests away without downtime.",
            )
        )
    if n.pending_updates:
        important = [
            u.package for u in n.pending_updates if u.package.startswith(_SECURITY_SENSITIVE)
        ]
        findings.append(
            Finding(
                severity=Severity.WARNING if important else Severity.INFO,
                code="NODE_UPDATES_PENDING",
                message=f"{len(n.pending_updates)} Proxmox package update(s) pending on "
                f"{n.node}" + (f", including {', '.join(important)}." if important else "."),
                resource=n.node,
                recommendation="Read the changelogs, then run 'apt update && apt full-upgrade' "
                "in a maintenance window. Never use plain 'apt upgrade' on Proxmox VE.",
            )
        )
    return findings


def _certificate(node: str, c: Mapping[str, Any], now: datetime) -> CertificateInfo:
    not_after = as_float(c.get("notafter"), 0)
    expires = datetime.fromtimestamp(not_after, UTC) if not_after > 0 else None
    return CertificateInfo(
        node=node,
        file=str(c.get("filename")),
        subject=c.get("subject"),
        issuer=c.get("issuer"),
        expires=expires,
        days_left=round((expires - now).total_seconds() / 86400, 1) if expires else None,
    )


def _certificate_findings(c: CertificateInfo, t: Thresholds) -> list[Finding]:
    if c.days_left is None or c.days_left > t.certificate_warning_days:
        return []
    label = f"{c.node}:{c.file}"
    expired = c.days_left <= 0
    return [
        Finding(
            severity=Severity.CRITICAL
            if expired or c.days_left <= t.certificate_critical_days
            else Severity.WARNING,
            code="CERTIFICATE_EXPIRED" if expired else "CERTIFICATE_EXPIRING",
            message=f"Certificate {c.file} on {c.node} "
            + ("has expired." if expired else f"expires in {c.days_left:.0f} days."),
            resource=label,
            recommendation="For ACME certificates check 'pvenode acme cert renew' and the "
            "renewal timer; self-signed node certificates are renewed with "
            "'pvecm updatecerts --force'.",
        )
    ]
