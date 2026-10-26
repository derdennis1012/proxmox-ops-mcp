"""Structured, append-only audit log for every state-changing request."""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO

AuditOutcome = Literal["previewed", "executed", "rejected", "failed"]

_SENSITIVE_KEY_RE = re.compile(
    r"secret|passw(or)?d|token|ticket|csrf|api[_-]?key|private[_-]?key", re.IGNORECASE
)
_REDACTED = "***"


def redact(value: Any) -> Any:
    """Recursively replace values of sensitive-looking keys with ``***``."""
    if isinstance(value, Mapping):
        return {
            str(k): _REDACTED if _SENSITIVE_KEY_RE.search(str(k)) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    return value


class AuditLogger:
    """Writes one JSON object per line.

    The file is opened in append mode for every event, so log rotation by external
    tools (logrotate, Docker log drivers) works without coordination.
    """

    def __init__(self, path: Path | None = None, *, stream: TextIO | None = None) -> None:
        """Log to ``path`` if given, otherwise to ``stream`` (default: stderr).

        Stdout is never used: it carries the MCP stdio protocol.
        """
        self._path = path
        self._stream = stream
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        action: str,
        outcome: AuditOutcome,
        tier: str,
        params: Mapping[str, Any],
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Append a single audit event."""
        event = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "action": action,
            "outcome": outcome,
            "tier": tier,
            "params": redact(params),
            "detail": redact(detail or {}),
        }
        line = json.dumps(event, sort_keys=True, default=str) + "\n"
        with self._lock:
            if self._path is not None:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            else:
                stream = self._stream or sys.stderr
                stream.write(line)
                stream.flush()
