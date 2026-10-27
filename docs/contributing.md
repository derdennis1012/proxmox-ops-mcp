# Contributing

Bug reports, fixes and new diagnostics are welcome. The full guide lives in the
repository: [CONTRIBUTING.md](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/CONTRIBUTING.md).

The short version:

- Open an issue first for anything bigger than a small fix, describing the operational
  problem you want solved.
- `make install` sets up the environment and `make check` runs ruff, mypy in strict mode
  and the tests.
- Fixtures should come from real API responses (`pvesh get … --output-format json`),
  anonymised. `tests/fixtures/pve9/` has examples recorded on Proxmox VE 9.2.
- `tests/live/` runs read-only checks against your own cluster:
  `PROXMOX_LIVE_ENV_FILE=~/lab.env uv run pytest tests/live`.
- Security problems go to a
  [private advisory](https://github.com/derdennis1012/proxmox-ops-mcp/security/advisories/new),
  not an issue.
