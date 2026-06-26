#!/usr/bin/env bash
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://100.98.161.106:8000}"
ENDPOINT="${ENDPOINT:-/timestamp}"
ARTIFACTS="${ARTIFACTS:-/home/gateway/timestamp-gateway-live-artifacts}"
OTS="${OTS:-/home/gateway/timestamp-gateway/.venv/bin/ots}"
FEE_LIMIT_SATS="${FEE_LIMIT_SATS:-10}"
PAY_TIMEOUT="${PAY_TIMEOUT:-60s}"

TS="$(date -u +%Y%m%dT%H%M%SZ)"
ART="$ARTIFACTS/${TS}-l402-paid-proof"
mkdir -p "$ART"
chmod 700 "$ART"

DIGEST="${DIGEST:-$(printf 'live-l402-paid-proof-%s' "$TS" | sha256sum | awk '{print $1}')}"
echo "$DIGEST" > "$ART/digest.txt"

echo "=== l402 paid proof ==="
echo "time_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "artifact: $ART"
echo "digest: $DIGEST"
echo

echo "=== request L402 challenge ==="
HTTP_CODE="$(
  curl -sS \
    -D "$ART/challenge.headers" \
    -o "$ART/challenge.json" \
    -w "%{http_code}" \
    -X POST \
    "$GATEWAY_URL$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"digest\":\"$DIGEST\"}"
)"

echo "challenge_http_code: $HTTP_CODE"

if [ "$HTTP_CODE" != "402" ]; then
  echo "state: needs_attention"
  echo "message: expected 402 challenge"
  echo "challenge_body:"
  cat "$ART/challenge.json" || true
  exit 1
fi

python3 - "$ART/challenge.json" "$ART/invoice.txt" "$ART/macaroon.txt" <<'PY'
import json, sys

body_path, invoice_path, macaroon_path = sys.argv[1:]
data = json.load(open(body_path))
detail = data.get("detail", {})

invoice = detail.get("invoice")
macaroon = detail.get("macaroon")

if not invoice or not macaroon:
    raise SystemExit("missing invoice or macaroon in challenge body")

open(invoice_path, "w").write(invoice + "\n")
open(macaroon_path, "w").write(macaroon + "\n")

print("invoice_saved:", invoice_path)
print("macaroon_saved:", macaroon_path)
PY

chmod 600 "$ART/invoice.txt" "$ART/macaroon.txt" "$ART/challenge.json" "$ART/challenge.headers" 2>/dev/null || true

INVOICE="$(cat "$ART/invoice.txt")"
MACAROON="$(cat "$ART/macaroon.txt")"

echo
echo "=== pay invoice with local LND ==="
set +e
lncli payinvoice \
  --force \
  --json \
  --fee_limit "$FEE_LIMIT_SATS" \
  --timeout "$PAY_TIMEOUT" \
  "$INVOICE" \
  > "$ART/lncli-pay.json" \
  2> "$ART/lncli-pay.stderr"
PAY_RC=$?
set -e

echo "lncli_pay_exit: $PAY_RC"
chmod 600 "$ART/lncli-pay.json" "$ART/lncli-pay.stderr" 2>/dev/null || true

if [ "$PAY_RC" -ne 0 ]; then
  echo "state: payment_failed"
  echo "stderr:"
  cat "$ART/lncli-pay.stderr"
  echo
  echo "stdout:"
  cat "$ART/lncli-pay.json"
  exit 1
fi

python3 - "$ART/lncli-pay.json" "$ART/preimage.txt" <<'PY'
import json, re, sys

pay_path, preimage_path = sys.argv[1:]
raw = open(pay_path).read()

def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and re.fullmatch(r"[0-9a-fA-F]{64}", v):
                if "preimage" in k.lower():
                    return v.lower()
            found = walk(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = walk(item)
            if found:
                return found
    return None

preimage = None

# lncli --json may emit one JSON object or multiple JSON objects line-by-line.
for line in raw.splitlines():
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    preimage = walk(obj)
    if preimage:
        break

if preimage is None:
    try:
        obj = json.loads(raw)
        preimage = walk(obj)
    except Exception:
        pass

if preimage is None:
    m = re.search(r'"[^"]*preimage[^"]*"\s*:\s*"([0-9a-fA-F]{64})"', raw, re.I)
    if m:
        preimage = m.group(1).lower()

if preimage is None:
    raise SystemExit("could not extract 64-hex payment preimage from lncli output")

open(preimage_path, "w").write(preimage + "\n")
print("preimage_saved:", preimage_path)
PY

chmod 600 "$ART/preimage.txt"
PREIMAGE="$(cat "$ART/preimage.txt")"

echo
echo "=== retry with L402 Authorization ==="
HTTP_CODE="$(
  curl -sS \
    -D "$ART/proof.headers" \
    -o "$ART/proof.ots" \
    -w "%{http_code}" \
    -X POST \
    "$GATEWAY_URL$ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "Authorization: L402 ${MACAROON}:${PREIMAGE}" \
    -d "{\"digest\":\"$DIGEST\"}"
)"

echo "proof_http_code: $HTTP_CODE"
chmod 644 "$ART/proof.ots" "$ART/proof.headers" 2>/dev/null || true

if [ "$HTTP_CODE" != "200" ]; then
  echo "state: proof_failed"
  echo "headers:"
  sed -n '1,80p' "$ART/proof.headers" || true
  echo
  echo "body:"
  cat "$ART/proof.ots" || true
  exit 1
fi

echo
echo "=== proof file ==="
ls -l "$ART/proof.ots"
wc -c "$ART/proof.ots"

echo
echo "=== ots info ==="
"$OTS" info "$ART/proof.ots" | tee "$ART/ots-info.txt"

echo
echo "=== refresh proof index ==="
/home/gateway/timestamp-gateway/ops/rebuild-proof-index.sh >/dev/null
tail -n 3 /home/gateway/timestamp-gateway-live-artifacts/proofs.tsv

echo
echo "=== artifact files ==="
find "$ART" -maxdepth 1 -type f -printf '%M %s %f\n' | sort

echo
echo "state: l402_paid_proof_complete"
echo "artifact: $ART"
