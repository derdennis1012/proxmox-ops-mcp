"""Exception hierarchy.

Every error raised deliberately by this package derives from :class:`ProxmoxOpsError`,
so the MCP layer can turn it into a clean tool error instead of leaking a traceback.
"""


class ProxmoxOpsError(Exception):
    """Base class for all errors raised by proxmox-ops-mcp."""


class ConfigurationError(ProxmoxOpsError):
    """The server is misconfigured (missing credentials, invalid values, ...)."""


class ValidationError(ProxmoxOpsError):
    """A caller-supplied argument is invalid (e.g. a malformed node name)."""


class ProxmoxAPIError(ProxmoxOpsError):
    """The Proxmox VE API returned an error or could not be reached."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """Create the error, optionally carrying the HTTP status code."""
        super().__init__(message)
        self.status_code = status_code


class PermissionDeniedError(ProxmoxOpsError):
    """The configured access tier does not allow the requested operation."""


class ConfirmationError(ProxmoxOpsError):
    """A write operation was attempted without a valid confirmation token."""
