#!/usr/bin/env bash
# Wallet liquidity alarm: read the otsd-hot wallet balance over Bitcoin
# JSON-RPC and write a one-line JSON status file for /health to surface.
# Run unattended by wallet-balance-check.timer (see ops/systemd/).
#
# The gateway process never talks to Bitcoin RPC and never holds wallet
# credentials — it only reads the file this script writes (same file-mediated
# pattern as the PAUSED switch).
#
# The credential this script uses is WALLET_RPC_URL: a Bitcoin Core RPC user
# of its own, restricted to getbalances (bitcoin.conf: rpcauth for the user
# plus `rpcwhitelist=<user>:getbalances`; operator guide, "Wallet liquidity
# alarm"). It lives in its own file, /etc/systemd/system/wallet-balance-
# check.env by default (WALLET_RPC_ENV_FILE), owned by the unit's user,
# mode 600 — never in the gateway's .env, which is the gateway service's
# EnvironmentFile. The otsd wallet credential (BITCOIN_RPC_SERVICE_URL,
# otsd.env) is read as a fallback only when WALLET_RPC_URL is unset: that is
# the compose island's shape, where every process is root and .env is the
# one configuration file. Either URL goes to curl via stdin config — never
# argv, never echoed, never logged.
#
# Every setting is resolved AFTER .env is loaded (ops/lib/env.sh): the
# 2026-09-15 review found WALLET_STATUS_PATH captured before sourcing, so a
# path set in .env was ignored and /health read a file nothing wrote.
set -u
umask 077

REPO="${REPO:-/home/gateway/timestamp-gateway}"
# shellcheck source=lib/env.sh
. "$(cd "$(dirname "$0")" && pwd)/lib/env.sh"
OPS_EXTRA_ENV_FILES="${WALLET_RPC_ENV_FILE:-/etc/systemd/system/wallet-balance-check.env}" load_env

STATUS_FILE="${WALLET_STATUS_PATH:-/var/lib/timestamp-gateway/wallet-status}"
MIN_SATS="${WALLET_MIN_SATS:-50000}"
RPC_URL="${WALLET_RPC_URL:-${BITCOIN_RPC_SERVICE_URL:-}}"

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

case "$MIN_SATS" in
  ''|*[!0-9]*) echo "WALLET_MIN_SATS must be a non-negative integer" >&2; exit 1 ;;
esac
[ -n "$ENV_LOADED" ] || fail_unknown "no configuration file readable"
[ -n "$RPC_URL" ] || fail_unknown "WALLET_RPC_URL not set"

# getbalances over JSON-RPC. The URL goes to curl via stdin config so it
# never appears in argv (ps) or output; curl stderr is discarded because it
# can echo the URL back.
RESPONSE="$(curl -sS --max-time 15 \
  -H 'Content-Type: text/plain' \
  --data-binary '{"jsonrpc":"1.0","id":"wallet-balance-check","method":"getbalances","params":[]}' \
  --config - 2>/dev/null <<EOF2
url = "$RPC_URL"
EOF2
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
echo "status_file: $STATUS_FILE"
