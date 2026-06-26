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

echo "=== network fees ==="
FEES=$(curl -sS --max-time 5 https://mempool.space/api/v1/fees/recommended 2>/dev/null || echo "")
if [ -n "$FEES" ]; then
  echo "$FEES" | python3 -c "
import sys, json
d = json.load(sys.stdin)
fastest = d.get('fastestFee', '?')
half    = d.get('halfHourFee', '?')
hour    = d.get('hourFee', '?')
economy = d.get('economyFee', '?')
print(f'fastest_fee:   {fastest} sat/vB')
print(f'half_hour_fee: {half} sat/vB')
print(f'hour_fee:      {hour} sat/vB')
print(f'economy_fee:   {economy} sat/vB')
est = hour * 256
print(f'est_anchor_tx: ~{est} sats (hour_fee x 256 vB)')
" 2>/dev/null
else
  echo "fees: unavailable"
fi
echo

echo "=== anchor economics ==="
BATCH_SIZE=$(awk 'NR > 1 && $1=="waiting_for_bitcoin" {count++} END {print (count ? count : 1)}' "$ARTIFACTS/proofs.tsv" 2>/dev/null || echo 1)
if [ -n "$FEES" ] && [ -n "${PRICE:-}" ]; then
  echo "$FEES" | python3 -c "
import sys, json
d = json.load(sys.stdin)
hour    = d.get('hourFee', 0)
fastest = d.get('fastestFee', 0)
anchor_tx_sats = hour * 256
batch = $BATCH_SIZE
cost_per_proof = anchor_tx_sats / batch if batch > 0 else anchor_tx_sats
price = ${PRICE:-500}
margin = price - cost_per_proof
pct = (margin / price * 100) if price > 0 else 0
print(f'current_batch_size:      {batch} proofs (waiting_for_bitcoin)')
print(f'anchor_tx_fee:           ~{anchor_tx_sats:.0f} sats ({hour} sat/vB x 256 vB)')
print(f'anchor_cost_per_proof:   ~{cost_per_proof:.1f} sats (tx_fee / batch_size)')
print(f'proof_price:             {price} sats')
print(f'margin_per_proof:        ~{margin:.1f} sats ({pct:.1f}%)')
print(f'batch_revenue:           ~{price * batch:.0f} sats ({batch} proofs x {price} sats)')
print()

" 2>/dev/null
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
