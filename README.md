# timestamp-gateway

timestamp-gateway is portable paid OpenTimestamps calendar-node software. It accepts a SHA-256 digest, charges a configured Lightning price, submits the paid digest to the operator's own OpenTimestamps calendar backend, and returns a raw .ots proof. It stores no files, requires no accounts, and does not need to be trusted after the proof is returned.

This is not a hosted service. It is software for running a Lightning-gated OpenTimestamps calendar node.

```
client
  → Lightning-gated gateway       (this repo)
  → operator-controlled OTS calendar  (otsd — the proof engine)
  → Bitcoin anchoring
  → .ots
```

Tor is supported, but not mandatory. VPS is supported, but not mandatory. Tor-only is possible, but not imposed.

---

## What the gateway does and does not do

**Does:**
- Validates SHA-256 digests.
- Issues a Lightning invoice via the operator's payment backend — Phoenixd (live default), or LND as a test payer / alternative.
- Verifies payment by preimage: checks that the invoice is settled, the memo matches the digest, and the paid amount meets the configured price.
- Submits the paid digest to the operator-controlled OTS calendar backend.
- Returns raw `.ots` bytes.

**Does not:**
- Store files or documents.
- Log digests, preimages, or client identities — there is no access log (see [Privacy trade-offs](#privacy-trade-offs)).
- Prove authorship, ownership, provenance, or claim validity.
- Prove truth.
- Anchor to Bitcoin itself — that is the OTS calendar backend's job.
- Provide a public calendar — the gateway is the paid front door to the operator's private calendar.

**Proves when.** A digest committed before a Bitcoin block existed at that time.

---

## What we corrected

An earlier version of this gateway forwarded paid digests to the public OpenTimestamps aggregators (`a.pool.opentimestamps.org`, etc.). That made the gateway a paid relay to other operators' infrastructure, not an independent calendar node.

The correct architecture is:

| Component | Role |
|---|---|
| Gateway | Paid front door. Validates, charges, verifies, forwards. |
| otsd | Operator-controlled proof engine. Aggregates digests, anchors to Bitcoin. |
| Phoenixd | Live Lightning payment backend (default). Issues and settles invoices. LND is supported as a test payer / alternative. |
| Bitcoin Core | Operator-provided (or shared) Bitcoin backend for otsd. |

Public calendar mode (`OTS_BACKEND_MODE=public`) is retained as a compatibility/testing option only. It is not the real target.

---

## What you need

- Git — the Quick start clones two repositories with it
- Docker with Compose v2 (version floor, install route, and why: operator guide, "Prerequisites"). Compose v2 ships two ways — the `docker compose` plugin or a standalone `docker-compose` binary. Detect which this box has, and read `docker compose` in every command in these docs as `$C`:

  ```bash
  docker compose version >/dev/null 2>&1 && C="docker compose" || C="docker-compose"
  ```
- A running phoenixd instance (the live payment backend) — or an LND node with REST API and invoice macaroon if using the LND test-payer / alternative backend
- An OpenTimestamps calendar backend (otsd) — bundled in the Compose stack or external
- An **existing, already-synced** Bitcoin Core node reachable by otsd, with a wallet loaded and funded for anchoring transactions. This is the heaviest prerequisite, and these docs do not teach it: building a node from nothing is a multi-day project — on the order of 750 GB of initial-block-download ingress, days of sync time, and real money to fund the wallet. If you do not already run a node, start at [bitcoincore.org](https://bitcoincore.org) and come back.
- Inbound Lightning liquidity on the payment backend node (see [Inbound liquidity](#inbound-liquidity))

### What a first paid stamp costs

Budget estimates for going from zero to one working paid stamp — sats only (fiat figures rot):

| Item | Estimate (sats) |
|---|---|
| Anchoring wallet (`otsd-hot`) | 50,000–100,000 |
| phoenixd pre-fund payment (operator guide, "Payment backend (phoenixd)") | ~25,000–30,000 |
| End-to-end test payment | ~5,000 |
| Payer-side overhead (funding the second wallet, routing fees) | ~5,000–10,000 |
| **Total** | **≈ 90,000–140,000 sats, plus the VPS** |

This is the full cost of a first working paid stamp, not an ongoing rate — the pre-fund and wallet balances keep working for you afterwards.

---

## Quick start

```bash
# Two repos: the gateway, and the calendar code it mounts into otsd.
git clone https://github.com/ab21tor/timestamp-gateway
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server
cd timestamp-gateway
cp .env.example .env
# Edit .env — set L402_SECRET_HEX (required, the gateway refuses to start
# without it; generate with:
#   python3 -c 'import secrets; print(secrets.token_hex(32))'
# ), PHOENIXD_HTTP_PASSWORD_LIMITED (live default backend;
# LND_* only if using the lnd test payer / alternative backend) and
# BITCOIN_RPC_SERVICE_URL for otsd (see .env.example for the three shapes).

# First run only: create the calendar's three identity files — otsd refuses
# to start without them. Replace both example values with your own (the
# address below is the BIP173 example, not yours). What they mean: operator
# guide, "Deploying the calendar (otsd)".
docker compose --profile calendar run --rm otsd sh -c \
  'echo "https://calendar.example.com/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key \
   && echo "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" > /calendar/donation_addr'

# First run: --build. After that, plain `docker compose --profile calendar
# up -d` is enough — otsd code updates come from the fork checkout, not the
# image (operator guide, "Updating").
docker compose --profile calendar up -d --build
```

Get your onion address:

```bash
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname
```

Test the endpoint:

```bash
DIGEST=a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef

curl -i -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\"}"
```

A working gateway returns HTTP 402 with an L402 challenge in the
`WWW-Authenticate` header: `L402 macaroon="<token>", invoice="<bolt11>"`.
Pay the invoice, then retry with the macaroon and the payment preimage:

```bash
curl -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -d "{\"digest\":\"$DIGEST\"}" \
  -o proof.ots
```

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAYMENT_BACKEND_TYPE` | No | `phoenixd` | `phoenixd` (live backend) or `lnd` (test payer / alternative only) |
| `PHOENIXD_URL` | No | `http://127.0.0.1:9740` | phoenixd HTTP API endpoint (live default backend); from the compose stack use `http://host.docker.internal:9740` — which reaches a loopback-bound phoenixd on Docker Desktop only; on a Linux engine see the operator guide, "Payment backend (phoenixd)" |
| `PHOENIXD_HTTP_PASSWORD_LIMITED` | When `phoenixd` | — | phoenixd API password: `http-password-limited-access` from `phoenix.conf` — never the full `http-password` (scope and why: operator guide, "Payment backend (phoenixd)"). Old name `PHOENIXD_HTTP_PASSWORD` read as a fallback. |
| `LND_HOST` | When `lnd` | — | Hostname, IP, or `.onion` address of your LND REST API (test payer / alternative) |
| `LND_PORT` | When `lnd` | — | LND REST port, typically `8080` (test payer / alternative) |
| `LND_MACAROON_HEX` | When `lnd` | — | Hex-encoded invoice macaroon (test payer / alternative) |
| `GATEWAY_PRICE_SATS` | Yes | — | Satoshis charged per timestamp |
| `PRICE_BLIND_SATS` | No | `5000` | Quote floor when no feerate is available (RPC unset, down, or no estimate): the 402 quotes max(`GATEWAY_PRICE_SATS`, this). Rationale and the full floor model: operator guide, "Pricing". |
| `RATE_LIMIT_PER_MINUTE` | No | `10` | Per-IP cap per minute on unauthenticated invoice minting (402 challenges). Every anonymous request makes phoenixd sign and store an invoice; this bounds what a spammer gets for free. `0` disables. Over-limit requests get 429 with Retry-After. |
| `VERIFY_RATE_LIMIT_PER_MINUTE` | No | `30` | Per-IP cap per minute on the free proof endpoints — one bucket shared by `/verify` and `/upgrade`. Separate from the mint limit so proof polling can never starve the paid path. `0` disables. |
| `L402_SECRET_HEX` | Yes | — | L402 macaroon root signing key (hex, at least 16 bytes; 32 recommended). Generate with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. The gateway refuses to start without it (dev-only escape: `L402_ALLOW_EPHEMERAL_SECRET=true`). |
| `OTS_BACKEND_MODE` | Yes | — | `calendar` (real mode) or `public` (compatibility/testing only) |
| `OTS_CALENDAR_URL` | When `calendar` | — | URL of the operator-controlled otsd instance (`http://otsd:14788` for the bundled compose profile; `http://127.0.0.1:14788` on the systemd path) |
| `TOR_PROXY` | No | — | SOCKS5h proxy for LND connections (lnd test payer / alternative only). Required if `LND_HOST` is `.onion`. |
| `LND_TLS_VERIFY` | No | `false` | Set `true` only for CA-signed LND TLS certs (lnd test payer / alternative only). |

`OTS_BACKEND_MODE` validation:
- `calendar` requires `OTS_CALENDAR_URL` to be set. Gateway fails to start if missing.
- `public` requires `OTS_CALENDAR_URL` to be absent. Gateway fails to start if both are set.
- Any other value fails at startup.
- There is no silent fallback between modes.

---

## OTS backend modes

### calendar — operator-controlled calendar (real mode)

```
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

The gateway forwards paid digests to the operator's own otsd instance. otsd aggregates submissions and anchors the aggregate root in Bitcoin when commitments are pending, batched on `--btc-min-tx-interval` (default and cost model: operator guide, "Bitcoin transaction cost"). This is the intended production mode.

If the calendar backend fails, the gateway returns generic 502. It does not retry against public calendars.

### public — compatibility/testing mode only

```
OTS_BACKEND_MODE=public
# OTS_CALENDAR_URL must NOT be set
```

The gateway forwards paid digests to the public OpenTimestamps aggregators. This mode is provided for testing without a running otsd. It is not the real target and must not be used in production as a substitute for running your own calendar node.

---

## Deployment modes

With the live default backend (Phoenixd on localhost) no `LND_*` vars or payment Tor proxy are needed; the `LND_HOST`/`TOR_PROXY` lines below apply only to the lnd test payer / alternative backend.

### Tor-only (maximum privacy)

Gateway exposed as a Tor hidden service. LND (test payer / alternative backend) reachable at a `.onion` address.

```
LND_HOST=yourlnd.onion
TOR_PROXY=tor:9050
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

Start the stack and read the onion address as in [Quick start](#quick-start).

**Trade-off:** Tor adds latency. Tor-only Lightning routing is harder — see [Inbound liquidity](#inbound-liquidity).

### Hybrid (practical self-hosted)

Gateway onion. LND (test payer / alternative backend) clearnet or hybrid. otsd on the same host.

```
LND_HOST=192.168.1.x
TOR_PROXY=              # blank — direct LND connection
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

**Trade-off:** permanent pubkey↔IP linkage on the Lightning graph — see [Privacy trade-offs](#privacy-trade-offs).

### Clearnet

The gateway publishes `127.0.0.1:8000` by default (reachable from the Docker
host only). For clearnet, edit the mapping in `docker-compose.yml`:

```yaml
ports:
  - "8000:8000"
```

---

## Connecting to LND (test payer / alternative backend only)

This section applies only when `PAYMENT_BACKEND_TYPE=lnd` (test payer / alternative). The live default backend is Phoenixd, which needs only `PHOENIXD_URL` and `PHOENIXD_HTTP_PASSWORD_LIMITED`.

The gateway needs an invoice macaroon — it authorises creating and reading invoices, nothing else.

```bash
# Convert the macaroon to hex
xxd -p -c 256 ~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

Paste the output as `LND_MACAROON_HEX`.

| LND location | `LND_HOST` value | `TOR_PROXY` |
|---|---|---|
| Same Docker host | `host.docker.internal` — reaches a loopback-bound LND on Docker Desktop only; on a Linux engine bind LND's REST where the container can reach it, or use the host's LAN IP (same caveat as `PHOENIXD_URL` — see the operator guide, "Payment backend (phoenixd)") | blank |
| Remote LAN machine | LAN IP | blank |
| Onion address | `.onion` address | `tor:9050` |
| Umbrel | the Umbrel host's LAN IP — `umbrel.local` is mDNS and does not resolve inside containers | blank |

---

## OTS calendar backend (otsd)

otsd is the OpenTimestamps calendar server. It is the proof engine. The gateway is the paid front door.

**What otsd needs:**
- A Bitcoin Core node with RPC enabled (pruned is acceptable for the submission role).
- A wallet loaded in Bitcoin Core with enough BTC to pay for periodic OP_RETURN anchoring transactions.
- A persistent data directory for calendar state.

**Transaction cost:** otsd submits an anchoring transaction only when commitments are pending, batched on an interval, containing an OP_RETURN with the Merkle root of all digests aggregated since the last anchoring. Normal on-chain fees apply. The batching interval, cost model, fee cap, and wallet sizing live in the operator guide, "Bitcoin transaction cost".

**Initial vs anchored proof:** When a digest is first submitted, otsd returns a receipt with a pending attestation pointing to the calendar URL. This is not yet Bitcoin-anchored. Once the calendar's anchoring transaction is confirmed (timing: operator guide, "Proof lifecycle"), the proof can be upgraded to a full Bitcoin-anchored `.ots` file via the gateway's `/upgrade` endpoint (see the status vocabulary below), or verified locally:

```bash
ots verify proof.ots
```

The initial `.ots` file returned by the gateway is a valid pending receipt, not a finalized proof. This is normal and expected behaviour.

**Bitcoin RPC config** (set in gitignored `.env` only — full URL including credentials):

```
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@host.docker.internal:18332/wallet/otsd-hot
```

(`host.docker.internal` reaches a loopback-bound bitcoind on Docker Desktop only — same caveat as `PHOENIXD_URL`; a LAN node addressed by IP is unaffected. See `.env.example`.) For an onion-only node, the bundled `--profile onion-rpc` bridge forwards `rpc-bridge:18332` to your node over Tor; the systemd path uses a host socat bridge instead (see the operator guide for both). Example systemd units — gateway, otsd container, socat bridge — are in `deploy/`.

**otsd is not publicly exposed.** Clients never talk to otsd; they interact only with the gateway. Topology and access rules: operator guide, "Starting with the bundled otsd profile" and "Pointing to an external otsd".

The calendar code is the `opentimestamps-server` fork, mounted into the otsd container at runtime — the clone command, branch, and update story live in the operator guide, "Deploying the calendar (otsd)".

---

## Inbound liquidity

To receive Lightning payments, the payment backend (Phoenixd by default) must have inbound liquidity. Other nodes must be able to route payments to your node. Note Phoenixd's first-payment channel-open fee caveat — see `ops/OPERATOR-NOTES.md`.

**This is a Lightning network and operator issue, not a gateway or OTS issue.**

Options:
- **Boltz submarine swap** — push sats from local channel balance to the remote side, creating inbound capacity without opening a new channel.
- **Receive a channel** — ask a well-connected node (Loop, Bitrefill Thor, ACINQ, Amboss Magma) to open a channel to you.
- **Lightning Terminal / Pool** — purchase inbound liquidity from the market.

**Tor-only nodes have harder routing.** A widely reported pattern — not something this project has measured — is that many nodes will not route payments to Tor-only endpoints. Options: accept lower reliability, use a hybrid node, or run the payment node on a VPS.

---

## Privacy trade-offs

| Scenario | What is publicly visible |
|---|---|
| Tor-only gateway + Tor-only Lightning node | No clearnet footprint |
| Tor gateway + clearnet Lightning node | Node pubkey and IP on Lightning graph |
| Clearnet gateway | Gateway IP is public; Lightning node depends on config |

Lightning graph exposure is permanent. If your Lightning node advertises a clearnet IP, that association is recorded by Lightning explorers and cannot be undone.

**What the gateway records.** The gateway runs with uvicorn's access log
disabled (`--no-access-log` in both shipped launch paths): there is no
per-request log, so no client-IP or request-timing record exists anywhere.
The application log contains no digests, preimages, or client addresses;
routine lines truncate payment hashes to an 8-hex prefix, and only
WARNING-level incident lines (a failing re-stamp, a Lightning liquidity-fee
event) carry a full payment hash. One bounded residual: malformed tokens or
proofs are logged with tracebacks, which can echo fragments of the malformed
input itself — accepted, since by definition such input is not a valid
secret or proof.

**Where a digest does persist.** The Lightning invoice memo is the digest —
that binding is how payment is verified — so every paid digest is stored,
with its payment hash, amount, and time, in the operator's payment backend
(phoenixd's own database) and in any backup of it. Clients should assume the
operator's wallet layer retains this linkage even though the gateway itself
keeps digests only in its obligation log.

---

## Running on home hardware and node boxes

### Verified

| Platform | Proof |
|---|---|
| VPS, systemd path — gateway under systemd, otsd as a Docker unit, phoenixd host-run | Live mainnet proofs issued and anchored: [LIVE_PROOF.md](LIVE_PROOF.md) (2026-06-16 and 2026-06-17). |
| VPS, Docker Compose path — docs-only stranger install, no operator assistance | Live mainnet proofs issued, anchored, and sold unattended: [LIVE_PROOF.md](LIVE_PROOF.md) (2026-07-27). |

### Untested — should work, not proven

Reasoned expectations, not test results. A row moves up to Verified when a dated `LIVE_PROOF.md` entry exists for it.

| Platform | What is reasoned vs. proven |
|---|---|
| Home Linux / mini PC (Docker Compose) | A live compose deployment is on record on a VPS ([LIVE_PROOF.md](LIVE_PROOF.md), 2026-07-27); on home hardware itself the stack has been driven end to end only against stub backends (2026-07-14, macOS/colima). |
| Raspberry Pi / ARM64 | The base images are multi-arch, so they should pull and run on arm64 — but multi-arch base images are not a test; no ARM build or run is on record. |
| Umbrel | A Linux Docker host, so the compose stack should run beside Umbrel's own apps. Untested. `umbrel.local` is mDNS and does not resolve inside containers — use the Umbrel host's LAN IP in `.env`. |
| Start9 | Running this stack as a plain docker compose project is not a supported StartOS flow, and no StartOS run is on record. (A Start9 box does serve as the Bitcoin Core backend of the verified deployment, over Tor — that half is proven; see LIVE_PROOF.md.) |

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in payment backend vars and set OTS_BACKEND_MODE
uvicorn main:app --reload
```

Run the test suite (no payment backend, Tor, or otsd required — all network calls are mocked). `requirements.txt` is runtime-only, so install the two test-only packages first:

```bash
pip install pytest httpx
pytest -q
```

---

## The payer side

The end-to-end test needs a **second, independently funded Lightning wallet**. You cannot pay the gateway's invoice from the gateway's own phoenixd — that would be the node paying itself. Any consumer Lightning wallet (Phoenix, Breez, Zeus, …) works as the payer; after paying, the payment **preimage** is shown on the wallet's payment-details screen for that payment — the L402 retry needs it (`Authorization: L402 <macaroon>:<preimage>`). The repo's own test payer, `ops/l402-paid-proof.sh`, presupposes an LND node with `lncli`; without one, a consumer wallet plus the two curl commands in [Quick start](#quick-start) is the manual path. If the payer is itself a phoenixd, do not budget mining fees only for its on-chain funding — under defaults it pays the same auto-liquidity toll as the receiving node (measured figures: operator guide, "Funding the payer side (phoenixd as payer)").

---

## Verifying a proof

After receiving a `.ots` file, the proof is pending calendar confirmation. Once the calendar's anchoring transaction is confirmed (timing: operator guide, "Proof lifecycle"), POST the pending proof (base64) with its digest to the gateway's `/upgrade` endpoint, which fetches the Bitcoin anchoring from the calendar and returns the anchored proof. (Plain `ots upgrade proof.ots` contacts the calendar URL inside the attestation directly, so it works only where the operator serves that URL publicly.) Then verify locally:

```bash
ots verify proof.ots    # verifies against Bitcoin
```

Or use the [OpenTimestamps web verifier](https://opentimestamps.org). The proof is independently verifiable against Bitcoin without trusting the gateway or the calendar after the fact.

### `/verify` and `/upgrade` status vocabulary

Both endpoints return HTTP 200 with a JSON body whose `status` field is one of:

| Status | Meaning |
|---|---|
| `anchored` | Digest matches and the proof carries a Bitcoin attestation. Independently verifiable against the Bitcoin block. |
| `pending` | Digest matches; the proof carries a calendar attestation awaiting Bitcoin anchoring. `/upgrade` returns the anchored proof once available. |
| `mismatch` | Well-formed proof, but it attests a different digest than the one supplied. |
| `no_attestations` | Well-formed proof, digest matches, but no recognized (bitcoin/pending) attestations — nothing to verify or upgrade. |
| `invalid` | The `ots` field is not decodable as an OTS proof (bad base64 or malformed bytes). |

`verified` is `true` only for `anchored`.

Both endpoints take the same JSON body: `digest` (64-char hex) and `ots` (the proof bytes, base64-encoded; the `tr` strips the line breaks GNU base64 inserts):

```bash
# Verify a proof against the digest it should attest:
curl -X POST http://localhost:8000/verify \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\",\"ots\":\"$(base64 < proof.ots | tr -d '\n')\"}"

# Upgrade a pending proof once the calendar has anchored — the response's
# `ots` field carries the anchored proof, base64-encoded:
curl -X POST http://localhost:8000/upgrade \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\",\"ots\":\"$(base64 < proof.ots | tr -d '\n')\"}" \
  | python3 -c 'import sys,json,base64; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)["ots"]))' \
  > proof-anchored.ots
```

**`/health`:** `"status":"ok"` (HTTP 200) requires `payment` = `ok`, `otsd` = `ok` or `n/a`, `wallet` and `proofs` = `ok` or `absent`, and `backup` = `ok`, `local_only`, or `absent` — anything else reports `degraded` with HTTP 503. The operator **PAUSED switch** is a file (default `/var/lib/timestamp-gateway/PAUSED`, path settable via `PAUSE_FILE`): while it exists the gateway answers `/health` (`"status":"paused"`, HTTP 503) and nothing else — every other endpoint returns 503 and the obligation sweeper skips its cycles (full-stop, ruled 2026-07-22). A pause takes nothing: settlement outranks expiry, so paid tokens redeem after unpause and recorded obligations wait in the log. Create it to stop the machine, delete it to resume.

**Calendar URI note:** the `calendar_url` shown in pending attestations is the calendar's `uri` identity file, chosen once at first run and baked into every attestation the calendar ever issues — treat it as permanent. Pick a stable identifier you control; it does not need to resolve — upgrades go through the gateway's `/upgrade`, not that URL. For a domainless island the natural choice is the gateway's own onion address (`http://<your-onion>.onion/`): it costs nothing, it is already yours, and it stays yours for as long as the Tor key exists. With the onion as the uri, the `tor_keys` backup (operator guide, "Tor hidden service keys") protects the island's identity in both senses: lose that key and you lose not only the front door but the name written inside every attestation the calendar has ever issued.
