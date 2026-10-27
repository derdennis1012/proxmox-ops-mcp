# Contributing

Bug reports, fixes and new diagnostics are welcome. Since this server can change things
on production clusters, reviews here focus on failure modes. Expect questions like
"what happens if the API returns X?".

## Before you start

For anything bigger than a small fix, open an issue first. Describe the operational
problem ("after a failed backup night I check X, Y and Z by hand"), not only the code you
have in mind.

New diagnostics are the easiest to get merged. New write operations need a good reason:
- why someone would let an assistant do it,
- which tier it belongs in,
- how a mistake gets undone.

Delete operations won't be added.

Don't post real hostnames, IPs, tokens or customer data in issues, fixtures or logs.

## Setup

You need [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer.

```bash
git clone https://github.com/derdennis1012/proxmox-ops-mcp
cd proxmox-ops-mcp
make install   # uv sync + pre-commit hooks
make check     # ruff, mypy --strict, pytest
```

## Tests

`tests/unit/` covers the analysis functions with JSON fixtures. `tests/integration/`
drives the real MCP server against an in-memory fake of the API (`tests/fakes.py`). Both
run in CI.

When you add a fixture, take the shape from a real response
(`pvesh get … --output-format json`) and anonymise it. Several bugs in this project came
from fixtures that looked plausible but didn't match what Proxmox actually returns.

`tests/live/` runs read-only checks against a real cluster and is skipped by default:

```bash
PROXMOX_LIVE_ENV_FILE=~/lab.env uv run pytest tests/live -v
```

If you change write operations, try them on a lab cluster with a token that can only
write to a dedicated pool (see [docs/proxmox-setup.md](docs/proxmox-setup.md)). Never
use production guests for this. Mention in the PR which Proxmox VE version you tested
against.

## Pull requests

CI has to pass: ruff, mypy in strict mode, tests with at least 90 % coverage, and CodeQL.

Keep `analysis/` free of I/O. Finding codes are part of the public interface, so
renaming one counts as a breaking change. Document new tools and codes in
`docs/tools.md` and add a line to `CHANGELOG.md` under *Unreleased*.

PRs are squash-merged, so use a [Conventional Commits](https://www.conventionalcommits.org/)
title, for example `fix(backups): honour per-job node filter`.

## Releases

This section is for maintainers.

1. Bump the version in `pyproject.toml` and move the changelog entries under it.
2. Run `make check` and the live tests against a lab cluster.
3. Push a `vX.Y.Z` tag. The release workflow then publishes to PyPI, pushes the image to
   GHCR and creates the GitHub release.

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
