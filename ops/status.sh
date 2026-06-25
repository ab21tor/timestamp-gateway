#!/usr/bin/env bash
set -u

REPO="/home/gateway/timestamp-gateway"
ARTIFACTS="/home/gateway/timestamp-gateway-live-artifacts"
GATEWAY_URL="http://100.98.161.106:8000"

echo "=== timestamp-gateway operator status ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "host: $(hostname)"
echo "user: $(whoami)"
echo

echo "=== git ==="
echo "branch: $(git -C "$REPO" branch --show-current 2>/dev/null || echo unknown)"
echo "commit: $(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "status:"
git -C "$REPO" status --short 2>/dev/null || true
echo

echo "=== systemd: timestamp-gateway ==="
systemctl --no-pager is-active timestamp-gateway.service || true
systemctl --no-pager show timestamp-gateway.service \
  -p ExecStart \
  -p WorkingDirectory \
  -p ActiveState \
  -p SubState \
  -p Restart \
  -p User || true
echo

echo "=== gateway health ==="
curl -sS --max-time 5 "$GATEWAY_URL/health" || true
echo
echo

echo "=== phoenixd ==="
pgrep -af phoenixd || echo "phoenixd: not running"
echo

echo "=== otsd docker ==="
docker ps --filter name=otsd --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
echo

echo "=== latest artifacts ==="
ls -lt "$ARTIFACTS" 2>/dev/null | head -10 || true
