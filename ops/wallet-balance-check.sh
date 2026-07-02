#!/usr/bin/env bash
# Wallet liquidity alarm: read the otsd-hot wallet balance over Bitcoin
# JSON-RPC and write a one-line JSON status file for /health to surface.
# Run unattended by wallet-balance-check.timer (see ops/systemd/).
#
# The gateway process never talks to Bitcoin RPC and never holds wallet
# credentials — it only reads the file this script writes (same file-mediated
# pattern as the PAUSED switch). The RPC URL (with credentials) is sourced
# from the gitignored .env, passed to curl via stdin config (never argv,
# never echoed, never logged).
set -u
umask 077

REPO="${REPO:-/home/gateway/timestamp-gateway}"
STATUS_FILE="${WALLET_STATUS_PATH:-/var/lib/timestamp-gateway/wallet-status}"
MIN_SATS="${WALLET_MIN_SATS:-50000}"

write_status() {
  # write_status <json-line> — atomic: tmp file in the same directory + mv.
  local tmp
  tmp="$(mktemp "${STATUS_FILE}.XXXXXX")" || {
    logger -p user.warning -t wallet-balance-check "cannot write status file at $STATUS_FILE"
    exit 1
  }
  printf '%s\n' "$1" > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

fail_unknown() {
  # fail_unknown <error-string> — RPC/config failure is exactly what this
  # alarm must surface: write status "unknown", never silently skip the write.
  # Error strings are fixed-format and never contain the RPC URL/credentials.
  write_status "{\"balance_sats\": null, \"min_sats\": $MIN_SATS, \"status\": \"unknown\", \"checked_at\": $(date +%s), \"error\": \"$1\"}"
  logger -p user.warning -t wallet-balance-check "wallet status unknown: $1"
  echo "state: unknown"
  echo "error: $1"
  exit 0
}

# Source the gitignored .env for BITCOIN_RPC_SERVICE_URL (used whole — no
# parsing). Also re-resolve WALLET_MIN_SATS so it can be set in .env.
[ -f "$REPO/.env" ] || fail_unknown "env file not found"
set -a
# shellcheck disable=SC1091
. "$REPO/.env"
set +a
MIN_SATS="${WALLET_MIN_SATS:-$MIN_SATS}"
case "$MIN_SATS" in
  ''|*[!0-9]*) echo "WALLET_MIN_SATS must be a non-negative integer" >&2; exit 1 ;;
esac
[ -n "${BITCOIN_RPC_SERVICE_URL:-}" ] || fail_unknown "BITCOIN_RPC_SERVICE_URL not set"

# getbalances over JSON-RPC. The URL goes to curl via stdin config so it
# never appears in argv (ps) or output; curl stderr is discarded because it
# can echo the URL back.
RESPONSE="$(curl -sS --max-time 15 \
  -H 'Content-Type: text/plain' \
  --data-binary '{"jsonrpc":"1.0","id":"wallet-balance-check","method":"getbalances","params":[]}' \
  --config - 2>/dev/null <<EOF
url = "$BITCOIN_RPC_SERVICE_URL"
EOF
)" || fail_unknown "rpc call failed (curl exit $?)"

# Read result.mine.trusted (BTC, decimal) and convert to sats.
BALANCE_SATS="$(printf '%s' "$RESPONSE" | python3 -c '
import json, sys
data = json.load(sys.stdin)
if data.get("error") is not None:
    sys.exit(1)
btc = data["result"]["mine"]["trusted"]
print(round(float(btc) * 100_000_000))
' 2>/dev/null)" || fail_unknown "rpc error or unexpected response"
case "$BALANCE_SATS" in
  ''|*[!0-9]*) fail_unknown "rpc error or unexpected response" ;;
esac

if [ "$BALANCE_SATS" -lt "$MIN_SATS" ]; then
  STATUS="low"
  logger -p user.warning -t wallet-balance-check \
    "otsd wallet low: ${BALANCE_SATS} sats < ${MIN_SATS} sats minimum — anchoring will stop when empty"
else
  STATUS="ok"
fi

write_status "{\"balance_sats\": $BALANCE_SATS, \"min_sats\": $MIN_SATS, \"status\": \"$STATUS\", \"checked_at\": $(date +%s)}"

echo "state: $STATUS"
echo "balance_sats: $BALANCE_SATS"
echo "min_sats: $MIN_SATS"
