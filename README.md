# timestamp-gateway

timestamp-gateway is an HTTP door in front of an OpenTimestamps calendar.
It accepts a SHA-256 digest, takes a Lightning payment for it, submits
the digest to the operator's own calendar (`otsd`, the calendar fork in
`opentimestamps-server`), and returns the calendar's `.ots` receipt;
later it upgrades that receipt to a proof carrying a Bitcoin attestation
on request and inspects proofs it is given — structurally: it holds no
Bitcoin view and never calls a proof verified ("`/verify` and
`/upgrade`"). It stores no client files and keeps no client accounts: the
client-facing interface is a digest in and a proof out. Its own
bookkeeping, the obligation log (SQLite under `OBLIGATIONS_DB_PATH`), is
operator-internal. Once a proof is anchored in Bitcoin it verifies
without this software; until then the pending receipt depends on the
operator's calendar ("What the client must keep").

```
client
  → gateway                       (this repository)
  → operator-controlled calendar  (otsd, the proof engine)
  → Bitcoin anchoring
  → .ots
```

## Configurations

Two configurations are supported across the four repositories, and this
one has one mode.

- **The single-host appliance**: the calendar alone, with the
  `api-endpoint` adapter in its `CALENDAR_URL` mode and no gateway; no
  Lightning, no payments. It is a configuration of the fork (its README,
  "Install: single host"), not of this repository.
- **The L402 public door**: this gateway in front of the operator's own
  `otsd`. `/timestamp` charges the flat `PRICE_PER_PROOF_SATS` through the
  402/L402 flow, always; the calendar is `OTS_CALENDAR_URL`, always the
  operator's, bundled in the compose stack (`--profile calendar`,
  `http://otsd:14788`) or external (`http://127.0.0.1:14788` on the
  systemd path). The free door (`L402_ENABLED=false`) and the relay to the
  public aggregators (`OTS_BACKEND_MODE=public`) were retired on
  2026-09-18: an `.env` that still configures either fails at startup
  with a diagnostic that says so ("Configuration").

Within the door, the configuration is the `.env`:

- **The payment backend.** phoenixd, the only one (the LND backend was
  removed on 2026-09-15); it runs outside the compose stack.
- **The front.** The bundled Tor hidden service, and the gateway published
  on `127.0.0.1:8000` only; or clearnet by changing the port mapping. The
  privacy cost of each is in "What is publicly visible".
- **The launch path.** Docker Compose (`docker-compose.yml`: gateway, tor,
  optionally otsd and an onion RPC bridge), or systemd units on the host
  (`deploy/`: gateway, otsd container, socat bridge, phoenixd).
- **The sibling repositories.** `api-endpoint` is a client adapter that
  submits through this door in its `GATEWAY_URL` mode; `auto-anchor` holds
  `pay402`, an L402 payer (one purchase per call, resumable) and its
  helpers. The fork's `Dockerfile` is a copy of `otsd/Dockerfile` here,
  kept in step by hand.

## What the gateway guarantees

Each rule is made by the code the section names, and pinned by the test
the "Tests" table names.

- Settlement outranks expiry: a settled invoice redeems its token after
  the token's expiry, after a pause, and after a float stop ("The L402
  door").
- A settled payment is never lost. The obligation is recorded before
  stamping; if stamping fails the row stays `needs_stamp` and the sweeper
  retries it until it is stamped ("The obligation log").
- The gateway submits to the operator's calendar and to nothing else:
  there is no fallback ("Configurations").
- A pause, the operator's or the float backstop's, loses nothing: paid
  tokens redeem after it and recorded obligations wait ("Stopping").
- There is no access log, and the application log carries no digest,
  preimage or client address; a malformed token or proof is logged as a
  fixed line naming an error class, never the input ("What the gateway
  records").
- `/verify` and `/upgrade` never say `verified`: they read a proof's
  structure and report `bitcoin_attestation_present` with `verified`
  null; only a verifier with a Bitcoin view can say more ("`/verify` and
  `/upgrade`").
- One `/upgrade` makes at most eight calendar lookups within fifteen
  seconds, and a calendar that does not answer is a 503, never
  "pending" ("`/verify` and `/upgrade`").
- Every POST body is bounded before routing and counted as it is read:
  any `Transfer-Encoding` is refused with 411, an ambiguous
  `Content-Length` with 400, and the endpoint never sees more than the
  declared length ("`/verify` and `/upgrade`").
- The gateway process holds no Bitcoin credential and no Docker access:
  under compose its environment is an explicit allowlist, never the
  whole `.env`; under systemd the RPC URL lives with the otsd user and
  the wallet's files with the phoenixd user ("Privilege boundary").

## Requirements

- Git, and Docker with Compose v2 for the compose path (version floor and
  install route: operator guide, "Prerequisites"). Compose v2 ships as
  the `docker compose` plugin or a standalone `docker-compose` binary;
  read `docker compose` in every command here as whichever this host has:

  ```bash
  docker compose version >/dev/null 2>&1 && C="docker compose" || C="docker-compose"
  ```

- A running phoenixd.
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
# Edit .env. Both of these are required:
#   L402_SECRET_HEX       python3 -c 'import secrets; print(secrets.token_hex(32))'
#   PRICE_PER_PROOF_SATS  the flat price every hash pays, integer >= 1
#                         (sizing arithmetic: operator guide, "Pricing")
# Then set:
#   PHOENIXD_HTTP_PASSWORD_LIMITED  phoenixd's limited-access password
#   BITCOIN_RPC_SERVICE_URL         for otsd ONLY (the gateway container
#                                   never receives it); three shapes in
#                                   .env.example

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

The answer is HTTP 402 with an L402 challenge in the `WWW-Authenticate`
header, `L402 macaroon="<token>", invoice="<bolt11>"`. Pay the invoice,
then retry with the macaroon and the payment preimage:

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

### Privilege boundary

The internet-facing gateway process never holds the anchor wallet's
Bitcoin RPC credential and has no Docker access, on either launch path
(operator guide, "Privilege boundary"):

- **Compose.** The gateway service's environment is an explicit
  allowlist in `docker-compose.yml`, interpolated name by name from
  `.env`; `BITCOIN_RPC_SERVICE_URL` is interpolated into the `otsd`
  service only. `docker compose config` shows it, and
  `test_review_compose_gateway_environment_is_an_allowlist` checks both
  directions: every variable `main.py` reads is in the list, and the RPC
  credential is in otsd's environment alone. A variable absent from
  `.env` reaches the container as the empty string, which the gateway
  reads as unset.
- **systemd.** Three users: `gateway` runs the gateway (`.env` is its
  `EnvironmentFile`, so `BITCOIN_RPC_SERVICE_URL` must not be in it on
  this path) and is not in the `docker` group, and its unit makes the
  Docker socket unreachable; `otsd` runs the otsd container, is the one
  docker-group member, and alone can read `/etc/systemd/system/otsd.env`
  where the RPC URL lives; `phoenixd` runs the wallet and owns its home
  (`/var/lib/phoenixd`, mode 700), which the gateway user cannot read, so
  the seed and the full spending password stay out of the web process's
  reach (until 2026-09-18 both units ran as `gateway`). The wallet alarm
  uses an RPC user of its own, whitelisted to `getbalances`, from its own
  file.

Tor adds latency, and a clearnet Lightning node puts its pubkey and IP
on the Lightning graph permanently ("What is publicly visible"). For a
clearnet gateway, change the port mapping in `docker-compose.yml` from
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
| `PAYMENT_BACKEND_TYPE` | No | `phoenixd` | `phoenixd`, the only backend; `lnd` fails startup naming its removal (2026-09-15) |
| `PHOENIXD_URL` | No | `http://127.0.0.1:9740` | phoenixd HTTP API endpoint; from the compose stack `http://host.docker.internal:9740`, which reaches a loopback-bound phoenixd on Docker Desktop only; on a Linux engine see the operator guide, "Payment backend (phoenixd)" |
| `PHOENIXD_HTTP_PASSWORD_LIMITED` | When `phoenixd` | — | phoenixd's `http-password-limited-access` from `phoenix.conf`, never the full `http-password` (scope and why: operator guide, "Payment backend (phoenixd)"). The old name `PHOENIXD_HTTP_PASSWORD` is read as a fallback. |
| `PRICE_PER_PROOF_SATS` | Yes | — | The flat price in sats every hash pays at submission, the whole quote; the gateway reads no feerates. No default: startup fails without it. Integer ≥ 1; `0` is refused (a token minted at 0 could never redeem). Sizing arithmetic: operator guide, "Pricing". |
| `STAMPER_FEE_CAP_SATS` | No | `20000` | Mirror of otsd's `--btc-max-fee` flag in sats (the flag takes BTC; 0.0002 BTC = 20,000 sats; keep the two in sync). Used only to derive the float backstop thresholds. |
| `RATE_LIMIT_PER_MINUTE` | No | `10` | Per-IP cap per minute on unauthenticated invoice minting (402 challenges); every anonymous request makes phoenixd sign and store an invoice. `0` disables. Over-limit requests get 429 with `Retry-After`. |
| `VERIFY_RATE_LIMIT_PER_MINUTE` | No | `30` | Per-IP cap per minute on the free endpoints, one bucket shared by `/verify`, `/upgrade` and `/health`, separate from the mint limit so proof polling never starves the paid path. `0` disables. Behind the bundled Tor hidden service every onion visitor shares one bucket (the Tor container is the peer). `UPGRADE_CLIENT_TOKEN` exempts a client's `/upgrade` calls. |
| `HEALTH_PROBE_CACHE_SECONDS` | No | `15` | `/health` caches its calendar probe (one status read, three bitcoind RPCs) for this long behind a single-flight lock: an expired answer is served stale while one caller refreshes it, so only a cold start makes callers wait. `0` disables the cache. |
| `UPGRADE_CLIENT_TOKEN` | No | — | Optional bearer token that exempts `/upgrade` (never `/verify`) from the verify bucket for the client that holds it; the client adapter presents it as `GATEWAY_UPGRADE_TOKEN`. A wrong token is simply anonymous; unset, the header is inert. Generate like `L402_SECRET_HEX`. |
| `L402_SECRET_HEX` | Yes | — | L402 macaroon root signing key (hex, at least 16 bytes; 32 recommended): `python3 -c 'import secrets; print(secrets.token_hex(32))'`. The gateway refuses to start without it (development-only escape: `L402_ALLOW_EPHEMERAL_SECRET=true`). |
| `OTS_CALENDAR_URL` | Yes | — | URL of the operator's otsd (`http://otsd:14788` for the bundled compose profile; `http://127.0.0.1:14788` on the systemd path). The gateway submits to it and to nothing else; startup fails without it. |

Retired names: `PRICE_MARKUP`, `ANCHOR_BILLING_ENABLED`,
`PER_RECORD_SATS`, `ANCHOR_RECEIPTS_PATH`, `ANCHOR_BILLS_TOKEN`,
`L402_ENABLED` and `OTS_BACKEND_MODE` (anchor billing, the free door and
the public relay, all retired on 2026-09-18) are named in one startup
warning and ignored, as are the earlier feerate-model names (operator
guide, "Retired pricing variables"). Two values are refused at startup
instead, each with a diagnostic naming the retirement and the way on:
`L402_ENABLED=false` (a deployed free door is never silently turned into
a paid one) and `OTS_BACKEND_MODE=public` (the gateway submits to the
operator's calendar only). The remaining variables are documented in
`.env.example`.


### The L402 door

An unauthenticated `POST /timestamp` mints a Lightning
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
| `otsd` | `ok` | `error` (unreachable, up but Bitcoin-blind, or not answering the JSON status); `needs_attention` (the calendar's deep-reorg detector reports a receipted anchor that left the chain; the findings are in `otsd_attention`) |
| `wallet` | `ok`; `absent` (alarm timer not installed) | `low`, `unknown`, `stale` |
| `proofs` | `ok`; `absent` | `mismatch`, `attention`, `unknown`, `stale` |
| `backup` | `ok`; `local_only`; `absent` | `attention`, `failed`, `unknown`, `stale` |
| `float` | `ok`; `inactive` (no balance reading: the backstop is off) | `alarm`, `stop` |

`payment` is the outcome of the last real mint, never a reachability
probe. `otsd`: the probe reads the calendar's status line (since fork
c1db4dd `GET /` on otsd is one JSON object: `best_block`,
`needs_attention`, the queue); `otsd_attention` carries the calendar's
findings verbatim, present only when there are any. A calendar still
serving the retired status page is read through its old `Best-block`
marker and named in the gateway log as behind. `wallet`, `proofs` and
`backup` are read from the status files the ops timers write; `absent`
means the timer is not installed, `stale` that its file is older than the
configured maximum age. Details: operator guide, "Monitoring".


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
failing re-stamp, a liquidity-fee event) carry a full payment hash. No
exception text carrying client input reaches the log: a malformed proof
sent to `/verify` or `/upgrade` is one fixed INFO line, a malformed token
one WARNING line naming the exception class and nothing else (until
2026-09-15 that line carried the exception's text, and so the token's
bytes — the review's kit put a synthetic record in a token and read it
back out of the log), and a calendar lookup that fails is one INFO line
naming the error class. `test_review_malformed_token_content_never_
reaches_the_log` pins it.

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
attestation from the calendar and returns the proof carrying it (status
`bitcoin_attestation_present`, `verified` null: structural). A client
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

Both are **structural inspection, not verification.** They read a
proof's encoding, the digest it is about and the attestation nodes it
carries; neither checks the attested merkle root against a Bitcoin block
header, because the gateway holds no Bitcoin view by design ("Privilege
boundary"). A fabricated attestation naming a real block height is
indistinguishable to them from a genuine one — the 2026-09-15 review
built one attesting to block 0 and, until then, both endpoints called it
verified. The verifier is whatever holds a Bitcoin view: `ots verify`
against your own node, or the calendar fork's `ops/verify_claim.py`
against an authenticated headers file.

Both take the same JSON body, `digest` (64-char hex) and `ots` (the proof
bytes, base64; the `tr` below strips the line breaks GNU base64 inserts),
and return HTTP 200 with a JSON body whose `status` is one of:

| Status | Meaning |
|---|---|
| `bitcoin_attestation_present` | Digest matches and the proof carries a Bitcoin attestation node. Not checked against Bitcoin here: `verified` is `null`. |
| `pending` | Digest matches; the proof carries a calendar attestation, and the calendar (asked, on `/upgrade`) has not anchored it yet. `/upgrade` returns the attested proof once available. |
| `mismatch` | Well-formed proof, but it attests a different digest than the one supplied. |
| `no_attestations` | Well-formed proof, digest matches, but no recognized (bitcoin/pending) attestations: nothing to inspect or upgrade. |
| `invalid` | The `ots` field is not decodable as an OTS proof (bad base64 or malformed bytes), or a proof nested deeper than the OpenTimestamps library will parse, which gets the same verdict from the public `ots` client. |

and, from `/upgrade` only, HTTP 503 with `status`
`calendar_unavailable` when every calendar lookup failed by transport:
the calendar did not answer, which is not "not anchored yet".

Every answer carries `verification: "structural"`, a `verification_note`
saying what was and was not checked, `bitcoin_attestation_present`
(with `bitcoin_anchored` as the same flag under its pre-2026-09-15 name,
for clients built against it) and `verified`: `null` for
`bitcoin_attestation_present` — present, not checked — and `false` for
every other status. `verified` is never `true`.

One `/upgrade` is bounded: at most 8 calendar lookups, at most 15 seconds
in all, 5 seconds per lookup, each distinct commitment looked up once,
and the walk stops as soon as a Bitcoin attestation is in hand; the
answer's `upgrade` field reports `calendar_queries` and
`budget_exhausted`. (A 3.5 KB proof with a hundred pending sub-stamps
used to make a hundred sequential lookups at ten seconds each.)

Every POST body is bounded before routing and counted as it is read: the
cap is 512 KiB, a declared length above it is refused with 413 before a
byte is read, any `Transfer-Encoding` header is refused with 411 (the
pinned HTTP parser accepts `Content-Length: 1` beside
`Transfer-Encoding: chunked` and frames the body by the chunks, which is
how a 525 KB body once reached a 200), a missing or non-numeric
`Content-Length` with 411, disagreeing repeated ones with 400, and the
ASGI receive is wrapped with a byte counter so the endpoint never sees
more than the declared length whatever the parser framed. The proof
inside is further limited to 256 KiB.

```bash
# Verify a proof against the digest it should attest:
curl -X POST http://localhost:8000/verify \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\",\"ots\":\"$(base64 < proof.ots | tr -d '\n')\"}"

# Upgrade a pending proof once the calendar has anchored — the response's
# `ots` field carries the proof with its Bitcoin attestation, base64:
curl -X POST http://localhost:8000/upgrade \
  -H "Content-Type: application/json" \
  -d "{\"digest\":\"$DIGEST\",\"ots\":\"$(base64 < proof.ots | tr -d '\n')\"}" \
  | python3 -c 'import sys,json,base64; sys.stdout.buffer.write(base64.b64decode(json.load(sys.stdin)["ots"]))' \
  > proof-attested.ots
ots verify proof-attested.ots   # the verification: against your own node
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
is the restore procedure. The obligation log is captured by a SQLite
online snapshot that is checked (integrity, the table, the newest row
read beforehand) before it counts; without a usable one the backup is
`failed` while the gateway runs, because a raw copy of a live WAL
database is not a snapshot of any instant. The calendar directory is a
backup only at a boundary the run established or verified
(`CALENDAR_BACKUP_BOUNDARY`: the calendar writer stopped by the run or by
the operator, or an operator's filesystem snapshot); copied while `otsd`
writes it is a hot copy, the run is `failed` and says so, and the fork's
contract says what such a copy starts as (`ops/BACKUP-RECOVERY.md`).

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
  own calendar.
- Reissue a lost proof, or stand behind a pending receipt after the
  calendar is gone ("What the client must keep").
- Retry against public calendars when the operator's calendar fails; the
  answer is a generic 502.
- Stamp for free, or relay to the public calendars: both were retired on
  2026-09-18.
- See an anchor re-mined at the same height after a calendar restart
  ("Reorgs").
- Verify a proof against Bitcoin: `/verify` and `/upgrade` are structural
  ("`/verify` and `/upgrade`"), and the gateway holds no Bitcoin view.

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
backend variables and `OTS_CALENDAR_URL`, then
`uvicorn main:app --reload --no-access-log`.

What the suite pins:

| Claim | Test |
|---|---|
| L402 tokens: signature, digest binding, capability, and tamper cases reject with a generic 401 | `test_verify_token_*`, `test_malformed_authorization_returns_401`, `test_garbage_token_logs_single_warning_no_traceback` |
| Settlement outranks expiry: a settled invoice redeems its token after expiry, after a pause, and after a float stop | `test_settled_but_expired_token_redeems`, `test_paused_blocks_paid_redemption_and_unpause_serves_it`, `test_float_stop_blocks_paid_redemption_and_recovery_serves_it` |
| A settled payment is never lost when stamping fails | `test_obligation_stamp_failure_returns_502_and_persists_needs_stamp`, `test_sweeper_completes_pending_obligation_and_bumps_attempts` |
| The liquidity fee nets the operator's credit, never the client's proof | `test_liquidity_fee_netted_receive_still_verifies`, `test_liquidity_fee_payment_yields_obligation_and_proof` |
| The gateway submits to the operator's calendar and nothing else | `test_calendar_mode_never_falls_back_to_public_calendars` |
| An `.env` still configuring the free door or the public relay is refused at startup; leftover billing names are warned about once and ignored | `test_free_door_false_is_refused_with_a_migration_diagnostic`, `test_public_relay_is_refused_with_a_migration_diagnostic`, `test_billing_names_are_warned_once_and_ignored` |
| A fabricated Bitcoin attestation is never reported verified; `verified` is null or false | `test_review_fabricated_attestation_is_never_reported_verified`, `test_review_verified_is_false_for_every_non_attested_state` |
| The body cap holds against the real HTTP parser with dual framing, and bytes are counted | `test_review_body_cap_holds_against_the_real_parser_with_dual_framing`, `test_review_body_cap_counts_bytes_the_parser_delivers` |
| A malformed token's content never reaches the log | `test_review_malformed_token_content_never_reaches_the_log` |
| One upgrade is bounded; calendar-unavailable is a 503 | `test_review_upgrade_calendar_work_is_bounded_per_request`, `test_review_calendar_unavailable_is_a_503_not_pending` |
| The gateway container's environment is an allowlist without the RPC credential | `test_review_compose_gateway_environment_is_an_allowlist` |
| Ops settings are resolved after `.env`; a failed proof scan is attention and nonzero; a backup without a usable snapshot is failed | `test_review_wallet_alarm_resolves_every_setting_after_env_is_loaded`, `test_review_proof_scan_traversal_failure_is_attention_and_nonzero`, `test_review_backup_without_a_usable_snapshot_is_failed_while_writers_run` |

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
  curl with four standard-library Python helpers.
