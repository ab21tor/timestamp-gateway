# Backup and recovery notes

This box is not recoverable from the git repo alone.

To recover the live gateway, the operator needs code, service units, secrets, Phoenixd state, otsd calendar state, and proof artifacts.

## Critical backup set

### Gateway

Repository:

`/home/gateway/timestamp-gateway`

Secrets/config:

`/home/gateway/timestamp-gateway/.env`

Systemd unit:

`/etc/systemd/system/timestamp-gateway.service`

Current service shape:

- user: `gateway`
- working directory: `/home/gateway/timestamp-gateway`
- env file: `/home/gateway/timestamp-gateway/.env`
- bind: `<gateway_url>`
- restart: always

Durable state directory:

`/var/lib/timestamp-gateway`

This is critical. It holds the durable obligation log — the record of settled payments that must still be stamped — and the operator `PAUSED` switch. If it is lost, a payment that settled but was not yet anchored can no longer be recovered.

Critical files (the DB runs in SQLite WAL mode, so back up all three sidecars together):

- `obligations.db`
- `obligations.db-wal`
- `obligations.db-shm`

Also present:

- `PAUSED` (only when the operator has paused the gateway)

For a consistent copy, back it up while the gateway is stopped, or use a `sqlite3 .backup` snapshot. `ops/backup-live-state.sh` does both protections automatically: it snapshots the database with `sqlite3 .backup` before archiving, and archives the whole `/var/lib/timestamp-gateway` directory, which captures the database and both sidecars together.

### Phoenixd

Systemd unit:

`/etc/systemd/system/phoenixd.service`

Phoenixd binary directory:

`/home/gateway/phoenixd/phoenixd-0.8.0-linux-x64`

Phoenixd home/state directory:

`/home/gateway/phoenixd/home/.phoenix`

Critical files:

- `phoenix.conf`
- `seed.dat`
- `phoenix.mainnet.*.db`
- `phoenix.mainnet.*.db-wal`
- `phoenix.mainnet.*.db-shm`

Log files are useful but less critical:

- `phoenix.log`
- `/home/gateway/phoenixd/phoenixd-systemd.log`

`seed.dat` is wallet material. Treat it as secret.

Phoenixd listens only on:

`127.0.0.1:9740`

Phoenixd service:

`phoenixd.service`

It is enabled on boot.

### Local otsd calendar

The local OpenTimestamps calendar data lives at:

`/var/lib/otsd/calendar`

This is critical. Do not delete it casually.

Important files include:

- `uri`
- `hmac-key`
- `donation_addr`
- `journal`
- `db/`

The running Docker container is:

`otsd`

Current Docker shape:

- image: `otsd-local`
- network: `host`
- working dir: `/app`
- app mount: `/home/gateway/opentimestamps-server:/app`
- calendar mount: `/var/lib/otsd/calendar:/calendar`
- command: `python3 otsd --calendar /calendar --btc-conf-target 12 -v`

Current plain anchoring policy:

- batch up to 6 hours by default
- when anchoring, target about 12-block Bitcoin confirmation
- save Bitcoin proof after 6 confirmations by default

### Bitcoin RPC bridge

Installed systemd unit:

`/etc/systemd/system/socat-bitcoin-rpc.service`

The repo ships only the template (`deploy/socat-bitcoin-rpc.service.example`); the installed unit carries the substituted node onion address. A restored box has no Bitcoin path without it.

### Proof artifacts

Proof artifacts live at:

`/home/gateway/timestamp-gateway-live-artifacts`

These contain proof receipts and test records.

Some artifact files may contain sensitive payment/auth material.

Keep artifact directories private.

## Automated backups

`ops/backup-live-state.sh` runs daily under `backup-live-state.timer` (templates in `ops/systemd/`, install command in the unit's header comment). It runs as root — parts of the backup set are readable by root only.

Each run snapshots the obligation log with `sqlite3 .backup`, archives the critical set above (including the opentimestamps-server checkout and the installed socat unit), encrypts the archive to `BACKUP_AGE_RECIPIENT`, pushes it to `BACKUP_REMOTE`, prunes to the `BACKUP_KEEP` newest archives — matched by archive-name pattern only, nothing else in `BACKUP_ROOT` is ever deleted — and writes `backup-status` to the state directory.

Prerequisites on the box (the script degrades loudly to `attention` when one is missing; it never silently skips a step):

- `sqlite3` — consistent obligation-log snapshot
- `age` — archive encryption
- `rsync` — off-box push

Configuration lives in the gitignored `.env` (entries in `.env.example`):

- `BACKUP_AGE_RECIPIENT` — age public key the archive is encrypted to. The matching private key must live off this box: it is the only way to read pushed backups, and losing it makes every one of them unrecoverable. Empty: the archive stays plaintext and is never pushed.
- `BACKUP_REMOTE` — rsync destination (`user@host:path`) for the encrypted archive. Plaintext archives are never pushed. Empty: backups stay on this box. A local-only backup shares fate with the box — the disk that fails, the attacker that wipes it, or the provider that closes the account takes the service and every backup of it at once.
- `BACKUP_KEEP` — newest archives kept locally (default 7).

Status file: `backup-status` in the state directory, one JSON line written atomically (same pattern as `wallet-status`). `status` is `ok` (encrypted and pushed), `local_only` (archive created, nothing pushed — `detail` says whether it is plaintext), `attention` (a backup exists but degraded — `detail` says why), or `failed` (no usable archive).

## Minimum restore checklist

On a replacement box:

1. Restore the repository.
2. Restore `.env`.
3. Restore `timestamp-gateway.service`.
   - Also restore `/var/lib/timestamp-gateway` (obligation log + `PAUSED`). The gateway initialises an empty obligation log at startup only if the directory exists and is writable by the service user — it refuses to start otherwise — so on a fresh box create the directory (owned by the service user) even when not restoring old contents. Restore the contents when recovering pending obligations from the old box.
4. Restore Phoenixd binary directory.
5. Restore Phoenixd home/state directory.
6. Restore `phoenixd.service`.
7. Restore `/var/lib/otsd/calendar`.
8. Re-clone `/home/gateway/opentimestamps-server`: `git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server /home/gateway/opentimestamps-server`.
9. Recreate the `otsd` Docker container with the same mounts and command.
10. Restore `socat-bitcoin-rpc.service` from the backup archive (the installed unit carries the substituted node onion; the repo ships only the template) and enable it — without it otsd has no Bitcoin path.
11. Restore proof artifacts if needed.
12. Reinstall the timers and their services from `ops/systemd/` (install commands in each unit's header): `wallet-balance-check`, `health-monitor`, `timestamp-gateway-upgrade-proofs`, `backup-live-state`. Without them the restored box has no wallet alarm, no health alarms, no proof sweeper, and no backups.
13. Run `systemctl daemon-reload`.
14. Start Phoenixd.
15. Start otsd.
16. Start timestamp-gateway.
17. Run the operator checks.

## Post-restore checks

Run:

`./ops/status.sh`

`./ops/phoenixd-status.sh`

`./ops/otsd-status.sh`

`./ops/list-proofs.sh`

A healthy restored box should show:

- gateway running
- payment backend ok
- Phoenixd running
- Phoenixd API local-only
- otsd running
- local calendar path present
- known proofs visible
