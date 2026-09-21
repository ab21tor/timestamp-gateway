#!/usr/bin/env bash
# Proof sweep: upgrade every proof.ots under the artifacts directory against
# the local calendar and write one JSON status line for /health.
# Run unattended by timestamp-gateway-upgrade-proofs.timer (ops/systemd/).
#
# Requires GNU find (-printf); BSD/macOS find has no -printf and is refused
# up front as needs_attention rather than read as "no proofs".
#
# A scan that cannot see the artifacts is a failure, never an ok-looking
# empty result: a traversal failure (find exits nonzero — an unreadable
# subtree, a missing tool) is status "attention", state needs_attention and
# exit 1, distinguished from a clean scan of a directory that holds no
# proofs (total 0, status ok, "no proofs"). find's exit status must not
# vanish in a process substitution with its stderr discarded, or a failed
# scan would produce status ok, total 0, scan_complete.
#
# Every setting is resolved AFTER .env is loaded (ops/lib/env.sh).
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"
# shellcheck source=lib/env.sh
. "$(cd "$(dirname "$0")" && pwd)/lib/env.sh"
load_env
ARTIFACTS="${ARTIFACTS:-/home/gateway/timestamp-gateway-live-artifacts}"
UPGRADE="$REPO/ops/upgrade-proof.sh"
STATUS_FILE="${PROOFS_STATUS_PATH:-/var/lib/timestamp-gateway/proofs-status}"

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

fail_scan() {
  # fail_scan <error-string> — the sweep could not run: status attention
  # with the reason, state needs_attention, exit 1. Fixed-format strings.
  write_status "{\"total\": 0, \"bitcoin_backed\": 0, \"waiting_for_bitcoin\": 0, \"attestation_mismatch\": 0, \"needs_attention\": 0, \"status\": \"attention\", \"checked_at\": $(date +%s), \"error\": \"$1\"}"
  logger -p user.warning -t upgrade-all-proofs "proof scan failed: $1"
  echo "state: needs_attention"
  echo "message: $1"
  exit 1
}

if [ ! -x "$UPGRADE" ]; then
  fail_scan "upgrade-proof.sh not found or not executable"
fi
if [ ! -d "$ARTIFACTS" ]; then
  fail_scan "artifacts directory missing"
fi
if ! find --version >/dev/null 2>&1; then
  fail_scan "GNU find required (find -printf)"
fi

echo "=== upgrade all proofs ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

# The file list is captured whole and its exit status checked BEFORE any
# proof is processed: a partial listing from a failed traversal must never
# be reported as the set of proofs.
LIST="$(mktemp "${TMPDIR:-/tmp}/upgrade-all-proofs.XXXXXX")" || fail_scan "cannot create temp file"
trap 'rm -f "$LIST"' EXIT
if ! find "$ARTIFACTS" -maxdepth 2 -name proof.ots -printf '%T@ %p\n' > "$LIST" 2>/dev/null; then
  fail_scan "proof scan failed (find exit nonzero: unreadable subtree or tool error)"
fi
SORTED="$(sort -nr "$LIST")" || fail_scan "proof scan failed (sort)"

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
done <<EOF2
$SORTED
EOF2

if [ "$MISMATCH" -gt 0 ]; then
  STATUS="mismatch"
elif [ "$ATTENTION" -gt 0 ]; then
  STATUS="attention"
else
  STATUS="ok"
fi

write_status "{\"total\": $TOTAL, \"bitcoin_backed\": $BACKED, \"waiting_for_bitcoin\": $WAITING, \"attestation_mismatch\": $MISMATCH, \"needs_attention\": $ATTENTION, \"status\": \"$STATUS\", \"checked_at\": $(date +%s)}"

echo
[ "$TOTAL" -eq 0 ] && echo "message: no proofs under $ARTIFACTS (scan complete, nothing to upgrade)"
echo "status: $STATUS"
echo "state: scan_complete"
