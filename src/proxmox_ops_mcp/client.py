"""Thin, validated access to the Proxmox VE REST API.

Everything above this module talks to :class:`ProxmoxClient`, a two-method protocol.
That keeps the analysis and tool layers trivially testable with an in-memory fake and
confines ``proxmoxer`` to a single place.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from requests.adapters import HTTPAdapter

from proxmox_ops_mcp.errors import ProxmoxAPIError, ValidationError

if TYPE_CHECKING:
    from proxmox_ops_mcp.config import Settings

GuestType = Literal["qemu", "lxc"]

_NODE_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_STORAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_SNAPSHOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")
_UPID_RE = re.compile(
    r"^UPID:[A-Za-z0-9-]+:[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8}:[0-9A-Fa-f]{8}:"
    r"[A-Za-z0-9_-]+:[A-Za-z0-9_.@!=-]*:[A-Za-z0-9_.@!=-]+:$"
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.:@!=-]+$")
_MIN_VMID, _MAX_VMID = 100, 999_999_999


def validate_node(node: str) -> str:
    """Return ``node`` if it is a valid Proxmox node name, else raise."""
    if not _NODE_RE.fullmatch(node):
        raise ValidationError(f"invalid node name: {node!r}")
    return node


def validate_vmid(vmid: int) -> int:
    """Return ``vmid`` if it is inside the Proxmox VMID range, else raise."""
    if isinstance(vmid, bool) or not _MIN_VMID <= vmid <= _MAX_VMID:
        raise ValidationError(f"vmid must be between {_MIN_VMID} and {_MAX_VMID}, got {vmid!r}")
    return vmid


def validate_storage(storage: str) -> str:
    """Return ``storage`` if it is a valid storage id, else raise."""
    if not _STORAGE_RE.fullmatch(storage):
        raise ValidationError(f"invalid storage id: {storage!r}")
    return storage


def validate_snapshot_name(name: str) -> str:
    """Return ``name`` if Proxmox would accept it as a snapshot name, else raise."""
    if not _SNAPSHOT_RE.fullmatch(name) or name == "current":
        raise ValidationError(
            "snapshot name must start with a letter, be 2-40 chars of [A-Za-z0-9_-] "
            f"and not be 'current'; got {name!r}"
        )
    return name


def validate_guest_type(kind: str) -> GuestType:
    """Return ``kind`` if it is ``qemu`` or ``lxc``, else raise."""
    if kind == "qemu":
        return "qemu"
    if kind == "lxc":
        return "lxc"
    raise ValidationError(f"unexpected guest type: {kind!r}")


def validate_upid(upid: str) -> str:
    """Return ``upid`` if it looks like a Proxmox task id, else raise."""
    if not _UPID_RE.fullmatch(upid):
        raise ValidationError(f"invalid task UPID: {upid!r}")
    return upid


class _PinnedAdapter(HTTPAdapter):
    """Accept exactly one server certificate, identified by its SHA-256 fingerprint."""

    def __init__(self, fingerprint: str) -> None:
        self._fingerprint = fingerprint
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any) -> Any:
        # requests builds a separate pool manager per proxy (HTTPS_PROXY & co.); without
        # this the pin would silently not apply to proxied connections.
        proxy_kwargs["assert_fingerprint"] = self._fingerprint
        return super().proxy_manager_for(proxy, **proxy_kwargs)


class ProxmoxClient(Protocol):
    """The minimal surface the rest of the package needs.

    Paths are relative to ``/api2/json`` and must be built only from validated segments.
    """

    def get(self, path: str, **params: Any) -> Any:
        """Issue a GET request and return the decoded ``data`` payload."""

    def post(self, path: str, **data: Any) -> Any:
        """Issue a POST request and return the decoded ``data`` payload."""


class ProxmoxerClient:
    """:class:`ProxmoxClient` backed by ``proxmoxer`` using API-token authentication."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token_user: str,
        token_name: str,
        token_secret: str,
        verify_ssl: bool | str,
        timeout: int,
        tls_fingerprint: str | None = None,
    ) -> None:
        """Create the client. No network traffic happens until the first request."""
        from proxmoxer import ProxmoxAPI  # noqa: PLC0415 - keep import cost off the test path

        self._api = ProxmoxAPI(
            host,
            port=port,
            user=token_user,
            token_name=token_name,
            token_value=token_secret,
            verify_ssl=verify_ssl,
            timeout=timeout,
            service="PVE",
        )
        if tls_fingerprint:
            # proxmoxer exposes no hook for this, so mount an adapter on its session.
            self._api._store["session"].mount("https://", _PinnedAdapter(tls_fingerprint))

    @classmethod
    def from_settings(cls, settings: Settings) -> ProxmoxerClient:
        """Build a client from :class:`~proxmox_ops_mcp.config.Settings`."""
        return cls(
            host=settings.host,
            port=settings.port,
            token_user=settings.token_user,
            token_name=settings.token_name,
            token_secret=settings.token_secret.get_secret_value(),
            verify_ssl=settings.tls_verify,
            timeout=settings.timeout,
            tls_fingerprint=settings.tls_fingerprint,
        )

    def get(self, path: str, **params: Any) -> Any:
        """See :meth:`ProxmoxClient.get`."""
        return self._call("get", path, params)

    def post(self, path: str, **data: Any) -> Any:
        """See :meth:`ProxmoxClient.post`."""
        return self._call("post", path, data)

    def _call(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        from proxmoxer.core import ResourceException  # noqa: PLC0415

        # Defence in depth: callers validate their inputs, but every segment is checked
        # again here so nothing (including values echoed back by the API) can smuggle in
        # traversal, query strings, fragments or percent-encoding.
        segments = path.split("/")
        if any(s in {".", ".."} or not _PATH_SEGMENT_RE.fullmatch(s) for s in segments):
            raise ValidationError(f"refusing to request unsafe API path: {path!r}")
        resource = self._api(path)
        try:
            return getattr(resource, method)(**payload)
        except ResourceException as exc:
            raise ProxmoxAPIError(
                f"Proxmox API {method.upper()} /{path} failed: "
                f"{exc.status_code} {exc.status_message}",
                status_code=cast("int", exc.status_code),
            ) from exc
        except Exception as exc:
            # Connection errors, TLS failures and timeouts from requests. Do not echo the
            # exception text verbatim: it can include request headers in some versions.
            raise ProxmoxAPIError(
                f"Proxmox API {method.upper()} /{path} failed: {type(exc).__name__}"
            ) from exc
