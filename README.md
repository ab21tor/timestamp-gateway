# timestamp-gateway

timestamp-gateway is portable paid OpenTimestamps calendar-node software. It accepts a SHA-256 digest, charges a configured Lightning price, submits the paid digest to the operator's own OpenTimestamps calendar backend, and returns a raw .ots proof. It stores no client files and keeps no client accounts: the client-facing interface is a digest in and a proof out, and the gateway's own bookkeeping (the obligation log and the anchor-bills ledger, SQLite under `OBLIGATIONS_DB_PATH`) is operator-internal, never a client interface. Once a proof is anchored in Bitcoin it does not need to be trusted at all, and until then the pending receipt depends on the operator's calendar (see "What you must keep, and how to verify without us").

This is not a hosted service. It is software for running a Lightning-gated OpenTimestamps calendar node.

Two shapes share the calendar. This repo, with the `auto-anchor` payer and the `api-endpoint` adapter in its `GATEWAY_URL` mode, is the **hosted** shape: a door sold across a trust boundary. The **appliance** shape runs the calendar alone on one box with its own bitcoind, the adapter in its `CALENDAR_URL` mode, the self-stamper and the watcher — no gateway, no Lightning, no payer — and is installed from the fork's README, "The appliance shape" (`docker-compose.enterprise.yml`; its `Dockerfile` is a copy of `otsd/Dockerfile` here, kept in step by hand).

```
client
  → Lightning-gated gateway       (this repo)
  → operator-controlled OTS calendar  (otsd — the proof engine)
  → Bitcoin anchoring
  → .ots
```

Tor, a VPS, and Tor-only operation are each optional.

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

**Proves when.** The digest existed before the Bitcoin block that anchors it.

**Charges before the seal — the one place it does.** The L402 door takes payment at submission: what the payment buys is the calendar's pending receipt and its commitment to carry the digest in the next anchor, hours before the Bitcoin block exists. That is the retail shape, and it is the product's one exception to owing nothing until the work is sealed. The other door is the pay-after shape: with `L402_ENABLED=false` records are free at submission and the operator bills a standing payer per anchor, after it has confirmed and been receipted (operator guide, "Anchor billing"; `auto-anchor/pay-anchor-bills.sh` is the payer that audits those bills).

---

## Components

| Component | Role |
|---|---|
| Gateway | Paid front door. Validates, charges, verifies, forwards. |
| otsd | Operator-controlled proof engine. Aggregates digests, anchors to Bitcoin. |
| Phoenixd | Live Lightning payment backend (default). Issues and settles invoices. LND is supported as a test payer / alternative. |
| Bitcoin Core | Operator-provided (or shared) Bitcoin backend for otsd. |

Public calendar mode (`OTS_BACKEND_MODE=public`) forwards paid digests to the public OpenTimestamps aggregators instead of an operator calendar — a paid relay to other operators' infrastructure, not an independent calendar node. It is retained as a compatibility/testing option only.

---

## What you need

- Git — the Quick start clones two repositories with it
- Docker with Compose v2 (version floor, install route, and why: operator guide, "Prerequisites"). Compose v2 ships two ways — the `docker compose` plugin or a standalone `docker-compose` binary. Detect which this box has, and read `docker compose` in every command in these docs as `$C`:

  ```bash
  docker compose version >/dev/null 2>&1 && C="docker compose" || C="docker-compose"
  ```
- A running phoenixd instance (the live payment backend) — or an LND node with REST API and invoice macaroon if using the LND test-payer / alternative backend
- An OpenTimestamps calendar backend (otsd) — bundled in the Compose stack or external
- An **existing, already-synced** Bitcoin Core node reachable by otsd, with a wallet loaded and funded for anchoring transactions. This is the heaviest prerequisite, and these docs do not teach it: building a node from nothing is a multi-day project — on the order of 750 GB of initial-block-download ingress, days of sync time, and a funded wallet. If you do not already run a node, start at [bitcoincore.org](https://bitcoincore.org).
- Inbound Lightning liquidity on the payment backend node (see [Inbound liquidity](#inbound-liquidity))

### What a first paid stamp costs

Budget estimates for going from zero to one working paid stamp, in sats:

| Item | Estimate (sats) |
|---|---|
| Anchoring wallet (`otsd-hot`) | 50,000–100,000 |
| phoenixd pre-fund payment (operator guide, "Payment backend (phoenixd)") | ~25,000–30,000 |
| End-to-end test payment | ~5,000 |
| Payer-side overhead (funding the second wallet, routing fees) | ~5,000–10,000 |
| **Total** | **≈ 90,000–140,000 sats, plus the VPS** |

This is the full cost of a first working paid stamp, not an ongoing rate — the pre-fund and wallet balances carry over.

---

## Quick start

```bash
# Two repos: the gateway, and the calendar code it mounts into otsd.
git clone https://github.com/ab21tor/timestamp-gateway
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server
cd timestamp-gateway
cp .env.example .env
# Edit .env. With the L402 door on (the default) set:
#   L402_SECRET_HEX       python3 -c 'import secrets; print(secrets.token_hex(32))'
#   PRICE_PER_PROOF_SATS  the flat price every hash pays, integer >= 1
#                         (sizing arithmetic: operator guide, "Pricing")
# A free door (L402_ENABLED=false) needs neither. Then set:
#   PHOENIXD_HTTP_PASSWORD_LIMITED  live default backend (LND_* only for
#                                   the lnd test payer / alternative backend)
#   BITCOIN_RPC_SERVICE_URL         for otsd; three shapes in .env.example

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
(With `L402_ENABLED=false` the same request returns the proof immediately,
free of charge — no 402; see [Configuration](#configuration).)
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
| `L402_ENABLED` | No | `true` | The L402 door switch. Strict `true`/`false`. `true`: `/timestamp` charges the flat per-proof rate (the 402/L402 flow below). `false`: free door — `/timestamp` stamps immediately and returns the proof free of charge; no invoice, no macaroon, no 402, the payment backend is never contacted from that path, and any `Authorization` header is ignored (a token minted before the flip gets its proof regardless). The per-record cost is charged at anchor time through anchor billing (`records` × `PER_RECORD_SATS` per confirmed anchor). With both this and `ANCHOR_BILLING_ENABLED` off, nothing charges anywhere — valid, and named in one startup warning. `/verify` and `/upgrade` are unaffected in both modes. |
| `PRICE_PER_PROOF_SATS` | When L402 on | — | The flat price in sats every hash pays at submission — the whole quote; the gateway reads no feerates. No default (startup fails without it when the door is on); integer ≥ 1 — `0` is refused: a free door is `L402_ENABLED=false`, not a price of zero. Never read with the door off. Sizing arithmetic: operator guide, "Pricing". Retired pricing vars (`GATEWAY_PRICE_SATS`, `PRICE_BLIND_SATS`, and the rest of the old floor model) are named in one startup warning and ignored — never a failure. |
| `STAMPER_FEE_CAP_SATS` | No | `20000` | Mirror of otsd's `--btc-max-fee` flag in sats (the flag takes BTC; 0.0002 BTC = 20,000 sats — keep the two in sync). Used only to derive the float backstop thresholds (operator guide, "Pricing"). |
| `ANCHOR_BILLING_ENABLED` | No | `false` | Part two of the pricing model: bill each anchor's records × `PER_RECORD_SATS` to a standing payer (operator guide, "Anchor billing"). Strict `true`/`false`. `false` = the feature is entirely off and the three vars below are never read. Deploy order: the fork must write the `records` receipt field first (`/anchor-bills` below). |
| `PER_RECORD_SATS` | When billing | — | The price in sats per record (digest submission) inside each anchor. A bill's `amount_sats` = `records` × this rate, computed once at ingestion and immutable — a later rate change never reprices an existing bill, as with the quote. Integer ≥ 1 — `0` is refused: to bill nothing, set `ANCHOR_BILLING_ENABLED=false`. Replaces the retired `PRICE_MARKUP`, which now warns when set and is ignored. |
| `ANCHOR_RECEIPTS_PATH` | When billing | — | The anchor-receipts JSONL file the calendar fork writes (the path its `OTSD_ANCHOR_RECEIPTS` names). Absent file = billing on, no anchors yet — healthy. Present but unreadable = `/health` reports `billing: error`. Wiring for both deployment shapes: operator guide, "Anchor billing". |
| `ANCHOR_BILLS_TOKEN` | When billing | — | Opaque bearer token guarding `GET /anchor-bills` (constant-time compare; 401 on missing/wrong). Operational history is not public. Generate like `L402_SECRET_HEX`. |
| `RATE_LIMIT_PER_MINUTE` | No | `10` | Per-IP cap per minute on unauthenticated invoice minting (402 challenges). Every anonymous request makes phoenixd sign and store an invoice; this bounds what a spammer gets for free. `0` disables. Over-limit requests get 429 with Retry-After. With `L402_ENABLED=false` nothing mints, so this bucket does not apply and no rate limit remains on `/timestamp` — whatever can reach a free door can load the calendar and the anchor bill. |
| `VERIFY_RATE_LIMIT_PER_MINUTE` | No | `30` | Per-IP cap per minute on the free proof endpoints — one bucket shared by `/verify`, `/upgrade` and `/health`. Separate from the mint limit so proof polling can never starve the paid path. `0` disables. Behind the bundled Tor hidden service every onion visitor shares one bucket (the Tor container is the peer); `UPGRADE_CLIENT_TOKEN` exempts a client's `/upgrade` calls. |
| `HEALTH_PROBE_CACHE_SECONDS` | No | `15` | `/health` caches its calendar probe (one homepage render, four bitcoind RPCs) for this long behind a single-flight lock — an expired answer is served stale while one caller refreshes it, so only a cold start makes callers wait — and `/health` is drawn from the verify bucket per peer. `0` disables the cache. |
| `UPGRADE_CLIENT_TOKEN` | No | — | Optional bearer token that exempts `/upgrade` (never `/verify`) from the verify bucket for the client that holds it — the client adapter presents it as `GATEWAY_UPGRADE_TOKEN` so it can finish every pending proof the free door hands out (about 20 a second at the demo's rate, against a 30-a-minute anonymous budget). A wrong token is simply anonymous; unset, the header is inert. Generate like `L402_SECRET_HEX`. |
| `L402_SECRET_HEX` | When L402 on | — | L402 macaroon root signing key (hex, at least 16 bytes; 32 recommended). Generate with `python3 -c 'import secrets; print(secrets.token_hex(32))'`. The gateway refuses to start without it when the door is on (dev-only escape: `L402_ALLOW_EPHEMERAL_SECRET=true`); never read with the door off. |
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
event) carry a full payment hash. One bounded residual: malformed
proofs sent to `/verify` and `/upgrade` are logged with tracebacks (at
INFO), which can echo fragments of the malformed input itself — accepted,
since by definition such input is not a valid proof. Malformed tokens never
get a traceback: unauthenticated input is logged as a single WARNING line
by design.

**Where a digest does persist.** The Lightning invoice memo is the digest —
that binding is how payment is verified — so every paid digest is stored,
with its payment hash, amount, and time, in the operator's payment backend
(phoenixd's own database) and in any backup of it. Clients should assume the
operator's wallet layer retains this linkage even though the gateway itself
keeps digests only in its obligation log. On the calendar side, the
`journal.counts` sidecar stores per-second submission volume — activity
metadata, no digests (fork README, "Anchor receipts").

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
uvicorn main:app --reload --no-access-log
```

### Running the tests

No payment backend, Tor, or otsd required — the suite sets its own dummy config and mocks all network calls. Test-only packages (pytest, httpx) are pinned in `requirements-dev.txt`:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

### What the suite pins

| Claim | Test |
|---|---|
| L402 tokens: signature, digest binding, capability, and tamper cases reject with a generic 401 | `test_verify_token_*`, `test_malformed_authorization_returns_401`, `test_garbage_token_logs_single_warning_no_traceback` |
| Settlement outranks expiry: a settled invoice redeems its token after expiry, after a pause, and after a float stop | `test_settled_but_expired_token_redeems`, `test_paused_blocks_paid_redemption_and_unpause_serves_it`, `test_float_stop_blocks_paid_redemption_and_recovery_serves_it` |
| A settled payment is never lost when stamping fails | `test_obligation_stamp_failure_returns_502_and_persists_needs_stamp`, `test_sweeper_completes_pending_obligation_and_bumps_attempts` |
| The liquidity fee nets the operator's credit, never the customer's proof | `test_liquidity_fee_netted_receive_still_verifies`, `test_liquidity_fee_payment_yields_obligation_and_proof` |
| Calendar mode never falls back to the public aggregators | `test_calendar_mode_never_falls_back_to_public_calendars` |
| Duplicate receipt lines bill once; `records: 0` never bills | `test_receipts_duplicate_txid_lines_dedupe`, `test_receipts_records_zero_not_billed_warns_txid` |
| A bill's amount never changes after ingestion | `test_amount_sats_immutable_across_rate_change` |
| A malformed receipt line never takes billing down; txid dedupe is case-insensitive | `test_receipts_overflow_line_rejected_not_fatal`, `test_receipts_txid_dedupe_is_case_insensitive` |
| A calendar anchoring with receipts off degrades `/health` | `test_health_billing_receipts_off_degrades` |
| The free door never contacts the payment backend | `test_free_mode_never_contacts_payment_backend` |

---

## Dependencies posture

- **pymacaroons 0.13.0** (last upstream release 2018) — the L402 token core. Pure Python; its cryptography is PyNaCl, which is maintained. The pinned version is treated as code we own: the verify/reject surface (signature, digest binding, capability, tamper cases) is covered by `test_verify_token_*`, `test_malformed_authorization_returns_401`, and `test_garbage_token_logs_single_warning_no_traceback` in `test_main.py`.
- **python-bitcoinlib** (0.12.x here; the otsd image pins 0.11.2) — upstream releases are years apart. The calendar fork codes to the 0.11.x–0.12.x intersection (`stamper.py` avoids `calc_weight()`, absent in 0.11.x); the RPC calls the fork makes are exercised by its `test_rpc_homepage.py` and `test_anchor_records.py`. Any Bitcoin Core RPC drift or CVE in this window is ours to patch.
- **py-leveldb 0.201** (2019) — its C extension uses `PyUnicode_AS_UNICODE`, removed in CPython 3.12, which is why the calendar runs on `python:3.11-slim` (security support ends October 2027). Planned replacement: a plyvel port before mid-2027. plyvel wraps the same libleveldb, so the on-disk calendar database carries over unchanged — a code-only migration (~four call sites plus a KeyError-semantics shim).
- **Base images** — all three Dockerfiles pin bases by digest, which also freezes CVEs. Refresh the digests and rebuild quarterly, and after any Debian or Python security advisory naming a pinned base.

Everything else in `requirements.txt` is current-line and maintained; the client trees are dependency-free (`api-endpoint` is pure stdlib, `auto-anchor` is bash + curl).

---

## The payer side

The end-to-end test needs a **second, independently funded Lightning wallet**. You cannot pay the gateway's invoice from the gateway's own phoenixd — that would be the node paying itself. Any consumer Lightning wallet (Phoenix, Breez, Zeus, …) works as the payer; after paying, the payment **preimage** is shown on the wallet's payment-details screen for that payment — the L402 retry needs it (`Authorization: L402 <macaroon>:<preimage>`). The scripted payer is `pay402` in the sibling `auto-anchor` tree — phoenixd-based, one L402 purchase per call (`auto-anchor/README.md`); a consumer wallet plus the two curl commands in [Quick start](#quick-start) is the manual path. If the payer is itself a phoenixd, do not budget mining fees only for its on-chain funding — under defaults it pays the same auto-liquidity toll as the receiving node (measured figures: operator guide, "Funding the payer side (phoenixd as payer)").

Two reference clients live in sibling checkouts. The **reference standing payer** for anchor billing is `pay-anchor-bills.sh` in the `auto-anchor` tree — it audits every bill (`amount_sats` = `records` × the contracted rate) and pays within a per-bill ceiling and daily budget, alongside `pay402`, the single-purchase tool; both are documented in `auto-anchor/README.md`. The **reference client adapter** is `api-endpoint` — the local door that fingerprints records in memory and buys each proof through the L402 flow so client systems never touch Lightning; documented in `api-endpoint/README.md`.

---

## Verifying a proof

After receiving a `.ots` file, the proof is pending calendar confirmation. Once the calendar's anchoring transaction is confirmed (timing: operator guide, "Proof lifecycle"), POST the pending proof (base64) with its digest to the gateway's `/upgrade` endpoint, which fetches the Bitcoin anchoring from the calendar and returns the anchored proof. A client with many pending proofs presents `UPGRADE_CLIENT_TOKEN` as a bearer on `/upgrade` and is not throttled by the verify bucket (the reference adapter does this with `GATEWAY_UPGRADE_TOKEN`). (Plain `ots upgrade proof.ots` contacts the calendar URL inside the attestation directly, so it works only where the operator serves that URL publicly and the client has whitelisted it with `-l <url>`; the public client talks only to calendars on its whitelist.) Then verify locally:

```bash
ots verify proof.ots    # against your own Bitcoin node or a header source you trust
```

Or use the [OpenTimestamps web verifier](https://opentimestamps.org) — noting that a web verifier is itself a third party you are trusting for the verdict. An anchored proof is independently verifiable against Bitcoin without trusting the gateway or the calendar after the fact; `ots verify` is only as independent as its view of Bitcoin — your own node, or a block-header source you chose to trust (see "What you must keep, and how to verify without us").

### `/verify` and `/upgrade` status vocabulary

Both endpoints return HTTP 200 with a JSON body whose `status` field is one of:

| Status | Meaning |
|---|---|
| `anchored` | Digest matches and the proof carries a Bitcoin attestation. Independently verifiable against the Bitcoin block. |
| `pending` | Digest matches; the proof carries a calendar attestation awaiting Bitcoin anchoring. `/upgrade` returns the anchored proof once available. |
| `mismatch` | Well-formed proof, but it attests a different digest than the one supplied. |
| `no_attestations` | Well-formed proof, digest matches, but no recognized (bitcoin/pending) attestations — nothing to verify or upgrade. |
| `invalid` | The `ots` field is not decodable as an OTS proof (bad base64 or malformed bytes) — or a proof nested deeper than the OpenTimestamps library will parse, which gets the same verdict from the public `ots` client. |

`verified` is `true` only for `anchored`.

Both endpoints take the same JSON body: `digest` (64-char hex) and `ots` (the proof bytes, base64-encoded; the `tr` strips the line breaks GNU base64 inserts). Every POST body is capped at 512 KiB — refused with 413 before it is read — and must declare its length (a chunked body is refused with 411); the proof inside is further limited to 256 KiB.

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

## Operator endpoints

### `/health`

`GET /health` (rate-limited per peer from the verify bucket; its calendar probe is cached for `HEALTH_PROBE_CACHE_SECONDS`, one render however many callers ask, and a stale answer is served while the one refresh runs) returns HTTP 200 with `"status":"ok"` when every field below is in its healthy set, and HTTP 503 otherwise — `"status":"degraded"`, or `"paused"` / `"auto_paused"` for the two stops described after the table. The body also carries `paused` (whether the PAUSED file exists), `payment_backend`, and `last_mint_at` (the time of the last real mint attempt; `null` before the first).

| Field | Healthy | Degrades |
|---|---|---|
| `payment` | `ok`; `unknown` (no real mint since start) | `degraded` (the last mint failed) |
| `otsd` | `ok`; `n/a` (public mode) | `error` (unreachable, or up but Bitcoin-blind) |
| `wallet` | `ok`; `absent` (alarm timer not installed) | `low`, `unknown`, `stale` |
| `proofs` | `ok`; `absent` | `mismatch`, `attention`, `unknown`, `stale` |
| `backup` | `ok`; `local_only`; `absent` | `attention`, `failed`, `unknown`, `stale` |
| `float` | `ok`; `inactive` (no balance reading — the backstop is off) | `alarm`, `stop` |
| `billing` | `off`; `ok` | `rejected`, `receipts_off`, `overdue`, `error` |
| `l402` | `on` or `off` — the door switch; never degrades | — |

`billing`: `rejected` means receipt lines are being rejected as malformed (the `billing_rejected` and `billing_bills` counters are present only with billing on); `receipts_off` means the calendar's status page says it is anchoring without writing receipts (its `Anchor receipts: off` line) — every anchor from then on is unbilled; a calendar without that line (an older fork) reads as unknown and never degrades on its own; `overdue` means an unpaid anchor bill is older than 24 h (a bookkeeping alarm — sales are never gated by billing state); `error` means the receipts file is present but unreadable. Details: operator guide, "Anchor billing".

**PAUSED switch.** A file (default `/var/lib/timestamp-gateway/PAUSED`, path in `PAUSE_FILE`). While it exists the gateway answers `/health` (`"status":"paused"`, HTTP 503) and nothing else: every other endpoint returns 503 and the obligation sweeper skips its cycles. A pause loses nothing: settlement outranks expiry, so paid tokens redeem after unpause and recorded obligations wait in the log. Create the file to stop, delete it to resume.

**Float backstop.** `float` classifies the anchor-wallet balance read from the wallet-status file: `alarm` below 5 × `STAMPER_FEE_CAP_SATS` (degrades; sales continue); below 1 × the cap the gateway pauses itself with the same semantics as PAUSED (`"status":"auto_paused"`) and clears that pause when the balance recovers. The operator's PAUSED label wins when both hold. Where the balance-check timer does not run, `float` is `inactive`: the backstop is off, reported, never degrading (operator guide, "Pricing").

### `/anchor-bills`

With `ANCHOR_BILLING_ENABLED=true`, `GET /anchor-bills` (`Authorization: Bearer <ANCHOR_BILLS_TOKEN>`; 404 when billing is off, 401 on a missing or wrong token) returns every unpaid anchor bill with a plain bolt11 — minted on poll, never at ingestion; anchor bills are not L402 — plus the last week's paid bills and a `summary` with `unpaid_count` and `unpaid_sats`. Each bill carries its `records` count, so the standing payer audits `amount_sats` = `records` × the contracted `PER_RECORD_SATS` before paying, refuses a count above its plausibility bound, and never pays an anchor whose txid it has paid before, whatever invoice the bill now carries (`auto-anchor/README.md`) — `records` is `null` on bills ingested under the retired markup formula; their stored amounts stand. The count errs low, with one bounded exception: a digest re-submitted across a calendar-fork restart, or past the fork's one-hour dedupe horizon, counts twice — and because the bill's record arithmetic is public, the payer's own ledger exposes any such duplicate. **Deploy order: the calendar fork must write the six-field receipt (with `records`) before this gateway version bills anything.** A receipt whose `records` is `0` (the fork could not prove a count) is never billed, with a warning naming the txid; a receipt missing `records` (an un-upgraded fork) is malformed and skipped with a warning, as is one whose txid is not 64 hex characters (txids are stored lowercase, so a re-cased copy of a line never bills an anchor twice), whose fee is negative, whose tree is empty, whose `confirmed_at` is more than a day ahead of the gateway's clock, or whose `records` × rate would not fit the ledger's 64-bit integer — each skipped with a warning, none able to stop the lines after it. Rate-limited by the `/verify` bucket. Contract and example response: operator guide, "Anchor billing". Size the payer's per-bill ceiling, daily budget, and channel capacity to records-per-anchor-window × the contracted rate — operator guide, "Sizing the payer's ceilings".

**Calendar URI.** The `calendar_url` in pending attestations is the calendar's `uri` identity file, chosen once at first run and written into every attestation the calendar issues — treat it as permanent. It need not resolve: upgrades go through `/upgrade`, not that URL. For a domainless island the gateway's own onion address (`http://<your-onion>.onion/`) is the natural choice: it is already yours and lasts as long as the Tor key. With the onion as the uri, the `tor_keys` backup (operator guide, "Tor hidden service keys") protects both the front door and the name inside every attestation.

## What you must keep, and how to verify without us

The gateway stores nothing for you. Two things are yours to keep:

- **The anchored `.ots` file.** The proof is the artifact; lose it and nothing on our side can reissue it.
- **A record of what the digest is a digest of.** The proof shows that a SHA-256 digest existed at a point in time — it says nothing about what hashed to it. Keep the file or data that produces the digest (or your own digest-to-document ledger); without it the proof proves nothing you can use.

**Upgrade pending receipts promptly.** The `.ots` returned at submission carries a pending attestation pointing at the operator's calendar. Until it is anchored and you have upgraded it (POST the pending proof to `/upgrade` once the anchor confirms — typically a few hours), the receipt's future depends on that calendar continuing to exist: an operator who disappears, or destroys calendar state, strands every pending receipt permanently — while every already-anchored proof is untouched. The pending window is the only period in which you are trusting us; upgrading closes it.

**Reorgs.** A Bitcoin reorg shallower than the calendar's confirmation
depth (6 blocks) returns the reorged commitments to the pending pool, and
no receipt is written before a transaction has 6 confirmations — so a
shallow reorg re-anchors once and bills once. That is the code path in the
fork's `stamper.py`, exercised by the fork's dead-cycle test and by the
billing red-team's reorg cases (2026-09-04, six variants); no live reorg
is on record. One consequence the fork handles explicitly: a reorg that
takes the parent of an anchor still in flight leaves that anchor
unbumpable, and the stamper abandons the dead cycle with one warning and
starts a fresh one instead of waiting for a restart. A reorg deeper than
the confirmation depth would leave
already-written attestations pointing at an orphaned block — invalid, and
nothing in the gateway or calendar notices (an upstream assumption this
fork inherits). Re-verifying your anchored proofs against Bitcoin is what
would expose it; 6-deep reorgs are historically extraordinary events.

**Verifying needs no vendor.** An anchored proof verifies with `ots verify` — open-source client, no account, no API of ours — against your own Bitcoin node or a block-header source you chose to trust. That choice is the whole trust decision: with your own node, verification is fully independent of this gateway, the calendar, and every third party; with someone else's headers or a web verifier, you are trusting that party for the verdict, not us. Nothing in the anchored proof references any service that needs to stay alive.
