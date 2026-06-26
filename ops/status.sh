#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
GATEWAY_URL="http://100.98.161.106:8000"

echo "=== timestamp-gateway operator status ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "host: $(hostname)"
echo "user: $(whoami)"
echo

echo "=== git ==="
echo "branch: $(git -C "$REPO" branch --show-current 2>/dev/null || echo unknown)"
echo "commit: $(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "status:"
git -C "$REPO" status --short 2>/dev/null || true
echo

echo "=== systemd: timestamp-gateway ==="
systemctl --no-pager is-active timestamp-gateway.service || true
systemctl --no-pager show timestamp-gateway.service \
  -p ExecStart \
  -p WorkingDirectory \
  -p ActiveState \
  -p SubState \
  -p Restart \
  -p User || true
echo

echo "=== gateway safety ==="
if [ -f "$REPO/.env" ]; then
  PRICE="$(grep '^GATEWAY_PRICE_SATS=' "$REPO/.env" | cut -d= -f2-)"
  MIN_PRICE="$(grep '^MIN_GATEWAY_PRICE_SATS=' "$REPO/.env" | cut -d= -f2-)"
  PAUSE_FILE="$(grep '^PAUSE_FILE=' "$REPO/.env" | cut -d= -f2-)"
  PAYMENT_BACKEND="$(grep '^PAYMENT_BACKEND_TYPE=' "$REPO/.env" | cut -d= -f2-)"
else
  PRICE=""
  MIN_PRICE=""
  PAUSE_FILE=""
  PAYMENT_BACKEND=""
fi

echo "payment_backend: ${PAYMENT_BACKEND:-unknown}"
echo "price_sats: ${PRICE:-unknown}"
echo "min_price_sats: ${MIN_PRICE:-unknown}"
echo "pause_file: ${PAUSE_FILE:-unknown}"

if [ -n "$PAUSE_FILE" ] && [ -e "$PAUSE_FILE" ]; then
  echo "paused: true"
else
  echo "paused: false"
fi

if [ -n "$PRICE" ] && [ -n "$MIN_PRICE" ] && [ "$PRICE" -ge "$MIN_PRICE" ] 2>/dev/null; then
  echo "price_floor: ok"
else
  echo "price_floor: needs_attention"
fi
echo

echo "=== gateway health ==="
curl -sS --max-time 5 "$GATEWAY_URL/health" || true
echo
echo

echo "=== phoenixd ==="
pgrep -af phoenixd || echo "phoenixd: not running"
echo

echo "=== otsd docker ==="
docker ps --filter name=otsd --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
echo

echo "=== latest artifacts ==="
ls -lt "$ARTIFACTS" 2>/dev/null | head -10 || true
