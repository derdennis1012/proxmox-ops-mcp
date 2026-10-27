# Support

Questions go to [Discussions](https://github.com/derdennis1012/proxmox-ops-mcp/discussions),
bugs to the [issue tracker](https://github.com/derdennis1012/proxmox-ops-mcp/issues). For a
bug, include the output of `proxmox-ops-mcp --version`, your Proxmox VE version and the
tool call, with anything private removed. Report security problems privately as
described in [SECURITY.md](SECURITY.md).

Common problems:

- `check` fails with `SSLError`: the certificate doesn't cover the address you connect
  to. See the TLS section in [docs/proxmox-setup.md](docs/proxmox-setup.md#3-tls).
- `backup_audit` reports `BACKUP_STORAGE_UNREADABLE`: the token isn't allowed to see
  backups. That is different from the backups not existing. The same guide explains
  which privileges are needed.
- A tool says the token lacks permission: on a node, run
  `pvesh get /access/permissions --userid '<token>' --path /vms/<vmid>`.

This is a spare-time project without commercial support.
