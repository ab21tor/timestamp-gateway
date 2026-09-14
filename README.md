# timestamp-gateway

timestamp-gateway is an HTTP door in front of an OpenTimestamps calendar.
It accepts a SHA-256 digest, takes a Lightning payment for it when the
L402 door is on, submits the digest to the operator's own calendar
(`otsd`, the calendar fork in `opentimestamps-server`), and returns the
calendar's `.ots` receipt; later it upgrades that receipt to a
Bitcoin-anchored proof on request and verifies proofs it is given. It
stores no client files and keeps no client accounts: the client-facing
interface is a digest in and a proof out. Its own bookkeeping, the
obligation log and the anchor-bills ledger (SQLite under
`OBLIGATIONS_DB_PATH`), is operator-internal. Once a proof is anchored in
Bitcoin it verifies without this software; until then the pending receipt
depends on the operator's calendar ("What the client must keep").

```
client
  → gateway                       (this repository)
  → operator-controlled calendar  (otsd, the proof engine)
  → Bitcoin anchoring
  → .ots
```

## Configurations

The same code runs in each of these; the configuration is the `.env`.

- **The calendar.** `OTS_BACKEND_MODE=calendar` with `OTS_CALENDAR_URL`
  naming the operator's `otsd`: bundled in the compose stack
  (`--profile calendar`, `http://otsd:14788`) or external
  (`http://127.0.0.1:14788` on the systemd path). `OTS_BACKEND_MODE=public`
  forwards paid digests to the public OpenTimestamps aggregators instead:
  a paid relay to other operators' infrastructure, retained for testing
  without a running `otsd` only.
- **The door.** `L402_ENABLED=true` (default): `/timestamp` charges the
  flat `PRICE_PER_PROOF_SATS` at submission through the 402/L402 flow.
  `L402_ENABLED=false`: `/timestamp` stamps free of charge, and with
  `ANCHOR_BILLING_ENABLED=true` the per-record cost is billed to a
  standing payer per confirmed anchor from the calendar's receipts file
  (`/anchor-bills`). With both off nothing charges anywhere, named in one
  startup warning.
- **The payment backend.** phoenixd (default) or LND (`PAYMENT_BACKEND_TYPE=lnd`);
  either runs outside the compose stack.
- **The front.** The bundled Tor hidden service, and the gateway published
  on `127.0.0.1:8000` only; or clearnet by changing the port mapping. The
  privacy cost of each is in "What is publicly visible".
- **The launch path.** Docker Compose (`docker-compose.yml`: gateway, tor,
  optionally otsd and an onion RPC bridge), or systemd units on the host
  (`deploy/`: gateway, otsd container, socat bridge, phoenixd).
- **The sibling repositories.** `api-endpoint` is a client adapter that
  submits through this door in its `GATEWAY_URL` mode; `auto-anchor` holds
  `pay402`, an L402 payer (one payment and retry per call), and
  `pay-anchor-bills.sh`, a standing payer for anchor bills. The calendar alone, with the adapter in
  its `CALENDAR_URL` mode and no gateway, is a configuration of the fork
  (its README, "Install: single host"); the fork's `Dockerfile` is a copy
  of `otsd/Dockerfile` here, kept in step by hand.

## What the gateway guarantees

Each rule is made by the code the section names, and pinned by the test
the "Tests" table names.

- Settlement outranks expiry: a settled invoice redeems its token after
  the token's expiry, after a pause, and after a float stop ("The L402
  door").
- A settled payment is never lost. The obligation is recorded before
  stamping; if stamping fails the row stays `needs_stamp` and the sweeper
  retries it until it is stamped ("The obligation log").
- The free door never contacts the payment backend, and calendar mode
  never falls back to the public aggregators ("Configurations").
- Billing gates nothing: the door, stamping and redemption never consult
  the bills table ("Anchor billing").
- A bill's amount never changes after ingestion; duplicate receipt lines
  bill once; a receipt with `records: 0` never bills; a malformed line
  never stops the lines after it ("Anchor billing").
- A pause, the operator's or the float backstop's, loses nothing: paid
  tokens redeem after it and recorded obligations wait ("Stopping").
- There is no access log, and the application log carries no digest,
  preimage or client address ("What the gateway records").

## Requirements

- Git, and Docker with Compose v2 for the compose path (version floor and
  install route: operator guide, "Prerequisites"). Compose v2 ships as
  the `docker compose` plugin or a standalone `docker-compose` binary;
  read `docker compose` in every command here as whichever this host has:

  ```bash
  docker compose version >/dev/null 2>&1 && C="docker compose" || C="docker-compose"
  ```

- A running phoenixd, or an LND node with its REST API and an invoice
  macaroon (`PAYMENT_BACKEND_TYPE=lnd`).
- An OpenTimestamps calendar (`otsd`), bundled in the compose stack or
  external.
- A synced Bitcoin Core node reachable by `otsd`, with a wallet loaded and
  funded for anchoring transactions; these documents do not teach running
  a node.
- Inbound Lightning liquidity on the payment backend's node ("Inbound
  liquidity").

## Install

### Quick start (compose)

```bash
# Two repositories: the gateway, and the calendar code it mounts into otsd.
git clone https://github.com/ab21tor/timestamp-gateway
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server
cd timestamp-gateway
cp .env.example .env
# Edit .env. With the L402 door on (the default) set:
#   L402_SECRET_HEX       python3 -c 'import secrets; print(secrets.token_hex(32))'
#   PRICE_PER_PROOF_SATS  the flat price every hash pays, integer >= 1
#                         (sizing arithmetic: operator guide, "Pricing")
# A free door (L402_ENABLED=false) needs neither. Then set:
#   PHOENIXD_HTTP_PASSWORD_LIMITED  the default backend (LND_* only for
#                                   PAYMENT_BACKEND_TYPE=lnd)
#   BITCOIN_RPC_SERVICE_URL         for otsd; three shapes in .env.example

# First run only: the calendar's two identity files, which otsd refuses to
# start without. What they mean: operator guide, "Deploying the calendar
# (otsd)".
docker compose --profile calendar run --rm otsd sh -c \
  'echo "https://calendar.example.com/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key'

# First run: --build. After that, plain `docker compose --profile calendar
# up -d` is enough — otsd code updates come from the fork checkout, not the
# image (operator guide, "Updating").
docker compose --profile calendar up -d --build
```

The onion address:

```bash
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname
```

A first request:

```bash
DIGEST=a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef

curl -i -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\"}"
```

With the door on the answer is HTTP 402 with an L402 challenge in the
`WWW-Authenticate` header, `L402 macaroon="<token>", invoice="<bolt11>"`.
With `L402_ENABLED=false` the same request returns the proof at once. Pay
the invoice, then retry with the macaroon and the payment preimage:

```bash
curl -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -d "{\"digest\":\"$DIGEST\"}" \
  -o proof.ots
```

The invoice cannot be paid from the gateway's own phoenixd; a second,
independently funded Lightning wallet pays it, and shows the preimage on
that payment's details screen. `pay402` in the `auto-anchor` repository
scripts the payment and retry; a phoenixd used as the payer pays phoenixd's
auto-liquidity fee on its first receipt like the receiving node (operator
guide, "Funding the payer side (phoenixd as payer)").

### The calendar

`otsd` needs a Bitcoin Core node with RPC (a pruned node is enough for the
submission role), a wallet loaded in it with enough BTC for the periodic
OP_RETURN anchoring transactions, and a persistent data directory. It
anchors only when commitments are pending, batched on
`--btc-min-tx-interval`, one OP_RETURN with the merkle root of everything
aggregated since the last anchor; the interval, cost model, fee cap and
wallet sizing are in the operator guide, "Bitcoin transaction cost". Its
RPC URL, credentials included, goes in the gitignored `.env` only:

```
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@host.docker.internal:18332/wallet/otsd-hot
```

`host.docker.internal` reaches a loopback-bound bitcoind on Docker
Desktop only, the same reachability caveat as `PHOENIXD_URL`; a LAN node
addressed by IP is unaffected (`.env.example`). For an onion-only node the
`--profile onion-rpc` bridge forwards `rpc-bridge:18332` to the node over
Tor; the systemd path uses a host socat bridge instead (operator guide,
both). `otsd` publishes no port; the gateway reaches it at
`http://otsd:14788` on the compose network, and clients never talk to it
(operator guide, "Starting with the bundled otsd profile", "Pointing to an
external otsd"). The calendar code is the `opentimestamps-server` fork,
mounted into the container at runtime; clone command, branch and update
story: operator guide, "Deploying the calendar (otsd)".

### The systemd path

Example units for the gateway, the otsd container, the socat RPC bridge
and phoenixd are in `deploy/`, each with its install commands in its
header; the operator guide covers that path beside the compose one.

Deployments on record, each with a dated entry in `LIVE_PROOF.md`: a VPS
on the systemd path (gateway under systemd, otsd as a Docker unit,
phoenixd on the host), 2026-06-16 and 2026-06-17; a VPS on the compose
path, installed from these documents alone, 2026-07-27.

### LND as the payment backend

With `PAYMENT_BACKEND_TYPE=lnd` the gateway needs an invoice macaroon,
which authorises creating and reading invoices and nothing else:

```bash
xxd -p -c 256 ~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

The output is `LND_MACAROON_HEX`.

| LND location | `LND_HOST` value | `TOR_PROXY` |
|---|---|---|
| Same Docker host | `host.docker.internal` — reaches a loopback-bound LND on Docker Desktop only; on a Linux engine bind LND's REST where the container can reach it, or use the host's LAN IP (same caveat as `PHOENIXD_URL` — operator guide, "Payment backend (phoenixd)") | blank |
| Remote LAN machine | LAN IP | blank |
| Onion address | `.onion` address | `tor:9050` |
| Umbrel | the Umbrel host's LAN IP — `umbrel.local` is mDNS and does not resolve inside containers | blank |

The front-door and payment-node combinations and their trade-offs:

```
# Tor-only: the gateway as a hidden service, LND reachable at an onion
LND_HOST=yourlnd.onion
TOR_PROXY=tor:9050
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788

# Onion front, clearnet or LAN Lightning node, otsd on the same host
LND_HOST=192.168.1.x
TOR_PROXY=              # blank: a direct LND connection
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

Tor adds latency, and Tor-only Lightning routing is harder ("Inbound
liquidity"); a clearnet Lightning node puts its pubkey and IP on the
Lightning graph permanently ("What is publicly visible"). For a clearnet
gateway, change the port mapping in `docker-compose.yml` from
`127.0.0.1:8000:8000` to `8000:8000` (operator guide, "Clearnet
exposure").

### Inbound liquidity

To receive payments the payment backend's node needs inbound liquidity:
other nodes must be able to route to it. This is a Lightning-network and
operator matter, not the gateway's; the ways to obtain it, and phoenixd's
first-payment channel-open fee, are in the operator guide, "Inbound
liquidity" and "Payment backend (phoenixd)". Tor-only nodes are harder to
route to.

## Operate

### Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `PAYMENT_BACKEND_TYPE` | No | `phoenixd` | `phoenixd` or `lnd` |
| `PHOENIXD_URL` | No | `http://127.0.0.1:9740` | phoenixd HTTP API endpoint; from the compose stack `http://host.docker.internal:9740`, which reaches a loopback-bound phoenixd on Docker Desktop only; on a Linux engine see the operator guide, "Payment backend (phoenixd)" |
| `PHOENIXD_HTTP_PASSWORD_LIMITED` | When `phoenixd` | — | phoenixd's `http-password-limited-access` from `phoenix.conf`, never the full `http-password` (scope and why: operator guide, "Payment backend (phoenixd)"). The old name `PHOENIXD_HTTP_PASSWORD` is read as a fallback. |
| `LND_HOST` | When `lnd` | — | Hostname, IP, or `.onion` address of the LND REST API |
| `LND_PORT` | When `lnd` | — | LND REST port, typically `8080` |
| `LND_MACAROON_HEX` | When `lnd` | — | Hex-encoded invoice macaroon |
| `L402_ENABLED` | No | `true` | The door switch; strict `true`/`false`. `true`: `/timestamp` charges the flat per-proof rate (the 402/L402 flow). `false`: `/timestamp` stamps immediately and returns the proof free of charge: no invoice, no macaroon, no 402, the payment backend is never contacted from that path, and any `Authorization` header is ignored (a token minted before the flip gets its proof regardless). The per-record cost is then charged through anchor billing (`records` × `PER_RECORD_SATS` per confirmed anchor). With both this and `ANCHOR_BILLING_ENABLED` off nothing charges anywhere, named in one startup warning. `/verify` and `/upgrade` are unaffected in both modes. |
| `PRICE_PER_PROOF_SATS` | When L402 on | — | The flat price in sats every hash pays at submission, the whole quote; the gateway reads no feerates. No default: startup fails without it when the door is on. Integer ≥ 1; `0` is refused (a free door is `L402_ENABLED=false`). Never read with the door off. Sizing arithmetic: operator guide, "Pricing". The retired variables of the earlier feerate model (`GATEWAY_PRICE_SATS`, `PRICE_BLIND_SATS` and the rest) are named in one startup warning and ignored, never a failure. |
| `STAMPER_FEE_CAP_SATS` | No | `20000` | Mirror of otsd's `--btc-max-fee` flag in sats (the flag takes BTC; 0.0002 BTC = 20,000 sats; keep the two in sync). Used only to derive the float backstop thresholds. |
| `ANCHOR_BILLING_ENABLED` | No | `false` | Bill each anchor's records × `PER_RECORD_SATS` to a standing payer (operator guide, "Anchor billing"). Strict `true`/`false`. `false`: the feature is entirely off and the three variables below are never read. The calendar fork must write the `records` receipt field first (`/anchor-bills`). |
| `PER_RECORD_SATS` | When billing | — | The price in sats per record (digest submission) inside each anchor. A bill's `amount_sats` = `records` × this rate, computed once at ingestion and immutable: a later rate change never reprices an existing bill. Integer ≥ 1; `0` is refused (to bill nothing, set `ANCHOR_BILLING_ENABLED=false`). Replaces the retired `PRICE_MARKUP`, which warns when set and is ignored. |
| `ANCHOR_RECEIPTS_PATH` | When billing | — | The anchor-receipts JSONL file the calendar fork writes (the path its `OTSD_ANCHOR_RECEIPTS` names). Absent file: billing on, no anchors yet, healthy. Present but unreadable: `/health` reports `billing: error`. Wiring: operator guide, "Anchor billing". |
| `ANCHOR_BILLS_TOKEN` | When billing | — | Bearer token guarding `GET /anchor-bills` (constant-time compare; 401 on missing or wrong). Generate like `L402_SECRET_HEX`. |
| `RATE_LIMIT_PER_MINUTE` | No | `10` | Per-IP cap per minute on unauthenticated invoice minting (402 challenges); every anonymous request makes phoenixd sign and store an invoice. `0` disables. Over-limit requests get 429 with `Retry-After`. With `L402_ENABLED=false` nothing mints, so this bucket does not apply and no rate limit remains on `/timestamp`: whatever can reach a free door can load the calendar and the anchor bill. |
| `VERIFY_RATE_LIMIT_PER_MINUTE` | No | `30` | Per-IP cap per minute on the free endpoints, one bucket shared by `/verify`, `/upgrade`, `/health` and `/anchor-bills`, separate from the mint limit so proof polling never starves the paid path. `0` disables. Behind the bundled Tor hidden service every onion visitor shares one bucket (the Tor container is the peer). `UPGRADE_CLIENT_TOKEN` exempts a client's `/upgrade` calls. |
| `HEALTH_PROBE_CACHE_SECONDS` | No | `15` | `/health` caches its calendar probe (one status read, three bitcoind RPCs) for this long behind a single-flight lock: an expired answer is served stale while one caller refreshes it, so only a cold start makes callers wait. `0` disables the cache. |
| `UPGRADE_CLIENT_TOKEN` | No | — | Optional bearer token that exempts `/upgrade` (never `/verify`) from the verify bucket for the client that holds it; the client adapter presents it as `GATEWAY_UPGRADE_TOKEN`. A wrong token is simply anonymous; unset, the header is inert. Generate like `L402_SECRET_HEX`. |
| `L402_SECRET_HEX` | When L402 on | — | L402 macaroon root signing key (hex, at least 16 bytes; 32 recommended): `python3 -c 'import secrets; print(secrets.token_hex(32))'`. The gateway refuses to start without it when the door is on (development-only escape: `L402_ALLOW_EPHEMERAL_SECRET=true`); never read with the door off. |
| `OTS_BACKEND_MODE` | Yes | — | `calendar`, or `public` (testing only) |
| `OTS_CALENDAR_URL` | When `calendar` | — | URL of the operator's otsd (`http://otsd:14788` for the bundled compose profile; `http://127.0.0.1:14788` on the systemd path) |
| `TOR_PROXY` | No | — | SOCKS5h proxy for LND connections (`lnd` only). Required if `LND_HOST` is `.onion`. |
| `LND_TLS_VERIFY` | No | `false` | Set `true` only for CA-signed LND TLS certificates (`lnd` only). |

`OTS_BACKEND_MODE` validation: `calendar` requires `OTS_CALENDAR_URL`;
`public` requires it absent; any other value fails at startup; there is
no fallback between modes. The remaining variables are documented in
`.env.example`.

### The L402 door

With the door on, an unauthenticated `POST /timestamp` mints a Lightning
invoice for the flat price and an L402 macaroon bound to it, and answers
402. A retry with `Authorization: L402 <macaroon>:<preimage>` is
verified by preimage: the token must verify (signature, digest binding,
capability; any failure is a generic 401), the invoice must be settled,
its memo must be the digest, and its face amount must meet the price the
token was minted at. Settlement outranks the token's expiry. The face
amount is what is checked: phoenixd credits an invoice net of any
liquidity fee, and that fee nets the operator's credit, never the
client's proof. Minting is rate-limited per IP before any backend is
touched.

### The obligation log

Each settled payment is recorded in the obligation log
(`OBLIGATIONS_DB_PATH`) before stamping is attempted. If stamping fails
the obligation is retried by a sweeper until it is stamped; a settled
payment is never lost. Details: operator guide, "Durable obligation log".

### Stopping

**PAUSED.** A file (default `/var/lib/timestamp-gateway/PAUSED`, path in
`PAUSE_FILE`). While it exists the gateway answers `/health`
(`"status":"paused"`, HTTP 503) and nothing else: every other endpoint
returns 503 and the obligation sweeper skips its cycles. Settlement
outranks expiry, so paid tokens redeem after unpause and recorded
obligations wait in the log. Create the file to stop, delete it to
resume.

**The float backstop.** `float` classifies the anchor-wallet balance read
from the wallet-status file the balance-check timer writes: `alarm` below
5 × `STAMPER_FEE_CAP_SATS` (degrades; the door stays open); below 1 × the cap
the gateway pauses itself with the same semantics as PAUSED
(`"status":"auto_paused"`) and clears that pause when the balance
recovers. The operator's PAUSED label wins when both hold. Where the
timer does not run, `float` is `inactive`: the backstop is off, reported,
never degrading (operator guide, "Pricing").

### `/health`

`GET /health` (rate-limited per peer from the verify bucket; its calendar
probe is cached for `HEALTH_PROBE_CACHE_SECONDS`, one read however many
callers ask, and a stale answer is served while the one refresh runs)
returns HTTP 200 with `"status":"ok"` when every field below is in its
healthy set, and HTTP 503 otherwise: `"status":"degraded"`, or `"paused"`
/ `"auto_paused"` for the two stops above. The body also carries
`paused`, `payment_backend`, and `last_mint_at` (the time of the last
real mint attempt; `null` before the first).

| Field | Healthy | Degrades |
|---|---|---|
| `payment` | `ok`; `unknown` (no real mint since start) | `degraded` (the last mint failed) |
| `otsd` | `ok`; `n/a` (public mode) | `error` (unreachable, up but Bitcoin-blind, or not answering the JSON status); `needs_attention` (the calendar's deep-reorg detector reports a receipted anchor that left the chain; the findings are in `otsd_attention`) |
| `wallet` | `ok`; `absent` (alarm timer not installed) | `low`, `unknown`, `stale` |
| `proofs` | `ok`; `absent` | `mismatch`, `attention`, `unknown`, `stale` |
| `backup` | `ok`; `local_only`; `absent` | `attention`, `failed`, `unknown`, `stale` |
| `float` | `ok`; `inactive` (no balance reading: the backstop is off) | `alarm`, `stop` |
| `billing` | `off`; `ok` | `rejected`, `receipts_off`, `overdue`, `error` |
| `l402` | `on` or `off`, the door switch; never degrades | — |

`payment` is the outcome of the last real mint, never a reachability
probe. `otsd`: the probe reads the calendar's status line (since fork
c1db4dd `GET /` on otsd is one JSON object: `best_block`,
`anchor_receipts`, `needs_attention`, the queue); `otsd_attention` carries
the calendar's findings verbatim, present only when there are any. A
calendar still serving the retired status page is read through its old
markers and named in the gateway log as behind. `wallet`, `proofs` and
`backup` are read from the status files the ops timers write; `absent`
means the timer is not installed, `stale` that its file is older than the
configured maximum age. `billing`: `rejected` means receipt lines are
being rejected as malformed (the `billing_rejected` and `billing_bills`
counters are present only with billing on); `receipts_off` means the calendar's status says it is
anchoring without writing receipts (`anchor_receipts: "off"`), so every
anchor from then on is unbilled; a calendar that does not say (an older
fork) reads as unknown and never degrades on its own; `overdue` means an
unpaid anchor bill is older than 24 h (a bookkeeping alarm; the door,
stamping and redemption are never gated by billing state); `error` means
the receipts file is present
but unreadable. Details: operator guide, "Anchor billing" and
"Monitoring".

### `/anchor-bills`

With `ANCHOR_BILLING_ENABLED=true`, `GET /anchor-bills`
(`Authorization: Bearer <ANCHOR_BILLS_TOKEN>`; 404 when billing is off,
401 on a missing or wrong token; rate-limited from the verify bucket)
ingests the calendar's receipts file and returns every unpaid anchor bill
with a plain bolt11, minted on poll, never at ingestion (anchor bills are
not L402), plus the last week's paid bills and a `summary` with
`unpaid_count` and `unpaid_sats`. Each bill carries its `records` count,
so a payer can check `amount_sats` = `records` × the contracted
`PER_RECORD_SATS` before paying (`pay-anchor-bills.sh` in `auto-anchor`
does, refuses a count above its plausibility bound, and never pays an
anchor whose txid it has paid before, whatever invoice the bill now
carries); `records` is `null` on bills ingested under
the retired markup formula, whose stored amounts stand. The count errs
low, with one bounded exception: a digest re-submitted across a calendar
restart, or past the calendar's one-hour dedupe horizon, counts twice. A
receipt whose `records` is `0` (the calendar could not prove a count) is
never billed, with a warning naming the txid; a receipt missing `records`
(an un-upgraded fork) is malformed and skipped with a warning, as is one
whose txid is not 64 hex characters (txids are stored lowercase, so a
re-cased copy of a line never bills an anchor twice), whose fee is
negative, whose tree is empty, whose `confirmed_at` is more than a day
ahead of the gateway's clock, or whose `records` × rate would not fit the
ledger's 64-bit integer; each is skipped with a warning, none stops the
lines after it. The calendar fork must write the six-field receipt (with
`records`) before this gateway bills anything. Contract, example
response, and sizing the payer's per-bill ceiling, daily budget and
channel capacity to records-per-anchor-window × the rate: operator guide,
"Anchor billing".

### The calendar URI

The `calendar_url` in pending attestations is the calendar's `uri`
identity file, chosen once at first run and written into every
attestation the calendar issues; treat it as permanent. It need not
resolve: upgrades go through `/upgrade`, not that URL. The gateway's own
onion address (`http://<onion>.onion/`) is one choice: it lasts as long as
the Tor key, and the `tor_keys` backup (operator guide, "Tor hidden
service keys") then protects both the front door and the name inside
every attestation.

### What the gateway records

The gateway runs with uvicorn's access log disabled (`--no-access-log` in
both shipped launch paths): there is no per-request log, so no client-IP
or request-timing record exists anywhere. The application log contains no
digests, preimages, or client addresses; routine lines truncate payment
hashes to an 8-hex prefix, and only WARNING-level incident lines (a
failing re-stamp, a liquidity-fee event) carry a full payment hash. One
bounded residual: malformed proofs sent to `/verify` and `/upgrade` are
logged with tracebacks (at INFO), which can echo fragments of the
malformed input itself; such input is by definition not a valid proof.
Malformed tokens never get a traceback: unauthenticated input is logged
as a single WARNING line.

Where a digest does persist: the Lightning invoice memo is the digest,
which is how payment is verified, so every paid digest is stored, with
its payment hash, amount and time, in the operator's payment backend
(phoenixd's own database) and in any backup of it. Clients should assume
the operator's wallet layer retains that linkage even though the gateway
itself keeps digests only in its obligation log. On the calendar side,
the `journal.counts` sidecar stores per-second submission volume, no
digests (fork README, "Anchor receipts").

### What is publicly visible

| Scenario | What is publicly visible |
|---|---|
| Tor-only gateway + Tor-only Lightning node | No clearnet footprint |
| Tor gateway + clearnet Lightning node | Node pubkey and IP on the Lightning graph |
| Clearnet gateway | Gateway IP is public; the Lightning node depends on config |

Lightning graph exposure is permanent: a Lightning node that advertises a
clearnet IP has that association recorded by Lightning explorers, and it
cannot be undone.

## Verify

### Upgrading and verifying a proof

The `.ots` returned at submission is a valid pending receipt, not a
finalized proof: it carries a pending attestation pointing at the
operator's calendar. Once the calendar's anchoring transaction is
confirmed (timing: operator guide, "Proof lifecycle"), POST the pending
proof (base64) with its digest to `/upgrade`, which fetches the Bitcoin
attestation from the calendar and returns the anchored proof. A client
with many pending proofs presents `UPGRADE_CLIENT_TOKEN` as a bearer on
`/upgrade` and is not throttled by the verify bucket. Plain `ots upgrade
proof.ots` contacts the calendar URL inside the attestation directly, so
it works only where the operator serves that URL and the client has
whitelisted it with `-l <url>`. Then:

```bash
ots verify proof.ots    # against your own Bitcoin node or a header source you trust
```

An anchored proof verifies against Bitcoin without the gateway or the
calendar; `ots verify` is as independent as its view of Bitcoin, your own
node or a block-header source you chose to trust. A web verifier is a
third party trusted for the verdict. Nothing in an anchored proof
references a service that needs to stay alive.

### `/verify` and `/upgrade`

Both take the same JSON body, `digest` (64-char hex) and `ots` (the proof
bytes, base64; the `tr` below strips the line breaks GNU base64 inserts),
and return HTTP 200 with a JSON body whose `status` is one of:

| Status | Meaning |
|---|---|
| `anchored` | Digest matches and the proof carries a Bitcoin attestation. Independently verifiable against the Bitcoin block. |
| `pending` | Digest matches; the proof carries a calendar attestation awaiting Bitcoin anchoring. `/upgrade` returns the anchored proof once available. |
| `mismatch` | Well-formed proof, but it attests a different digest than the one supplied. |
| `no_attestations` | Well-formed proof, digest matches, but no recognized (bitcoin/pending) attestations: nothing to verify or upgrade. |
| `invalid` | The `ots` field is not decodable as an OTS proof (bad base64 or malformed bytes), or a proof nested deeper than the OpenTimestamps library will parse, which gets the same verdict from the public `ots` client. |

`verified` is `true` only for `anchored`. Every POST body is capped at
512 KiB, refused with 413 before it is read, and must declare its length
(a chunked body is refused with 411); the proof inside is further limited
to 256 KiB.

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

## Recover

### What the client must keep

The gateway stores nothing for the client. Two things are the client's
to keep: the anchored `.ots` file (the proof is the artifact; nothing on
the operator's side reissues it), and a record of what the digest is a
digest of (the proof shows that a SHA-256 digest existed at a point in
time and says nothing about what hashed to it; without the file or data
that produces the digest, or a digest-to-document ledger, the proof
proves nothing usable).

The pending receipt is to be upgraded promptly. Until the anchor confirms
and the client has upgraded (typically a few hours), the receipt's future
depends on that calendar continuing to exist: an operator who disappears,
or destroys calendar state, strands every pending receipt permanently,
while every already-anchored proof is untouched. The pending window is
the only period in which the client is trusting the operator; upgrading
closes it.

### What the operator must keep

The operator's side, the obligation log under `OBLIGATIONS_DB_PATH`, the
calendar's state and identity files, the Tor hidden-service keys and the
receipts file, is what the backup timer archives; `ops/BACKUP-RECOVERY.md`
is the restore procedure.

### Reorgs

A Bitcoin reorg shallower than the calendar's confirmation depth (6
blocks) returns the reorged commitments to the pending pool, and no
receipt is written before a transaction has 6 confirmations, so a shallow
reorg re-anchors once and bills once; that is the code path in the fork's
`stamper.py`, exercised by its dead-cycle test. A reorg that takes the
parent of an anchor still in flight leaves that anchor unbumpable, and the
stamper abandons the dead cycle with one warning and starts a fresh one
instead of waiting for a restart. A reorg deeper than the confirmation
depth leaves already-written attestations pointing at an orphaned block,
invalid. The calendar's deep-reorg detector (fork README, "Deep-reorg
detector") asks the wallet hourly about the last hundred receipted
anchors and reports one that left the chain in its status line's
`needs_attention`, logged at ERROR, never re-anchored automatically; this
gateway's `/health` carries it as `otsd: needs_attention` (503) with the
findings in `otsd_attention`, and the health monitor alerts on the
transition like any other degradation. What the detector cannot see: an
anchor re-mined at the same height after a calendar restart.
Re-verifying anchored proofs against Bitcoin is the check that remains.

## What it does not do

- Store files or documents.
- Log digests, preimages, or client identities ("What the gateway
  records").
- Prove authorship, ownership, provenance, or the truth of anything. A
  proof shows that the digest existed before the Bitcoin block that
  anchors it.
- Anchor to Bitcoin itself; that is the calendar's job.
- Provide a public calendar; the gateway is the door to the operator's
  own calendar, and `public` mode is a paid relay for testing.
- Reissue a lost proof, or stand behind a pending receipt after the
  calendar is gone ("What the client must keep").
- Retry against public calendars when the operator's calendar fails; the
  answer is a generic 502.
- Gate the door, stamping or redemption on billing state.
- See an anchor re-mined at the same height after a calendar restart
  ("Reorgs").

## Tests

No payment backend, Tor, or otsd required: the suite sets its own dummy
config and mocks all network calls. Test-only packages (pytest, httpx)
are pinned in `requirements-dev.txt`:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

For local development, `cp .env.example .env`, fill in the payment
backend variables and `OTS_BACKEND_MODE`, then
`uvicorn main:app --reload --no-access-log`.

What the suite pins:

| Claim | Test |
|---|---|
| L402 tokens: signature, digest binding, capability, and tamper cases reject with a generic 401 | `test_verify_token_*`, `test_malformed_authorization_returns_401`, `test_garbage_token_logs_single_warning_no_traceback` |
| Settlement outranks expiry: a settled invoice redeems its token after expiry, after a pause, and after a float stop | `test_settled_but_expired_token_redeems`, `test_paused_blocks_paid_redemption_and_unpause_serves_it`, `test_float_stop_blocks_paid_redemption_and_recovery_serves_it` |
| A settled payment is never lost when stamping fails | `test_obligation_stamp_failure_returns_502_and_persists_needs_stamp`, `test_sweeper_completes_pending_obligation_and_bumps_attempts` |
| The liquidity fee nets the operator's credit, never the client's proof | `test_liquidity_fee_netted_receive_still_verifies`, `test_liquidity_fee_payment_yields_obligation_and_proof` |
| Calendar mode never falls back to the public aggregators | `test_calendar_mode_never_falls_back_to_public_calendars` |
| Duplicate receipt lines bill once; `records: 0` never bills | `test_receipts_duplicate_txid_lines_dedupe`, `test_receipts_records_zero_not_billed_warns_txid` |
| A bill's amount never changes after ingestion | `test_amount_sats_immutable_across_rate_change` |
| A malformed receipt line never takes billing down; txid dedupe is case-insensitive | `test_receipts_overflow_line_rejected_not_fatal`, `test_receipts_txid_dedupe_is_case_insensitive` |
| A calendar anchoring with receipts off degrades `/health` | `test_health_billing_receipts_off_degrades` |
| The free door never contacts the payment backend | `test_free_mode_never_contacts_payment_backend` |

## Dependencies

- **pymacaroons 0.13.0** (last upstream release 2018): the L402 token
  core. Pure Python; its cryptography is PyNaCl, which is maintained. Its
  verify and reject surface (signature, digest binding, capability, tamper
  cases) is covered by the token tests above, so the pinned version is
  maintained here.
- **python-bitcoinlib** (0.12.x here; the otsd image pins 0.11.2):
  upstream releases are years apart. The calendar fork codes to the
  0.11.x–0.12.x intersection (`stamper.py` avoids `calc_weight()`, absent
  in 0.11.x); the RPC calls the fork makes are exercised by its
  `test_rpc_status.py` and `test_anchor_records.py`. Any Bitcoin Core RPC
  drift or CVE in this window is patched here.
- **plyvel** (the calendar's LevelDB binding, built from source in the
  otsd image against Debian's libleveldb): replaced py-leveldb, whose last
  release did not compile past Python 3.11, so the otsd image now runs on
  `python:3.13-slim`; the on-disk calendar database is LevelDB's either
  way and carried over unchanged (fork README, "Recover").
- **Base images**: all three Dockerfiles pin bases by digest, which also
  freezes CVEs. Refresh the digests and rebuild quarterly, and after any
  Debian or Python security advisory naming a pinned base.
- `api-endpoint` is standard-library Python; `auto-anchor` is shell and
  curl with one Python helper.
