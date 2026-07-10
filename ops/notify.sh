#!/usr/bin/env bash
# Alarm delivery seam: reads a message on stdin and sends it via the transport
# configured in .env. ntfy today; to swap transports (nostr, email, ...)
# replace this file — nothing else in the repo knows the transport exists.
#
# NTFY_URL is a capability: anyone holding the topic URL can read alarms and
# post to the topic. It lives only in the gitignored .env and is passed to
# curl via stdin config (never argv, never echoed, never logged).
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"

fail() {
  # fail <error-string> — an alarm that cannot be delivered is a failure, not
  # silence: log it and exit non-zero so callers know delivery did not happen.
  # Error strings are fixed-format and never contain NTFY_URL.
  logger -p user.warning -t notify "$1"
  echo "state: needs_attention"
  echo "message: $1"
  exit 1
}

MSG="$(cat)"
[ -n "$MSG" ] || fail "empty message on stdin; nothing to deliver"

# Source the gitignored .env for NTFY_URL (used whole — no parsing).
[ -f "$REPO/.env" ] || fail "env file not found"
set -a
# shellcheck disable=SC1091
. "$REPO/.env"
set +a
[ -n "${NTFY_URL:-}" ] || fail "NTFY_URL not set; alarm channel unconfigured"

# POST the message to the ntfy topic. -f makes an HTTP error a delivery
# failure; curl stderr is discarded because it can echo the URL back.
RESPONSE="$(curl -fsS --max-time 15 --data-binary "$MSG" --config - 2>/dev/null <<EOF
url = "$NTFY_URL"
EOF
)" || fail "ntfy delivery failed (curl exit $?)"

echo "state: sent"
