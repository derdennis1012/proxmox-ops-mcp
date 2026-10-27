"""Updates, reboot need and certificates, tested against payloads from Proxmox VE 9.2."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from proxmox_ops_mcp.analysis.findings import Severity
from proxmox_ops_mcp.analysis.upkeep import analyze_upkeep

FIXTURES = Path(__file__).parent.parent / "fixtures" / "pve9"
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_recorded_host_has_pending_updates_but_no_reboot() -> None:
    report = analyze_upkeep(
        versions={"pve1": _load("apt_versions.json")},
        certificates={"pve1": _load("certificates_info.json")},
        now=NOW,
    )
    node = report.nodes[0]
    assert node.running_kernel == "7.0.14-6-pve"
    assert node.newest_installed_kernel == "7.0.14-6-pve"
    assert node.reboot_required is False
    pending = {u.package: u for u in node.pending_updates}
    assert pending["pve-manager"].installed == "9.2.5"
    assert pending["pve-manager"].available == "9.2.21"
    assert "ifupdown" not in pending  # only config files left, not installed
    assert "proxmox-backup-restore-image" not in pending  # not installed
    assert [f.code for f in report.findings] == ["NODE_UPDATES_PENDING"]
    assert report.findings[0].severity is Severity.WARNING  # kernel/QEMU among them
    assert {c.file for c in report.certificates} == {"pve-root-ca.pem", "pve-ssl.pem"}


def test_newer_installed_kernel_requires_reboot() -> None:
    versions = _load("apt_versions.json")
    versions.append(
        {
            "Package": "proxmox-kernel-7.0.14-25-pve-signed",
            "Version": "7.0.14-25",
            "OldVersion": "7.0.14-25",
            "CurrentState": "Installed",
        }
    )
    report = analyze_upkeep(versions={"pve1": versions}, certificates={}, now=NOW)
    assert report.nodes[0].reboot_required is True
    assert report.nodes[0].newest_installed_kernel == "7.0.14-25-pve"
    assert "NODE_REBOOT_REQUIRED" in {f.code for f in report.findings}


def test_only_minor_updates_are_info() -> None:
    versions = [
        {"Package": "proxmox-ve", "RunningKernel": "6.8.12-4-pve", "CurrentState": "Installed",
         "OldVersion": "8.3.0", "Version": "8.3.0"},
        {"Package": "pve-docs", "CurrentState": "Installed", "OldVersion": "8.3.1",
         "Version": "8.3.2"},
    ]  # fmt: skip
    report = analyze_upkeep(versions={"pve1": versions}, certificates={}, now=NOW)
    assert report.findings[0].severity is Severity.INFO


def test_certificate_expiry() -> None:
    certs = _load("certificates_info.json")
    ssl = next(c for c in certs if c["filename"] == "pve-ssl.pem")
    root = next(c for c in certs if c["filename"] == "pve-root-ca.pem")
    day = 86400
    now_ts = NOW.timestamp()
    certs.append(dict(ssl, filename="pveproxy-ssl.pem", notafter=now_ts + 20 * day))
    ssl["notafter"] = now_ts + 3 * day
    root["notafter"] = now_ts - day
    report = analyze_upkeep(versions={}, certificates={"pve1": certs}, now=NOW)
    by = {f.resource: (f.code, f.severity) for f in report.findings}
    assert by["pve1:pve-root-ca.pem"] == ("CERTIFICATE_EXPIRED", Severity.CRITICAL)
    assert by["pve1:pve-ssl.pem"] == ("CERTIFICATE_EXPIRING", Severity.CRITICAL)
    assert by["pve1:pveproxy-ssl.pem"] == ("CERTIFICATE_EXPIRING", Severity.WARNING)
