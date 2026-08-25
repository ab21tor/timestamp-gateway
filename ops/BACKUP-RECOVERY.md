# Backup and recovery notes

A deployment is not recoverable from the git repo alone.

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

This is critical. It holds the durable obligation log — the record of settled payments that must still be stamped — and the operator `PAUSED` switch. If it is lost, a payment that settled but was not yet anchored can no longer be recovered. (What the obligation log is and how it is configured: operator guide, "Durable obligation log".)

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

the release directory under `/home/gateway/phoenixd/` (e.g. `phoenixd-0.8.0-linux-x64`)

Phoenixd home/state directory:

`/home/gateway/phoenixd/home/.phoenix`

Back up the whole directory. The critical-file list lives in
OPERATOR-NOTES.md, "Phoenixd boundary" — `seed.dat` in particular is
wallet material; treat it as secret.

**Recovery is by seed — a restored phoenixd is NEVER started.** Lightning
channel state is not file-restorable: the wallet database in the archive is
a raw copy of a hot file, and even a perfect copy goes stale the moment the
live node signs another state — starting a node from restored channel state
risks broadcasting a revoked state and losing the channel balance to the
penalty path. The archive preserves `.phoenix` (above all `seed.dat`) as
the recovery *material* and as a record, not as something to boot. To
recover: install a fresh phoenixd and restore from the seed (phoenixd's
documented seed-restore path); funds recover through ACINQ's server-side
view of the channel. The seed IS the wallet — anyone holding it holds the
funds.

Log files are useful but less critical:

- `phoenix.log`
- `/home/gateway/phoenixd/phoenixd-systemd.log`

Service boundary (localhost-only bind, systemd management): see
OPERATOR-NOTES.md, "Phoenixd boundary".

### Local otsd calendar

The local OpenTimestamps calendar data lives at:

`/var/lib/otsd/calendar`

This is critical.

The complete file list:

- `uri` — the calendar's permanent identity (baked into every attestation)
- `hmac-key` — commitment MAC secret
- `donation_addr`
- `journal` — the append-only commitment record; the durable source of truth
- `journal.counts` — the record-count sidecar (billing evidence; 4 bytes
  per journal entry)
- `db/` — the LevelDB of per-commitment timestamps (serves upgrades)
- `backup_cache`

**Hot-copy consistency:** `journal` and `journal.counts` are append-only
and copy safely while otsd runs — a torn tail entry is padded out on the
next writer open (the journal is written fsync-per-entry). `db/` is a
LevelDB with no online-backup method, so its copy in the archive is
**crash-consistent at best**. The recovery implication: a torn `db/` may
fail to open or miss entries, which costs the calendar its served-upgrade
path for the affected old commitments — while client-held anchored proofs
verify against Bitcoin regardless, and pending commitments re-enter
stamping from the journal scan. For a clean `db/` copy, back up while otsd
is stopped.

The running Docker container is:

`otsd`

Docker shape (the installed unit matches `deploy/otsd.service.example`):

- image: `otsd-local`
- network: `host`
- working dir: `/app`
- app mount: `/home/gateway/opentimestamps-server:/app`
- calendar mount: `/var/lib/otsd/calendar:/calendar`
- env: via `--env-file /etc/systemd/system/otsd.env` (holds `BITCOIN_RPC_SERVICE_URL`; owned by the service user, mode 600 — a root-owned file fails EACCES, see the template header)
- command: `python3 otsd --calendar /calendar --btc-conf-target 12 --btc-max-fee 0.0002` (no `-v`: INFO log level)

Anchoring policy: see OPERATOR-NOTES.md, "otsd boundary" (the reference
layout vs. the shipped defaults).

### Tor hidden service keys

On the compose deployment the Tor state lives in the `tor_keys` volume
(host path `/var/lib/docker/volumes/timestamp-gateway_tor_keys/_data`);
the hidden-service directory inside it holds the onion hostname and its
ed25519 secret key. This is the box's public identity twice over: the
front door, and — where the onion address is the calendar `uri` (the
README's "Calendar URI note") — the name written inside every attestation
the calendar has ever issued. Lose it and both are gone permanently. The
backup script archives it via `TOR_KEYS_DIR`.

### Anchor receipts

Billing evidence: the fork's append-only receipts JSONL (compose volume
`anchor_receipts`, host path
`/var/lib/docker/volumes/timestamp-gateway_anchor_receipts/_data`).
Append-only, copies safely hot. Archived via `ANCHOR_RECEIPTS_DIR`.

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

Each run snapshots the obligation log with `sqlite3 .backup` (where the host has no sqlite3, the same online-backup API runs through the gateway container's python — read-locked, staged in the container's ephemeral `/tmp`, never writing production state), archives the critical set above (including the opentimestamps-server checkout, the Tor hidden-service keys, the anchor receipts, and the installed socat unit), encrypts the archive to `BACKUP_AGE_RECIPIENT`, pushes it to `BACKUP_REMOTE`, prunes to the `BACKUP_KEEP` newest archives — matched by archive-name pattern only, nothing else in `BACKUP_ROOT` is ever deleted — and writes `backup-status` to the state directory.

Layouts differ (systemd VPS vs compose island); a member's disposition follows its configured path — present, absent (degrades the run to `attention`, naming it; the archive is made without it), or declared **explicitly empty** (not applicable to this deployment shape; skipped silently) — as documented in the script header. Use empty only for a member absent *by design* here, never to paper over one that should exist. On the compose island, pass the layout as invocation env (`REPO_DIR=/root/timestamp-gateway`, `STATE_DIR=/var/lib/docker/volumes/timestamp-gateway_gateway_data/_data`, `PHOENIX_HOME=/root/.phoenix`; leaving `TOR_KEYS_DIR`/`ANCHOR_RECEIPTS_DIR` **unset** falls through to the compose-volume defaults) — the gitignored `.env` wins only for the keys it actually sets. On a bare-metal/systemd box with no inbound onion, set `TOR_KEYS_DIR=` empty (N/A) and point `ANCHOR_RECEIPTS_DIR` at the real host receipts file (`ANCHOR_RECEIPTS_PATH`, e.g. `/var/lib/otsd/calendar/anchor-receipts.jsonl`) so the billing evidence is captured by name.

Prerequisites on the box (the script degrades loudly to `attention` when one is missing; it never silently skips a step):

- `sqlite3` — consistent obligation-log snapshot (or a running gateway container for the fallback above)
- `age` — archive encryption
- `rsync` — off-box push

Configuration lives in the gitignored `.env` (entries in `.env.example`):

- `BACKUP_AGE_RECIPIENT` — age public key the archive is encrypted to. The matching private key must live off this box: it is the only way to read pushed backups, and losing it makes every one of them unrecoverable. Empty: the archive stays plaintext and is never pushed.
- `BACKUP_REMOTE` — rsync destination (`user@host:path`) for the encrypted archive. Plaintext archives are never pushed. Empty: backups stay on this box. A local-only backup shares fate with the box: whatever takes the box takes every backup of it.
- `BACKUP_KEEP` — newest archives kept locally (default 7).

Status file: `backup-status` in the state directory, one JSON line written atomically (same pattern as `wallet-status`). `status` is `ok` (encrypted and pushed), `local_only` (archive created, nothing pushed — `detail` says whether it is plaintext), `attention` (a backup exists but degraded — `detail` says why), or `failed` (no usable archive).

## Capacity and the recovery hierarchy

**Capacity.** The archive grows with the calendar, and the calendar grows without bound by design: the journal adds 44 bytes per commitment (~1.4 GB/year at a sustained 1 commitment/second) and the LevelDB `db/` directory a few hundred bytes per anchored commitment (order tens of GB by year three at that rate). `BACKUP_KEEP` bounds the **count** of archives, never their size — at scale the daily run's duration and the local set's footprint both grow linearly, so revisit `BACKUP_KEEP`, the push destination's disk, and possibly a slower cadence for the calendar member (splitting it from the daily secrets/state set) as the calendar ages. Watch the run duration in the journal; it measures where you are.

**Recovery order.** Members differ in what they can be rebuilt from; restore in this order:

1. **Irreplaceable, small, and constant-size:** the phoenixd seed, the Tor hidden-service keys (the calendar's onion identity), `.env`, the calendar's `uri`/`hmac-key`, and the obligation log snapshot. Losing these loses identity or money.
2. **The journal (and `journal.counts`) is the record.** Append-only flat files, cheap to copy, trivially consistent. Every commitment ever accepted is in the journal; the record-count sidecar can only undercount, never invent.
3. **The LevelDB `db/` is rebuildable.** It is derived state — completed timestamps assembled from journal entries plus Bitcoin. The live-tar'd copy in the archive is best-effort (LevelDB is copied while running and a mid-compaction snapshot may not open); that is acceptable **because** it sits below the journal in this hierarchy. If a restored `db/` will not open, restore the journal and let the stamper re-derive pending state; already-anchored commitments remain provable via the Bitcoin attestations already handed to clients and the calendar's re-served history.

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
8. Restore the `opentimestamps-server` checkout **from the archive** — never by re-cloning from GitHub. The deployed fork carries uncommitted work by design; a re-clone silently loses it and deploys different code than the calendar was running.
9. Install `otsd.service` from `deploy/otsd.service.example` and recreate its companion `/etc/systemd/system/otsd.env` (owned by the service user, mode 600; it holds `BITCOIN_RPC_SERVICE_URL` and is NOT part of the automated backup archive — recreate it from the restored `.env`'s value). Enable the unit; it recreates the `otsd` container with the shape recorded above.
10. Restore `socat-bitcoin-rpc.service` from the backup archive (the installed unit carries the substituted node onion; the repo ships only the template) and enable it — without it otsd has no Bitcoin path.
11. Restore proof artifacts if needed.
12. Reinstall the timers and their services from `ops/systemd/` (install commands in each unit's header): `wallet-balance-check`, `health-monitor`, `timestamp-gateway-upgrade-proofs`, `backup-live-state`. Without them the restored box has no wallet alarm, no health alarms, no proof sweeper, and no backups.
13. Run `systemctl daemon-reload`.
14. Recover Lightning by seed ("Recovery is by seed" above) — do **not** start phoenixd from the restored state directory.
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
