#!/usr/bin/env bash
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"
ARTIFACTS="${ARTIFACTS:-/home/gateway/timestamp-gateway-live-artifacts}"
# set GATEWAY_URL in .env (e.g. your Tailscale IP)
GATEWAY_URL="${GATEWAY_URL:-$(grep "^GATEWAY_URL=" "$REPO/.env" 2>/dev/null | cut -d= -f2- || echo "http://127.0.0.1:8000")}"

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
  PRICE="$(grep '^PRICE_PER_PROOF_SATS=' "$REPO/.env" | cut -d= -f2-)"
  PAUSE_FILE="$(grep '^PAUSE_FILE=' "$REPO/.env" | cut -d= -f2-)"
  PAYMENT_BACKEND="$(grep '^PAYMENT_BACKEND_TYPE=' "$REPO/.env" | cut -d= -f2-)"
else
  PRICE=""
  PAUSE_FILE=""
  PAYMENT_BACKEND=""
fi

echo "payment_backend: ${PAYMENT_BACKEND:-unknown}"
echo "price_per_proof_sats: ${PRICE:-needs_attention}"
echo "pause_file: ${PAUSE_FILE:-unknown}"

if [ -n "$PAUSE_FILE" ] && [ -e "$PAUSE_FILE" ]; then
  echo "paused: true"
else
  echo "paused: false"
fi
echo

echo "=== gateway health ==="
curl -sS --max-time 5 "$GATEWAY_URL/health" || true
echo
echo

echo "=== phoenixd ==="
# Count only — pgrep -af would echo phoenixd's argv (paths, flags) into
# status output, and status.sh output lands in backup snapshots
# (phoenixd-status.sh pattern).
if pgrep -fc phoenixd >/dev/null 2>&1; then
  echo "phoenixd: running"
else
  echo "phoenixd: not running"
fi
echo

echo "=== otsd docker ==="
docker ps --filter name=otsd --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
echo

echo "=== latest artifacts ==="
ls -lt "$ARTIFACTS" 2>/dev/null | head -10 || true

echo "=== anchoring cadence ==="
WAITING=$(docker logs otsd 2>/dev/null | grep "Waiting" | tail -1 | grep -o '[0-9]*' | head -1 || echo "")
if [ -n "$WAITING" ]; then
  HOURS=$(( WAITING / 3600 ))
  MINS=$(( (WAITING % 3600) / 60 ))
  SECS=$(( WAITING % 60 ))
  echo "next_anchor_in: ${WAITING}s (${HOURS}h ${MINS}m ${SECS}s)"
else
  echo "next_anchor_in: unavailable"
fi
BTC_TARGET=$(docker inspect otsd 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
args = d[0].get('Args', [])
for i, a in enumerate(args):
    if a == '--btc-conf-target' and i+1 < len(args):
        print(args[i+1])
        break
" 2>/dev/null || echo "unavailable")
echo "btc_conf_target: ${BTC_TARGET} blocks"
echo

# No network-fee or margin sections here: under flat pricing the anchor fee
# cap is otsd's (otsd-status.sh), and billing/float state is in the /health
# JSON printed under "gateway health" above.

echo "=== proof ledger ==="
TSV="$ARTIFACTS/proofs.tsv"
if [ -f "$TSV" ]; then
  awk 'NR > 1 {print $1}' "$TSV" | sort | uniq -c | awk '{print $2": "$1}'
else
  echo "ledger: not found"
fi
