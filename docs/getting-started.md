---
description: Install proxmox-ops-mcp, create a least-privilege Proxmox API token and connect it to Claude Code, Claude Desktop, Cursor or VS Code.
---

# Getting started

## 1. Install

=== "uvx (recommended)"

    ```bash
    uvx proxmox-ops-mcp --version
    ```

    [uv](https://docs.astral.sh/uv/) downloads and runs the latest release in an isolated
    environment. Nothing is installed globally.

=== "pipx"

    ```bash
    pipx install proxmox-ops-mcp
    ```

=== "Docker"

    ```bash
    docker pull ghcr.io/derdennis1012/proxmox-ops-mcp
    ```

    Multi-arch (amd64, arm64), runs as an unprivileged user, ships with SBOM and
    provenance attestations.

=== "From source"

    ```bash
    git clone https://github.com/derdennis1012/proxmox-ops-mcp
    cd proxmox-ops-mcp && uv sync
    uv run proxmox-ops-mcp --version
    ```

Python 3.11 or newer is required for the non-Docker options.

## 2. Create an API token

On any node, as root:

```bash
pveum user add mcp@pve
pveum acl modify / --users mcp@pve --roles PVEAuditor
pveum user token add mcp@pve ops --privsep 1          # prints the secret once
pveum acl modify / --tokens 'mcp@pve!ops' --roles PVEAuditor
```

This is enough for every tool except `backup_audit`, because Proxmox hides backups from
pure auditors. [Proxmox setup](proxmox-setup.md) covers backup visibility, write tiers
and two ACL rules that commonly trip people up.

## 3. Check the connection

```bash
export PROXMOX_HOST=pve1.example.com
export PROXMOX_TOKEN_ID='mcp@pve!ops'
export PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
uvx proxmox-ops-mcp check
```

If you get an `SSLError`, the node certificate does not cover the address you are
using. That is normal with IPs or VPN addresses. `check` prints the `openssl` command
for a certificate pin; put the result into `PROXMOX_TLS_FINGERPRINT`.

## 4. Connect your MCP client

=== "Claude Code"

    ```bash
    claude mcp add proxmox-ops \
      -e PROXMOX_HOST=pve1.example.com \
      -e PROXMOX_TOKEN_ID='mcp@pve!ops' \
      -e PROXMOX_TOKEN_SECRET=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
      -- uvx proxmox-ops-mcp
    ```

=== "Claude Desktop"

    In `claude_desktop_config.json`:

    ```json
    {
      "mcpServers": {
        "proxmox-ops": {
          "command": "uvx",
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

=== "Cursor"

    In `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a project), the same block as for
    Claude Desktop.

=== "VS Code"

    In `.vscode/mcp.json`:

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

=== "Docker"

    ```json
    {
      "mcpServers": {
        "proxmox-ops": {
          "command": "docker",
          "args": ["run", "--rm", "-i", "--env-file", "/path/to/proxmox.env",
                   "ghcr.io/derdennis1012/proxmox-ops-mcp"]
        }
      }
    }
    ```

## 5. Ask something

Good first prompts:

- "Triage my Proxmox cluster." The server also ships this as the `triage_cluster` prompt.
- "Which guests have no backup from the last 24 hours?"
- "Is the storage on all nodes healthy?"
- "Which nodes need updates or a reboot?"
- "Can I put pve2 into maintenance tonight? Give me the runbook." Also available as the
  `prepare_maintenance` prompt.

## Compatibility

| Environment | Status |
|---|---|
| Proxmox VE 9.2, single node, ZFS, local and CIFS backup storages | All tools verified live, write tools on an isolated test VM |
| Multi-node clusters, HA, Ceph | Covered by tests with realistic payloads, not yet verified live |
| Proxmox Backup Server storages | Verification state is read from the PVE storage listing; not yet verified live |
| Proxmox VE 8 | Expected to work, not yet verified |

If you run it somewhere new, the [live test suite](contributing.md) takes a minute.
Report the result in an issue, whether it passes or fails.
