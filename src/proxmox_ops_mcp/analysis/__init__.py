"""Pure analysis functions.

Nothing in this package performs I/O. Each function takes raw Proxmox API payloads and
returns a typed report, which makes the diagnostics deterministic and easy to test.
"""

from proxmox_ops_mcp.analysis.findings import Finding, Severity
from proxmox_ops_mcp.analysis.thresholds import Thresholds

__all__ = ["Finding", "Severity", "Thresholds"]
