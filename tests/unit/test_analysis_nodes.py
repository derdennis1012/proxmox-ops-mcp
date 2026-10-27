from __future__ import annotations

from typing import Any

from proxmox_ops_mcp.analysis.findings import Severity
from proxmox_ops_mcp.analysis.nodes import compare_nodes, node_metrics
from tests.fakes import GIB, build_cluster


def _metrics(overrides: dict[str, dict[str, Any]] | None = None) -> list[Any]:
    c = build_cluster()
    out = []
    for n in ("pve1", "pve2", "pve3"):
        status = c[f"nodes/{n}/status"]
        status.update((overrides or {}).get(n, {}))
        out.append(node_metrics(n, status, guests_running=1))
    return out


def test_normalisation() -> None:
    m = _metrics()[0]
    assert m.cpus == 32
    assert m.cpu_pct == 35.0
    assert m.load_per_cpu == 0.25
    assert m.memory_pct == 37.5
    assert m.kernel == "6.8.12-4-pve"
    assert m.uptime_hours == 240.0


def test_balanced_cluster_is_clean() -> None:
    report = compare_nodes(_metrics())
    assert report.status is Severity.INFO
    assert report.findings == []


def test_slow_node_is_explained() -> None:
    report = compare_nodes(
        _metrics(
            {
                "pve2": {
                    "wait": 0.3,
                    "loadavg": ["70.0", "60", "50"],
                    "swap": {"used": 6 * GIB, "total": 8 * GIB},
                    "memory": {"used": 250 * GIB, "total": 256 * GIB},
                    "rootfs": {"used": 85 * GIB, "total": 100 * GIB},
                    "cpu": 0.9,
                    "pveversion": "pve-manager/8.2.4/xyz",
                    "kversion": "Linux 6.8.4-2-pve #1 SMP",
                }
            }
        )
    )
    on_pve2 = {f.code: f.severity for f in report.findings if f.resource == "pve2"}
    assert on_pve2["NODE_IOWAIT_HIGH"] is Severity.CRITICAL
    assert on_pve2["NODE_LOAD_HIGH"] is Severity.CRITICAL
    assert on_pve2["NODE_SWAP_ACTIVE"] is Severity.CRITICAL
    assert on_pve2["NODE_MEMORY_HIGH"] is Severity.CRITICAL
    assert on_pve2["NODE_CPU_HIGH"] is Severity.WARNING
    assert on_pve2["NODE_ROOTFS_FULL"] is Severity.WARNING
    assert on_pve2["CLUSTER_MEMORY_IMBALANCE"] is Severity.INFO
    codes = {f.code for f in report.findings}
    assert {"PVE_VERSION_MISMATCH", "KERNEL_MISMATCH"} <= codes


def test_sparse_status_payload() -> None:
    m = node_metrics("x", {}, 0)
    assert m.cpus == 1
    assert m.load_1m == 0.0
    assert m.kernel is None


def test_rrd_averages_override_zeroed_status_values() -> None:
    # Proxmox VE 9 reports cpu/wait as 0 in /nodes/{node}/status.
    status = build_cluster()["nodes/pve1/status"] | {"cpu": 0, "wait": 0}
    rrd = [{"cpu": 0.9, "iowait": 0.9}] * 10 + [
        {"cpu": 0.2, "iowait": 0.02, "pressurememoryfull": 0.5, "pressureiofull": 30.0}
    ] * 5
    m = node_metrics("pve1", status, 0, rrd)
    assert m.cpu_pct == 20.0  # average of the last 5 samples only
    assert m.iowait_pct == 2.0
    assert m.memory_pressure_pct == 0.5
    report = compare_nodes([m])
    assert {f.code: f.severity for f in report.findings}["NODE_IO_PRESSURE"] is Severity.CRITICAL


def test_full_swap_without_memory_pressure_is_only_a_warning() -> None:
    status = build_cluster()["nodes/pve1/status"] | {"swap": {"used": 8 * GIB, "total": 8 * GIB}}
    calm = node_metrics("pve1", status, 0, [{"cpu": 0.1, "pressurememoryfull": 0.07}])
    finding = next(f for f in compare_nodes([calm]).findings if f.code == "NODE_SWAP_ACTIVE")
    assert finding.severity is Severity.WARNING
    assert "cold pages" in finding.message

    stalled = node_metrics("pve1", status, 0, [{"cpu": 0.1, "pressurememoryfull": 12.0}])
    by_code = {f.code: f.severity for f in compare_nodes([stalled]).findings}
    assert by_code["NODE_SWAP_ACTIVE"] is Severity.CRITICAL
    assert by_code["NODE_MEMORY_PRESSURE"] is Severity.CRITICAL

    unknown = node_metrics("pve1", status, 0)  # no RRD: keep the conservative severity
    by_code = {f.code: f.severity for f in compare_nodes([unknown]).findings}
    assert by_code["NODE_SWAP_ACTIVE"] is Severity.CRITICAL
