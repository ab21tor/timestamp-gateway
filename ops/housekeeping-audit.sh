#!/usr/bin/env bash
set -euo pipefail

export GIT_PAGER=cat
export PAGER=cat

ROOT="/home/gateway/timestamp-gateway"
ART="/home/gateway/timestamp-gateway-live-artifacts"
BACKUPS="/home/gateway/timestamp-gateway-live-backups"

cd "$ROOT"

echo "=== repo ==="
echo "branch: $(git branch --show-current)"
echo "commit: $(git rev-parse HEAD)"
echo "status:"
git status --short

echo
echo "=== tracked files ==="
git ls-files | sort

echo
echo "=== tracked secret-risk scan ==="
git grep -n -I -E 'password|passwd|secret|token|macaroon|preimage|invoice|seed|mnemonic|private|apikey|api_key|bearer|authorization' -- \
  . \
  ':!.env.example' \
  ':!.gitignore' \
  ':!README.md' \
  ':!docs/*' \
  ':!ops/*.md' \
  ':!LIVE_PROOF.md' \
  ':!test_main.py' \
  ':!static/index.html' \
  ':!ops/l402-paid-proof.sh' \
  ':!ops/phoenixd-status.sh' \
  ':!requirements.txt' || true

echo
echo "=== env permissions and keys only ==="
ls -l .env || true
if [ -f .env ]; then
  sed -n 's/^\([A-Za-z0-9_][A-Za-z0-9_]*\)=.*/\1=<set>/p' .env | sort
fi

echo
echo "=== syntax ==="
python3 -m py_compile main.py
find . -maxdepth 3 -name '*.py' -not -path './.venv/*' -print0 | xargs -0 python3 -m py_compile

echo
echo "=== tests ==="
./.venv/bin/python -m pytest -q test_main.py

echo
echo "=== live health ==="
curl -sS http://100.98.161.106:8000/health
echo

echo
echo "=== systemd ==="
systemctl is-active timestamp-gateway.service
systemctl is-active timestamp-gateway-upgrade-proofs.timer >/dev/null && echo "timestamp-gateway-upgrade-proofs.timer active"

echo
echo "=== docker otsd ==="
docker ps --filter name=otsd

echo
echo "=== artifact permission drift ==="
if [ -d "$ART" ]; then
  find "$ART" -type d ! -perm 700 -printf '%M %p\n'
  find "$ART" -type f ! -perm 600 -printf '%M %p\n'
fi

echo
echo "=== backup permission drift ==="
if [ -d "$BACKUPS" ]; then
  find "$BACKUPS" -type d ! -perm 700 -printf '%M %p\n'
  find "$BACKUPS" -type f ! -perm 600 -printf '%M %p\n'
fi

echo
echo "=== proof counts ==="
if [ -f "$ART/proofs.tsv" ]; then
  awk 'NR > 1 {print $1}' "$ART/proofs.tsv" | sort | uniq -c
fi

echo
echo "=== operator status ==="
./ops/status.sh

echo
echo "state: housekeeping_audit_complete"
