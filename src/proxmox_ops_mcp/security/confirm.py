"""Two-phase confirmation for write operations.

Every write tool first runs as a *preview*. The preview describes exactly what would
happen and returns a confirmation token that is

* bound to the action and its exact parameters (HMAC-SHA256 over canonical JSON),
* short-lived (default 5 minutes),
* single-use, and
* signed with a per-process random key, so tokens die with the server.

The model has to pass the token back unchanged to execute. This guarantees the change
that runs is the change that was shown - it does not replace a human in the loop. Pair it
with your MCP client's per-tool approval prompt; write tools are annotated as such.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from proxmox_ops_mcp.errors import ConfirmationError


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A previewed action waiting for confirmation."""

    action: str
    params: Mapping[str, Any]
    expires_at: float


class ConfirmationManager:
    """Issues and redeems confirmation tokens."""

    def __init__(self, ttl_seconds: int = 300, *, clock: Callable[[], float] = time.time) -> None:
        """Create a manager whose tokens live for ``ttl_seconds``."""
        self._key = secrets.token_bytes(32)
        self._ttl = ttl_seconds
        self._clock = clock
        self._pending: dict[str, PendingAction] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> int:
        """Lifetime of issued tokens."""
        return self._ttl

    def issue(self, action: str, params: Mapping[str, Any]) -> str:
        """Return a fresh token for ``action`` with ``params``."""
        nonce = secrets.token_urlsafe(12)
        expires_at = self._clock() + self._ttl
        digest = self._sign(nonce, action, params)
        token = f"{nonce}.{digest}"
        with self._lock:
            self._purge_expired()
            self._pending[token] = PendingAction(action, dict(params), expires_at)
        return token

    def redeem(self, token: str, action: str, params: Mapping[str, Any]) -> None:
        """Consume ``token`` or raise :class:`ConfirmationError`.

        The token is consumed even when the parameters do not match, so a leaked or
        mistyped token cannot be retried against different parameters.
        """
        with self._lock:
            pending = self._pending.pop(token, None)
            self._purge_expired()
        if pending is None:
            raise ConfirmationError(
                "Unknown, expired or already used confirmation token. Run the tool again "
                "without 'confirmation_token' to get a fresh preview."
            )
        if self._clock() > pending.expires_at:
            raise ConfirmationError("Confirmation token expired. Request a new preview.")
        nonce, _, digest = token.partition(".")
        expected = self._sign(nonce, action, params)
        if pending.action != action or not hmac.compare_digest(digest, expected):
            raise ConfirmationError(
                "Confirmation token does not match this action and these parameters. "
                "The token has been invalidated; request a new preview."
            )

    def _sign(self, nonce: str, action: str, params: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {"nonce": nonce, "action": action, "params": params},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def _purge_expired(self) -> None:
        now = self._clock()
        for token in [t for t, p in self._pending.items() if now > p.expires_at]:
            del self._pending[token]
