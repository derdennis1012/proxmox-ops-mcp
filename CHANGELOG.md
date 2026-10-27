# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Finding
codes are part of the public API.

## [Unreleased]

## [0.1.0] - 2026-10-27

### Added

- Diagnostics: `cluster_health`, `ceph_health`, `compare_nodes`, `recent_task_failures`.
- Backup audit with direct volume inspection and `vzdump` job-selection semantics.
- Planning: `recommend_placement` and `plan_node_evacuation` with runbook generation.
- Inventory: `list_nodes`, `list_guests`, `get_guest` (secrets redacted), `get_task_status`.
- Guarded operations: `guest_power`, `create_snapshot`, `migrate_guest`.
- Access tiers (`read_only`, `operator`, `admin`), two-phase confirmation tokens and a
  JSONL audit log.
- `triage_cluster` and `prepare_maintenance` prompts.
- `proxmox-ops-mcp check` connectivity test; stdio and streamable HTTP transports.
- Container image, CI (lint, strict typing, tests on Python 3.11-3.14), CodeQL and a
  release pipeline with PyPI trusted publishing.
- `storage_health`: ZFS pool state, device and data errors, fill level, scrub age, SMART
  health, SSD wear and bad sectors.
- `node_maintenance_status`: pending Proxmox updates, needed reboots, node certificate
  expiry.
- `backup_audit` reports Proxmox Backup Server verification state.
- Server-side write policy: `PROXMOX_WRITE_POOLS`, `PROXMOX_WRITE_VMIDS`,
  `PROXMOX_MAX_WRITES_PER_HOUR`.
- Documentation site (MkDocs Material, GitHub Pages) and an MCP Registry entry
  (`server.json`), published by the release workflow.
- Supply chain: SHA-pinned actions, `pip-audit`, `zizmor`, OpenSSF Scorecard, build
  provenance for wheels and image.
- `PROXMOX_TLS_FINGERPRINT` certificate pinning for nodes reached via VPN or IP.
- Opt-in live test suite (`tests/live`, read-only) for real clusters.

### Verified against Proxmox VE 9.2

- `compare_nodes` reads CPU and I/O wait from `rrddata` (the status call reports 0 on
  PVE 9) and uses pressure-stall information to rate swap and I/O findings.
- `backup_audit` checks backup visibility privileges first, because Proxmox returns an
  empty listing instead of an error; affected guests are `unknown`, never `missing`.
- Write preconditions use the node's live guest state instead of the resource cache,
  and reject locked or paused guests.
- `ceph_health` explains clearly when Ceph is not configured.

[Unreleased]: https://github.com/derdennis1012/proxmox-ops-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/derdennis1012/proxmox-ops-mcp/releases/tag/v0.1.0
