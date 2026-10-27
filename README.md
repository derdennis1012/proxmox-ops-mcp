<img src="docs/assets/logo.svg" alt="" width="64" height="64" align="right">

# proxmox-ops-mcp

<!-- mcp-name: io.github.derdennis1012/proxmox-ops-mcp -->

An [MCP](https://modelcontextprotocol.io) server for Proxmox VE, built for operations
work: finding out what is wrong with a cluster, whether the backups are actually there,
and whether a node can be taken down for maintenance. Write access exists, but it is off
by default and every change needs an explicit second call.

[![CI](https://github.com/derdennis1012/proxmox-ops-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/derdennis1012/proxmox-ops-mcp/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/derdennis1012/proxmox-ops-mcp/graph/badge.svg)](https://codecov.io/gh/derdennis1012/proxmox-ops-mcp)
[![PyPI](https://img.shields.io/pypi/v/proxmox-ops-mcp)](https://pypi.org/project/proxmox-ops-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## Why another Proxmox MCP server

The existing ones mostly expose the API endpoint by endpoint. That is fine for "start
VM 104", but questions like *"which guests have no backup from last night?"* then need
a dozen calls, and the model has to know Proxmox well enough to combine them correctly.
It usually doesn't. For example, it won't know that a token without `VM.Backup` gets an
empty backup list instead of an error.

This server puts that knowledge into the tools. `backup_audit` reads the backup volumes,
applies the same guest selection as `vzdump` jobs and checks whether the token is even
allowed to see the volumes. The model gets a list of findings back, each with a stable
code and a recommendation.

## Status

Version 0.1, usable but young. What has been tested where:

| | |
|---|---|
| Proxmox VE 9.2, single node, ZFS, two backup storages | all read tools, all write tools (on a throwaway VM in a dedicated pool) |
| Multi-node clusters, HA, Ceph | unit and integration tests with realistic payloads only |
| Proxmox VE 8, Proxmox Backup Server storages | not tested live yet; reports welcome |

The live tests found three things the API documentation doesn't mention, and the server
handles each of them:
- PVE 9 reports `cpu` and `wait` as 0 in the node status call.
- Backup listings come back empty, without an error, when the token lacks privileges.
- `/cluster/resources` lags a few seconds behind the real guest state.

## Install

```bash
uvx proxmox-ops-mcp --version          # run without installing (uv)
pipx install proxmox-ops-mcp           # or as a CLI tool
docker pull ghcr.io/derdennis1012/proxmox-ops-mcp
```

Python 3.11 or newer. From source: `git clone … && uv sync && uv run proxmox-ops-mcp`.

## Setup

**1. Create an API token.** Read-only to start with:

```bash
pveum user add mcp@pve
pveum acl modify / --users mcp@pve --roles PVEAuditor
pveum user token add mcp@pve ops --privsep 1          # prints the secret once
pveum acl modify / --tokens 'mcp@pve!ops' --roles PVEAuditor
```

With only this role, `backup_audit` cannot see backups. Higher tiers need more
privileges, and Proxmox's ACL rules have a few traps. Both are covered in
[docs/proxmox-setup.md](docs/proxmox-setup.md).

**2. Check the connection.**

```console
$ export PROXMOX_HOST=pve1.example.com PROXMOX_TOKEN_ID='mcp@pve!ops' PROXMOX_TOKEN_SECRET=…
$ uvx proxmox-ops-mcp check
OK    connected to pve1.example.com:8006 as mcp@pve!ops
      Proxmox VE 9.2.5 (release 9.2)
      3 node(s) visible, access tier: read_only
```

If this fails with `SSLError`, the node certificate probably does not cover the address
you use. That is common when connecting over a VPN. `check` prints the command to pin
the certificate instead.

**3. Add it to your client.** Claude Code:

```bash
claude mcp add proxmox-ops \
  -e PROXMOX_HOST=pve1.example.com \
  -e PROXMOX_TOKEN_ID='mcp@pve!ops' \
  -e PROXMOX_TOKEN_SECRET=… \
  -- uvx proxmox-ops-mcp
```

Claude Desktop, Cursor, VS Code, OpenAI Codex, Gemini CLI, Hermes Agent, OpenClaw and
Docker: see [docs/clients.md](docs/clients.md). It also covers what to watch out for when
an autonomous agent uses the server.

## Tools

| Tool | Tier | |
|---|---|---|
| `cluster_health` | read_only | Quorum, offline nodes, CPU/memory, storage fill levels, HA errors |
| `ceph_health` | read_only | Ceph health checks with hints, OSD and PG state, monitor quorum, capacity |
| `compare_nodes` | read_only | I/O wait, load, swap, memory/I/O pressure (PSI), version drift between nodes |
| `backup_audit` | read_only | Missing, stale and unscheduled backups; failed jobs; PBS verification |
| `recent_task_failures` | read_only | Failed tasks in a time window |
| `storage_health` | read_only | ZFS pool state, device and data errors, scrub age, SMART, SSD wear |
| `node_maintenance_status` | read_only | Pending Proxmox updates, needed reboots, expiring node certificates |
| `recommend_placement` | read_only | Where a new guest fits, with a score and the reason for every node |
| `plan_node_evacuation` | read_only | Can a node be drained, what blocks it, and the commands to do it |
| `list_nodes`, `list_guests`, `get_guest`, `get_task_status` | read_only | Inventory, guest config (secrets redacted), task logs |
| `guest_power` | operator | start, shutdown, reboot; `stop` needs admin |
| `create_snapshot` | operator | Snapshot before a risky change |
| `migrate_guest` | admin | Live migration for VMs, restart migration for containers |

Parameters and all finding codes: [docs/tools.md](docs/tools.md).

Example, `plan_node_evacuation(node="pve1")`, shortened:

```jsonc
{
  "feasible": true,
  "summary": "pve1 can be drained: 3 guests to 2 target nodes, 1 container(s) with a brief restart.",
  "guests": [
    { "vmid": 100, "name": "db01",  "mode": "ha-managed",            "target": "pve3" },
    { "vmid": 101, "name": "web01", "mode": "live-with-local-disks", "target": "pve2",
      "warnings": ["local disks will be copied: scsi0@local-lvm"] },
    { "vmid": 200, "name": "dns01", "mode": "restart",               "target": "pve3" }
  ],
  "steps": [
    "ceph osd set noout   # prevent rebalancing while the node is down",
    "ha-manager crm-command node-maintenance enable pve1   # HA moves its guests automatically",
    "qm migrate 101 pve2 --online --with-local-disks",
    "pct migrate 200 pve3 --restart",
    "# perform maintenance on pve1, reboot, verify it rejoined the cluster",
    "ha-manager crm-command node-maintenance disable pve1",
    "ceph osd unset noout"
  ]
}
```

## Write access

`PROXMOX_ACCESS_TIER` is `read_only` unless you change it, and tools above the tier are
not registered at all. A write tool works in two steps:

1. Called normally, it checks the request against the live guest state and returns a
   preview of what would happen, plus a confirmation token.
2. Called again with the same arguments and that token, it executes.

The token works once, expires after five minutes and is bound to the exact parameters.
That includes the node, so a guest that migrated in between invalidates it. Writes can
additionally be limited to named pools or VMIDs and capped per hour. Every preview,
execution and rejection is written to an audit log.

The token makes sure that what runs is what was previewed. It does not prove that a
person approved it, because the model sees the token. Keep tool approval switched on in
your MCP client for the write tools. There are no tools that delete anything.

The details, including the HTTP transport defaults, are in
[docs/security-model.md](docs/security-model.md).

## Configuration

Environment variables with the `PROXMOX_` prefix: `HOST`, `TOKEN_ID` and `TOKEN_SECRET`
are required. Optional are `ACCESS_TIER`, `CA_BUNDLE` or `TLS_FINGERPRINT`, `AUDIT_LOG`
and a few more. A `.env` file is only read when passed with `--env-file`. The full list
is in [docs/configuration.md](docs/configuration.md).

## Development

```bash
make install   # uv sync + pre-commit
make check     # ruff, mypy --strict, pytest
```

The diagnostic logic in `src/proxmox_ops_mcp/analysis/` is plain functions over API
payloads and is tested without a cluster. `tests/live/` runs read-only checks against a
real one if you point it at an env file. [docs/architecture.md](docs/architecture.md)
explains the layout, and [CONTRIBUTING.md](CONTRIBUTING.md) explains how to contribute.

Documentation site: <https://derdennis1012.github.io/proxmox-ops-mcp/>. Planned next:
direct Proxmox Backup Server checks (garbage collection, prune and verify jobs),
replication jobs, guest snapshot hygiene.

## License

MIT. Proxmox is a trademark of Proxmox Server Solutions GmbH; this project is not
affiliated with it.
