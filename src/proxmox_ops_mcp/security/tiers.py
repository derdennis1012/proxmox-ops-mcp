"""Access tiers.

Tiers are ordered: ``read_only < operator < admin``. A tool that requires a higher tier
than the configured one is never *registered*, so the model cannot even see it. The
Proxmox API token should be scoped to the same level (see ``docs/proxmox-setup.md``) so
that the server-side check and the Proxmox-side ACL form two independent layers.
"""

from __future__ import annotations

from enum import StrEnum

from proxmox_ops_mcp.errors import PermissionDeniedError


class AccessTier(StrEnum):
    """What the server is allowed to do."""

    READ_ONLY = "read_only"
    """Inventory, diagnostics and planning. No state changes."""

    OPERATOR = "operator"
    """Adds graceful power actions and snapshots. Nothing that can lose data."""

    ADMIN = "admin"
    """Adds hard stop and live migration."""

    @property
    def rank(self) -> int:
        """Numeric rank used for comparisons."""
        return _RANK[self]

    def allows(self, required: AccessTier) -> bool:
        """Return whether this tier grants at least ``required``."""
        return self.rank >= required.rank

    def require(self, required: AccessTier, action: str) -> None:
        """Raise :class:`PermissionDeniedError` unless this tier grants ``required``."""
        if not self.allows(required):
            raise PermissionDeniedError(
                f"'{action}' requires access tier '{required}', but the server runs with '{self}'."
            )


_RANK = {AccessTier.READ_ONLY: 0, AccessTier.OPERATOR: 1, AccessTier.ADMIN: 2}
