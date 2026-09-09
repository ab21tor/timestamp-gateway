#!/usr/bin/env bash
# Live-state backup: archive the deployment's critical set (see
# ops/BACKUP-RECOVERY.md), snapshot the obligation log, encrypt to an age
# recipient, push off-box, prune old archives, and write a status file.
# Run unattended by backup-live-state.timer (see ops/systemd/) as root —
# parts of the tar set are readable by root only. No CWD reliance.
#
# Deployment specifics come from env / the gitignored .env:
#   REPO_DIR              gateway repo checkout (env only — locates .env)
#   STATE_DIR             durable state dir (obligation log, status files)
#   BACKUP_ROOT           where archives are written on this box
#   OTSD_FORK_PATH        opentimestamps-server checkout (calendar code)
#   BACKUP_AGE_RECIPIENT  age public key; empty = archive stays plaintext
#   BACKUP_REMOTE         rsync destination user@host:path; empty = no push
#   BACKUP_KEEP           newest archives kept in BACKUP_ROOT (default 7)
#   PHOENIX_HOME          phoenixd state dir (seed.dat, wallet db)
#   OTSD_CALENDAR_DIR     otsd calendar state, host path
#   TOR_KEYS_DIR          tor hidden-service state (onion identity — and,
#                         where the onion is the calendar uri, the calendar's
#                         name); default is the compose tor_keys volume. Set
#                         empty to declare it not-applicable to this shape
#                         (e.g. a bare-metal box with no inbound onion).
#   ANCHOR_RECEIPTS_DIR   anchor receipts (billing evidence); default is the
#                         compose anchor_receipts volume. On a non-compose
#                         layout point it at the real host receipts path
#                         (the .env ANCHOR_RECEIPTS_PATH), or empty for N/A.
#   ARTIFACTS             proof artifacts dir
#   UNIT_DIR              installed systemd units dir
#   GATEWAY_UNIT          the three installed-unit members individually;
#   PHOENIXD_UNIT         default is $UNIT_DIR/<name>. Set one empty to
#   SOCAT_UNIT            declare it not-applicable to this shape (the compose
#                         island runs the gateway and the RPC bridge as
#                         containers — no gateway/socat units exist there by
#                         design — while phoenixd is host-systemd on both).
#
# Layouts differ (systemd VPS vs compose island). A member's disposition:
#   - path set and present  -> archived
#   - path NON-EMPTY but absent -> degrades loudly (attention, naming it) and
#     the archive is made without it — a degraded backup that exists beats a
#     perfect one that doesn't.
#   - path EXPLICITLY EMPTY -> declared not-applicable to this deployment
#     shape and skipped silently (no attention). Only ever use empty for a
#     member that is absent BY DESIGN here (a compose-only volume on a
#     bare-metal box), never to paper over a member that should exist.
# On the compose island, pass the layout as invocation env (REPO_DIR,
# STATE_DIR, PHOENIX_HOME, ...) — the gitignored .env wins only for the keys
# it actually sets. Unset (not empty) falls through to the compose defaults.
set -euo pipefail
umask 077

REPO_DIR="${REPO_DIR:-/home/gateway/timestamp-gateway}"
STATE_DIR="${STATE_DIR:-/var/lib/timestamp-gateway}"
STATUS_FILE="$STATE_DIR/backup-status"
STATUS="ok"
DETAIL=""

write_status() {
  # write_status <status> <archive-name> [detail] — atomic: tmp file in the
  # same directory + mv (wallet-status pattern).
  local tmp
  tmp="$(mktemp "$STATUS_FILE.XXXXXX")"
  if [ -n "${3:-}" ]; then
    printf '{"status": "%s", "archive": "%s", "checked_at": %s, "detail": "%s"}\n' \
      "$1" "$2" "$(date +%s)" "$3" > "$tmp"
  else
    printf '{"status": "%s", "archive": "%s", "checked_at": %s}\n' \
      "$1" "$2" "$(date +%s)" > "$tmp"
  fi
  mv "$tmp" "$STATUS_FILE"
  # The gateway process (user gateway) reads this file for /health; the
  # script runs as root, so hand the file over like the archive. Layouts
  # without a gateway user (compose island: everything runs as root) keep
  # root ownership.
  if [ "$(id -u)" -eq 0 ] && id -u gateway >/dev/null 2>&1; then
    chown gateway:gateway "$STATUS_FILE"
  fi
}

degrade() {
  # degrade <error-string> — record, warn, continue; never silently skip a step.
  STATUS="attention"
  DETAIL="${DETAIL:+$DETAIL; }$1"
  logger -p user.warning -t backup-live-state "$1" || true
  echo "attention: $1"
}

# Source the gitignored .env for the BACKUP_* knobs and OTSD_FORK_PATH (same
# pattern as ops/wallet-balance-check.sh; .env wins over the defaults below).
if [ ! -f "$REPO_DIR/.env" ]; then
  mkdir -p "$STATE_DIR"
  write_status "failed" "none" "env file not found at $REPO_DIR/.env"
  echo "state: failed"
  exit 1
fi
set -a
# shellcheck disable=SC1091
# || true: a .env can carry unfilled placeholder lines (e.g. an
# <angle-bracket> host) that error as shell; under set -e that would abort
# the whole backup. The error still prints, and sourcing continues past it
# to the remaining assignments.
. "$REPO_DIR/.env" || true
set +a
STATE_DIR="${STATE_DIR:-/var/lib/timestamp-gateway}"
STATUS_FILE="$STATE_DIR/backup-status"
BACKUP_ROOT="${BACKUP_ROOT:-/home/gateway/timestamp-gateway-live-backups}"
OTSD_FORK_PATH="${OTSD_FORK_PATH:-/home/gateway/opentimestamps-server}"
PHOENIX_HOME="${PHOENIX_HOME:-/home/gateway/phoenixd/home/.phoenix}"
OTSD_CALENDAR_DIR="${OTSD_CALENDAR_DIR:-/var/lib/otsd/calendar}"
# Unset-only defaults (${VAR=...}, not ${VAR:-...}): a deployment can set
# either to the empty string to declare the member not-applicable to its
# shape (skipped silently below); only a truly UNSET var falls through to the
# compose-volume default.
: "${TOR_KEYS_DIR=/var/lib/docker/volumes/timestamp-gateway_tor_keys/_data}"
: "${ANCHOR_RECEIPTS_DIR=/var/lib/docker/volumes/timestamp-gateway_anchor_receipts/_data}"
# Unset-only as well: the compose island keeps no proof artifacts dir by
# design (ARTIFACTS= declares it N/A there).
: "${ARTIFACTS=/home/gateway/timestamp-gateway-live-artifacts}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
# Same unset-only idiom for the individual unit members: empty = N/A on this
# shape (skipped silently), unset = $UNIT_DIR/<name>.
: "${GATEWAY_UNIT=$UNIT_DIR/timestamp-gateway.service}"
: "${PHOENIXD_UNIT=$UNIT_DIR/phoenixd.service}"
: "${SOCAT_UNIT=$UNIT_DIR/socat-bitcoin-rpc.service}"

# OTSD_FORK_PATH may be compose-relative (e.g. ../opentimestamps-server,
# relative to the compose file) — resolve it against REPO_DIR so the tar
# member is a real absolute path.
case "$OTSD_FORK_PATH" in
  /*) ;;
  *) OTSD_FORK_PATH="$(cd "$REPO_DIR/$OTSD_FORK_PATH" 2>/dev/null && pwd || echo "$REPO_DIR/$OTSD_FORK_PATH")" ;;
esac
BACKUP_AGE_RECIPIENT="${BACKUP_AGE_RECIPIENT:-}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
case "$BACKUP_KEEP" in
  ''|0|*[!0-9]*) echo "BACKUP_KEEP must be a positive integer" >&2; exit 1 ;;
esac

TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="$BACKUP_ROOT/$TS"
ARCHIVE="$BACKUP_ROOT/$TS-live-state.tar.gz"

on_err() {
  # Any unhandled command failure is a failed backup.
  write_status "failed" "$(basename "$ARCHIVE")" "backup aborted (see journal)" || true
  logger -p user.err -t backup-live-state "backup failed — see journal" || true
  echo "state: failed"
}
trap on_err ERR

mkdir -p "$OUTDIR" "$STATE_DIR"
chmod 700 "$BACKUP_ROOT"
chmod 700 "$OUTDIR"

echo "=== backup live state ==="
echo "time_utc: $TS"
echo "backup_dir: $OUTDIR"
echo "archive: $ARCHIVE"
echo

echo "=== writing metadata ==="
{
  echo "time_utc: $TS"
  echo "host: $(hostname)"
  echo "user: $(whoami)"
  echo "repo: $REPO_DIR"
  echo "commit: $(git -C "$REPO_DIR" rev-parse HEAD)"
  echo "branch: $(git -C "$REPO_DIR" branch --show-current)"
} > "$OUTDIR/metadata.txt"

(cd "$REPO_DIR" && ./ops/status.sh) > "$OUTDIR/status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/phoenixd-status.sh) > "$OUTDIR/phoenixd-status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/otsd-status.sh) > "$OUTDIR/otsd-status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/list-proofs.sh) > "$OUTDIR/list-proofs.txt" 2>&1 || true

docker inspect otsd > "$OUTDIR/otsd-docker-inspect.json" 2>&1 || true
systemctl --no-pager cat timestamp-gateway.service > "$OUTDIR/timestamp-gateway.service.txt" 2>&1 || true
systemctl --no-pager cat phoenixd.service > "$OUTDIR/phoenixd.service.txt" 2>&1 || true

echo "=== snapshotting obligation log ==="
# A .backup through sqlite3 is consistent even mid-write; the raw WAL trio
# archived below is the fallback if this snapshot is absent. Where the host
# has no sqlite3 (the compose island), the same online-backup API runs
# through the gateway container's python — read-locked, writing only to the
# container's ephemeral /tmp, never to production state.
snapshot_via_container() {
  docker compose --project-directory "$REPO_DIR" exec -T gateway python -c "
import sqlite3
src = sqlite3.connect('file:/var/lib/timestamp-gateway/obligations.db?mode=ro', uri=True)
dst = sqlite3.connect('/tmp/obligations.db.snapshot')
src.backup(dst)
dst.close(); src.close()
" \
    && docker compose --project-directory "$REPO_DIR" exec -T gateway \
         cat /tmp/obligations.db.snapshot > "$OUTDIR/obligations.db.snapshot" \
    && docker compose --project-directory "$REPO_DIR" exec -T gateway \
         rm -f /tmp/obligations.db.snapshot
}

if [ ! -f "$STATE_DIR/obligations.db" ]; then
  degrade "obligations.db not found at $STATE_DIR - snapshot skipped"
elif command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$STATE_DIR/obligations.db" ".backup '$OUTDIR/obligations.db.snapshot'" \
    || degrade "sqlite3 snapshot failed"
elif docker compose --project-directory "$REPO_DIR" ps gateway >/dev/null 2>&1; then
  snapshot_via_container \
    || degrade "container snapshot failed (raw WAL trio in archive is crash-consistent; see ops/BACKUP-RECOVERY.md)"
else
  degrade "sqlite3 not installed and no gateway container - obligations.db snapshot skipped (raw WAL trio in archive is crash-consistent; see ops/BACKUP-RECOVERY.md)"
fi

echo "=== creating sensitive archive ==="
# The state dir holds the durable obligation log (obligations.db plus its
# WAL -wal/-shm sidecars) and the operator PAUSED switch; archiving the
# whole directory captures the database and both sidecars together, so a
# settled-but-unstamped obligation survives a rebuild. The
# opentimestamps-server checkout (uncommitted work by design), the tor
# hidden-service keys (the onion identity — and the calendar's uri name
# where the onion is the uri), the anchor receipts (billing evidence), and
# the installed socat unit (substituted node onion) are deployment state
# that git cannot restore. Member disposition (present, absent, declared
# empty): the header above.
MEMBERS=(
  "$REPO_DIR/.env"
  "$GATEWAY_UNIT"
  "$PHOENIXD_UNIT"
  "$SOCAT_UNIT"
  "$PHOENIX_HOME"
  "$OTSD_CALENDAR_DIR"
  "$TOR_KEYS_DIR"
  "$ANCHOR_RECEIPTS_DIR"
  "$STATE_DIR"
  "$OTSD_FORK_PATH"
  "$ARTIFACTS"
  "$OUTDIR"
)
PRESENT=()
for MEMBER in "${MEMBERS[@]}"; do
  if [ -z "$MEMBER" ]; then
    # Declared not-applicable to this deployment shape (empty path) — skip
    # silently, no attention. See the member-disposition note in the header.
    echo "n/a (not applicable to this deployment shape): a member is declared empty — skipped"
    continue
  elif [ -e "$MEMBER" ]; then
    echo "present: $MEMBER"
    PRESENT+=("$MEMBER")
  else
    degrade "missing from backup set: $MEMBER"
  fi
done
tar -czf "$ARCHIVE" "${PRESENT[@]}" 2>"$OUTDIR/tar-warnings.txt"

if [ -s "$OUTDIR/tar-warnings.txt" ]; then
  logger -p user.notice -t backup-live-state \
    "tar warnings: $(head -1 "$OUTDIR/tar-warnings.txt")" || true
fi

# The plaintext archive holds root-only material (hidden-service keys,
# macaroons): it stays root-owned, mode 600, for as long as it exists.
# Ownership is handed to the gateway user only for the encrypted archive,
# below — a plaintext archive is never made readable to a service account.
chmod 600 "$ARCHIVE"
# $OUTDIR's contents travel inside the archive; drop the loose copy so
# BACKUP_ROOT accumulates archives only.
rm -rf "$OUTDIR"

FINAL_ARCHIVE="$ARCHIVE"
echo "=== encrypting ==="
if [ -n "$BACKUP_AGE_RECIPIENT" ]; then
  if command -v age >/dev/null 2>&1; then
    age -r "$BACKUP_AGE_RECIPIENT" -o "$ARCHIVE.age" "$ARCHIVE"
    chmod 600 "$ARCHIVE.age"
    if [ "$(id -u)" -eq 0 ] && id -u gateway >/dev/null 2>&1; then
      chown gateway:gateway "$ARCHIVE.age"
    fi
    rm -f "$ARCHIVE"
    FINAL_ARCHIVE="$ARCHIVE.age"
  else
    degrade "age not installed - archive left unencrypted on this box (install prerequisite, see ops/BACKUP-RECOVERY.md)"
  fi
else
  echo "BACKUP_AGE_RECIPIENT unset - archive stays plaintext on this box"
fi

echo "=== off-box push ==="
if [ -n "$BACKUP_REMOTE" ]; then
  if [ "$FINAL_ARCHIVE" = "$ARCHIVE.age" ]; then
    if rsync -a "$FINAL_ARCHIVE" "$BACKUP_REMOTE/"; then
      # The remote keeps a bounded set, as BACKUP_ROOT does:
      # prune to the BACKUP_KEEP newest, matched by the anchored archive
      # name pattern ONLY — nothing else at the destination is touched.
      REMOTE_HOST="${BACKUP_REMOTE%%:*}"
      REMOTE_PATH="${BACKUP_REMOTE#*:}"
      REMOTE_PRUNE="$(ssh "$REMOTE_HOST" "ls -1 \"$REMOTE_PATH\"" 2>/dev/null \
        | grep -E '^[0-9]{8}T[0-9]{6}Z-live-state\.tar\.gz(\.age)?$' \
        | sort -r | tail -n +"$((BACKUP_KEEP + 1))" || true)"
      while IFS= read -r OLD; do
        [ -n "$OLD" ] || continue
        if ssh "$REMOTE_HOST" "rm -f \"$REMOTE_PATH/$OLD\""; then
          echo "pruned remote: $OLD"
        else
          degrade "remote prune failed for $OLD"
        fi
      done <<< "$REMOTE_PRUNE"
    else
      degrade "rsync push to BACKUP_REMOTE failed"
    fi
  else
    degrade "BACKUP_REMOTE set but archive is plaintext - refusing to push"
  fi
else
  echo "BACKUP_REMOTE unset - no off-box copy"
fi

echo "=== retention ==="
# Prune to the BACKUP_KEEP newest archives, matched by archive-name pattern
# ONLY - nothing else in BACKUP_ROOT is ever deleted.
# shellcheck disable=SC2010 # candidate names are machine-generated (the TS
# pattern below is the whole filter); ls|grep keeps the match anchored.
PRUNE_LIST="$(ls -1 "$BACKUP_ROOT" \
  | grep -E '^[0-9]{8}T[0-9]{6}Z-live-state\.tar\.gz(\.age)?$' \
  | sort -r \
  | tail -n +"$((BACKUP_KEEP + 1))" || true)"
while IFS= read -r OLD; do
  [ -n "$OLD" ] || continue
  rm -f "$BACKUP_ROOT/$OLD"
  echo "pruned: $OLD"
done <<< "$PRUNE_LIST"

if [ "$STATUS" = "ok" ]; then
  if [ -z "$BACKUP_AGE_RECIPIENT" ] && [ -z "$BACKUP_REMOTE" ]; then
    STATUS="local_only"; DETAIL="plaintext archive, no off-box copy"
  elif [ -z "$BACKUP_REMOTE" ]; then
    STATUS="local_only"; DETAIL="encrypted archive, no off-box copy"
  fi
fi
write_status "$STATUS" "$(basename "$FINAL_ARCHIVE")" "$DETAIL"

echo
echo "=== backup complete ==="
ls -lh "$FINAL_ARCHIVE"
echo
echo "state: $STATUS"
