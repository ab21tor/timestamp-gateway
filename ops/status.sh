#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
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

echo "=== network fees (operator node) ==="
# estimatesmartfee via the operator's own node — no third-party fee API in
# the ops path. The RPC URL (with credentials) comes from the gitignored
# .env and goes to curl via stdin config (never argv); stderr is discarded
# because it can echo the URL back (wallet-balance-check.sh pattern).
# PRICE_RPC_URL preferred, BITCOIN_RPC_SERVICE_URL fallback — mirroring the
# gateway's own pricing-floor lookup. Anchor vsize comes from
# PRICE_TX_VSIZE_ESTIMATE so status, quote floor, and docs share one number.
FEE_RPC_URL=""
CONF_TARGET="6"
VSIZE="150"
if [ -f "$REPO/.env" ]; then
  FEE_RPC_URL="$(grep '^PRICE_RPC_URL=' "$REPO/.env" | cut -d= -f2-)"
  if [ -z "$FEE_RPC_URL" ]; then
    FEE_RPC_URL="$(grep '^BITCOIN_RPC_SERVICE_URL=' "$REPO/.env" | cut -d= -f2-)"
  fi
  CT="$(grep '^PRICE_CONF_TARGET=' "$REPO/.env" | cut -d= -f2-)"
  [ -n "$CT" ] && CONF_TARGET="$CT"
  VS="$(grep '^PRICE_TX_VSIZE_ESTIMATE=' "$REPO/.env" | cut -d= -f2-)"
  [ -n "$VS" ] && VSIZE="$VS"
fi
FEERATE_SAT_VB=""
if [ -n "$FEE_RPC_URL" ]; then
  RESPONSE="$(curl -sS --max-time 10 \
    -H 'Content-Type: text/plain' \
    --data-binary "{\"jsonrpc\":\"1.0\",\"id\":\"status-fees\",\"method\":\"estimatesmartfee\",\"params\":[$CONF_TARGET]}" \
    --config - 2>/dev/null <<EOF
url = "$FEE_RPC_URL"
EOF
)" || RESPONSE=""
  FEERATE_SAT_VB="$(printf '%s' "$RESPONSE" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    rate = data["result"]["feerate"]  # BTC/kvB
    print(round(rate * 100_000_000 / 1000, 1))
except Exception:
    pass
' 2>/dev/null)"
fi
if [ -n "$FEERATE_SAT_VB" ]; then
  echo "feerate: $FEERATE_SAT_VB sat/vB (estimatesmartfee, conf_target $CONF_TARGET)"
  python3 -c "print(f'est_anchor_tx: ~{round($FEERATE_SAT_VB * $VSIZE)} sats ($FEERATE_SAT_VB sat/vB x $VSIZE vB)')"
else
  echo "fees: unavailable (no RPC URL in .env, node unreachable, or no estimate)"
fi
echo

echo "=== anchor economics ==="
BATCH_SIZE=$(awk 'NR > 1 && $1=="waiting_for_bitcoin" {count++} END {print (count ? count : 1)}' "$ARTIFACTS/proofs.tsv" 2>/dev/null || echo 1)
if [ -n "$FEERATE_SAT_VB" ] && [ -n "${PRICE:-}" ]; then
  python3 - "$FEERATE_SAT_VB" "$VSIZE" "$BATCH_SIZE" "${PRICE:-500}" 2>/dev/null <<'PY'
import sys
rate, vsize, batch, price = float(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
anchor = rate * vsize
cost = anchor / batch if batch > 0 else anchor
margin = price - cost
pct = (margin / price * 100) if price > 0 else 0
print(f'current_batch_size:      {batch} proofs (waiting_for_bitcoin)')
print(f'anchor_tx_fee:           ~{anchor:.0f} sats ({rate} sat/vB x {vsize} vB)')
print(f'anchor_cost_per_proof:   ~{cost:.1f} sats (tx_fee / batch_size)')
print(f'proof_price:             {price} sats')
print(f'margin_per_proof:        ~{margin:.1f} sats ({pct:.1f}%)')
print(f'batch_revenue:           ~{price * batch:.0f} sats ({batch} proofs x {price} sats)')
PY
else
  echo "economics: unavailable"
fi
echo

echo "=== proof ledger ==="
TSV="$ARTIFACTS/proofs.tsv"
if [ -f "$TSV" ]; then
  awk 'NR > 1 {print $1}' "$TSV" | sort | uniq -c | awk '{print $2": "$1}'
else
  echo "ledger: not found"
fi
