#!/usr/bin/env bash
set -euo pipefail

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
INDEX="$ARTIFACTS/proofs.tsv"
TMP="$INDEX.tmp"
OTS="$REPO/.venv/bin/ots"

echo -e "state\tblock\ttxid\tdigest\tproof_path\tartifact\tupdated_utc" > "$TMP"

find "$ARTIFACTS" -maxdepth 2 -name proof.ots -printf '%T@ %p\n' 2>/dev/null \
  | sort -nr \
  | while read -r _ proof; do
      dir="$(dirname "$proof")"
      artifact="$(basename "$dir")"
      updated_utc="$(date -u -r "$proof" +%Y-%m-%dT%H:%M:%SZ)"

      info="$("$OTS" info "$proof" 2>&1 || true)"

      digest="$(echo "$info" | sed -n 's/^File sha256 hash: //p' | head -1)"
      block="$(echo "$info" | sed -n 's/.*BitcoinBlockHeaderAttestation(\([0-9][0-9]*\)).*/\1/p' | tail -1)"
      txid="$(echo "$info" | sed -n 's/^# Transaction id //p' | tail -1)"

      if [ -n "$block" ]; then
        state="bitcoin_backed"
      elif echo "$info" | grep -q "PendingAttestation"; then
        state="waiting_for_bitcoin"
      else
        state="needs_attention"
      fi

      echo -e "${state}\t${block:-}\t${txid:-}\t${digest:-}\t${proof}\t${artifact}\t${updated_utc}" >> "$TMP"
    done

PROOF_COUNT=$(grep -c "proof.ots" "$TMP" || true)
if [ "$PROOF_COUNT" -eq 0 ] && [ -f "$INDEX" ]; then
  echo "state: needs_attention"
  echo "message: no proofs found, preserving existing index"
  rm -f "$TMP"
  exit 1
fi

mv "$TMP" "$INDEX"
chmod 600 "$INDEX"

echo "state: index_written"
echo "index: $INDEX"
echo
cat "$INDEX"
