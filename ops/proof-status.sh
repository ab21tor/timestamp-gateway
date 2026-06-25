#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
CALENDAR_URL="${CALENDAR_URL:-http://127.0.0.1:14788}"
OTS="$REPO/.venv/bin/ots"

ARTIFACT="${1:-}"

if [ -z "$ARTIFACT" ]; then
  ARTIFACT="$(find /home/gateway/timestamp-gateway-live-artifacts -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
fi

if [ -z "$ARTIFACT" ]; then
  echo "status: error"
  echo "reason: no proof.ots found"
  exit 1
fi

if [ -d "$ARTIFACT" ]; then
  ARTIFACT="$ARTIFACT/proof.ots"
fi

if [ ! -f "$ARTIFACT" ]; then
  echo "status: error"
  echo "reason: proof file not found: $ARTIFACT"
  exit 1
fi

if [ ! -x "$OTS" ]; then
  echo "status: error"
  echo "reason: ots CLI not found or not executable: $OTS"
  exit 1
fi

echo "=== proof status ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "proof: $ARTIFACT"
echo "calendar: $CALENDAR_URL"
echo

echo "=== file ==="
ls -l "$ARTIFACT"
file "$ARTIFACT"
echo

INFO="$("$OTS" info "$ARTIFACT" 2>&1)"
echo "=== ots info ==="
echo "$INFO"
echo

DRYRUN="$("$OTS" upgrade -n -c "$CALENDAR_URL" "$ARTIFACT" 2>&1)"
DRYRUN_EXIT=$?

echo "=== upgrade dry-run ==="
echo "$DRYRUN"
echo

echo "=== classification ==="
if echo "$INFO" | grep -q "BitcoinBlockHeaderAttestation"; then
  echo "status: bitcoin_upgraded"
  exit 0
fi

if [ "$DRYRUN_EXIT" -eq 0 ] && echo "$DRYRUN" | grep -qi "Success! Timestamp complete"; then
  echo "status: upgrade_available"
  exit 0
fi

if echo "$DRYRUN" | grep -qi "Pending confirmation"; then
  echo "status: pending_bitcoin_confirmation"
  exit 0
fi

if echo "$INFO" | grep -q "PendingAttestation"; then
  echo "status: pending_calendar_attestation"
  exit 0
fi

echo "status: unknown_or_error"
exit 2
