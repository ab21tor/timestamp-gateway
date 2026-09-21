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
#   PHOENIX_HOME          phoenixd state dir (seed.dat, wallet db); the
#                         phoenixd user's home, /var/lib/phoenixd/.phoenix
#                         (deploy/phoenixd.service.example)
#   OTSD_CALENDAR_DIR     otsd calendar state, host path
#   OBLIGATIONS_DB_PATH   the obligation log the gateway is configured to
#                         use (its .env setting); default
#                         $STATE_DIR/obligations.db on the host. Every
#                         snapshot, check, archive member and metadata line
#                         names this file, never the default beside it.
#   TOR_KEYS_DIR          tor hidden-service state (onion identity — and,
#                         where the onion is the calendar uri, the calendar's
#                         name); default is the compose tor_keys volume. Set
#                         empty to declare it not-applicable to this shape
#                         (e.g. a bare-metal box with no inbound onion).
#   ANCHOR_RECEIPTS_DIR   the calendar's anchor accounting (its receipts
#                         file and markers, where the fork's
#                         OTSD_ANCHOR_RECEIPTS points) when that directory
#                         is OUTSIDE the calendar directory; inside it (the
#                         systemd path) it is already a member. Default
#                         empty (N/A); an older compose layout that wired a
#                         dedicated anchor_receipts volume names its host
#                         path here to keep archiving it.
#   CALENDAR_BACKUP_BOUNDARY
#                         how the calendar directory becomes a backup
#                         rather than a hot copy (below): stop | stopped |
#                         snapshot. Unset: a running calendar writer makes
#                         the run failed.
#   OTSD_SERVICE          the calendar's systemd unit name (default otsd)
#                         when CALENDAR_BACKUP_BOUNDARY=stop stops it; on
#                         the compose path the service is otsd.
#   OTSD_CALENDAR_SNAPSHOT_DIR
#                         with CALENDAR_BACKUP_BOUNDARY=snapshot: the
#                         operator's filesystem snapshot of the calendar
#                         directory, archived in place of the live one.
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
# Provisional, for a failure before .env is loaded; resolved again below.
STATUS_FILE="${BACKUP_STATUS_PATH:-$STATE_DIR/backup-status}"
STATUS="ok"
DETAIL=""
# The calendar writer this run stopped for the copy, to be restarted
# whatever happens after the stop ("" = none).
RESTART_CALENDAR=""

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
  [ "$STATUS" = "failed" ] || STATUS="attention"
  DETAIL="${DETAIL:+$DETAIL; }$1"
  logger -p user.warning -t backup-live-state "$1" || true
  echo "attention: $1"
}

fail_member() {
  # fail_member <error-string> — a critical member could not be captured
  # usably: the run continues (the rest of the set is still archived) but
  # its status is "failed", which /health degrades on and the monitor pushes.
  STATUS="failed"
  DETAIL="${DETAIL:+$DETAIL; }$1"
  logger -p user.err -t backup-live-state "$1" || true
  echo "failed: $1"
}

# Load the gitignored .env for the BACKUP_* knobs and OTSD_FORK_PATH through
# the shared loader (ops/lib/env.sh; .env wins over the defaults below).
# Every setting, BACKUP_STATUS_PATH included, is resolved after it.
if [ ! -f "$REPO_DIR/.env" ]; then
  mkdir -p "$STATE_DIR"
  write_status "failed" "none" "env file not found at $REPO_DIR/.env"
  echo "state: failed"
  exit 1
fi
# shellcheck source=lib/env.sh
. "$(cd "$(dirname "$0")" && pwd)/lib/env.sh"
REPO="$REPO_DIR" load_env
STATE_DIR="${STATE_DIR:-/var/lib/timestamp-gateway}"
STATUS_FILE="${BACKUP_STATUS_PATH:-$STATE_DIR/backup-status}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/gateway/timestamp-gateway-live-backups}"
OTSD_FORK_PATH="${OTSD_FORK_PATH:-/home/gateway/opentimestamps-server}"
PHOENIX_HOME="${PHOENIX_HOME:-/var/lib/phoenixd/.phoenix}"
OTSD_CALENDAR_DIR="${OTSD_CALENDAR_DIR:-/var/lib/otsd/calendar}"
# Unset-only defaults (${VAR=...}, not ${VAR:-...}): a deployment can set
# either to the empty string to declare the member not-applicable to its
# shape (skipped silently below); only a truly UNSET var falls through to the
# compose-volume default.
: "${TOR_KEYS_DIR=/var/lib/docker/volumes/timestamp-gateway_tor_keys/_data}"
: "${ANCHOR_RECEIPTS_DIR=}"
# Unset-only as well: the compose island keeps no proof artifacts dir by
# design (ARTIFACTS= declares it N/A there).
: "${ARTIFACTS=/home/gateway/timestamp-gateway-live-artifacts}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
# The obligation log, resolved once: the configured path on the host, and
# the path the gateway sees inside its container (the same setting, or the
# gateway's own default there). A configured path that is not a file on
# this host is an unresolved mapping and fails the obligation member below
# rather than falling back to a default that is not the live log.
OBLIGATIONS_DB="${OBLIGATIONS_DB_PATH:-$STATE_DIR/obligations.db}"
OBLIGATIONS_DB_IN_CONTAINER="${OBLIGATIONS_DB_PATH:-/var/lib/timestamp-gateway/obligations.db}"
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
  # Any unhandled command failure is a failed backup. A calendar writer this
  # run stopped is restarted first: the copy is never worth a stopped
  # calendar.
  restart_calendar_writer || true
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
  echo "commit: $(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "branch: $(git -C "$REPO_DIR" branch --show-current 2>/dev/null || echo unknown)"
  echo "obligations_db_path: $OBLIGATIONS_DB"
} > "$OUTDIR/metadata.txt"

(cd "$REPO_DIR" && ./ops/status.sh) > "$OUTDIR/status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/phoenixd-status.sh) > "$OUTDIR/phoenixd-status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/otsd-status.sh) > "$OUTDIR/otsd-status.txt" 2>&1 || true
(cd "$REPO_DIR" && ./ops/list-proofs.sh) > "$OUTDIR/list-proofs.txt" 2>&1 || true

docker inspect otsd > "$OUTDIR/otsd-docker-inspect.json" 2>&1 || true
systemctl --no-pager cat timestamp-gateway.service > "$OUTDIR/timestamp-gateway.service.txt" 2>&1 || true
systemctl --no-pager cat phoenixd.service > "$OUTDIR/phoenixd.service.txt" 2>&1 || true

echo "=== snapshotting obligation log ==="
# The obligation log is captured by SQLite's online backup (a .backup
# through sqlite3, or the same API through the gateway container's python
# where the host has no sqlite3 — read-locked, staged in the container's
# ephemeral /tmp, never writing production state), and the snapshot is then
# checked: integrity_check, the obligations table, and the newest obligation
# row read from the live database beforehand (the known-record check,
# ops/verify-obligations-snapshot.sh). Only a snapshot that passes is
# usable. Without one, the raw obligations.db/-wal/-shm files that the
# archive also carries are a consistent copy ONLY if no writer was running:
# a database copied before a WAL checkpoint and a WAL copied after it
# restore to a copy that has lost committed rows. So: no usable snapshot
# and the gateway running = the backup is
# "failed" (the rest of the set is still archived); no usable snapshot and
# the gateway stopped = "attention", the raw copy stands as a stopped-writer
# copy. Nothing here is ever called crash-consistent.
snapshot_via_container() {
  docker compose --project-directory "$REPO_DIR" exec -T -e "SNAPSHOT_SRC=$OBLIGATIONS_DB_IN_CONTAINER" gateway python -c "
import os, sqlite3
src = sqlite3.connect('file:%s?mode=ro' % os.environ['SNAPSHOT_SRC'], uri=True)
dst = sqlite3.connect('/tmp/obligations.db.snapshot')
src.backup(dst)
dst.close(); src.close()
" \
    && docker compose --project-directory "$REPO_DIR" exec -T gateway \
         cat /tmp/obligations.db.snapshot > "$OUTDIR/obligations.db.snapshot" \
    && docker compose --project-directory "$REPO_DIR" exec -T gateway \
         rm -f /tmp/obligations.db.snapshot
}

writers_stopped() {
  # True when no gateway process can be writing the obligation log: the
  # systemd unit is not active and no compose gateway container is running.
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet timestamp-gateway 2>/dev/null; then
    return 1
  fi
  if docker compose --project-directory "$REPO_DIR" ps --status running --services 2>/dev/null | grep -qx gateway; then
    return 1
  fi
  return 0
}

SNAPSHOT="$OUTDIR/obligations.db.snapshot"
SNAPSHOT_STATE="none"
EXPECT_HASH=""
if [ ! -f "$OBLIGATIONS_DB" ]; then
  if [ -n "${OBLIGATIONS_DB_PATH:-}" ]; then
    case "$OBLIGATIONS_DB_PATH" in
      /*) WHY="is not a file on this host" ;;
      *) WHY="is not an absolute host path" ;;
    esac
    fail_member "obligation log: OBLIGATIONS_DB_PATH=$OBLIGATIONS_DB_PATH $WHY; name the host path of the live log (on a compose layout STATE_DIR is the volume's host path and the container keeps its default), never a default beside it"
  else
    degrade "obligations.db not found at $OBLIGATIONS_DB - snapshot skipped"
  fi
else
  # The newest obligation row, read before the snapshot: what the snapshot
  # (and, after a restore, the restored database) must contain.
  EXPECT_HASH="$(python3 - "$OBLIGATIONS_DB" 2>/dev/null <<'PYQ' || true
import sqlite3, sys
conn = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
row = conn.execute("SELECT payment_hash FROM obligations ORDER BY rowid DESC LIMIT 1").fetchone()
print(row[0] if row else "")
PYQ
)"
  echo "obligations_newest_payment_hash: ${EXPECT_HASH:-none}" >> "$OUTDIR/metadata.txt"
  TAKEN=false
  if command -v sqlite3 >/dev/null 2>&1; then
    if sqlite3 "$OBLIGATIONS_DB" ".backup '$SNAPSHOT'"; then
      TAKEN=true
    else
      echo "sqlite3 snapshot failed"
    fi
  elif docker compose --project-directory "$REPO_DIR" ps gateway >/dev/null 2>&1; then
    if snapshot_via_container; then
      TAKEN=true
    else
      echo "container snapshot failed"
    fi
  else
    echo "sqlite3 not installed and no gateway container: no online snapshot possible"
  fi
  if [ "$TAKEN" = true ]; then
    if [ -n "$EXPECT_HASH" ]; then
      VERDICT="$("$REPO_DIR/ops/verify-obligations-snapshot.sh" "$SNAPSHOT" --expect-payment-hash "$EXPECT_HASH")" && SNAPSHOT_STATE="ok" || SNAPSHOT_STATE="unusable"
    else
      VERDICT="$("$REPO_DIR/ops/verify-obligations-snapshot.sh" "$SNAPSHOT")" && SNAPSHOT_STATE="ok" || SNAPSHOT_STATE="unusable"
    fi
    echo "snapshot check: $VERDICT"
    echo "obligations_snapshot_check: $VERDICT" >> "$OUTDIR/metadata.txt"
    [ "$SNAPSHOT_STATE" = "ok" ] || rm -f "$SNAPSHOT"
  fi
  if [ "$SNAPSHOT_STATE" != "ok" ]; then
    if writers_stopped; then
      degrade "obligation log: no usable online snapshot; the gateway is stopped, so the raw obligations.db copy in the archive is a stopped-writer copy (verify it after restore: ops/verify-obligations-snapshot.sh)"
    else
      fail_member "obligation log: no usable online snapshot and the gateway is running; the raw obligations.db copy in the archive is NOT a consistent snapshot (see ops/BACKUP-RECOVERY.md)"
    fi
  fi
fi

echo "=== calendar backup boundary ==="
# A copy of the calendar directory taken while otsd writes is a hot copy:
# its members were read at different moments, whatever tar reports, and
# the calendar's own contract calls only a stopped copy a backup (fork
# docs/contracts.md, section 10, R1). The calendar member therefore
# succeeds only at a boundary this run established or verified, and never
# one it inferred:
#   CALENDAR_BACKUP_BOUNDARY=stop      this run stops the calendar writer,
#                                      sees it stopped, copies, restarts it
#   CALENDAR_BACKUP_BOUNDARY=stopped   the operator stopped it before the
#                                      run; verified here, never assumed
#   CALENDAR_BACKUP_BOUNDARY=snapshot  the operator's filesystem snapshot
#                                      (OTSD_CALENDAR_SNAPSHOT_DIR) is
#                                      archived in place of the live dir
# Unset with no writer running counts as stopped. Unset with a writer
# running: the run refuses success (status failed, the reason named), and
# the hot copy is still archived as a degraded member.
CALENDAR_BACKUP_BOUNDARY="${CALENDAR_BACKUP_BOUNDARY:-}"
OTSD_SERVICE="${OTSD_SERVICE:-otsd}"
: "${OTSD_CALENDAR_SNAPSHOT_DIR=}"
CALENDAR_MEMBER="$OTSD_CALENDAR_DIR"
BOUNDARY_NOTE="none"

calendar_writer() {
  # Prints which calendar writer is running: systemd, compose, or none.
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$OTSD_SERVICE" 2>/dev/null; then
    echo systemd; return
  fi
  if docker compose --project-directory "$REPO_DIR" ps --status running --services 2>/dev/null | grep -qx otsd; then
    echo compose; return
  fi
  echo none
}

stop_calendar_writer() {
  case "$1" in
    systemd) systemctl stop "$OTSD_SERVICE" ;;
    compose) docker compose --project-directory "$REPO_DIR" stop otsd ;;
  esac
}

restart_calendar_writer() {
  # Restart the writer this run stopped, once; "" means nothing to do.
  [ -n "$RESTART_CALENDAR" ] || return 0
  local writer="$RESTART_CALENDAR"
  RESTART_CALENDAR=""
  echo "=== restarting the calendar writer ==="
  case "$writer" in
    systemd) systemctl start "$OTSD_SERVICE" ;;
    compose) docker compose --project-directory "$REPO_DIR" start otsd ;;
  esac && [ "$(calendar_writer)" = "$writer" ] || {
    fail_member "calendar: otsd ($writer) did not restart after the copy; start it by hand now"
    return 1
  }
  echo "calendar writer restarted ($writer)"
}

case "$CALENDAR_BACKUP_BOUNDARY" in
  ""|stop|stopped|snapshot) ;;
  *) echo "CALENDAR_BACKUP_BOUNDARY must be stop, stopped, snapshot or unset (got '$CALENDAR_BACKUP_BOUNDARY')" >&2; exit 1 ;;
esac
WRITER="$(calendar_writer)"
case "$CALENDAR_BACKUP_BOUNDARY" in
  "")
    if [ "$WRITER" = none ]; then
      BOUNDARY_NOTE="stopped (no calendar writer was running)"
      echo "calendar writer: none running; the copy is a stopped copy"
    else
      BOUNDARY_NOTE="none (otsd running under $WRITER; hot copy)"
      fail_member "calendar: hot copy — otsd is running under $WRITER and no boundary was set; a calendar backup needs CALENDAR_BACKUP_BOUNDARY=stop (this run stops and restarts otsd), =stopped (stop otsd before the run) or =snapshot (OTSD_CALENDAR_SNAPSHOT_DIR); the hot copy is archived but is not a backup of the calendar"
    fi ;;
  stopped)
    if [ "$WRITER" = none ]; then
      BOUNDARY_NOTE="stopped (verified: no calendar writer running)"
      echo "calendar writer: none running (boundary: stopped, verified)"
    else
      BOUNDARY_NOTE="none (otsd running under $WRITER; hot copy)"
      fail_member "calendar: CALENDAR_BACKUP_BOUNDARY=stopped but otsd is running under $WRITER; stop it before the run, or use =stop; the hot copy is archived but is not a backup of the calendar"
    fi ;;
  stop)
    if [ "$WRITER" = none ]; then
      BOUNDARY_NOTE="stopped (no calendar writer was running; nothing to stop)"
      echo "calendar writer: none running; nothing to stop"
    else
      echo "stopping the calendar writer ($WRITER) for the copy"
      RESTART_CALENDAR="$WRITER"
      if stop_calendar_writer "$WRITER" && [ "$(calendar_writer)" = none ]; then
        BOUNDARY_NOTE="stop (otsd stopped for the copy, restarted after it)"
        echo "calendar writer stopped and seen stopped"
      else
        BOUNDARY_NOTE="none (otsd did not stop; hot copy)"
        fail_member "calendar: otsd ($WRITER) did not stop for the copy; the calendar member is a hot copy and is not a backup of the calendar"
      fi
    fi ;;
  snapshot)
    if [ -n "$OTSD_CALENDAR_SNAPSHOT_DIR" ] && [ -d "$OTSD_CALENDAR_SNAPSHOT_DIR" ] && [ -e "$OTSD_CALENDAR_SNAPSHOT_DIR/journal" ]; then
      CALENDAR_MEMBER="$OTSD_CALENDAR_SNAPSHOT_DIR"
      BOUNDARY_NOTE="snapshot (operator-declared: $OTSD_CALENDAR_SNAPSHOT_DIR)"
      echo "calendar member: the declared snapshot $OTSD_CALENDAR_SNAPSHOT_DIR"
    else
      BOUNDARY_NOTE="none (no snapshot at OTSD_CALENDAR_SNAPSHOT_DIR; hot copy of the live directory)"
      fail_member "calendar: CALENDAR_BACKUP_BOUNDARY=snapshot but OTSD_CALENDAR_SNAPSHOT_DIR is unset, not a directory, or holds no journal; the live directory is archived as the hot copy it is"
    fi ;;
esac
echo "calendar_backup_boundary: $BOUNDARY_NOTE" >> "$OUTDIR/metadata.txt"

echo "=== creating sensitive archive ==="
# The state dir holds the durable obligation log (obligations.db plus its
# WAL -wal/-shm sidecars) and the operator PAUSED switch. The checked
# snapshot above is the copy to restore; the raw files travel too, and are
# a consistent copy only when they were taken with no writer running. The
# opentimestamps-server checkout (uncommitted work by design), the tor
# hidden-service keys (the onion identity — and the calendar's uri name
# where the onion is the uri), the calendar's anchor receipts where they
# live outside its directory, and
# the installed socat unit (substituted node onion) are deployment state
# that git cannot restore. Member disposition (present, absent, declared
# empty): the header above.
MEMBERS=(
  "$REPO_DIR/.env"
  "$GATEWAY_UNIT"
  "$PHOENIXD_UNIT"
  "$SOCAT_UNIT"
  "$PHOENIX_HOME"
  "$CALENDAR_MEMBER"
  "$TOR_KEYS_DIR"
  "$ANCHOR_RECEIPTS_DIR"
  "$STATE_DIR"
  "$OTSD_FORK_PATH"
  "$ARTIFACTS"
  "$OUTDIR"
)
# The configured obligation log and its WAL sidecars travel by name when
# they live outside STATE_DIR (inside it they are already members).
case "$OBLIGATIONS_DB" in
  "$STATE_DIR"/*) ;;
  *)
    for RAW in "$OBLIGATIONS_DB" "$OBLIGATIONS_DB-wal" "$OBLIGATIONS_DB-shm"; do
      [ -e "$RAW" ] && MEMBERS+=("$RAW")
    done
    ;;
esac
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
# The writer stopped for the copy goes back up as soon as the copy is on
# disk, before encryption and the push.
restart_calendar_writer || true

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
