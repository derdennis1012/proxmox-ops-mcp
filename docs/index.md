---
title: MCP server for Proxmox VE operations
description: >-
  proxmox-ops-mcp lets AI assistants such as Claude diagnose Proxmox VE clusters,
  audit backups, check ZFS, disks, updates and certificates, and plan node maintenance,
  with write access off by default.
---

# proxmox-ops-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant look after a
Proxmox VE cluster. It works with Claude Code, Claude Desktop, Cursor, VS Code, OpenAI
Codex, Gemini CLI, Hermes Agent, OpenClaw and any other MCP client. It answers the
questions an administrator actually asks, and does the API work needed to answer them
correctly:

- *Is anything wrong with the cluster right now?*
- *Which guests have no backup from last night, and is the token even allowed to see them?*
- *Are the ZFS pools and disks healthy? When was the last scrub?*
- *Which nodes need updates or a reboot? Are certificates about to expire?*
- *Can I take `pve2` down tonight without breaking HA, and which commands do I run?*
- *Where should a new VM with 8 cores and 32 GB go?*

Every answer comes back as a structured report. Findings carry a stable code such as
`ZFS_SCRUB_OVERDUE` or `BACKUP_VERIFY_FAILED`, a severity and a concrete recommendation.

```console
$ uvx proxmox-ops-mcp check
OK    connected to pve1.example.com:8006 as mcp@pve!ops
      Proxmox VE 9.2.5 (release 9.2)
      3 node(s) visible, access tier: read_only
```

[Get started](getting-started.md){ .md-button .md-button--primary }
[See all tools](tools.md){ .md-button }

## What makes it different

**Proxmox knowledge built into the tools.** Proxmox has behaviour the API reference
doesn't mention, and this server handles it:

- A token without `VM.Backup` gets an empty backup list instead of an error.
  `backup_audit` checks the token's permissions before it trusts that list.
- Proxmox VE 9 reports CPU and I/O wait as 0 in the node status call. The server reads
  them from the RRD statistics instead.
- `/cluster/resources` lags a few seconds behind the real guest state. Before any change,
  the server reads the state from the node itself.

**Read-only unless you decide otherwise.** Write tools only exist if you enable a higher
access tier. Each change takes two calls: one for a preview and one, with a confirmation
token, to execute. Writes can be limited to named pools or VMIDs and capped per hour. An
audit log records every attempt. The server has no tool that deletes anything.

**Tested against a real host.** Every tool was run against Proxmox VE 9.2. The write
operations ran on a throwaway VM in an isolated pool. Recorded API responses from that
host are part of the test suite. The [status table](getting-started.md#compatibility)
lists what has and hasn't been verified yet.

## Tools at a glance

| Area | Tools |
|---|---|
| Health | `cluster_health`, `ceph_health`, `compare_nodes`, `recent_task_failures` |
| Hardware and upkeep | `storage_health` (ZFS, SMART, SSD wear), `node_maintenance_status` (updates, reboots, certificates) |
| Backups | `backup_audit` (existence, age, job coverage, PBS verification) |
| Planning | `recommend_placement`, `plan_node_evacuation` |
| Inventory | `list_nodes`, `list_guests`, `get_guest`, `get_task_status` |
| Changes (opt-in) | `guest_power`, `create_snapshot`, `migrate_guest` |
