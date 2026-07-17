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
#   ARTIFACTS             proof artifacts dir
#   UNIT_DIR              installed systemd units dir
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
  # script runs as root, so hand the file over like the archive.
  if [ "$(id -u)" -eq 0 ]; then
    chown gateway:gateway "$STATUS_FILE"
  fi
}

degrade() {
  # degrade <error-string> — a degraded backup that exists beats a perfect
  # one that doesn't: record, warn, continue. Never silently skip a step.
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
. "$REPO_DIR/.env"
set +a
STATE_DIR="${STATE_DIR:-/var/lib/timestamp-gateway}"
STATUS_FILE="$STATE_DIR/backup-status"
BACKUP_ROOT="${BACKUP_ROOT:-/home/gateway/timestamp-gateway-live-backups}"
OTSD_FORK_PATH="${OTSD_FORK_PATH:-/home/gateway/opentimestamps-server}"
PHOENIX_HOME="${PHOENIX_HOME:-/home/gateway/phoenixd/home/.phoenix}"
OTSD_CALENDAR_DIR="${OTSD_CALENDAR_DIR:-/var/lib/otsd/calendar}"
ARTIFACTS="${ARTIFACTS:-/home/gateway/timestamp-gateway-live-artifacts}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
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
  # Any unhandled command failure is a failed backup, said loudly.
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
# archived below is the fallback if this snapshot is absent.
if ! command -v sqlite3 >/dev/null 2>&1; then
  degrade "sqlite3 not installed - obligations.db snapshot skipped (install prerequisite, see ops/BACKUP-RECOVERY.md)"
elif [ ! -f "$STATE_DIR/obligations.db" ]; then
  degrade "obligations.db not found at $STATE_DIR - snapshot skipped"
else
  sqlite3 "$STATE_DIR/obligations.db" ".backup '$OUTDIR/obligations.db.snapshot'" \
    || degrade "sqlite3 snapshot failed"
fi

echo "=== creating sensitive archive ==="
# /var/lib/timestamp-gateway holds the durable obligation log
# (obligations.db plus its WAL -wal/-shm sidecars) and the operator PAUSED
# switch. Archiving the whole directory captures the database and both sidecar
# files together, so a settled-but-unstamped obligation survives a rebuild.
# The opentimestamps-server checkout and the installed socat unit (which
# carries the substituted node onion address) are deployment state that git
# cannot restore.
tar -czf "$ARCHIVE" \
  "$REPO_DIR/.env" \
  "$UNIT_DIR/timestamp-gateway.service" \
  "$UNIT_DIR/phoenixd.service" \
  "$UNIT_DIR/socat-bitcoin-rpc.service" \
  "$PHOENIX_HOME" \
  "$OTSD_CALENDAR_DIR" \
  "$STATE_DIR" \
  "$OTSD_FORK_PATH" \
  "$ARTIFACTS" \
  "$OUTDIR" \
  2>"$OUTDIR/tar-warnings.txt"

if [ -s "$OUTDIR/tar-warnings.txt" ]; then
  logger -p user.notice -t backup-live-state \
    "tar warnings: $(head -1 "$OUTDIR/tar-warnings.txt")" || true
fi

if [ "$(id -u)" -eq 0 ]; then
  chown gateway:gateway "$ARCHIVE"
fi
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
    rsync -a "$FINAL_ARCHIVE" "$BACKUP_REMOTE/" \
      || degrade "rsync push to BACKUP_REMOTE failed"
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
