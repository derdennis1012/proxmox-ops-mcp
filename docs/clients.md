---
description: Connect proxmox-ops-mcp to Claude Code, Claude Desktop, Cursor, VS Code, OpenAI Codex, Gemini CLI, Hermes Agent, OpenClaw or any other MCP client.
---

# Connect a client

proxmox-ops-mcp is a standard MCP server that talks over stdio. Every client below
starts it as a child process with the same command and the same environment variables.
Only the place where you write them differs.

## What every client needs

| Variable | Example | |
|---|---|---|
| `PROXMOX_HOST` | `pve1.example.com` | required |
| `PROXMOX_TOKEN_ID` | `mcp@pve!ops` | required |
| `PROXMOX_TOKEN_SECRET` | `xxxxxxxx-…` | required |
| `PROXMOX_TLS_FINGERPRINT` | `AB:CD:…` | if the certificate doesn't cover the host (common with IPs/VPN) |
| `PROXMOX_ACCESS_TIER` | `read_only` | default; see [write access](#autonomous-agents) before changing it |

The command is `uvx proxmox-ops-mcp`. Run `proxmox-ops-mcp check` with the same
variables first; if that passes, any client will work.

!!! tip "Running from a checkout instead of PyPI"
    Replace `uvx proxmox-ops-mcp` with
    `uv --directory /path/to/proxmox-ops-mcp run proxmox-ops-mcp`, i.e. command `uv` and
    args `["--directory", "/path/to/proxmox-ops-mcp", "run", "proxmox-ops-mcp"]`.

!!! warning "GUI apps don't see your shell's PATH"
    Desktop apps on macOS (Claude Desktop, Cursor) start without your shell profile, so
    `uvx` in `~/.local/bin` or Homebrew is often not found ("spawn uvx ENOENT"). Use the
    absolute path from `which uvx` as the command.

## Claude Code

```bash
claude mcp add proxmox-ops --scope user \
  -e PROXMOX_HOST=pve1.example.com \
  -e PROXMOX_TOKEN_ID='mcp@pve!ops' \
  -e PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
  -- uvx proxmox-ops-mcp
```

`--scope user` makes it available in every project; `--scope project` writes a
`.mcp.json` you can commit. A shared file must not contain the secret: reference it as
`${PROXMOX_TOKEN_SECRET}` and set the variable in your shell
([example](https://github.com/derdennis1012/proxmox-ops-mcp/blob/main/examples/.mcp.json)).
Check with `claude mcp list` or `/mcp` inside a session.

## Claude Desktop

Settings → Developer → Edit Config opens `claude_desktop_config.json`
(macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "proxmox-ops": {
      "command": "/Users/you/.local/bin/uvx",
      "args": ["proxmox-ops-mcp"],
      "env": {
        "PROXMOX_HOST": "pve1.example.com",
        "PROXMOX_TOKEN_ID": "mcp@pve!ops",
        "PROXMOX_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      }
    }
  }
}
```

Restart Claude Desktop afterwards. Besides the tools, the server offers the prompts
`triage_cluster` and `prepare_maintenance`.

## Cursor

`~/.cursor/mcp.json` (all projects) or `.cursor/mcp.json` (one project), same structure
as for Claude Desktop. The server then shows up in Cursor's MCP settings.

## VS Code (GitHub Copilot agent mode)

`.vscode/mcp.json` in the workspace. VS Code can prompt for the secret instead of storing
it:

```json
{
  "inputs": [
    { "id": "pve-secret", "type": "promptString", "password": true,
      "description": "Proxmox API token secret" }
  ],
  "servers": {
    "proxmox-ops": {
      "command": "uvx",
      "args": ["proxmox-ops-mcp"],
      "env": {
        "PROXMOX_HOST": "pve1.example.com",
        "PROXMOX_TOKEN_ID": "mcp@pve!ops",
        "PROXMOX_TOKEN_SECRET": "${input:pve-secret}"
      }
    }
  }
}
```

## OpenAI Codex (CLI and IDE extension)

```bash
codex mcp add proxmox-ops \
  --env PROXMOX_HOST=pve1.example.com \
  --env PROXMOX_TOKEN_ID='mcp@pve!ops' \
  --env PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
  -- uvx proxmox-ops-mcp
```

or directly in `~/.codex/config.toml`:

```toml
[mcp_servers.proxmox-ops]
command = "uvx"
args = ["proxmox-ops-mcp"]

[mcp_servers.proxmox-ops.env]
PROXMOX_HOST = "pve1.example.com"
PROXMOX_TOKEN_ID = "mcp@pve!ops"
PROXMOX_TOKEN_SECRET = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

The key is `mcp_servers` (snake case), not `mcpServers`. The CLI and the IDE extension
share this file. Check with `codex mcp list`.

## Gemini CLI

`~/.gemini/settings.json` (or `.gemini/settings.json` in a project). `env` values may
reference shell variables, which keeps the secret out of the file:

```json
{
  "mcpServers": {
    "proxmox-ops": {
      "command": "uvx",
      "args": ["proxmox-ops-mcp"],
      "env": {
        "PROXMOX_HOST": "pve1.example.com",
        "PROXMOX_TOKEN_ID": "mcp@pve!ops",
        "PROXMOX_TOKEN_SECRET": "$PROXMOX_TOKEN_SECRET"
      }
    }
  }
}
```

Check with `/mcp` inside a session.

## Hermes Agent (Nous Research)

`~/.hermes/config.yaml`. Hermes does not pass your shell environment to MCP servers, so
every variable must be listed under `env`:

```yaml
mcp_servers:
  proxmox:
    command: "uvx"
    args: ["proxmox-ops-mcp"]
    env:
      PROXMOX_HOST: "pve1.example.com"
      PROXMOX_TOKEN_ID: "mcp@pve!ops"
      PROXMOX_TOKEN_SECRET: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      PROXMOX_ACCESS_TIER: "read_only"
    tools:
      include: [cluster_health, ceph_health, compare_nodes, storage_health,
                node_maintenance_status, backup_audit, recent_task_failures,
                list_nodes, list_guests, get_guest, plan_node_evacuation]
```

Run `/reload-mcp` in a session; a running gateway picks up changes by itself. Tools
appear as `mcp_proxmox_<tool>`. The `tools.include` list is optional; it keeps the tool
list small and makes the allowed surface explicit.

## OpenClaw

OpenClaw keeps outbound MCP servers under `mcp.servers` and manages them with
`openclaw mcp`. A stdio entry takes `command`, `args`, `env` and `cwd`:

```bash
openclaw mcp set proxmox '{
  "command": "uvx",
  "args": ["proxmox-ops-mcp"],
  "env": {
    "PROXMOX_HOST": "pve1.example.com",
    "PROXMOX_TOKEN_ID": "mcp@pve!ops",
    "PROXMOX_TOKEN_SECRET": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  }
}'
openclaw mcp doctor proxmox --probe
```

`doctor --probe` starts the server once and lists its tools; if that works, the agent
can use them.

## Docker instead of uvx

Any client above can run the container instead. Keep the variables in an env file:

```json
{
  "command": "docker",
  "args": ["run", "--rm", "-i", "--env-file", "/path/to/proxmox.env",
           "ghcr.io/derdennis1012/proxmox-ops-mcp"]
}
```

`-i` is required (stdio); `--rm` removes the container when the client closes it.

## Remote clients (HTTP)

For clients that cannot start local processes, run the streamable HTTP transport behind
an authenticating reverse proxy and point the client at `https://<host>/mcp`. The server
has no authentication of its own; read [Transports](configuration.md#transports) first.

## Autonomous agents

Hermes, OpenClaw and scheduled Codex or Claude Code runs often act without a person
watching each tool call. The preview/confirm protocol then doesn't add a human, because
the agent can confirm its own preview. For such setups:

- Keep `PROXMOX_ACCESS_TIER=read_only`. Write tools are then not even registered.
- If an agent must act, give it a token whose Proxmox ACL covers only a dedicated pool,
  and set `PROXMOX_WRITE_POOLS` to the same pool and `PROXMOX_MAX_WRITES_PER_HOUR` to a
  small number.
- Set `PROXMOX_AUDIT_LOG` to a file and look at it.

A good use for an always-on agent is a daily report: run the `triage_cluster` prompt (or
`cluster_health`, `storage_health`, `node_maintenance_status` and `backup_audit`) every
morning and send the result to chat only if there are warnings.
