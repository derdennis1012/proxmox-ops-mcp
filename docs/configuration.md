# Configuration

## Environment variables

All settings use the `PROXMOX_` prefix.

| Variable | Default | Description |
|---|---|---|
| `PROXMOX_HOST` | *required* | Any cluster node. Accepts `pve1`, `pve1:8006` or `https://pve1:8006/`. |
| `PROXMOX_TOKEN_ID` | *required* | `user@realm!tokenname`, e.g. `mcp@pve!ops` |
| `PROXMOX_TOKEN_SECRET` | *required* | The token secret printed when the token was created |
| `PROXMOX_PORT` | `8006` | API port |
| `PROXMOX_VERIFY_SSL` | `true` | Verify the server certificate |
| `PROXMOX_CA_BUNDLE` | – | Path to a CA bundle, e.g. the cluster's `pve-root-ca.pem` |
| `PROXMOX_TLS_FINGERPRINT` | – | SHA-256 certificate pin (`AB:CD:…`); replaces CA/hostname checks, see [proxmox-setup.md](proxmox-setup.md#3-tls) |
| `PROXMOX_TIMEOUT` | `30` | Per-request timeout in seconds (1-300) |
| `PROXMOX_ACCESS_TIER` | `read_only` | `read_only`, `operator` or `admin` |
| `PROXMOX_AUDIT_LOG` | stderr | Append-only JSONL audit log file |
| `PROXMOX_CONFIRMATION_TTL` | `300` | Confirmation token lifetime in seconds (30-3600) |
| `PROXMOX_WRITE_POOLS` | – | Comma-separated pools. If set (or `WRITE_VMIDS`), write tools only act on guests in these pools |
| `PROXMOX_WRITE_VMIDS` | – | Comma-separated VMIDs that may be written to, in addition to the pools |
| `PROXMOX_MAX_WRITES_PER_HOUR` | `30` | Executed writes allowed per rolling hour; previews don't count |

### `.env` files

`.env` files are **not** loaded implicitly. MCP clients often start servers with the
current project as the working directory. If `.env` were loaded automatically, a file
committed to some repository could raise the access tier or disable TLS verification.
To opt in, pass the file explicitly:

```bash
proxmox-ops-mcp --env-file ~/.config/proxmox-ops-mcp/env serve
```

Real environment variables always take precedence over the file. A template is in
[`.env.example`](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/.env.example).

## CLI

```text
proxmox-ops-mcp [--env-file PATH] [serve|check]

serve (default)
  --transport {stdio,streamable-http}   default: stdio
  --host HOST                           default: 127.0.0.1 (HTTP only)
  --port PORT                           default: 8000 (HTTP only)
  --allow-remote                        allow binding to a non-loopback address
  --allowed-host HOST[:PORT]            extra Host header to accept (repeatable)
  --log-level LEVEL                     default: WARNING (logs go to stderr)

check
  Verifies configuration and connectivity, prints the PVE version, then exits.
```

## Transports

**stdio** is the default, and the right choice for local clients such as Claude Code,
Claude Desktop and Cursor.

**Streamable HTTP** serves a remote or shared deployment. The server has **no built-in
authentication**. Put it behind a reverse proxy that authenticates users (OAuth2 proxy,
mTLS, VPN), then:

```bash
proxmox-ops-mcp serve --transport streamable-http --host 0.0.0.0 --allow-remote \
  --allowed-host mcp.example.com
```

DNS-rebinding protection is always on. Requests whose `Host` header is not a loopback
address or an `--allowed-host` are rejected.

## Client examples

- [Claude Desktop / Cursor (`examples/claude_desktop_config.json`)](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/examples/claude_desktop_config.json)
- [Project-scoped Claude Code (`examples/.mcp.json`)](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/examples/.mcp.json)
- [Docker Compose for the HTTP transport (`examples/docker-compose.yml`)](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/examples/docker-compose.yml)
