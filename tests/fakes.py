"""In-memory stand-in for the Proxmox API, plus a realistic three-node cluster fixture."""

from __future__ import annotations

import copy
import re
from typing import Any

from proxmox_ops_mcp.errors import ProxmoxAPIError

GIB = 1024**3
NOW = 1_767_225_600  # 2026-01-01T00:00:00Z


class FakeProxmoxClient:
    """Serves GET responses from a dict and records every POST."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = routes if routes is not None else build_cluster()
        self.gets: list[tuple[str, dict[str, Any]]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.post_result: Any = "UPID:pve1:0000ABCD:00112233:6955B600:qmstart:101:ops@pve!mcp:"

    def get(self, path: str, **params: Any) -> Any:
        self.gets.append((path, params))
        if path == "cluster/resources" and "type" in params:
            wanted = {"vm": {"qemu", "lxc"}}.get(params["type"], {params["type"]})
            return [r for r in self.routes[path] if r.get("type") in wanted]
        live = re.fullmatch(r"nodes/[^/]+/(?:qemu|lxc)/(\d+)/status/current", path)
        if live and path not in self.routes:
            vmid = int(live.group(1))
            guest = next(r for r in self.routes["cluster/resources"] if r.get("vmid") == vmid)
            return {"status": guest["status"], "qmpstatus": guest["status"]}
        if path not in self.routes:
            raise ProxmoxAPIError(f"GET /{path} failed: 404 Not Found", status_code=404)
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def post(self, path: str, **data: Any) -> Any:
        self.posts.append((path, data))
        return self.post_result


def build_cluster() -> dict[str, Any]:
    """Three nodes, Ceph RBD, a local-lvm per node, NFS backups and five guests."""
    nodes = [
        {
            "type": "node",
            "node": "pve1",
            "status": "online",
            "cpu": 0.35,
            "maxcpu": 32,
            "mem": 96 * GIB,
            "maxmem": 256 * GIB,
            "uptime": 864000,
        },
        {
            "type": "node",
            "node": "pve2",
            "status": "online",
            "cpu": 0.20,
            "maxcpu": 32,
            "mem": 64 * GIB,
            "maxmem": 256 * GIB,
            "uptime": 864000,
        },
        {
            "type": "node",
            "node": "pve3",
            "status": "online",
            "cpu": 0.10,
            "maxcpu": 32,
            "mem": 32 * GIB,
            "maxmem": 256 * GIB,
            "uptime": 864000,
        },
    ]
    guests = [
        {
            "type": "qemu",
            "vmid": 100,
            "name": "db01",
            "node": "pve1",
            "status": "running",
            "maxcpu": 8,
            "cpu": 0.4,
            "mem": 24 * GIB,
            "maxmem": 32 * GIB,
            "maxdisk": 200 * GIB,
            "uptime": 3600,
            "tags": "prod;db",
            "hastate": "started",
        },
        {
            "type": "qemu",
            "vmid": 101,
            "name": "web01",
            "node": "pve1",
            "status": "running",
            "maxcpu": 4,
            "cpu": 0.1,
            "mem": 4 * GIB,
            "maxmem": 8 * GIB,
            "maxdisk": 32 * GIB,
            "uptime": 3600,
            "tags": "prod",
            "pool": "web",
        },
        {
            "type": "lxc",
            "vmid": 200,
            "name": "dns01",
            "node": "pve1",
            "status": "running",
            "maxcpu": 2,
            "cpu": 0.01,
            "mem": 1 * GIB,
            "maxmem": 2 * GIB,
            "maxdisk": 8 * GIB,
            "uptime": 3600,
        },
        {
            "type": "qemu",
            "vmid": 102,
            "name": "gpu01",
            "node": "pve2",
            "status": "running",
            "maxcpu": 16,
            "cpu": 0.8,
            "mem": 60 * GIB,
            "maxmem": 64 * GIB,
            "maxdisk": 100 * GIB,
            "uptime": 3600,
        },
        {
            "type": "qemu",
            "vmid": 103,
            "name": "old01",
            "node": "pve3",
            "status": "stopped",
            "maxcpu": 2,
            "cpu": 0,
            "mem": 0,
            "maxmem": 4 * GIB,
            "maxdisk": 16 * GIB,
            "uptime": 0,
        },
        {
            "type": "qemu",
            "vmid": 9000,
            "name": "tpl-debian",
            "node": "pve3",
            "status": "stopped",
            "maxcpu": 2,
            "cpu": 0,
            "mem": 0,
            "maxmem": 2 * GIB,
            "maxdisk": 10 * GIB,
            "template": 1,
        },
    ]
    storages = []
    for n in ("pve1", "pve2", "pve3"):
        storages += [
            {
                "type": "storage",
                "storage": "ceph-vm",
                "node": n,
                "status": "available",
                "shared": 1,
                "plugintype": "rbd",
                "content": "images,rootdir",
                "disk": 4000 * GIB,
                "maxdisk": 10000 * GIB,
            },
            {
                "type": "storage",
                "storage": "local-lvm",
                "node": n,
                "status": "available",
                "shared": 0,
                "plugintype": "lvmthin",
                "content": "images,rootdir",
                "disk": 100 * GIB,
                "maxdisk": 500 * GIB,
            },
            {
                "type": "storage",
                "storage": "nfs-backup",
                "node": n,
                "status": "available",
                "shared": 1,
                "plugintype": "nfs",
                "content": "backup,iso",
                "disk": 2000 * GIB,
                "maxdisk": 8000 * GIB,
            },
        ]

    def node_status(cpu: float, wait: float, load: str, used: int, ver: str) -> dict[str, Any]:
        return {
            "cpu": cpu,
            "wait": wait,
            "loadavg": [load, "1.0", "1.0"],
            "uptime": 864000,
            "cpuinfo": {"cpus": 32, "model": "AMD EPYC 7302P"},
            "memory": {"used": used * GIB, "total": 256 * GIB},
            "swap": {"used": 0, "total": 8 * GIB},
            "rootfs": {"used": 10 * GIB, "total": 100 * GIB},
            "pveversion": f"pve-manager/{ver}/abcdef",
            "kversion": "Linux 6.8.12-4-pve #1 SMP PREEMPT_DYNAMIC PMX 6.8.12-4",
        }

    return {
        "cluster/status": [
            {"type": "cluster", "name": "prod", "quorate": 1, "nodes": 3, "version": 7},
            *({"type": "node", "name": n, "online": 1} for n in ("pve1", "pve2", "pve3")),
        ],
        "cluster/resources": nodes + guests + storages,
        "cluster/ha/status/current": [
            {"type": "quorum", "status": "OK"},
            {"type": "lrm", "node": "pve1", "status": "active"},
            {"type": "service", "sid": "vm:100", "state": "started", "node": "pve1"},
        ],
        "cluster/ha/resources": [{"sid": "vm:100", "state": "started", "group": "prod"}],
        "cluster/ceph/status": {
            "fsid": "b5b1c1f2-0000-4000-8000-000000000000",
            "health": {"status": "HEALTH_OK", "checks": {}},
            "osdmap": {"num_osds": 9, "num_up_osds": 9, "num_in_osds": 9},
            "pgmap": {
                "num_pgs": 256,
                "pgs_by_state": [{"state_name": "active+clean", "count": 256}],
                "bytes_used": 4000 * GIB,
                "bytes_total": 10000 * GIB,
            },
            "monmap": {"epoch": 3, "num_mons": 3, "min_mon_release_name": "reef"},
            "quorum_names": ["pve1", "pve2", "pve3"],
        },
        "nodes/pve1/status": node_status(0.35, 0.02, "8.0", 96, "8.3.2"),
        "nodes/pve2/status": node_status(0.20, 0.01, "4.0", 64, "8.3.2"),
        "nodes/pve3/status": node_status(0.10, 0.00, "1.0", 32, "8.3.2"),
        "nodes/pve1/qemu/100/config": {
            "scsi0": "ceph-vm:vm-100-disk-0,size=200G",
            "ide2": "none,media=cdrom",
            "memory": "32768",
            "cipassword": "$5$hash",
        },
        "nodes/pve1/qemu/101/config": {
            "scsi0": "local-lvm:vm-101-disk-0,size=32G",
            "memory": "8192",
        },
        "nodes/pve1/lxc/200/config": {
            "rootfs": "ceph-vm:subvol-200-disk-0,size=8G",
            "memory": "2048",
        },
        "nodes/pve2/qemu/102/config": {"scsi0": "ceph-vm:vm-102-disk-0", "hostpci0": "0000:01:00"},
        "nodes/pve3/qemu/103/config": {"scsi0": "ceph-vm:vm-103-disk-0"},
        "nodes/pve1/qemu/101/snapshot": [{"name": "current"}],
        "nodes/pve1/qemu/100/snapshot": [{"name": "pre-upgrade"}, {"name": "current"}],
        "cluster/backup": [
            {
                "id": "backup-daily",
                "enabled": 1,
                "vmid": "100,200",
                "schedule": "02:00",
                "storage": "nfs-backup",
            },
            {
                "id": "backup-web",
                "enabled": 1,
                "pool": "web",
                "schedule": "03:00",
                "storage": "nfs-backup",
            },
            {"id": "backup-legacy", "enabled": 0, "all": 1, "storage": "nfs-backup"},
        ],
        "nodes/pve1/storage/nfs-backup/content": [
            {
                "volid": "nfs-backup:backup/vzdump-qemu-100-a.vma.zst",
                "vmid": 100,
                "ctime": NOW - 6 * 3600,
                "content": "backup",
            },
            {
                "volid": "nfs-backup:backup/vzdump-qemu-100-b.vma.zst",
                "vmid": 100,
                "ctime": NOW - 30 * 3600,
                "content": "backup",
            },
            {
                "volid": "nfs-backup:backup/vzdump-qemu-101-a.vma.zst",
                "vmid": 101,
                "ctime": NOW - 72 * 3600,
                "content": "backup",
            },
            {
                "volid": "nfs-backup:backup/vzdump-lxc-200-a.tar.zst",
                "vmid": 200,
                "ctime": NOW - 5 * 3600,
                "content": "backup",
            },
        ],
        "cluster/tasks": [
            {
                "upid": "UPID:pve1:00001111:00002222:6955A000:vzdump::root@pam:",
                "node": "pve1",
                "type": "vzdump",
                "id": "",
                "user": "root@pam",
                "starttime": NOW - 3600,
                "endtime": NOW - 3000,
                "status": "job errors",
            },
            {
                "upid": "UPID:pve2:00001111:00002222:6955A100:qmstart:102:root@pam:",
                "node": "pve2",
                "type": "qmstart",
                "id": "102",
                "user": "root@pam",
                "starttime": NOW - 7200,
                "endtime": NOW - 7190,
                "status": "OK",
            },
            {
                "upid": "UPID:pve2:00001111:00002222:6955A200:qmigrate:102:root@pam:",
                "node": "pve2",
                "type": "qmigrate",
                "id": "102",
                "user": "root@pam",
                "starttime": NOW - 600,
                "endtime": NOW - 500,
                "status": "WARNINGS: 1",
            },
        ],
    }
