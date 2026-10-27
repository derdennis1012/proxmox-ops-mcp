from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from proxmox_ops_mcp.client import (
    ProxmoxerClient,
    validate_guest_type,
    validate_node,
    validate_snapshot_name,
    validate_storage,
    validate_upid,
    validate_vmid,
)
from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError
from tests.conftest import make_settings


@pytest.mark.parametrize("name", ["pve1", "node-a", "a", "PVE01"])
def test_valid_node_names(name: str) -> None:
    assert validate_node(name) == name


@pytest.mark.parametrize("name", ["", "../etc", "pve1/qemu", "-pve", "pve_1", "pve 1", "a" * 64])
def test_invalid_node_names(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_node(name)


@pytest.mark.parametrize("vmid", [99, 0, -1, 1_000_000_000, True])
def test_invalid_vmids(vmid: int) -> None:
    with pytest.raises(ValidationError):
        validate_vmid(vmid)


def test_valid_vmid() -> None:
    assert validate_vmid(100) == 100


@pytest.mark.parametrize("name", ["current", "a", "1abc", "has space", "x" * 41, "a/b"])
def test_invalid_snapshot_names(name: str) -> None:
    with pytest.raises(ValidationError):
        validate_snapshot_name(name)


def test_valid_storage_and_snapshot() -> None:
    assert validate_storage("local-lvm") == "local-lvm"
    assert validate_snapshot_name("pre_upgrade-2026") == "pre_upgrade-2026"
    with pytest.raises(ValidationError):
        validate_storage("../x")


@pytest.mark.parametrize(
    "upid",
    [
        "UPID:pve1:0000ABCD:00112233:6955B600:qmstart:101:ops@pve!mcp:",
        "UPID:pve1:0000ABCD:00112233:6955B600:vzdump::root@pam:",
    ],
)
def test_valid_upids(upid: str) -> None:
    assert validate_upid(upid) == upid


@pytest.mark.parametrize(
    "upid",
    [
        "UPID:pve1:../../etc:",
        "UPID:n:00000000:00000000:00000000:x?foo=1:y:z:",
        "UPID:n:00000000:00000000:00000000:x#frag:y:z:",
        "UPID:n:00000000:00000000:00000000:x:%2F..%2F:z:",
    ],
)
def test_invalid_upids(upid: str) -> None:
    with pytest.raises(ValidationError):
        validate_upid(upid)


def test_guest_type_validation() -> None:
    assert validate_guest_type("qemu") == "qemu"
    assert validate_guest_type("lxc") == "lxc"
    with pytest.raises(ValidationError):
        validate_guest_type("qemu/../x")


class _ResourceException(Exception):
    def __init__(self, status_code: int, status_message: str) -> None:
        super().__init__(status_message)
        self.status_code = status_code
        self.status_message = status_message


class _Resource:
    def __init__(self, api: _FakeAPI, path: str) -> None:
        self.api, self.path = api, path

    def get(self, **params: Any) -> Any:
        return self.api.respond("get", self.path, params)

    def post(self, **data: Any) -> Any:
        return self.api.respond("post", self.path, data)


class _FakeAPI:
    instances: list[_FakeAPI] = []

    def __init__(self, host: str, **kwargs: Any) -> None:
        self.host, self.kwargs = host, kwargs
        self.behaviour: Exception | None = None
        _FakeAPI.instances.append(self)

    def __call__(self, path: str) -> _Resource:
        return _Resource(self, path)

    def respond(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        behaviour = self.behaviour
        if isinstance(behaviour, Exception):
            raise behaviour
        return {"method": method, "path": path, "payload": payload}


@pytest.fixture
def fake_proxmoxer(monkeypatch: pytest.MonkeyPatch) -> type[_FakeAPI]:
    pkg = types.ModuleType("proxmoxer")
    pkg.ProxmoxAPI = _FakeAPI  # type: ignore[attr-defined]
    core = types.ModuleType("proxmoxer.core")
    core.ResourceException = _ResourceException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "proxmoxer", pkg)
    monkeypatch.setitem(sys.modules, "proxmoxer.core", core)
    _FakeAPI.instances.clear()
    return _FakeAPI


def test_client_uses_token_auth(fake_proxmoxer: type[_FakeAPI]) -> None:
    client = ProxmoxerClient.from_settings(make_settings())
    api = fake_proxmoxer.instances[0]
    assert api.host == "pve.example.com"
    assert api.kwargs["user"] == "ops@pve"
    assert api.kwargs["token_name"] == "mcp"
    assert api.kwargs["verify_ssl"] is True
    assert "password" not in api.kwargs
    assert client.get("nodes", type="x") == {
        "method": "get",
        "path": "nodes",
        "payload": {"type": "x"},
    }
    assert client.post("nodes/pve1/qemu/100/status/start")["method"] == "post"


@pytest.mark.parametrize(
    "path", ["../nodes", "/nodes", "nodes//x", "nodes/./x", "nodes?x=1", "a%2Fb", "nodes/a b"]
)
def test_client_refuses_unsafe_paths(fake_proxmoxer: type[_FakeAPI], path: str) -> None:
    client = ProxmoxerClient.from_settings(make_settings())
    with pytest.raises(ValidationError):
        client.get(path)


def test_api_errors_are_wrapped(fake_proxmoxer: type[_FakeAPI]) -> None:
    client = ProxmoxerClient.from_settings(make_settings())
    fake_proxmoxer.instances[0].behaviour = _ResourceException(403, "Permission check failed")
    with pytest.raises(ProxmoxAPIError, match="403 Permission check failed") as info:
        client.get("cluster/ceph/status")
    assert info.value.status_code == 403


def test_transport_errors_do_not_leak_details(fake_proxmoxer: type[_FakeAPI]) -> None:
    client = ProxmoxerClient.from_settings(make_settings())
    fake_proxmoxer.instances[0].behaviour = ConnectionError("Authorization: PVEAPIToken=secret")
    with pytest.raises(ProxmoxAPIError) as info:
        client.get("version")
    assert "secret" not in str(info.value)
    assert "ConnectionError" in str(info.value)


def test_pin_also_applies_to_proxied_connections() -> None:
    from proxmox_ops_mcp.client import _PinnedAdapter

    adapter = _PinnedAdapter("ab" * 32)
    assert adapter.poolmanager.connection_pool_kw["assert_fingerprint"] == "ab" * 32
    proxied = adapter.proxy_manager_for("http://proxy.example.com:3128")
    assert proxied.connection_pool_kw["assert_fingerprint"] == "ab" * 32


def test_client_mounts_pin_on_the_proxmoxer_session(fake_proxmoxer: type[_FakeAPI]) -> None:
    from proxmox_ops_mcp.client import _PinnedAdapter

    class _Session:
        def __init__(self) -> None:
            self.mounted: dict[str, object] = {}

        def mount(self, prefix: str, adapter: object) -> None:
            self.mounted[prefix] = adapter

    session = _Session()
    original = _FakeAPI.__init__

    def init(self: _FakeAPI, host: str, **kwargs: Any) -> None:
        original(self, host, **kwargs)
        self._store = {"session": session}  # type: ignore[attr-defined]

    _FakeAPI.__init__ = init  # type: ignore[method-assign]
    try:
        settings = make_settings().model_copy(update={"tls_fingerprint": "cd" * 32})
        ProxmoxerClient.from_settings(settings)
    finally:
        _FakeAPI.__init__ = original  # type: ignore[method-assign]
    assert isinstance(session.mounted["https://"], _PinnedAdapter)
    assert fake_proxmoxer.instances[-1].kwargs["verify_ssl"] is False
