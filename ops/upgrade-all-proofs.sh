#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
UPGRADE="$REPO/ops/upgrade-proof.sh"

if [ ! -x "$UPGRADE" ]; then
  echo "state: needs_attention"
  echo "message: upgrade-proof.sh not found or not executable"
  exit 1
fi

echo "=== upgrade all proofs ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

FOUND=0
BACKED=0
WAITING=0
ATTENTION=0

find "$ARTIFACTS" -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr \
  | while read -r _ proof; do
      FOUND=1
      dir="$(dirname "$proof")"
      name="$(basename "$dir")"

      out="$("$UPGRADE" "$proof" 2>&1)"
      state="$(echo "$out" | awk -F': ' '/^state: / {print $2; exit}')"
      block="$(echo "$out" | awk -F': ' '/^bitcoin_block: / {print $2; exit}')"

      [ -z "$state" ] && state="needs_attention"

      if [ -n "$block" ]; then
        echo "$state  block=$block  $name"
      else
        echo "$state  $name"
      fi
    done

echo
echo "state: scan_complete"
