"""Tunable thresholds shared by the diagnostics."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Thresholds(BaseModel):
    """Utilisation ratios (0-1) at which findings are raised."""

    cpu_warning: float = Field(default=0.85, ge=0, le=1)
    cpu_critical: float = Field(default=0.95, ge=0, le=1)
    memory_warning: float = Field(default=0.85, ge=0, le=1)
    memory_critical: float = Field(default=0.95, ge=0, le=1)
    storage_warning: float = Field(default=0.80, ge=0, le=1)
    storage_critical: float = Field(default=0.90, ge=0, le=1)
    iowait_warning: float = Field(default=0.10, ge=0, le=1)
    iowait_critical: float = Field(default=0.25, ge=0, le=1)
    load_per_cpu_warning: float = Field(default=1.0, gt=0)
    load_per_cpu_critical: float = Field(default=2.0, gt=0)
    swap_warning: float = Field(default=0.10, ge=0, le=1)
    swap_critical: float = Field(default=0.50, ge=0, le=1)
    rootfs_warning: float = Field(default=0.80, ge=0, le=1)
    rootfs_critical: float = Field(default=0.90, ge=0, le=1)
    memory_pressure_warning_pct: float = Field(default=1.0, ge=0, le=100)
    memory_pressure_critical_pct: float = Field(default=10.0, ge=0, le=100)
    io_pressure_warning_pct: float = Field(default=5.0, ge=0, le=100)
    io_pressure_critical_pct: float = Field(default=20.0, ge=0, le=100)
    scrub_max_age_days: float = Field(default=35, gt=0)
    disk_life_warning: float = Field(
        default=0.20, ge=0, le=1, description="Remaining SSD endurance that triggers a warning."
    )
    disk_life_critical: float = Field(default=0.10, ge=0, le=1)
    certificate_warning_days: float = Field(default=30, ge=0)
    certificate_critical_days: float = Field(default=7, ge=0)
    memory_imbalance_pp: float = Field(
        default=30.0, ge=0, le=100, description="Memory usage spread between nodes, in points."
    )


DEFAULT_THRESHOLDS = Thresholds()
