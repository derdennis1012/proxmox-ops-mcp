## What does this change?

<!-- One or two sentences. Link the issue it resolves, e.g. "Closes #12". -->

## Why?

## How was it tested?

<!-- Unit tests, integration tests, and - for API behaviour - which Proxmox VE version you checked against. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check .` passes
- [ ] `uv run mypy` passes
- [ ] `uv run pytest` passes and new behaviour is covered by tests
- [ ] New write operations are two-phase, tier-gated and audit-logged
- [ ] `CHANGELOG.md` updated under *Unreleased*
- [ ] Docs updated (`README.md` / `docs/`) if user-facing behaviour changed
