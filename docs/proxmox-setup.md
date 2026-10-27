# Proxmox setup

proxmox-ops-mcp authenticates with **API tokens only**. A token can be scoped tightly and
revoked on its own, and it never needs the password of a real account.

Everything on this page was verified against Proxmox VE 9.2.

## How Proxmox evaluates token permissions

1. **Privilege separation.** Create tokens with `--privsep 1`. A separated token has its
   own ACLs, and its effective permissions are the *intersection* of the user's
   permissions and the token's. Every grant below is therefore made twice: once for the
   user and once for the token.
2. **More specific ACLs replace inherited ones.** A role granted on `/vms` does not add to
   a role inherited from `/`, it *replaces* it for that subtree. If you grant
   `VM.PowerMgmt` on `/vms` without `VM.Audit`, the token can no longer see a single
   guest. Every custom role below therefore repeats the audit privileges it needs.

## 1. Create a dedicated user

```bash
pveum user add mcp@pve --comment "proxmox-ops-mcp"
```

## 2. Pick a tier

### `read_only` (default, recommended to start)

The built-in `PVEAuditor` role covers every diagnostic and planning tool.

```bash
pveum acl modify / --users mcp@pve --roles PVEAuditor
pveum user token add mcp@pve ops --privsep 1          # prints the secret once
pveum acl modify / --tokens 'mcp@pve!ops' --roles PVEAuditor
```

#### Backup visibility (needed for `backup_audit`)

Proxmox lists a backup volume only if the caller has `Datastore.Allocate` on the
storage, **or** `Datastore.AllocateSpace` on the storage **plus** `VM.Backup` on the guest
(`PVE::Storage::check_volume_access`). `Datastore.Audit` is not enough. Without these
privileges the API does not return an error, only an **empty list**.

`backup_audit` checks the token's effective permissions first. If backups cannot be
seen, it reports `BACKUP_STORAGE_UNREADABLE` and marks the guests as `unknown`. It never
reports a backup as missing when it might simply be invisible. To make backups visible:

```bash
pveum role add ProxmoxOpsBackupAudit \
  --privs "VM.Audit VM.Backup Datastore.Audit Datastore.AllocateSpace"
for path in /vms /storage; do
  pveum acl modify $path --users mcp@pve --roles ProxmoxOpsBackupAudit
  pveum acl modify $path --tokens 'mcp@pve!ops' --roles ProxmoxOpsBackupAudit
done
```

> **Trade-off.** `VM.Backup` and `Datastore.AllocateSpace` would technically let the
> token *create* backups. proxmox-ops-mcp has no tool that does this at any tier, but the
> token itself could. Decide whether a complete backup audit is worth that to you.

### `operator`

Adds graceful power actions and snapshots. Scope the role to a **resource pool**, so the
assistant can only touch guests you explicitly put there:

```bash
pveum role add ProxmoxOpsOperator --privs "VM.Audit VM.PowerMgmt VM.Snapshot"
pveum acl modify /pool/<pool> --users mcp@pve --roles ProxmoxOpsOperator
pveum acl modify /pool/<pool> --tokens 'mcp@pve!ops' --roles ProxmoxOpsOperator
```

Start the server with `PROXMOX_ACCESS_TIER=operator`. Guests outside the pool stay
read-only on the Proxmox side, even if the server tier allows writes.

### `admin`

Adds hard stop and migration.

```bash
pveum role add ProxmoxOpsAdmin --privs "VM.Audit VM.PowerMgmt VM.Snapshot VM.Migrate"
pveum acl modify /pool/<pool> --users mcp@pve --roles ProxmoxOpsAdmin
pveum acl modify /pool/<pool> --tokens 'mcp@pve!ops' --roles ProxmoxOpsAdmin
```

Migrating guests with local disks (`with_local_disks=true`) also needs
`Datastore.AllocateSpace` on the target storage.

Start the server with `PROXMOX_ACCESS_TIER=admin`.

### Check what a token can really do

```bash
pvesh get /access/permissions --userid 'mcp@pve!ops' --path /vms/<vmid>
```

## 3. TLS

Proxmox nodes use certificates from the cluster's own CA. Those certificates usually
list only the node name, `localhost` and the IP addresses known at installation time.
Connecting via a VPN or management address then fails with `SSLError`, even though the
CA is right. Never turn verification off. Pick one of these instead:

**Pin the certificate.** `proxmox-backup-client` does the same:

```bash
openssl s_client -connect pve1.example.com:8006 </dev/null 2>/dev/null \
  | openssl x509 -noout -fingerprint -sha256
# PROXMOX_TLS_FINGERPRINT=AB:CD:...   (colons optional)
```

The pin replaces CA and hostname checks (also for connections through an HTTPS proxy)
and must be updated when the certificate is
renewed. `proxmox-ops-mcp check` prints this command when it hits a TLS error.

**Trust the cluster CA** and connect via a name that is in the certificate:

```bash
scp root@pve1:/etc/pve/pve-root-ca.pem ./pve-root-ca.pem
export PROXMOX_CA_BUNDLE=$PWD/pve-root-ca.pem PROXMOX_HOST=pve1   # name must match
```

## Required privileges per tool

| Tools | Privileges |
|---|---|
| Everything read-only except backups | `PVEAuditor` on `/` (`Sys.Audit`, `VM.Audit`, `Datastore.Audit`, …) |
| `backup_audit` (to see backup volumes) | additionally `Datastore.AllocateSpace` on the storage and `VM.Backup` on the guests |
| `guest_power`, `create_snapshot` | `VM.Audit`, `VM.PowerMgmt`, `VM.Snapshot` on the guests |
| `migrate_guest` | additionally `VM.Migrate` (and `Datastore.AllocateSpace` on the target storage for local disks) |

`storage_health` and `node_maintenance_status` need nothing beyond `Sys.Audit`. The
complete apt package list (`/apt/update`) would need `Sys.Modify`, so the server
deliberately uses `/apt/versions`.

## 4. Verify

```bash
proxmox-ops-mcp --env-file ./proxmox.env check
```

## Revoking access

```bash
pveum user token remove mcp@pve ops
pveum user delete mcp@pve
```
