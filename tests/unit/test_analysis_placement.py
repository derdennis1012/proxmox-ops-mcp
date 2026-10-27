from __future__ import annotations

from proxmox_ops_mcp.analysis.placement import PlacementRequest, recommend_placement
from tests.fakes import build_cluster


def test_prefers_emptiest_node() -> None:
    report = recommend_placement(
        PlacementRequest(cores=4, memory_gib=16, disk_gib=50), build_cluster()["cluster/resources"]
    )
    assert report.recommended_node == "pve3"
    assert report.recommended_storage == "ceph-vm"  # most free space
    scores = [c.score for c in report.candidates]
    assert scores == sorted(scores, reverse=True)


def test_memory_limit_and_offline_nodes_disqualify() -> None:
    resources = build_cluster()["cluster/resources"]
    resources[2]["status"] = "offline"  # pve3
    report = recommend_placement(PlacementRequest(cores=2, memory_gib=150), resources)
    by_node = {c.node: c for c in report.candidates}
    assert by_node["pve3"].eligible is False
    assert "node is offline" in by_node["pve3"].reasons
    assert by_node["pve1"].eligible is False  # 96 + 150 GiB > 85 % of 256 GiB
    assert report.recommended_node == "pve2"


def test_storage_constraints() -> None:
    resources = build_cluster()["cluster/resources"]
    report = recommend_placement(
        PlacementRequest(cores=1, memory_gib=1, disk_gib=450, storage="local-lvm"), resources
    )
    assert report.recommended_node is None
    assert all("local-lvm" in " ".join(c.reasons) for c in report.candidates)

    report = recommend_placement(
        PlacementRequest(cores=1, memory_gib=1, disk_gib=100, storage="local-lvm"), resources
    )
    assert report.recommended_storage == "local-lvm"


def test_vcpu_overcommit_and_exclusions() -> None:
    resources = build_cluster()["cluster/resources"]
    report = recommend_placement(
        PlacementRequest(cores=100, memory_gib=1, max_vcpu_ratio=3.5),
        resources,
        exclude_nodes=frozenset({"pve3"}),
    )
    assert {c.node for c in report.candidates} == {"pve1", "pve2"}
    assert report.recommended_node is None
    assert any("over-commit" in r for c in report.candidates for r in c.reasons)


def test_storage_filter_applies_without_disk_size() -> None:
    resources = build_cluster()["cluster/resources"]
    resources = [
        r for r in resources if not (r.get("storage") == "local-lvm" and r["node"] != "pve2")
    ]
    report = recommend_placement(
        PlacementRequest(cores=1, memory_gib=1, storage="local-lvm"), resources
    )
    assert report.recommended_node == "pve2"
    assert report.recommended_storage == "local-lvm"
