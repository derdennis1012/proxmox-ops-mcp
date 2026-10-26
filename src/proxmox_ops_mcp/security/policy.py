"""Server-side limits for write operations, independent of Proxmox ACLs.

The token's Proxmox permissions are the primary boundary. This policy is a second one
that holds even if a token was granted more than intended: writes can be confined to
named pools or VMIDs, and their rate is capped so a looping model cannot fire off
dozens of actions.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from proxmox_ops_mcp.errors import PermissionDeniedError

_HOUR = 3600.0


@dataclass
class WritePolicy:
    """Which guests may be changed, and how often."""

    pools: frozenset[str] = frozenset()
    vmids: frozenset[int] = frozenset()
    max_per_hour: int = 30
    clock: Callable[[], float] = time.monotonic
    _recent: deque[float] = field(default_factory=deque, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def scoped(self) -> bool:
        """Whether writes are restricted to an allowlist."""
        return bool(self.pools or self.vmids)

    def check_scope(self, guest: Mapping[str, Any]) -> None:
        """Raise unless ``guest`` is inside the configured pools or VMIDs."""
        if not self.scoped:
            return
        vmid = int(guest.get("vmid") or 0)
        if vmid in self.vmids or (guest.get("pool") and str(guest["pool"]) in self.pools):
            return
        allowed = sorted(self.pools) + [str(v) for v in sorted(self.vmids)]
        raise PermissionDeniedError(
            f"guest {vmid} is outside the write scope of this server "
            f"(allowed: {', '.join(allowed)})."
        )

    def acquire(self) -> None:
        """Count one write, or raise if the hourly budget is used up."""
        now = self.clock()
        with self._lock:
            while self._recent and now - self._recent[0] >= _HOUR:
                self._recent.popleft()
            if len(self._recent) >= self.max_per_hour:
                wait = int(_HOUR - (now - self._recent[0])) + 1
                raise PermissionDeniedError(
                    f"write limit of {self.max_per_hour} per hour reached; next slot in "
                    f"{wait} s. Raise PROXMOX_MAX_WRITES_PER_HOUR if this is intended."
                )
            self._recent.append(now)
