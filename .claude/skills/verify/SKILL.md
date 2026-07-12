---
name: verify
description: Drive the timestamp-gateway end to end locally with stub backends (bitcoind RPC, phoenixd, OTS calendar) — no VPS, no real Lightning or Bitcoin node needed.
---

# Verify timestamp-gateway locally

The gateway's surface is HTTP on `/timestamp` (L402 402-challenge → pay → redeem `.ots`).
It needs three backends, all stubable on localhost:

- **phoenixd** (payments): POST `/createinvoice` (form-encoded, returns `serialized` +
  `paymentHash`), GET `/payments/incoming/{hash}` (returns `isPaid`/`receivedSat`/`description`).
  Pick a fixed preimage, return sha256(preimage) as paymentHash, and you can redeem.
- **OTS calendar**: POST `/digest` with raw digest bytes → serialized `Timestamp` built with
  the venv's own opentimestamps lib (`Timestamp(digest)` + `PendingAttestation`,
  `BytesSerializationContext`). The gateway then serves a real deserializable `.ots`.
- **bitcoind JSON-RPC** (pricing floor only): POST `/` estimatesmartfee →
  `{"result": {"feerate": <BTC/kvB>, "blocks": n}}`; fresh-node shape is
  `{"errors": [...], "blocks": 0}` with no feerate key.

A working three-stub implementation pattern lives in the session that added the pricing
floor (stubs.py: ThreadingHTTPServer x3 on 18401-18403, modes switched via small files).

## Launch

- A real `.env` exists at the repo root. Run uvicorn with **cwd outside the repo** and
  `--app-dir /Users/operator/timestamp-gateway` so `load_dotenv()` finds nothing,
  and pass config via `env -i ... VAR=...` explicitly.
- Minimum env: `GATEWAY_PRICE_SATS`, `OTS_BACKEND_MODE=calendar`, `OTS_CALENDAR_URL`,
  `L402_SECRET_HEX` (64 hex), `PAYMENT_BACKEND_TYPE=phoenixd`, `PHOENIXD_URL`,
  `OBLIGATIONS_DB_PATH` (writable path — default is /var/lib and fails locally),
  `PAUSE_FILE` (nonexistent path = unpaused). `PRICE_RPC_URL` to enable the floor.
- `.venv/bin/python -m uvicorn main:app --app-dir <repo> --port 18400`

## Drive

1. `POST /timestamp {"digest": "<64 hex>"}` → 402; body has `price_sats`, `invoice`,
   `macaroon`, `expiry`; `WWW-Authenticate: L402 macaroon="...", invoice="..."`.
2. Mark the stub invoice paid at ≥ the quoted amount, then
   `POST /timestamp` with `Authorization: L402 <macaroon>:<preimage>` → 200 `.ots`.
   Validate with `DetachedTimestampFile.deserialize(BytesDeserializationContext(body))`.
3. Restart the gateway between pricing-floor scenarios — the feerate cache TTL is 60s
   and there is no way to flush it from outside.

## Gotchas

- Underpaid redemption is checked BEFORE the proof cache, so re-probing the same
  payment_hash after a success still exercises payment verification.
- Config validation fires at import: bad `PRICE_*`/`GATEWAY_*` values make uvicorn exit
  with the RuntimeError in the last lines of output — that IS the fail-loud surface.
- Pricing-floor warnings go to root logging (uvicorn stderr); grep "Pricing floor".
  Logs must never contain the RPC URL or its credentials.
