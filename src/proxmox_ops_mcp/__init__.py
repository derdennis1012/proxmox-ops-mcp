"""proxmox-ops-mcp: an MCP server for operating Proxmox VE."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("proxmox-ops-mcp")
except PackageNotFoundError:  # pragma: no cover - only hit when running from a raw checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
