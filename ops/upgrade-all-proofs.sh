#!/usr/bin/env bash
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"
ARTIFACTS="${ARTIFACTS:-/home/gateway/timestamp-gateway-live-artifacts}"
UPGRADE="$REPO/ops/upgrade-proof.sh"
STATUS_FILE="${PROOFS_STATUS_PATH:-/var/lib/timestamp-gateway/proofs-status}"

if [ ! -x "$UPGRADE" ]; then
  echo "state: needs_attention"
  echo "message: upgrade-proof.sh not found or not executable"
  exit 1
fi

write_status() {
  # write_status <json-line> — atomic: tmp file in the same directory + mv
  # (same file-mediated pattern as ops/wallet-balance-check.sh).
  local tmp
  tmp="$(mktemp "${STATUS_FILE}.XXXXXX")" || {
    logger -p user.warning -t upgrade-all-proofs "cannot write status file at $STATUS_FILE"
    exit 1
  }
  printf '%s\n' "$1" > "$tmp"
  mv "$tmp" "$STATUS_FILE"
}

# A scan that cannot see the artifacts must surface as a failure, never as an
# ok-looking empty result (same rule as fail_unknown in wallet-balance-check.sh).
if [ ! -d "$ARTIFACTS" ]; then
  write_status "{\"total\": 0, \"bitcoin_backed\": 0, \"waiting_for_bitcoin\": 0, \"attestation_mismatch\": 0, \"needs_attention\": 0, \"status\": \"attention\", \"checked_at\": $(date +%s), \"error\": \"artifacts directory missing\"}"
  echo "state: needs_attention"
  echo "message: artifacts directory missing: $ARTIFACTS"
  exit 1
fi

echo "=== upgrade all proofs ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

TOTAL=0
BACKED=0
WAITING=0
MISMATCH=0
ATTENTION=0

while read -r _ proof; do
  [ -z "$proof" ] && continue
  dir="$(dirname "$proof")"
  name="$(basename "$dir")"

  out="$("$UPGRADE" "$proof" 2>&1)"
  state="$(echo "$out" | awk -F': ' '/^state: / {print $2; exit}')"
  block="$(echo "$out" | awk -F': ' '/^bitcoin_block: / {print $2; exit}')"

  [ -z "$state" ] && state="needs_attention"

  TOTAL=$((TOTAL + 1))
  case "$state" in
    bitcoin_backed)       BACKED=$((BACKED + 1)) ;;
    waiting_for_bitcoin)  WAITING=$((WAITING + 1)) ;;
    attestation_mismatch) MISMATCH=$((MISMATCH + 1)) ;;
    *)                    ATTENTION=$((ATTENTION + 1)) ;;
  esac

  if [ -n "$block" ]; then
    echo "$state  block=$block  $name"
  else
    echo "$state  $name"
  fi
done < <(find "$ARTIFACTS" -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null | sort -nr)

if [ "$MISMATCH" -gt 0 ]; then
  STATUS="mismatch"
elif [ "$ATTENTION" -gt 0 ]; then
  STATUS="attention"
else
  STATUS="ok"
fi

write_status "{\"total\": $TOTAL, \"bitcoin_backed\": $BACKED, \"waiting_for_bitcoin\": $WAITING, \"attestation_mismatch\": $MISMATCH, \"needs_attention\": $ATTENTION, \"status\": \"$STATUS\", \"checked_at\": $(date +%s)}"

echo
echo "status: $STATUS"
echo "state: scan_complete"
