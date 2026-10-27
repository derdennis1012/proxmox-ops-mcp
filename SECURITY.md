# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | yes |

## Reporting a vulnerability

**Please do not open a public issue.** Report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/derdennis1012/proxmox-ops-mcp/security/advisories/new).

Please include:

- the affected version and access tier,
- steps to reproduce, or a proof of concept,
- the impact you expect, for example "read_only token can trigger a guest stop".

You can expect an acknowledgement within 72 hours and an assessment within 7 days. Fixes
for confirmed issues are released as soon as practical. With your consent, you will be
credited in the advisory.

## Verifying releases

Release artefacts are built in GitHub Actions and carry signed build provenance:

```bash
gh attestation verify <file.whl> --repo derdennis1012/proxmox-ops-mcp
gh attestation verify oci://ghcr.io/derdennis1012/proxmox-ops-mcp:<version> --repo derdennis1012/proxmox-ops-mcp
```

## Scope

In scope, among others:

- bypassing the access tier or the two-phase confirmation,
- path or parameter injection into Proxmox API requests,
- leaking secrets through logs, errors or tool output,
- weaknesses in the HTTP transport defaults.

The [security model](docs/security-model.md) describes the intended guarantees and
what is out of scope.
