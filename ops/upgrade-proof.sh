#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
CALENDAR_URL="${CALENDAR_URL:-http://127.0.0.1:14788}"
OTS="$REPO/.venv/bin/ots"

PROOF="${1:-}"

if [ -z "$PROOF" ]; then
  PROOF="$(find /home/gateway/timestamp-gateway-live-artifacts -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
fi

if [ -z "$PROOF" ]; then
  echo "state: needs_attention"
  echo "message: no proof found"
  exit 1
fi

if [ -d "$PROOF" ]; then
  PROOF="$PROOF/proof.ots"
fi

if [ ! -f "$PROOF" ]; then
  echo "state: needs_attention"
  echo "message: proof file not found"
  echo "proof: $PROOF"
  exit 1
fi

INFO="$("$OTS" info "$PROOF" 2>&1)"

if echo "$INFO" | grep -q "BitcoinBlockHeaderAttestation"; then
  BLOCK="$(echo "$INFO" | sed -n 's/.*BitcoinBlockHeaderAttestation(\([0-9][0-9]*\)).*/\1/p' | tail -1)"
  TXID="$(echo "$INFO" | sed -n 's/^# Transaction id //p' | tail -1)"
  echo "state: bitcoin_backed"
  echo "proof: $PROOF"
  [ -n "$BLOCK" ] && echo "bitcoin_block: $BLOCK"
  [ -n "$TXID" ] && echo "bitcoin_txid: $TXID"
  exit 0
fi

UPGRADE="$("$OTS" upgrade -c "$CALENDAR_URL" "$PROOF" 2>&1)"
UPGRADE_EXIT=$?

if [ "$UPGRADE_EXIT" -eq 0 ] && echo "$UPGRADE" | grep -qi "Success! Timestamp complete"; then
  INFO2="$("$OTS" info "$PROOF" 2>&1)"
  BLOCK="$(echo "$INFO2" | sed -n 's/.*BitcoinBlockHeaderAttestation(\([0-9][0-9]*\)).*/\1/p' | tail -1)"
  TXID="$(echo "$INFO2" | sed -n 's/^# Transaction id //p' | tail -1)"
  echo "state: bitcoin_backed"
  echo "message: proof upgraded"
  echo "proof: $PROOF"
  [ -n "$BLOCK" ] && echo "bitcoin_block: $BLOCK"
  [ -n "$TXID" ] && echo "bitcoin_txid: $TXID"
  exit 0
fi

if echo "$UPGRADE" | grep -qi "Pending confirmation"; then
  echo "state: waiting_for_bitcoin"
  echo "proof: $PROOF"
  echo "message: local calendar has not finished Bitcoin anchoring yet"
  exit 0
fi

if echo "$INFO" | grep -q "PendingAttestation"; then
  echo "state: waiting_for_bitcoin"
  echo "proof: $PROOF"
  echo "message: receipt issued, Bitcoin proof not ready yet"
  exit 0
fi

echo "state: needs_attention"
echo "proof: $PROOF"
echo "message: upgrade did not complete and state is unclear"
echo
echo "$UPGRADE"
exit 2
