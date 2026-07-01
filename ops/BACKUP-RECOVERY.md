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

For a consistent copy, back it up while the gateway is stopped, or use `sqlite3 obligations.db ".backup <dest>"`. `ops/backup-live-state.sh` archives the whole `/var/lib/timestamp-gateway` directory, which captures the database and both sidecars together.

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
- command: `python3 otsd --calendar /calendar --btc-conf-target 2 -v`

Current plain anchoring policy:

- batch up to 6 hours by default
- when anchoring, target about 2-block Bitcoin confirmation
- save Bitcoin proof after 6 confirmations by default

### Proof artifacts

Proof artifacts live at:

`/home/gateway/timestamp-gateway-live-artifacts`

These contain proof receipts and test records.

Some artifact files may contain sensitive payment/auth material.

Keep artifact directories private.

## Minimum restore checklist

On a replacement box:

1. Restore the repository.
2. Restore `.env`.
3. Restore `timestamp-gateway.service`.
   - Also restore `/var/lib/timestamp-gateway` (obligation log + `PAUSED`). On a fresh box the gateway recreates this directory and an empty obligation log at startup; restore it only if you are recovering pending obligations from the old box.
4. Restore Phoenixd binary directory.
5. Restore Phoenixd home/state directory.
6. Restore `phoenixd.service`.
7. Restore `/var/lib/otsd/calendar`.
8. Restore or rebuild `/home/gateway/opentimestamps-server`.
9. Recreate the `otsd` Docker container with the same mounts and command.
10. Restore proof artifacts if needed.
11. Run `systemctl daemon-reload`.
12. Start Phoenixd.
13. Start otsd.
14. Start timestamp-gateway.
15. Run the operator checks.

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
