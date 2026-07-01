#!/usr/bin/env bash
set -euo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-/home/gateway/timestamp-gateway-live-backups}"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUTDIR="$BACKUP_ROOT/$TS"
ARCHIVE="$BACKUP_ROOT/$TS-live-state.tar.gz"

mkdir -p "$OUTDIR"
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
  echo "repo: /home/gateway/timestamp-gateway"
  echo "commit: $(git -C /home/gateway/timestamp-gateway rev-parse HEAD)"
  echo "branch: $(git -C /home/gateway/timestamp-gateway branch --show-current)"
} > "$OUTDIR/metadata.txt"

./ops/status.sh > "$OUTDIR/status.txt" 2>&1 || true
./ops/phoenixd-status.sh > "$OUTDIR/phoenixd-status.txt" 2>&1 || true
./ops/otsd-status.sh > "$OUTDIR/otsd-status.txt" 2>&1 || true
./ops/list-proofs.sh > "$OUTDIR/list-proofs.txt" 2>&1 || true

docker inspect otsd > "$OUTDIR/otsd-docker-inspect.json" 2>&1 || true
systemctl --no-pager cat timestamp-gateway.service > "$OUTDIR/timestamp-gateway.service.txt" 2>&1 || true
systemctl --no-pager cat phoenixd.service > "$OUTDIR/phoenixd.service.txt" 2>&1 || true

echo "=== creating sensitive archive ==="
# /var/lib/timestamp-gateway holds the durable obligation log
# (obligations.db plus its WAL -wal/-shm sidecars) and the operator PAUSED
# switch. Archiving the whole directory captures the database and both sidecar
# files together, so a settled-but-unstamped obligation survives a rebuild.
sudo tar -czf "$ARCHIVE" \
  /home/gateway/timestamp-gateway/.env \
  /etc/systemd/system/timestamp-gateway.service \
  /etc/systemd/system/phoenixd.service \
  /home/gateway/phoenixd/home/.phoenix \
  /var/lib/otsd/calendar \
  /var/lib/timestamp-gateway \
  /home/gateway/timestamp-gateway-live-artifacts \
  "$OUTDIR" \
  2>"$OUTDIR/tar-warnings.txt"

sudo chown gateway:gateway "$ARCHIVE"
chmod 600 "$ARCHIVE"
chmod 600 "$OUTDIR"/* 2>/dev/null || true

echo
echo "=== backup complete ==="
ls -lh "$ARCHIVE"
echo
echo "state: backup_created"
