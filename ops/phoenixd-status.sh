#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
SERVICE="${PHOENIXD_SERVICE:-phoenixd.service}"
PHOENIX_HOME="${PHOENIX_HOME:-/home/gateway/phoenixd/home/.phoenix}"
PHOENIX_URL="${PHOENIXD_URL:-http://127.0.0.1:9740}"

echo "=== phoenixd status ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "=== plain state ==="

ACTIVE="$(systemctl --no-pager is-active "$SERVICE" 2>/dev/null || true)"
ENABLED="$(systemctl --no-pager is-enabled "$SERVICE" 2>/dev/null || true)"

if [ "$ACTIVE" = "active" ]; then
  echo "service: running"
else
  echo "service: needs_attention"
fi

echo "enabled_on_boot: ${ENABLED:-unknown}"

if pgrep -af phoenixd >/dev/null 2>&1; then
  echo "process: running"
else
  echo "process: needs_attention"
fi

if ss -ltnp 2>/dev/null | grep -q '127.0.0.1:9740'; then
  echo "api: local_only"
else
  echo "api: needs_attention"
fi
echo

echo "=== systemd ==="
systemctl --no-pager show "$SERVICE" \
  -p ActiveState \
  -p SubState \
  -p ExecStart \
  -p WorkingDirectory \
  -p User \
  -p Restart \
  2>/dev/null || true
echo

echo "=== process ==="
# Count only — pgrep -af would echo phoenixd's argv (paths, flags) into
# status output that lands in backup snapshots and journald.
PROC_COUNT="$(pgrep -fc phoenixd 2>/dev/null || true)"
echo "phoenixd_processes: ${PROC_COUNT:-0}"
echo

echo "=== api ==="
if [ -f "$REPO/.env" ]; then
  PASSWORD="$(grep '^PHOENIXD_HTTP_PASSWORD_LIMITED=' "$REPO/.env" | cut -d= -f2-)"
  # Pre-rename alias, same fallback the gateway applies.
  if [ -z "$PASSWORD" ]; then
    PASSWORD="$(grep '^PHOENIXD_HTTP_PASSWORD=' "$REPO/.env" | cut -d= -f2-)"
  fi
else
  PASSWORD=""
fi

if [ -n "$PASSWORD" ]; then
  # Password via curl stdin-config — never argv (ps-visible). Same pattern as
  # notify.sh / wallet-balance-check.sh; curl stderr discarded because a
  # config parse error can echo the config (password) back.
  curl -sS --max-time 10 --config - 2>/dev/null <<EOF || true
url = "$PHOENIX_URL/getinfo"
user = ":$PASSWORD"
EOF
  echo
else
  echo "state: needs_attention"
  echo "message: PHOENIXD_HTTP_PASSWORD_LIMITED (or legacy PHOENIXD_HTTP_PASSWORD) not found in $REPO/.env"
fi
echo

echo "=== important files ==="
if [ -d "$PHOENIX_HOME" ]; then
  echo "phoenix_home: $PHOENIX_HOME"
  for f in \
    "$PHOENIX_HOME/phoenix.conf" \
    "$PHOENIX_HOME/seed.dat" \
    "$PHOENIX_HOME"/phoenix.mainnet.*.db \
    "$PHOENIX_HOME"/phoenix.mainnet.*.db-wal \
    "$PHOENIX_HOME"/phoenix.mainnet.*.db-shm \
    "$PHOENIX_HOME/phoenix.log"
  do
    [ -e "$f" ] && ls -l "$f"
  done
else
  echo "state: needs_attention"
  echo "message: phoenix home not found: $PHOENIX_HOME"
fi
