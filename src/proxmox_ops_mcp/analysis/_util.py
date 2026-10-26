"""Small helpers for reading loosely-typed Proxmox API payloads."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def as_float(value: Any, default: float = 0.0) -> float:
    """Coerce an API value (often a string or ``None``) to ``float``."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    """Coerce an API value to ``int``."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ratio(used: Any, total: Any) -> float:
    """Return ``used / total`` in ``[0, 1]``-ish, or ``0.0`` when ``total`` is unusable."""
    t = as_float(total)
    return as_float(used) / t if t > 0 else 0.0


def pct(value: float) -> float:
    """Express a ratio as a percentage rounded to one decimal."""
    return round(value * 100, 1)


def gib(num_bytes: Any) -> float:
    """Bytes to GiB, rounded to two decimals."""
    return round(as_float(num_bytes) / 1024**3, 2)


def items(payload: Any) -> list[Mapping[str, Any]]:
    """Return ``payload`` as a list of mappings, tolerating ``None`` and junk entries."""
    if not isinstance(payload, list):
        return []
    return [p for p in payload if isinstance(p, Mapping)]
