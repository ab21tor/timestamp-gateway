#!/usr/bin/env bash
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"
# Every setting is resolved AFTER .env is loaded through the shared loader
# (ops/lib/env.sh; .env wins over an older value in the environment). The
# 2026-09-15 review's F23 found this script probing 127.0.0.1:9740 while
# .env named the docker0 bind, and reporting the configured listener absent.
# shellcheck source=lib/env.sh
. "$(cd "$(dirname "$0")" && pwd)/lib/env.sh"
load_env
SERVICE="${PHOENIXD_SERVICE:-phoenixd.service}"
# The wallet home is the phoenixd user's, not the gateway's (review F06).
PHOENIX_HOME="${PHOENIX_HOME:-/var/lib/phoenixd/.phoenix}"
PHOENIX_URL="${PHOENIXD_URL:-http://127.0.0.1:9740}"
# The process is matched by exact name (pgrep -x): a substring match on the
# full argv counted this script itself, and anything else mentioning
# "phoenixd", as a running daemon (full review D9, 2026-09-08).
PHOENIXD_PROC="${PHOENIXD_PROC:-phoenixd}"
# The listener check looks for the host:port PHOENIXD_URL names, not a
# hard-wired loopback address: the shipped unit binds the docker0 bridge.
PHOENIX_BIND="${PHOENIX_URL#*://}"
PHOENIX_BIND="${PHOENIX_BIND%%/*}"

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

if pgrep -x "$PHOENIXD_PROC" >/dev/null 2>&1; then
  echo "process: running"
else
  echo "process: needs_attention"
fi

if ss -ltn 2>/dev/null | grep -Fq " $PHOENIX_BIND "; then
  echo "api: listening ($PHOENIX_BIND)"
else
  echo "api: needs_attention (nothing listening on $PHOENIX_BIND)"
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
# Count only — listing argv would echo phoenixd's paths and flags into
# status output that lands in backup snapshots and journald.
PROC_COUNT="$(pgrep -xc "$PHOENIXD_PROC" 2>/dev/null || true)"
echo "phoenixd_processes: ${PROC_COUNT:-0}"
echo

echo "=== api ==="
# From the loaded .env; the pre-rename alias is the same fallback the
# gateway applies.
PASSWORD="${PHOENIXD_HTTP_PASSWORD_LIMITED:-${PHOENIXD_HTTP_PASSWORD:-}}"

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

echo "=== state files ==="
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
