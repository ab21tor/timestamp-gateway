#!/usr/bin/env bash
set -u

CONTAINER="${OTSD_CONTAINER:-otsd}"
CALENDAR_HOST="${OTSD_CALENDAR_HOST:-/var/lib/otsd/calendar}"

echo "=== otsd status ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "container: $CONTAINER"
echo

echo "=== docker ==="
if ! docker ps --filter "name=$CONTAINER" --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "state: needs_attention"
  echo "message: otsd container is not running"
  exit 1
fi

docker ps --filter "name=$CONTAINER" --format 'name={{.Names}} status={{.Status}}'
echo

echo "=== command ==="
docker inspect "$CONTAINER" \
  --format 'image={{.Config.Image}}
cmd={{json .Config.Cmd}}
working_dir={{.Config.WorkingDir}}
network={{.HostConfig.NetworkMode}}'
echo

echo "=== plain policy ==="
CMD="$(docker inspect "$CONTAINER" --format '{{json .Config.Cmd}}' 2>/dev/null || true)"

if echo "$CMD" | grep -q -- '--btc-conf-target","2'; then
  echo "bitcoin_fee_target: about_2_blocks"
else
  echo "bitcoin_fee_target: check_command"
fi

if echo "$CMD" | grep -q -- '--btc-min-tx-interval'; then
  INTERVAL="$(echo "$CMD" | sed -n 's/.*--btc-min-tx-interval","\([^"]*\)".*/\1/p')"
  echo "min_anchor_interval_seconds: ${INTERVAL:-unknown}"
else
  echo "min_anchor_interval_seconds: 21600"
  echo "min_anchor_interval: 6_hours_default"
fi

if echo "$CMD" | grep -q -- '--btc-min-confirmations'; then
  CONFS="$(echo "$CMD" | sed -n 's/.*--btc-min-confirmations","\([^"]*\)".*/\1/p')"
  echo "confirmations_before_saved: ${CONFS:-unknown}"
else
  echo "confirmations_before_saved: 6_default"
fi
echo

echo "=== calendar files ==="
if [ -d "$CALENDAR_HOST" ]; then
  echo "calendar_host_path: $CALENDAR_HOST"
  find "$CALENDAR_HOST" -maxdepth 2 -type f -printf '%TY-%Tm-%TdT%TH:%TM:%TSZ %s %p\n' 2>/dev/null | sort | tail -20
else
  echo "state: needs_attention"
  echo "message: calendar path not found: $CALENDAR_HOST"
fi
echo

echo "=== recent useful logs ==="
docker logs --tail 300 "$CONTAINER" 2>&1 \
  | grep -Ei 'commit|pending|tx|transaction|broadcast|confirm|bitcoin|fee|timestamp|No pending commitments' \
  | tail -40 || true
