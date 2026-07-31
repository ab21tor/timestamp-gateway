#!/usr/bin/env bash
# Out-of-process alarm poller: curls /health and pushes a concise message
# through ops/notify.sh (the transport seam) whenever the gateway is not ok —
# degraded, paused, or unreachable — and once more when it recovers.
#
# Debounce: the state file holds the last condition actually DELIVERED plus
# the delivery time. A push happens on condition change (including recovery
# back to ok) or, for a persisting problem, after HEALTH_REALERT_SECONDS.
# If notify.sh fails the state file is left unwritten, so the next run
# retries — the debounce can never swallow an undelivered alarm.
# Run unattended by health-monitor.timer (see ops/systemd/).
set -u

REPO="${REPO:-/home/gateway/timestamp-gateway}"
NOTIFY="$REPO/ops/notify.sh"

if [ ! -x "$NOTIFY" ]; then
  echo "state: needs_attention"
  echo "message: notify.sh not found or not executable"
  exit 1
fi

# Source the gitignored .env (optional here — notify.sh enforces its own
# requirements), then resolve settings.
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env"
  set +a
fi
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
STATE_FILE="${HEALTH_MONITOR_STATE_PATH:-/var/lib/timestamp-gateway/health-monitor-state}"
REALERT="${HEALTH_REALERT_SECONDS:-14400}"
case "$REALERT" in
  ''|*[!0-9]*) echo "HEALTH_REALERT_SECONDS must be a non-negative integer" >&2; exit 1 ;;
esac

NOW="$(date +%s)"

# Poll /health. No -f: a 503 body is data (the degraded details), not a
# transport error. An unreachable gateway is itself the alarm. 60s budget:
# /health's otsd probe can honestly take ~50s through a Tor stall, and a
# shorter timeout misreads honest-slow as UNREACHABLE.
BODY="$(curl -sS --max-time 60 "$HEALTH_URL" 2>/dev/null)"
CURL_EXIT=$?

if [ "$CURL_EXIT" -ne 0 ] || [ -z "$BODY" ]; then
  FP="unreachable"
  MSG="timestamp-gateway UNREACHABLE: no response from /health (curl exit $CURL_EXIT)"
else
  FP="$(printf '%s' "$BODY" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    print(" ".join("%s=%s" % (k, d.get(k)) for k in ("status", "paused", "payment", "otsd", "wallet", "float", "proofs", "backup", "billing")))
except Exception:
    print("unparseable")
')"
  MSG="timestamp-gateway NOT OK: $FP"
fi

STATUS="${FP#status=}"
STATUS="${STATUS%% *}"

LAST_FP=""
LAST_TS=0
if [ -f "$STATE_FILE" ]; then
  LAST_FP="$(sed -n 1p "$STATE_FILE")"
  LAST_TS="$(sed -n 2p "$STATE_FILE")"
  case "$LAST_TS" in ''|*[!0-9]*) LAST_TS=0 ;; esac
fi

write_state() {
  # Atomic: tmp file in the same directory + mv. Called ONLY after a push was
  # delivered (or when there was nothing to deliver) — never after a failed one.
  local tmp
  tmp="$(mktemp "${STATE_FILE}.XXXXXX")" || {
    logger -p user.warning -t health-monitor "cannot write state file at $STATE_FILE"
    exit 1
  }
  printf '%s\n%s\n' "$FP" "$NOW" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

push() {
  # push <message> — deliver or die: on failure the state file stays as it
  # was, so the next timer run retries this same alarm.
  if ! printf '%s' "$1" | REPO="$REPO" "$NOTIFY" >/dev/null; then
    logger -p user.warning -t health-monitor "alarm not delivered; state unchanged, next run retries"
    echo "state: needs_attention"
    echo "message: alarm not delivered: $1"
    exit 1
  fi
}

if [ "$FP" != "$LAST_FP" ]; then
  if [ "$STATUS" = "ok" ]; then
    case "$LAST_FP" in
      "")           ;;  # first ever run: nothing to recover from
      "status=ok"*) ;;  # ok -> ok detail shuffle (e.g. wallet absent -> ok)
      *)
        push "timestamp-gateway RECOVERED: all ok"
        echo "state: recovery_sent"
        ;;
    esac
    write_state
    echo "state: ok"
    exit 0
  fi
  push "$MSG"
  write_state
  echo "state: alarm_sent"
  echo "condition: $FP"
  exit 0
fi

if [ "$STATUS" != "ok" ] && [ $((NOW - LAST_TS)) -ge "$REALERT" ]; then
  push "$MSG (persisting past re-alert cooldown)"
  write_state
  echo "state: realert_sent"
  echo "condition: $FP"
  exit 0
fi

echo "state: quiet"
echo "condition: $FP"
