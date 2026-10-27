from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from proxmox_ops_mcp.config import Settings
from proxmox_ops_mcp.security import AccessTier


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "host": "pve.example.com",
        "token_id": "ops@pve!mcp",
        "token_secret": SecretStr("s3cr3t"),
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_defaults_are_safe() -> None:
    s = _settings()
    assert s.access_tier is AccessTier.READ_ONLY
    assert s.verify_ssl is True
    assert s.tls_verify is True
    assert s.port == 8006


def test_token_id_is_split() -> None:
    s = _settings(token_id="automation@pam!ops-mcp")
    assert s.token_user == "automation@pam"
    assert s.token_name == "ops-mcp"


@pytest.mark.parametrize("bad", ["ops@pve", "ops!mcp", "ops@pve!", "@pve!mcp", "a b@pve!x"])
def test_malformed_token_id_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        _settings(token_id=bad)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pve.example.com", "pve.example.com"),
        ("https://pve.example.com:8006/", "pve.example.com"),
        ("pve.example.com:8006", "pve.example.com"),
        ("10.0.0.5", "10.0.0.5"),
        ("[fd00::1]:8006", "fd00::1"),
    ],
)
def test_host_is_normalised(raw: str, expected: str) -> None:
    assert _settings(host=raw).host == expected


def test_unparseable_host_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(host="https://")


def test_tls_verify_variants(tmp_path: Path) -> None:
    bundle = tmp_path / "ca.pem"
    assert _settings(verify_ssl=False).tls_verify is False
    assert _settings(ca_bundle=bundle).tls_verify == str(bundle)


def test_secret_is_not_rendered() -> None:
    assert "s3cr3t" not in repr(_settings())


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXMOX_HOST", "pve1.lab")
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "root@pam!mcp")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "x")
    monkeypatch.setenv("PROXMOX_ACCESS_TIER", "operator")
    s = Settings(_env_file=None)
    assert s.host == "pve1.lab"
    assert s.access_tier is AccessTier.OPERATOR


def test_tls_fingerprint_is_normalised_and_validated() -> None:
    fp = ":".join(["AB"] * 32)
    s = _settings(tls_fingerprint=fp)
    assert s.tls_fingerprint == "ab" * 32
    assert s.tls_verify is False  # the pin replaces CA/hostname verification
    assert _settings(tls_fingerprint="  ").tls_fingerprint is None
    with pytest.raises(ValidationError):
        _settings(tls_fingerprint="abc")


def test_write_scope_parsing() -> None:
    s = _settings(write_pools=" lab , sandbox,", write_vmids="900, 901")
    assert s.write_pool_set == frozenset({"lab", "sandbox"})
    assert s.write_vmid_set == frozenset({900, 901})
    with pytest.raises(ValidationError):
        _settings(write_vmids="900,abc")
