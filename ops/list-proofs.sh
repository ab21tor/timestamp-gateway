#!/usr/bin/env bash
# Read-only: classifies from `ots info` alone and never touches the artifacts.
# BACKUP-RECOVERY.md and backup-live-state.sh call this during backup/restore
# checks — a check must not mutate what it is checking.
set -u

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
OTS="$REPO/.venv/bin/ots"

if [ ! -x "$OTS" ]; then
  echo "state: needs_attention"
  echo "message: ots CLI not found or not executable: $OTS"
  exit 1
fi

echo "=== proofs ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

find "$ARTIFACTS" -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr \
  | while read -r _ proof; do
      dir="$(dirname "$proof")"
      name="$(basename "$dir")"

      info="$("$OTS" info "$proof" 2>&1)"
      block="$(echo "$info" | sed -n 's/.*BitcoinBlockHeaderAttestation(\([0-9][0-9]*\)).*/\1/p' | tail -1)"

      if [ -n "$block" ]; then
        echo "bitcoin_backed  block=$block  $name"
      elif echo "$info" | grep -q "PendingAttestation"; then
        echo "waiting_for_bitcoin  $name"
      else
        echo "needs_attention  $name"
      fi
    done
