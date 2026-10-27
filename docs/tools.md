# Tool reference

Every tool returns structured content with a published JSON output schema. Diagnostic
reports share one shape. `status` holds the worst severity found, and `findings` is
sorted worst-first:

```jsonc
{
  "severity": "critical | warning | info",
  "code": "CEPH_OSD_DOWN",          // stable, safe to filter on
  "message": "1 osds down",
  "resource": "ceph",               // node, guest id, storage, ... (optional)
  "recommendation": "Check the OSD service ..."
}
```

## Inventory (`read_only`)

| Tool | Parameters | Notes |
|---|---|---|
| `list_nodes` | – | Status, CPUs, CPU %, memory, uptime |
| `list_guests` | `node`, `status` (`running`/`stopped`/`paused`), `guest_type` (`qemu`/`lxc`), `tag`, `include_templates` | Usage, tags, pool, HA state |
| `get_guest` | `vmid` | Live status (read from the node, not the resource cache), active lock, full configuration; keys that look secret (`cipassword`, …) are redacted |
| `get_task_status` | `node`, `upid`, `log_lines` (0-500) | State, exit status, last log lines (pages through long logs) |

## Diagnostics (`read_only`)

### `cluster_health`
Reads `/cluster/status`, `/cluster/resources` and `/cluster/ha/status/current`.

| Code | Severity | Meaning |
|---|---|---|
| `CLUSTER_NOT_QUORATE` | critical | Quorum lost; HA and configuration changes are blocked |
| `NODE_OFFLINE` | critical | A node is offline |
| `NODE_CPU_HIGH` / `NODE_MEMORY_HIGH` | warning/critical | Above 85 % / 95 % by default |
| `STORAGE_USAGE_HIGH` | warning/critical | Above 80 % / 90 %; shared storages are reported once |
| `STORAGE_UNAVAILABLE` | warning | Storage on an online node is not `available` |
| `HA_SERVICE_DEGRADED` | warning/critical | HA resource in `recovery` (warning) or `error`/`fence` (critical) |
| `HA_LRM_STALE` | warning | A node's local resource manager has stopped updating |

### `ceph_health`
Reads `/cluster/ceph/status`. Each active Ceph health check becomes a finding named
`CEPH_<CHECK>`, for example `CEPH_OSD_DOWN`, `CEPH_PG_DEGRADED`, `CEPH_PG_AVAILABILITY`,
`CEPH_OSD_NEARFULL`, `CEPH_MON_CLOCK_SKEW`, `CEPH_SLOW_OPS` or `CEPH_RECENT_CRASH`.
Common checks come with operator guidance. Muted checks are downgraded to `info`.

| Code | Meaning |
|---|---|
| `CEPH_MON_OUT_OF_QUORUM` | Fewer monitors in quorum than exist (when Ceph itself did not raise `MON_DOWN`) |
| `CEPH_CAPACITY_HIGH` | Raw usage above the storage thresholds; leave room to re-replicate a failed host |
| `CEPH_HEALTH_UNKNOWN` | No health status was returned |

The report also contains OSD counts (`total`, `up`, `in_cluster`, `down`, `out`), the PG
state distribution with the share of `active+clean`, monitor quorum and raw capacity.

### `compare_nodes`
Parameter `nodes` (optional, defaults to all online nodes). Reads `/nodes/{node}/status`
and averages the last five minutes of `/nodes/{node}/rrddata`: Proxmox VE 9 reports
`cpu` and `wait` as 0 in the status call. Where the node exposes pressure-stall
information (PSI), it is used to judge how much memory and I/O shortage actually hurts.

| Code | Default thresholds | Meaning |
|---|---|---|
| `NODE_IOWAIT_HIGH` | 10 % / 25 % | Storage is the bottleneck |
| `NODE_LOAD_HIGH` | 1.0 / 2.0 per CPU | Run queue saturated (CPU or blocked I/O) |
| `NODE_SWAP_ACTIVE` | 10 % / 50 % of swap | Swap in use; capped at *warning* when PSI shows no memory stalls (cold pages) |
| `NODE_MEMORY_PRESSURE` | 1 % / 10 % stalled | Tasks fully stalled waiting for memory (PSI) |
| `NODE_IO_PRESSURE` | 5 % / 20 % stalled | Tasks fully stalled waiting for I/O (PSI) |
| `NODE_ROOTFS_FULL` | 80 % / 90 % | Root filesystem filling up |
| `NODE_CPU_HIGH` / `NODE_MEMORY_HIGH` | as above | |
| `PVE_VERSION_MISMATCH` | – | Unfinished rolling upgrade |
| `KERNEL_MISMATCH` | – | Some nodes are waiting for a reboot into a new kernel |
| `CLUSTER_MEMORY_IMBALANCE` | spread ≥ 30 pp | Candidate for rebalancing |

### `recent_task_failures`
Parameters `hours` (1-720), `task_type` (e.g. `vzdump`, `qmigrate`) and `limit`. Tasks
that ended with `WARNINGS` are not counted as failures. The data comes from the
cluster's recent task list, which Proxmox keeps short.

### `storage_health`
Parameter `node` (optional). Reads `/nodes/{node}/disks/zfs` (plus per-pool detail),
`/nodes/{node}/disks/list` and `/nodes/{node}/disks/smart` for every disk.

| Code | Severity | Meaning |
|---|---|---|
| `ZFS_POOL_UNHEALTHY` | warning (`DEGRADED`) / critical (`FAULTED`, `UNAVAIL`, `SUSPENDED`) | Pool is not `ONLINE`; the affected devices are named |
| `ZFS_DEVICE_ERRORS` | warning | Read, write or checksum errors on a device of an otherwise healthy pool |
| `ZFS_DATA_ERRORS` | critical | Permanent errors in data (`zpool status -v` lists the files) |
| `ZFS_POOL_FULL` | warning/critical | Above 80 % / 90 % (ZFS slows down sharply when full) |
| `ZFS_SCRUB_OVERDUE` | warning | No completed scrub, or the last one is older than 35 days |
| `DISK_SMART_FAILED` | critical | SMART overall health is not `PASSED` |
| `DISK_WEAROUT` | warning/critical | SSD has ≤ 20 % / ≤ 10 % of its rated endurance left |
| `DISK_MEDIA_ERRORS` | warning | Reallocated, pending or uncorrectable sectors (ATA); media errors or critical warning flags (NVMe) |

Proxmox reports SSD `wearout` as *remaining* life; the report uses the same convention
(`life_remaining_pct`).

Both `storage_health` and `node_maintenance_status` report every endpoint they could not
read as `DATA_UNAVAILABLE` (warning). A partial report is therefore never shown as
healthy.

### `node_maintenance_status`
Parameter `node` (optional). Reads `/nodes/{node}/apt/versions` and
`/nodes/{node}/certificates/info`; both need only `Sys.Audit`.

| Code | Severity | Meaning |
|---|---|---|
| `NODE_UPDATES_PENDING` | warning if kernel, QEMU, ZFS or pve-manager are among them, else info | Newer Proxmox packages are available |
| `NODE_REBOOT_REQUIRED` | warning | A newer kernel is installed than the one running |
| `CERTIFICATE_EXPIRING` | warning (≤ 30 days) / critical (≤ 7 days) | Node certificate expires soon |
| `CERTIFICATE_EXPIRED` | critical | Node certificate has expired |

Updates cover the Proxmox package set (kernel, QEMU, ZFS, `pve-*`, Ceph client) as of
apt's last list refresh. The full system package list (`/apt/update`) would need
`Sys.Modify`, which a read-only token should not have. Certificates are the node's own
(`pve-ssl.pem`, `pveproxy-ssl.pem`, the cluster CA); certificates of services running
*inside* guests are out of scope.

### `backup_audit`
Parameters `max_age_hours` (default 26, which suits daily schedules; use about 170 for
weekly ones) and `node`.

Verdicts per guest: `ok`, `stale`, `missing` or `unknown`. It lists backup volumes on
every backup-capable storage (shared ones once) and
reproduces the `vzdump` job selection: explicit `vmid` lists, `pool`, `all` minus
`exclude`, and the per-job `node` filter.

| Code | Severity | Meaning |
|---|---|---|
| `BACKUP_MISSING` | critical (running) / warning (stopped) | No backup volume exists |
| `BACKUP_STALE` | warning | Newest backup is older than `max_age_hours` |
| `BACKUP_NOT_SCHEDULED` | warning | No enabled job selects this guest |
| `BACKUP_TASK_FAILED` | warning | A recent `vzdump` task failed |
| `BACKUP_VERIFY_FAILED` | critical | The newest backup failed Proxmox Backup Server verification |
| `BACKUP_NOT_VERIFIED` | info | The newest backup on a PBS storage was never verified |
| `BACKUP_JOB_DISABLED` | info | A job exists but is disabled |
| `BACKUP_STORAGE_UNREADABLE` | warning | Backups on a storage cannot be listed; affected guests become `unknown` instead of `missing` |

Proxmox hides backup volumes from callers without the right privileges and returns an
empty list rather than an error. `backup_audit` therefore checks the token's effective
permissions (`/access/permissions`) before trusting a listing. See
[proxmox-setup.md](proxmox-setup.md#backup-visibility-needed-for-backup_audit).

## Planning (`read_only`)

### `recommend_placement`
Parameters: `cores`, `memory_gib`, `disk_gib`, `storage`, `exclude_nodes`.

A node is eligible if it is online, stays below 85 % memory after placement, stays below
4:1 vCPU over-commit, and has a storage with room for the disk plus 10 % headroom. The
score weights memory headroom (55 %), current CPU load (25 %) and over-commit (20 %).
Every candidate lists the reasons it was accepted or rejected.

### `plan_node_evacuation`
Parameters: `node`, `exclude_targets`, `memory_limit` (default 0.85).

For each guest it determines the migration mode:

| Mode | When |
|---|---|
| `live` | Running VM, all disks on shared storage |
| `live-with-local-disks` | Running VM with local disks (copied over the network) |
| `restart` | Running container |
| `offline` | Stopped guest |
| `ha-managed` | Guest under HA; it moves when the node enters maintenance mode |

These are **blockers**: PCI/USB passthrough, container device passthrough, bind mounts,
locks, an HA guest with local disks, missing local storage on every target, insufficient
memory headroom, and an unreadable configuration. Targets are packed first-fit-decreasing
against each guest's *configured* memory. When the plan is feasible, `steps` contains the
command sequence.

## Operations

All write tools are two-phase. They also respect the server's write policy
(`PROXMOX_WRITE_POOLS`, `PROXMOX_WRITE_VMIDS`, `PROXMOX_MAX_WRITES_PER_HOUR`, see
[configuration](configuration.md)). Call once without `confirmation_token` to receive a
preview, then call again with the identical arguments plus the token. The result
contains the Proxmox task `task_upid`, so you can follow it with `get_task_status`.

| Tool | Tier | Parameters | Pre-checks |
|---|---|---|---|
| `guest_power` | operator (`stop`: admin) | `vmid`, `action` | Live state from the node (not the few-seconds-stale resource cache); rejects no-ops, paused VMs and locked guests; warns about HA and missing guest agents |
| `create_snapshot` | operator | `vmid`, `name`, `description`, `include_ram` | Name rules, duplicate names, locks, RAM state only for VMs, warns about long snapshot chains |
| `migrate_guest` | admin | `vmid`, `target_node`, `with_local_disks` | Target is online, guest has no passthrough or bind mount and is not locked, local disks require `with_local_disks` |

## Prompts

| Prompt | Arguments | Purpose |
|---|---|---|
| `triage_cluster` | – | Full read-only health triage with prioritised next steps |
| `prepare_maintenance` | `node` | Evacuation analysis and runbook; at the admin tier it offers guided execution |
