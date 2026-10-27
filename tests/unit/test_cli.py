from __future__ import annotations

from typing import Any

import pytest

from proxmox_ops_mcp import cli
from proxmox_ops_mcp.errors import ProxmoxAPIError


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)  # keep a developer's .env out of the tests
    monkeypatch.setenv("PROXMOX_HOST", "pve.example.com")
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "ops@pve!mcp")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "secret-value")


class _Client:
    fail = False

    def get(self, path: str, **params: Any) -> Any:
        if self.fail:
            raise ProxmoxAPIError("GET /version failed: 401 authentication failure")
        return {"version": "8.3.2", "release": "8.3"} if path == "version" else [{}, {}, {}]


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert capsys.readouterr().out.startswith("proxmox-ops-mcp ")


def test_missing_configuration_names_the_variables(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("PROXMOX_HOST", "PROXMOX_TOKEN_ID", "PROXMOX_TOKEN_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit, match=r"PROXMOX_HOST.*PROXMOX_TOKEN_ID"):
        cli.main(["check"])


@pytest.mark.usefixtures("env")
def test_check_success(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("PROXMOX_VERIFY_SSL", "false")
    monkeypatch.setattr("proxmox_ops_mcp.client.ProxmoxerClient.from_settings", lambda s: _Client())
    assert cli.main(["check"]) == 0
    out = capsys.readouterr()
    assert "Proxmox VE 8.3.2" in out.out
    assert "3 node(s)" in out.out
    assert "secret-value" not in out.out + out.err
    assert "TLS verification is disabled" in out.err


@pytest.mark.usefixtures("env")
def test_check_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = _Client()
    client.fail = True
    monkeypatch.setattr("proxmox_ops_mcp.client.ProxmoxerClient.from_settings", lambda s: client)
    assert cli.main(["check"]) == 1
    assert "401" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "pve-mcp.example.com"])
def test_http_refuses_non_loopback_without_opt_in(
    host: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["serve", "--transport", "streamable-http", "--host", host]) == 2
    assert "refusing" in capsys.readouterr().err


class _Server:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def run(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.mark.usefixtures("env")
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        ([], (("stdio",), {})),
        (["serve"], (("stdio",), {})),
    ],
)
def test_serve_stdio(monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: Any) -> None:
    server = _Server()
    monkeypatch.setattr("proxmox_ops_mcp.server.build_server", lambda settings: server)
    monkeypatch.setattr("logging.basicConfig", lambda **kwargs: None)
    assert cli.main(argv) == 0
    assert server.calls == [expected]


@pytest.mark.usefixtures("env")
def test_serve_http_enforces_host_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _Server()
    monkeypatch.setattr("proxmox_ops_mcp.server.build_server", lambda settings: server)
    monkeypatch.setattr("logging.basicConfig", lambda **kwargs: None)
    argv = [
        *("serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--allow-remote"),
        *("--allowed-host", "mcp.example.com", "--allowed-host", "mcp.example.com:443"),
    ]
    assert cli.main(argv) == 0
    ((args, kwargs),) = server.calls
    assert args == ("streamable-http",)
    assert (kwargs["host"], kwargs["port"]) == ("0.0.0.0", 8000)
    security = kwargs["transport_security"]
    assert security.enable_dns_rebinding_protection is True
    assert "mcp.example.com:*" in security.allowed_hosts
    assert "mcp.example.com:443" in security.allowed_hosts
    assert "https://mcp.example.com:443" in security.allowed_origins


def test_env_file_is_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("PROXMOX_HOST", "PROXMOX_TOKEN_ID", "PROXMOX_TOKEN_SECRET", "PROXMOX_ACCESS_TIER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    planted = ["HOST=h", "TOKEN_ID=a@pve!b", "TOKEN_SECRET=s", "ACCESS_TIER=admin"]
    (tmp_path / ".env").write_text("".join(f"PROXMOX_{line}\n" for line in planted))
    with pytest.raises(SystemExit, match="PROXMOX_HOST"):
        cli.main(["check"])  # a .env in the working directory is ignored

    monkeypatch.setattr("proxmox_ops_mcp.client.ProxmoxerClient.from_settings", lambda s: _Client())
    assert cli.main(["--env-file", str(tmp_path / ".env"), "check"]) == 0
    assert "access tier: admin" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="env file not found"):
        cli.main(["--env-file", str(tmp_path / "missing.env"), "check"])


def test_loopback_detection() -> None:
    assert cli._is_loopback("localhost")
    assert cli._is_loopback("127.0.0.1")
    assert cli._is_loopback("::1")
    assert not cli._is_loopback("10.0.0.1")


@pytest.mark.usefixtures("env")
def test_serve_caps_proxmoxer_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    monkeypatch.setattr("proxmox_ops_mcp.server.build_server", lambda settings: _Server())
    monkeypatch.setattr("logging.basicConfig", lambda **kwargs: None)
    logging.getLogger("proxmoxer").setLevel(logging.DEBUG)
    assert cli.main(["serve", "--log-level", "debug"]) == 0
    assert logging.getLogger("proxmoxer").level == logging.WARNING
