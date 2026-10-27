# Security model

## Threats considered

| Threat | Example |
|---|---|
| Over-reaching model | The assistant "helpfully" stops a VM while debugging |
| Prompt injection | A guest description or task log contains instructions aimed at the model |
| Parameter drift | A model previews action A and then executes a slightly different action B |
| Path injection | A crafted node name or UPID rewrites the API path (`../`, `?`, `%2F`) |
| Configuration tampering | A `.env` file in a cloned repository silently raises the access tier |
| Secret leakage | The token secret or guest passwords end up in logs, errors or tool output |
| Network exposure | The HTTP transport is reachable by other hosts or by a malicious web page (DNS rebinding) |

## Out of scope

- A compromised MCP client or host. Anything that can read the process environment can
  read the token.
- A model that has been explicitly given the `admin` tier and per-tool approval switched
  off. That is the operator's decision.

## Layers

### 1. Least privilege, enforced twice
`PROXMOX_ACCESS_TIER` decides which tools are registered. Tools above the tier are never
registered, so the model doesn't see them at all. The
token's Proxmox ACLs ([proxmox-setup.md](proxmox-setup.md)) enforce the same limits on
the API side. A bug in either layer is therefore not enough to escalate.

### 2. Two-phase writes with bound confirmation tokens
A preview returns a token in the form `nonce.HMAC-SHA256(key, nonce ‖ action ‖ params)`.

- Any change to the action or a parameter fails verification. The node is
  part of the parameters and is always resolved on the server. If a guest moves between
  preview and execution, the token no longer matches.
- A token is consumed when it is redeemed, even on a mismatch, so it
  cannot be retried against other parameters.
- Tokens expire after 5 minutes by default (`PROXMOX_CONFIRMATION_TTL`).
- The key is random per process, so a restart invalidates all tokens.

This guarantees integrity: what runs is what was shown. It does **not** guarantee
approval, because the model holds the token. Human approval comes from the MCP client's
per-tool confirmation. Write tools carry `readOnlyHint: false`, and those that can
interrupt service carry `destructiveHint: true`, so clients can prompt for them.

### 3. Write policy inside the server
`PROXMOX_WRITE_POOLS` / `PROXMOX_WRITE_VMIDS` confine every write tool to an allowlist of
guests, checked against the guest's pool as Proxmox reports it. This holds even if the
token was granted more than intended. `PROXMOX_MAX_WRITES_PER_HOUR` caps executed writes,
so a model stuck in a loop cannot work through a whole cluster. The counter lives in the
server process: with stdio, every client session starts its own process and its own
budget, and a restart resets it. Rejections are audit-logged like everything else.

The scope applies to the guest being changed. `migrate_guest` can move an in-scope guest
to any online node; restrict target nodes through the token's Proxmox ACLs if that
matters in your environment.

### 4. Validation at the boundary
Every value that ends up in an API path is checked against a strict allowlist: node
names, VMIDs, storage IDs, snapshot names, UPIDs and guest types. The HTTP client then
validates every path segment a second time. This covers values the API itself returned,
so traversal, query strings, fragments and percent-encoding can never reach `proxmoxer`.

### 5. Audit trail
Each preview, execution, rejection and failure is written as one JSON line, with the
timestamp, action, tier, parameters and result (the task UPID or the error). Keys that
look sensitive are redacted. Unexpected exceptions are recorded by type only. The log
goes to `PROXMOX_AUDIT_LOG`, or to stderr. Nothing is written to stdout, which carries
the MCP protocol.

### 6. Secret hygiene
- The token secret is held as a `SecretStr` and never rendered in `repr` or logs.
- Errors from the HTTP layer are reduced to the status code and reason, or the exception
  type, because some transport errors echo request headers.
- `proxmoxer`'s own logger is capped at `WARNING`, since it logs request and response
  bodies at lower levels.
- `get_guest` redacts configuration keys such as `cipassword`.

### 7. Transport hardening
- The HTTP transport binds to `127.0.0.1` by default. Any other address needs
  `--allow-remote`.
- DNS-rebinding protection (Host and Origin checks) is always on, not only for the
  literal loopback strings.
- `.env` files are loaded only through an explicit `--env-file`.
- TLS verification is on by default. Self-signed clusters are handled with
  `PROXMOX_CA_BUNDLE`, not by disabling verification.

### 8. Deliberately missing features
There are no tools to delete guests, disks, snapshots or backups, and none to change
cluster, network, firewall or storage configuration. Those operations are hard to undo,
and an assistant does not need them to be useful.

## Prompt injection

Free-text fields such as guest descriptions, notes and task logs come from your cluster,
and the model sees them. They could contain instructions. This is the main reason write
tools are tier-gated and should be paired with per-tool approval. Keep the read-only tier
for assistants that summarise content from systems other people control.

## Supply chain

- Every GitHub Action is pinned to a full commit SHA; Dependabot proposes updates.
- CI audits the runtime dependencies for known vulnerabilities (`pip-audit`) and lints the
  workflows themselves (`zizmor`). CodeQL and OpenSSF Scorecard run on a schedule.
- Releases are built in GitHub Actions only. PyPI uploads use trusted publishing, so no
  API token exists that could leak. Wheels and the container image carry signed build
  provenance; the image also ships an SBOM.

Verify a download before you run it:

```bash
gh attestation verify proxmox_ops_mcp-0.1.0-py3-none-any.whl --repo derdennis1012/proxmox-ops-mcp
gh attestation verify oci://ghcr.io/derdennis1012/proxmox-ops-mcp:0.1.0 --repo derdennis1012/proxmox-ops-mcp
```
