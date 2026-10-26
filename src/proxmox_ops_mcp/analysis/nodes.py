"""Side-by-side node comparison to answer "why is node X slower than node Y?"."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, Field

from proxmox_ops_mcp.analysis._util import as_float, as_int, gib, items, pct, ratio
from proxmox_ops_mcp.analysis.findings import (
    Finding,
    Severity,
    overall_status,
    sort_findings,
    threshold_finding,
)
from proxmox_ops_mcp.analysis.thresholds import DEFAULT_THRESHOLDS, Thresholds


class NodeMetrics(BaseModel):
    """Metrics for one node from ``/nodes/{node}/status`` and recent RRD averages.

    CPU, I/O wait and pressure values are averaged over the last few RRD samples when
    available: Proxmox VE 9 reports ``cpu``/``wait`` in the status call as 0.
    """

    node: str
    cpus: int
    cpu_model: str | None
    cpu_pct: float
    iowait_pct: float
    load_1m: float
    load_per_cpu: float
    memory_used_gib: float
    memory_total_gib: float
    memory_pct: float
    swap_pct: float
    memory_pressure_pct: float | None = Field(
        default=None, description="PSI 'full' memory stall share, if the node reports it."
    )
    io_pressure_pct: float | None = Field(
        default=None, description="PSI 'full' I/O stall share, if the node reports it."
    )
    rootfs_pct: float
    uptime_hours: float
    guests_running: int
    pve_version: str | None
    kernel: str | None


class NodeComparisonReport(BaseModel):
    """Result of :func:`compare_nodes`."""

    status: Severity
    nodes: list[NodeMetrics]
    findings: list[Finding]


_RRD_SAMPLES = 5


def _rrd_average(rrd: Any, key: str) -> float | None:
    values = [as_float(s.get(key)) for s in items(rrd) if s.get(key) is not None]
    recent = values[-_RRD_SAMPLES:]
    return sum(recent) / len(recent) if recent else None


def node_metrics(
    node: str, status: Mapping[str, Any], guests_running: int, rrd: Any = None
) -> NodeMetrics:
    """Normalise ``/nodes/{node}/status``, preferring ``rrddata`` averages where present."""
    cpuinfo = status.get("cpuinfo") or {}
    memory = status.get("memory") or {}
    swap = status.get("swap") or {}
    rootfs = status.get("rootfs") or {}
    loadavg = status.get("loadavg") or []
    cpus = as_int(cpuinfo.get("cpus"), 1) or 1
    load_1m = as_float(loadavg[0]) if loadavg else 0.0
    kversion = status.get("kversion")
    cpu = _rrd_average(rrd, "cpu")
    iowait = _rrd_average(rrd, "iowait")
    memory_pressure = _rrd_average(rrd, "pressurememoryfull")
    io_pressure = _rrd_average(rrd, "pressureiofull")
    return NodeMetrics(
        node=node,
        cpus=cpus,
        cpu_model=cpuinfo.get("model"),
        cpu_pct=pct(cpu if cpu is not None else as_float(status.get("cpu"))),
        iowait_pct=pct(iowait if iowait is not None else as_float(status.get("wait"))),
        load_1m=round(load_1m, 2),
        load_per_cpu=round(load_1m / cpus, 2),
        memory_used_gib=gib(memory.get("used")),
        memory_total_gib=gib(memory.get("total")),
        memory_pct=pct(ratio(memory.get("used"), memory.get("total"))),
        swap_pct=pct(ratio(swap.get("used"), swap.get("total"))),
        memory_pressure_pct=round(memory_pressure, 2) if memory_pressure is not None else None,
        io_pressure_pct=round(io_pressure, 2) if io_pressure is not None else None,
        rootfs_pct=pct(ratio(rootfs.get("used"), rootfs.get("total"))),
        uptime_hours=round(as_float(status.get("uptime")) / 3600, 1),
        guests_running=guests_running,
        pve_version=status.get("pveversion"),
        kernel=str(kversion).split(" ")[1] if kversion and " " in str(kversion) else kversion,
    )


def compare_nodes(
    metrics: list[NodeMetrics], thresholds: Thresholds = DEFAULT_THRESHOLDS
) -> NodeComparisonReport:
    """Flag per-node anomalies and cluster-wide inconsistencies."""
    findings: list[Finding] = []
    for m in metrics:
        findings += threshold_finding(
            m.cpu_pct / 100,
            thresholds.cpu_warning,
            thresholds.cpu_critical,
            code="NODE_CPU_HIGH",
            message=f"CPU usage on {m.node} is {m.cpu_pct}%.",
            resource=m.node,
            recommendation="Find the top consumers in the guest list and consider rebalancing.",
        )
        findings += threshold_finding(
            m.memory_pct / 100,
            thresholds.memory_warning,
            thresholds.memory_critical,
            code="NODE_MEMORY_HIGH",
            message=f"Memory usage on {m.node} is {m.memory_pct}%.",
            resource=m.node,
            recommendation="Check ZFS ARC size, ballooning and memory over-commitment.",
        )
        findings += threshold_finding(
            m.iowait_pct / 100,
            thresholds.iowait_warning,
            thresholds.iowait_critical,
            code="NODE_IOWAIT_HIGH",
            message=f"I/O wait on {m.node} is {m.iowait_pct}%; storage is the likely bottleneck.",
            resource=m.node,
            recommendation="Inspect disk latency (iostat -x), ZFS/Ceph health and "
            "noisy-neighbour guests.",
        )
        findings += threshold_finding(
            m.load_per_cpu,
            thresholds.load_per_cpu_warning,
            thresholds.load_per_cpu_critical,
            code="NODE_LOAD_HIGH",
            message=f"Load on {m.node} is {m.load_1m} for {m.cpus} CPUs "
            f"({m.load_per_cpu} per CPU).",
            resource=m.node,
            recommendation="Processes are queueing for CPU or blocked on I/O.",
        )
        findings += _swap_finding(m, thresholds)
        findings += _pressure_findings(m, thresholds)
        findings += threshold_finding(
            m.rootfs_pct / 100,
            thresholds.rootfs_warning,
            thresholds.rootfs_critical,
            code="NODE_ROOTFS_FULL",
            message=f"Root filesystem on {m.node} is {m.rootfs_pct}% full.",
            resource=m.node,
            recommendation="Clean /var/log, old kernels and local ISO/backup files.",
        )

    findings += _consistency_findings(metrics, thresholds)
    return NodeComparisonReport(
        status=overall_status(findings),
        nodes=sorted(metrics, key=lambda m: m.node),
        findings=sort_findings(findings),
    )


def _swap_finding(m: NodeMetrics, t: Thresholds) -> list[Finding]:
    findings = threshold_finding(
        m.swap_pct / 100,
        t.swap_warning,
        t.swap_critical,
        code="NODE_SWAP_ACTIVE",
        message=f"{m.swap_pct}% of swap is in use on {m.node}.",
        resource=m.node,
        recommendation="Swapping hypervisors hurt every guest. Reduce memory pressure.",
    )
    calm = (
        m.memory_pressure_pct is not None and m.memory_pressure_pct < t.memory_pressure_warning_pct
    )
    if findings and calm:
        # Full swap with no memory stalls usually means cold pages were pushed out long
        # ago. Still worth fixing (no headroom left), but not an emergency.
        findings[0] = findings[0].model_copy(
            update={
                "severity": Severity.WARNING,
                "message": f"{m.swap_pct}% of swap is in use on {m.node}, but memory "
                f"pressure is low ({m.memory_pressure_pct}% stalled): likely cold pages, "
                "yet no swap headroom is left for a real spike.",
                "recommendation": "Check which guests are over-provisioned (ballooning, ZFS "
                "ARC limit) and consider 'swapoff -a && swapon -a' in a quiet window.",
            }
        )
    return findings


def _pressure_findings(m: NodeMetrics, t: Thresholds) -> list[Finding]:
    findings: list[Finding] = []
    if m.memory_pressure_pct is not None:
        findings += threshold_finding(
            m.memory_pressure_pct,
            t.memory_pressure_warning_pct,
            t.memory_pressure_critical_pct,
            code="NODE_MEMORY_PRESSURE",
            message=f"Tasks on {m.node} are fully stalled on memory "
            f"{m.memory_pressure_pct}% of the time (PSI).",
            resource=m.node,
            recommendation="The node is short on RAM right now. Move or shrink guests.",
        )
    if m.io_pressure_pct is not None:
        findings += threshold_finding(
            m.io_pressure_pct,
            t.io_pressure_warning_pct,
            t.io_pressure_critical_pct,
            code="NODE_IO_PRESSURE",
            message=f"Tasks on {m.node} are fully stalled on I/O "
            f"{m.io_pressure_pct}% of the time (PSI).",
            resource=m.node,
            recommendation="Find the noisy guest (per-guest disk I/O) and check pool health.",
        )
    return findings


def _consistency_findings(metrics: list[NodeMetrics], t: Thresholds) -> list[Finding]:
    findings = []
    versions = {m.pve_version for m in metrics if m.pve_version}
    if len(versions) > 1:
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code="PVE_VERSION_MISMATCH",
                message="Nodes run different Proxmox VE versions: " + ", ".join(sorted(versions)),
                recommendation="Finish the rolling upgrade; mixed versions are only supported "
                "temporarily.",
            )
        )
    kernels = {m.kernel for m in metrics if m.kernel}
    if len(kernels) > 1:
        findings.append(
            Finding(
                severity=Severity.INFO,
                code="KERNEL_MISMATCH",
                message="Nodes run different kernels: " + ", ".join(sorted(kernels)),
                recommendation="Reboot nodes that have a pending kernel update.",
            )
        )
    if len(metrics) > 1:
        lo = min(metrics, key=lambda m: m.memory_pct)
        hi = max(metrics, key=lambda m: m.memory_pct)
        if hi.memory_pct - lo.memory_pct >= t.memory_imbalance_pp:
            findings.append(
                Finding(
                    severity=Severity.INFO,
                    code="CLUSTER_MEMORY_IMBALANCE",
                    message=f"Memory usage ranges from {lo.memory_pct}% ({lo.node}) to "
                    f"{hi.memory_pct}% ({hi.node}).",
                    resource=hi.node,
                    recommendation=f"Consider migrating guests from {hi.node} to {lo.node}.",
                )
            )
    return findings
