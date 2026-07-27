# Operator guide

timestamp-gateway is portable paid OpenTimestamps calendar-node software. The gateway is the paid front door. The OTS calendar backend (otsd) is the proof engine. This document covers both.

---

## Architecture

```
client
  → Lightning-gated gateway       (this repo — collects payment, forwards digest)
  → operator-controlled OTS calendar  (otsd — aggregates, anchors in Bitcoin)
  → Bitcoin anchoring             (batched OP_RETURN transactions)
  → .ots                          (returned to client as pending receipt)
```

The gateway cannot produce Bitcoin-anchored proofs on its own. It requires a running OTS calendar backend (otsd). The initial `.ots` returned to the client is a pending receipt. Once the anchoring transaction confirms — timing in "Proof lifecycle" — the proof can be upgraded to a Bitcoin-anchored one.

---

## Prerequisites

- Git — the first-run checklist starts with two `git clone`s (`apt install git` on a fresh Ubuntu box)
- Docker Engine 24+ and Docker Compose v2.17+ — quoted as the earliest releases with the BuildKit named-context support the calendar profile's build uses; not tested further back. On a fresh Ubuntu box, install both from Docker's official repository — https://docs.docker.com/engine/install/ubuntu/ — which ships current Engine plus the Compose plugin; distro packages can trail these floors. Compose v2 ships two ways — the `docker compose` plugin or a standalone `docker-compose` binary. Detect which this box has, and read `docker compose` in every command in these docs as `$C`:

  ```bash
  docker compose version >/dev/null 2>&1 && C="docker compose" || C="docker-compose"
  ```
- A running phoenixd instance (the live payment backend — see "Payment backend (phoenixd)" below for how to get one) — or an LND node with REST API and invoice macaroon if using the LND test-payer / alternative backend
- Inbound Lightning liquidity on the payment backend
- An OTS calendar backend (otsd) — bundled via `--profile calendar` or external
- An **existing, already-synced** Bitcoin Core node reachable by otsd, with a wallet loaded and funded. This is the heaviest prerequisite, and this guide does not teach it: standing up a node from nothing is a multi-day project — on the order of 750 GB of initial-block-download ingress, days of sync time, and real money to fund the wallet. If you do not already run a node, start at https://bitcoincore.org and come back.

You do not need a VPS. You do not need a static IP. You do not need to expose any clearnet ports if you use Tor-only mode.

---

## First-run checklist

1. Clone this repository, clone the calendar fork next to it (clone command: "Deploying the calendar (otsd)" below), and copy `.env.example` to `.env`.
2. Generate the L402 signing key and set `L402_SECRET_HEX` in `.env`: `python3 -c 'import secrets; print(secrets.token_hex(32))'`. The gateway refuses to start without it.
3. Set `PAYMENT_BACKEND_TYPE=phoenixd` (the default) and fill in `PHOENIXD_URL` (`http://host.docker.internal:9740` for a phoenixd on this host — Docker Desktop only; on a Linux engine phoenixd must be bound where the container can reach it, see "Payment backend (phoenixd)") and `PHOENIXD_HTTP_PASSWORD_LIMITED`. Only fill in `LND_*` if using the LND test-payer / alternative backend.
4. (lnd test-payer backend only) Set `TOR_PROXY=tor:9050` if `LND_HOST` is a `.onion` address; leave blank otherwise.
5. Set `OTS_BACKEND_MODE=calendar` and `OTS_CALENDAR_URL=http://otsd:14788`.
6. Set `BITCOIN_RPC_SERVICE_URL` for otsd (full URL including credentials, in `.env` only — see `.env.example` for the LAN, onion-bridge, and systemd shapes).
7. First run only: initialise the calendar identity (see "Deploying the calendar" below).
8. Start the full stack: `docker compose --profile calendar up -d --build` (first run; plain `up -d` thereafter — see "Deploying the calendar (otsd)").
9. Check logs: `docker compose logs -f`.
10. Retrieve onion address: `docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname`. Then verify the onion answers, from the box itself, through the stack's own Tor client:

    ```bash
    # The network is <project>_ts_net; <project> defaults to this repo's
    # directory name (`docker network ls` shows the real name).
    docker run --rm --network timestamp-gateway_ts_net curlimages/curl -s \
      --socks5-hostname tor:9050 http://<your-onion>.onion/health
    ```

    Expected output: the same health JSON as the direct check against `http://127.0.0.1:8000/health`.
11. Test the endpoint with `curl` (see README quick start). The paid leg needs a second, independently funded Lightning wallet — you cannot pay the gateway from its own phoenixd; see the README, "The payer side".

---

## OTS backend modes

### calendar (real mode — use this in production)

```
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788      # bundled compose profile
# OTS_CALENDAR_URL=http://127.0.0.1:14788  # systemd path (host-networked otsd)
```

The gateway forwards paid digests to the operator's own otsd instance. otsd aggregates submissions and anchors the Merkle root in Bitcoin when commitments are pending, batched on the anchoring interval (see "Bitcoin transaction cost"). This is the only production mode.

**There is no silent fallback.** Calendar failure means 502, never a retry against public aggregators — the failure behavior and the strict mode validation are pinned in the README ("OTS backend modes").

### public (compatibility/testing only — not for production)

```
OTS_BACKEND_MODE=public
# OTS_CALENDAR_URL must NOT be set
```

The gateway forwards paid digests to the four public OpenTimestamps aggregators (`a.pool.opentimestamps.org`, etc.). This mode is provided so you can test the payment flow without running otsd. It is not the real target.

**Do not use `public` mode in production.** In public mode, the gateway is a paid relay to other operators' infrastructure — not an independent calendar node (the README's "What we corrected" section records why that matters).

---

## OTS calendar backend (otsd)

### What otsd is

otsd is the OpenTimestamps calendar server. It accepts raw digest bytes over HTTP (`POST /digest`), aggregates them into a Merkle tree, and — when commitments are pending — submits a Bitcoin transaction containing an OP_RETURN output with the Merkle root, at most one per anchoring interval (see "Bitcoin transaction cost"). The resulting proof links any submitted digest to the block height of that Bitcoin transaction.

### What otsd needs

- A Bitcoin Core node reachable via JSON-RPC.
- A wallet loaded in Bitcoin Core (use `bitcoin-cli loadwallet` or `createwallet`).
- Enough BTC in the wallet to pay for OP_RETURN transaction fees (sizing estimate under "Bitcoin transaction cost" below). otsd does not spend to the wallet — it only draws from it to fund transactions.
- A persistent data directory for the calendar state (`/calendar` in the container — the `otsd_calendar` volume under compose).

### Bitcoin transaction cost

otsd submits an anchoring transaction only when commitments are pending, and at most one per `--btc-min-tx-interval` (default 6 hours = 21600 seconds) — at most 4 per day, and none while idle. Each transaction contains a single OP_RETURN output. Cost depends on prevailing on-chain fee rates: at 5–20 sat/vbyte a single anchoring transaction costs roughly 500–2000 sats, and the fork fee-bumps a stuck transaction, which can raise the per-anchor cost. Wallet sizing is an estimate, not a measured figure — no consumption record is committed yet: at the default cadence a 100,000-sat wallet should cover weeks of continuous anchoring, far longer at low volume where most intervals see no pending commitments, and 50k–100k sats is a reasonable floor for extended low-volume operation.

The bump ladder is capped: the shipped run commands set `--btc-max-fee 0.0002` (the flag takes BTC; 0.0002 BTC = 20,000 sats), bounding what one anchor cycle can spend in total — an RBF replacement pays its own whole fee and only one transaction of the ladder confirms, so the cap is the most one anchor can cost. When the cap binds, otsd logs `Maximum txfee reached!` and stops bumping; the last under-cap transaction stays pending until fees fall or it confirms. A sustained fee spike above the cap means proofs stay pending longer — that is the intended trade: wallet safety over anchor latency.

otsd will stop anchoring if the wallet is empty. Proofs submitted during a gap are eventually anchored when the wallet is refilled, but the gap delays proof finalisation.

### Bitcoin RPC configuration

otsd reads a single env var — the full RPC URL including credentials. Set it in gitignored `.env` ONLY (never in tracked files, never on a command line). Three shapes, by where your node is:

```
# Node reachable from this host (LAN, or bitcoind on the Docker host):
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@host.docker.internal:18332/wallet/otsd-hot
# Onion-only node, bundled Tor bridge (--profile onion-rpc; set BITCOIN_RPC_ONION):
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@rpc-bridge:18332/wallet/otsd-hot
# systemd path (host socat bridge, deploy/socat-bitcoin-rpc.service.example):
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@127.0.0.1:18332/wallet/otsd-hot
```

(`host.docker.internal` reaches a loopback-bound bitcoind on Docker Desktop only — on a Linux engine bind the node's RPC where the container can reach it, or address a LAN node by IP; the same caveat as `PHOENIXD_URL`, see "Payment backend (phoenixd)".)

Two conventions in these examples to know about before copying one:

- **Port:** mainnet Bitcoin Core answers RPC on **8332**. The `18332` in the examples is this repo's local-bridge convention — the socat/onion bridges listen on 18332 and forward to the node's 8332 — kept consistent across the shapes so the bridge and no-bridge URLs differ only in host. Talking to a mainnet node directly with no bridge, use 8332.
- **Wallet name:** the `/wallet/<name>` path segment must name the wallet actually loaded in Bitcoin Core — the examples use `otsd-hot`, so either `bitcoin-cli createwallet otsd-hot` (then load and fund it) or change the segment to your wallet's name. A URL naming a wallet that is not loaded fails every call.

On the systemd path otsd reads this URL from `/etc/systemd/system/otsd.env` instead of the repo `.env` — see `deploy/otsd.service.example`.

For a directly reachable node, ensure the otsd host is allowed by `rpcbind`/`rpcallowip` in bitcoin.conf.

**Pruned nodes:** A pruned Bitcoin Core node is acceptable for otsd's transaction submission role. otsd does not need to download the full chain — it only submits transactions and reads the current tip.

### Same-host bitcoind under compose

Running bitcoind on the same box as the compose stack needs three things lined up: where bitcoind listens, whom it allows, and whether the wallet survives a bitcoind restart.

**Bind and allow.** The otsd container reaches the host at `host.docker.internal`, which compose maps to the docker0 bridge address (`172.17.0.1` on a default Linux engine); the connection arrives with a source address on the compose network's subnet. So bitcoind must bind the bridge address and allow the compose subnet — credentials go in `bitcoin.conf` (and in the `.env` URL — nowhere else):

```
# bitcoin.conf
rpcbind=127.0.0.1
rpcbind=172.17.0.1
rpcallowip=127.0.0.1
rpcallowip=172.18.0.0/16    # the compose subnet — read the real one, below
rpcuser=<rpc-user>
rpcpassword=<rpc-password>
```

Docker assigns the compose subnet — read it rather than guessing:

```bash
docker network inspect timestamp-gateway_ts_net \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
```

(The network is `<project>_ts_net`; `<project>` defaults to this repo's directory name.) The RPC URL is then the direct-node shape on mainnet's own port — no bridge, so 8332:

```
BITCOIN_RPC_SERVICE_URL=http://<rpc-user>:<rpc-password>@host.docker.internal:8332/wallet/otsd-hot
```

**Wallet load persistence.** A wallet loaded with plain `loadwallet` does not survive a bitcoind restart; the failure arrives later and quietly: `/health` shows `otsd: error`, the gateway log says `otsd HTTP up but Bitcoin-blind` (see "Monitoring"), and otsd's own log shows `JSONRPCError -18: Requested wallet does not exist or is not loaded`. Make the load persistent:

```bash
bitcoin-cli loadwallet otsd-hot true    # second argument = load_on_startup
# or at creation:
bitcoin-cli -named createwallet wallet_name=otsd-hot load_on_startup=true
```

### Starting with the bundled otsd profile

```bash
docker compose --profile calendar up -d
```

This starts `gateway`, `tor`, and `otsd`. otsd is not publicly exposed — it publishes no port; the gateway reaches it at `http://otsd:14788` on the compose network (set `OTS_CALENDAR_URL=http://otsd:14788`).

### Deploying the calendar (otsd)

The otsd image ships Python + dependencies only. The calendar **code** is the `opentimestamps-server` fork — `https://github.com/ab21tor/opentimestamps-server`, branch `calendar-ops` — mounted at `/app` at runtime, so code-only updates deploy with a pull + restart, no rebuild. The fork checkout is passed to the build as a named context; compose does this automatically from `OTSD_FORK_PATH`.

```bash
# 1. Clone the opentimestamps-server fork (the calendar code) next to this
#    repo — or anywhere, if you set OTSD_FORK_PATH in .env to match.
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server ../opentimestamps-server

# 2. Set BITCOIN_RPC_SERVICE_URL in .env (gitignored). See .env.example for
#    the three shapes; for an onion-only node also set BITCOIN_RPC_ONION and
#    start with --profile onion-rpc in step 4 (the bundled Tor bridge), or
#    install the host socat bridge instead (systemd path):
#      sudo cp deploy/socat-bitcoin-rpc.service.example /etc/systemd/system/socat-bitcoin-rpc.service
#      sudo sed -i 's/YOUR_NODE_ONION/replace-with-your-node-onion.onion/' /etc/systemd/system/socat-bitcoin-rpc.service
#      sudo systemctl daemon-reload && sudo systemctl enable --now socat-bitcoin-rpc

# 3. First run only: give the calendar its identity — the URI callers will
#    see in pending attestations, the HMAC key, and a donation address its
#    web page displays. otsd exits at startup until all three exist.
#    Replace both example values with your own (the address below is the
#    BIP173 example, not yours).
docker compose --profile calendar run --rm otsd sh -c \
  'echo "https://calendar.example.com/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key \
   && echo "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" > /calendar/donation_addr'

# 4. Start it (add --profile onion-rpc if using the bundled bridge).
#    First run: --build. Thereafter plain `up -d` is enough — code updates
#    come from the fork checkout, not the image (see "Updating").
docker compose --profile calendar up -d --build

# 5. Verify: expect a Bitcoin RPC connection and no auth errors.
docker compose logs otsd
```

Building the image by hand (outside compose) uses the same named context:

```bash
docker build -t otsd-local --build-context fork=../opentimestamps-server otsd/
```

On the systemd path that image runs as a unit: `deploy/otsd.service.example` wraps it in `docker run --network host` with the fork checkout mounted at `/app`, so code-only updates deploy with a pull + restart.

### Pointing to an external otsd

If you run otsd on a separate host or VM:

```
OTS_CALENDAR_URL=http://<host>:<port>
```

Do not expose otsd on a public port. It has no authentication. Access should be restricted to the gateway only.

### Proof lifecycle

1. **Immediate:** The gateway submits the digest to otsd and receives a receipt with a `PendingAttestation` pointing to the calendar URL. This is the `.ots` file returned to the client. It is not yet Bitcoin-anchored.
2. **Within hours:** otsd submits a Bitcoin transaction anchoring the Merkle root of the pending digests — at most one transaction per anchoring interval ("Bitcoin transaction cost") — then waits for `--btc-min-confirmations` (default 6) before writing the Bitcoin attestation. Typically a few hours end to end: best case about an hour, worst case the full anchoring interval plus confirmation time. (An expectation derived from the defaults — no timing record is committed.)
3. **Upgrade:** The client POSTs the pending proof (base64) with its digest to the gateway's `/upgrade` endpoint, which fetches the Bitcoin anchoring from the operator's calendar and returns the anchored proof (see the README's `/verify` and `/upgrade` status vocabulary). Plain `ots upgrade proof.ots` reaches the calendar URL inside the pending attestation directly, so it works only if the operator serves that URL publicly — with the calendar private, as this guide recommends, the gateway endpoint is the client path.
4. **Verify:** The client runs `ots verify proof.ots` to verify the proof against the Bitcoin blockchain independently.

The `.ots` file returned immediately by the gateway is a valid receipt. It is not incomplete or broken. It simply has not been finalized yet because Bitcoin blocks take time.

---

## Durable obligation log

Payment settlement and calendar submission are two separate steps. A caller can pay, the invoice can settle in the payment backend, and then the OTS calendar (otsd) can be unreachable past the submission retry window. Without a durable record, that settled payment would be lost: the caller paid but no proof was ever produced.

The gateway closes that gap with an **obligation log** — a small SQLite database, separate from the OTS proof path.

### How it works

1. On a fully verified paid request (macaroon valid and digest-bound, preimage matches the payment hash, invoice settled for this digest at the required amount), the gateway records an obligation row keyed on the payment hash with status `needs_stamp` **before** it submits the digest to otsd. The write is committed to disk first.
2. If stamping succeeds, the row is marked `stamped` and the proof is also placed in the in-memory proof cache for instant re-serve. The caller gets their `.ots`.
3. If stamping fails, the caller gets a `502` and the row stays `needs_stamp`.
4. A background **sweeper** thread (started with the gateway) retries every `needs_stamp` row on an interval. On success it stamps the digest, marks the row `stamped`, and populates the proof cache. It retries indefinitely and never drops a paid obligation.

The obligation log and the proof cache coexist: the cache is the instant path for a re-presented token, the DB is the durable backstop. Both are always live for every paying caller.

### What it is not

- It is **not** part of proof validity. The database is never consulted to verify a proof. A finished `.ots` verifies against Bitcoin with zero dependency on this database, the payment backend, or the gateway itself.
- It is **not** proof storage. It records payment-hash → digest → status only; it never holds proof bytes durably (the proof cache is in-memory and resets on restart, which is fine — the obligation row drives re-stamping, and re-stamping the same digest is idempotent).

### Configuration

Two environment variables (see `.env.example`):

- `OBLIGATIONS_DB_PATH` — path to the SQLite file. Default `/var/lib/timestamp-gateway/obligations.db`. The process **fails loud at startup** if this path is unwritable, rather than running without a durable log.
- `OBLIGATION_SWEEP_INTERVAL` — how often the sweeper retries `needs_stamp` rows, in seconds. Default `1800` (30 minutes).

### Persistence

Under Docker Compose the database lives on the persistent `gateway_data` volume, mounted at `/var/lib/timestamp-gateway`. This volume must survive container recreation — otherwise a settled-but-unstamped obligation could be lost. SQLite runs in WAL mode, so the database is accompanied by `-wal` and `-shm` sidecar files; back up all three together (see the backup notes below).

> Backup: on the live VPS, `ops/backup-live-state.sh` and `ops/BACKUP-RECOVERY.md` cover the operational backup set and now include the obligations database and its `-wal`/`-shm` sidecars. If you run the gateway outside that layout, ensure your own backups capture `OBLIGATIONS_DB_PATH` and its two sidecar files while the gateway is stopped (or use SQLite's `.backup`/`VACUUM INTO` for a consistent hot copy).

---

## Payment backend (phoenixd)

phoenixd is the live payment backend: a self-custodial Lightning node daemon by ACINQ. The gateway needs exactly two values from it — `PHOENIXD_URL` (its HTTP API) and `PHOENIXD_HTTP_PASSWORD_LIMITED`.

- **Install:** download a release from https://github.com/ACINQ/phoenixd (or build from source) and run `phoenixd`. Upstream docs: https://phoenix.acinq.co/server. On first run it creates its data directory (`~/.phoenix`) including the wallet seed — back the seed up; it is the money.
- **API password:** first run also generates two passwords in `~/.phoenix/phoenix.conf`. Use `http-password-limited-access` as `PHOENIXD_HTTP_PASSWORD_LIMITED` — it covers the gateway's entire phoenixd surface (`createinvoice`, `getinfo`, `payments/incoming`) and cannot reach `/payinvoice` or `/sendtoaddress` (verified against phoenixd 0.8.0). Never use the full `http-password` here: that hands an internet-facing process the authority to drain the wallet.
- **URL:** the HTTP API listens on `127.0.0.1:9740` by default. For a host-run gateway (systemd path) `PHOENIXD_URL=http://127.0.0.1:9740` works as-is. For a container-run gateway (compose path) reachability is NOT automatic: `host.docker.internal` reaches a loopback-bound phoenixd only on Docker Desktop (macOS/Windows, via its VM proxy). On a Linux engine — VPS, Pi, Umbrel, Start9 — it maps to a bridge IP where a loopback-bound phoenixd is not listening, and the connection is refused. phoenixd must be bound where the container can reach it: `--http-bind-ip` on the docker0 bridge address, or a reverse proxy in front of it. phoenixd stays outside the compose stack — it is the wallet holding your funds.
- **The recipe (Linux engine + compose):** the pair to configure is phoenixd started with `--http-bind-ip 172.17.0.1` (the docker0 bridge address) and `PHOENIXD_URL=http://host.docker.internal:9740` in `.env` — compose maps `host.docker.internal` to that same bridge via `host-gateway`, so the two settings meet. `deploy/phoenixd.service.example` is a minimal systemd unit with exactly this bind; run `phoenixd` once interactively first (first run creates `~/.phoenix`, including the wallet seed) before enabling the unit. Never bind `0.0.0.0` on a public VPS — the API answers to anyone who finds the port.
- **Inbound liquidity:** a fresh phoenixd has no channels and cannot receive. It opens (and later extends) a channel from ACINQ automatically when a received payment needs one, at a fee deducted from that payment — see `ops/OPERATOR-NOTES.md` and the "Inbound liquidity" section below.

### First payment: pre-fund before going live

A fresh phoenixd's first received payment triggers the automatic channel open, and ACINQ's liquidity fee is deducted **from that payment**. A settled first payment still gets its proof — verify_payment checks the invoice's face amount, so the fee nets the operator's credit, never the customer's proof (mechanics and the pinning tests: `ops/OPERATOR-NOTES.md`, "Phoenixd first payment warning"). The risk sits upstream: a first payment too small to carry the fee can fail to settle at all (phoenixd liquidity policy; unverified on this deployment), and either way the fee comes out of your margin.

So provision the channel yourself, before the first real sale. Two rails end in the same purchase; only one is proven from these docs.

**Primary — on-chain swap-in deposit (measured working).** Deposit on-chain to phoenixd's swap-in address and let auto-liquidity make the purchase; no Lightning sender is involved:

```bash
# 1. Read the swap-in address from phoenixd (the limited password may
#    read it; it cannot spend). Endpoint exists from phoenixd 0.9
#    (absent in 0.8.0); on older versions the address appears in
#    phoenix.log — grep "setting current swap-in address".
#    URL = where phoenixd is bound as seen from this host:
#    127.0.0.1:9740 on the systemd path; 172.17.0.1:9740 with the
#    deploy/phoenixd.service.example bind.
curl -sS -u ":$PHOENIXD_HTTP_PASSWORD_LIMITED" \
  http://127.0.0.1:9740/getswapinaddress

# 2. Send ~25,000–30,000 sats on-chain to that address, from any wallet.

# 3. Wait for 3 confirmations. phoenixd's auto-liquidity then purchases
#    a ~2M-sat inbound channel itself (the default --auto-liquidity 2m),
#    the fees deducted from the deposit.
```

Measured (stranger run, 2026-07): 31,232 sats deposited → 21,561 sats total toll → 9,671 sats remaining, buying a channel of 2,046,082 sats capacity with 2,035,087 sats inbound. The toll is the price of the inbound channel; what remains is the node's starting balance.

**Alternative — Lightning pre-fund (caveated).** The original walkthrough: mint an invoice directly from phoenixd and pay it from the payer wallet — one deliberately larger payment (~25,000–30,000 sats — see the README's budget table) that absorbs the channel-open fee:

```bash
# 1. On the gateway host: mint an invoice directly from phoenixd
#    (the limited password may create and read invoices; it cannot spend).
#    URL: same as above.
curl -sS -u ":$PHOENIXD_HTTP_PASSWORD_LIMITED" \
  -X POST http://127.0.0.1:9740/createinvoice \
  -d amountSat=30000 \
  -d description=prefund

# 2. Pay the returned bolt11 ("serialized" field) from the payer wallet —
#    the second, independently funded wallet (README, "The payer side").

# 3. Confirm receipt: incoming payments listing shows it settled, minus
#    the channel-open fee.
curl -sS -u ":$PHOENIXD_HTTP_PASSWORD_LIMITED" \
  http://127.0.0.1:9740/payments/incoming?limit=5
```

Caveat from live testing (2026-07): three attempts to pay such a pre-fund invoice from a Phoenix mobile sender were refused upstream — ACINQ returned `UpdateFailHtlc` within ~1 second, without ever contacting the receiving phoenixd. The walkthrough's prescribed sender (the payer wallet) remains untested on this rail. If the payment is refused, use the on-chain deposit above; it needs no Lightning sender at all.

After either rail, the channel exists and subsequent payments — the real sales — arrive at full value.

### Funding the payer side (phoenixd as payer)

The end-to-end test needs a second, independently funded wallet (README, "The payer side"). If that payer is itself a phoenixd, do not budget mining fees only for its on-chain funding: under the default `--auto-liquidity 2m`, a phoenixd used purely as a payer pays the full auto-liquidity toll on first funding, exactly as the shop's node does. Measured (stranger run, 2026-07): 81,736 sats deposited on-chain → toll 21,561 sats, identical to the shop's → 60,175 sats spendable. The mining-fees-only assumption for on-chain payer funding is false under defaults.

This can be a deliberate choice rather than a surprise — operators may set `--auto-liquidity` for payer roles on purpose; the deposited funds are spendable once the deposit has 3 confirmations and the splice completes. Budget the toll either way.

---

## Pricing

The 402 challenge quotes `max(GATEWAY_PRICE_SATS, floor)`. With a feerate available (`estimatesmartfee` over `PRICE_RPC_URL`, or the `BITCOIN_RPC_SERVICE_URL` fallback) the floor is the solvency floor — estimated anchor cost with bump reserve and margin, so the gateway is structurally unable to quote below what anchoring costs it. Without a feerate — no RPC URL configured, node down or timing out, no estimate for the target — the floor is `PRICE_BLIND_SATS` (default 5000). That is what the blind price is for: a blind gateway cannot tell a calm fee market from a spiking one, so it quotes a price the market cannot hurt it with — 5000 sats covers a 20 sat/vB anchor at cost with margin. It never quotes the bare static price on failure and it never refuses to quote; every blind quote is preceded by a logged warning. A token minted at any of these prices validates at its mint-time price for as long as its settled invoice backs it: settlement, not the clock, gates redemption (the expiry in the challenge is advisory).

---

## Getting the invoice macaroon (LND test payer / alternative backend only)

This section applies only when `PAYMENT_BACKEND_TYPE=lnd` (test payer / alternative). The live default backend is Phoenixd, which needs no macaroon — only `PHOENIXD_URL` and `PHOENIXD_HTTP_PASSWORD_LIMITED`.

The invoice macaroon authorises creating and reading invoices. It cannot spend funds, open channels, or take any other action.

### LND CLI (minimal macaroon)

```bash
lncli bakemacaroon \
  invoices:read \
  invoices:write \
  address:read \
  offchain:read \
  --save_to invoice.macaroon
xxd -p -c 256 invoice.macaroon
```

### Pre-baked macaroon (most home node setups)

LND ships with `invoice.macaroon` pre-created at:

```
~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

Convert it:

```bash
xxd -p -c 256 ~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

### Umbrel

```
~/umbrel/app-data/lightning/data/lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

LND REST host: `umbrel.local` resolves via mDNS from LAN machines only — inside the gateway container it does not resolve; use the Umbrel host's LAN IP instead. Port `8080`.

### RaspiBlitz

```bash
cat /mnt/hdd/lnd/data/chain/bitcoin/mainnet/invoice.macaroon | xxd -p -c 256
```

### Start9

Retrieve from the LND app's Properties page or via SSH. Path varies by EmbassyOS version.

---

## Tor hidden service keys

Tor generates a private key for your hidden service on first start. It is stored in the `tor_keys` Docker volume. If you destroy this volume, your `.onion` address changes permanently.

**Back up the key:**

```bash
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hs_ed25519_secret_key | base64
```

Store the output somewhere safe. To restore, copy the key back into the volume before starting the stack.

**Never share the secret key.** Anyone with it can impersonate your hidden service.

---

## Inbound liquidity

To receive Lightning payments, the payment backend must have inbound capacity — channel balance the remote peer can push toward you.

**This is a Lightning network problem, not a gateway or OTS problem.** The gateway issues valid invoices regardless; routing failures happen before the invoice is ever paid.

### phoenixd (live default backend)

phoenixd manages its own liquidity: it purchases inbound capacity from ACINQ automatically, either when an on-chain swap-in deposit confirms or when a received Lightning payment needs a channel, the fee deducted from that deposit or payment (see `ops/OPERATOR-NOTES.md`). Pre-provision it with the on-chain deposit before going live — the walkthrough is "First payment: pre-fund before going live". Do not leave the channel purchase to the first real payment: in live testing (2026-07) three pre-fund payment attempts from a Phoenix mobile sender were refused upstream, ACINQ returning `UpdateFailHtlc` within ~1 second without ever contacting the receiving phoenixd — and even when that rail works, the first payment simply nets less.

Both funding rails — the on-chain deposit and a received Lightning payment — purchase inbound liquidity from ACINQ, phoenixd's only peer. That is a deliberate single-provider dependency of the phoenixd backend; third-party channel-open services do not apply. Operators who require peer choice need the alternative backend (LND, below), where inbound capacity is arranged manually.

### LND (test payer / alternative backend only)

Inbound capacity must be arranged manually:

**Boltz submarine swap** (no new channel needed):

```bash
lncli addinvoice --memo boltz-swap --amt 30000
# Submit invoice at boltz.exchange
```

Boltz pays the invoice over Lightning (creating inbound capacity on that channel) and gives you on-chain BTC in return, minus a fee.

**Receive a channel from a well-connected node:**

Services like Bitrefill Thor or Amboss Magma open a channel to your node for a fee. Gives you immediate inbound capacity.

**Lightning Terminal (Loop In):**

Submarine swap via Terminal to move sats from your local channel balance to the remote side, creating inbound capacity.

### Tor-only routing difficulty (self-hosted LND-style nodes)

Tor-only Lightning nodes see fewer routing paths. The trade-off, its permanence on the clearnet side, and the options (accept lower reliability, hybrid node, VPS payment node) are in the README — "Inbound liquidity" and "Privacy trade-offs".

---

## Updating

```bash
git pull
docker compose --profile calendar build
docker compose --profile calendar up -d
```

The `tor_keys` and `otsd_calendar` volumes are preserved across updates. Your `.onion` address and calendar state are retained. otsd code updates need no image rebuild — pull the fork checkout and restart the service.

---

## Clearnet exposure (optional)

The gateway publishes `127.0.0.1:8000` by default — reachable from the Docker host (curl, the ops scripts, the health monitor) but not from the network. To expose it on clearnet in addition to Tor, edit the mapping in `docker-compose.yml`:

```yaml
services:
  gateway:
    ports:
      - "8000:8000"
```

To put the gateway behind a reverse proxy (nginx, Caddy):

```caddyfile
timestamp.yourdomain.com {
    reverse_proxy localhost:8000
}
```

When the gateway sits behind a reverse proxy, also set `GATEWAY_BEHIND_PROXY=true` in `.env`: the per-IP rate limiters (`RATE_LIMIT_PER_MINUTE` on unauthenticated minting; `VERIFY_RATE_LIMIT_PER_MINUTE` on the free `/verify` and `/upgrade` endpoints — a separate budget, so proof polling can never starve the paid mint path) then read the client address from the `X-Forwarded-For` entry your proxy appends. Without it every client shares the proxy's address — and one bucket per limiter. The header is never trusted unless this is set, because clients can forge it.

---

## Monitoring

```bash
docker compose logs -f gateway   # gateway application log (no access log)
docker compose logs -f tor       # Tor process
docker compose logs -f otsd      # OTS calendar server
```

The gateway runs without an access log (`--no-access-log` in both shipped launch paths) and logs warnings/errors for payment backend and OTS backend failures at `WARNING`/`ERROR` level. For the precise logging posture — what is never logged, payment-hash truncation, the traceback residual, and where digests do persist — see the README's "Privacy trade-offs" section.

**If your Bitcoin node is still syncing (IBD), expect two false alarms.** A wallet on a syncing node reads balance 0 until the sync passes the funding transaction — the wallet alarm fires `low` and `/health` degrades to 503 on a perfectly healthy box — so install the monitoring timers below only after the node is synced. And `estimatesmartfee` returns no estimate during IBD, so quotes fall back to the blind floor: first 402 challenges quote `PRICE_BLIND_SATS` (5,000 sats by default), not `GATEWAY_PRICE_SATS` — expected behaviour, not a pricing bug (see "Pricing").

### What `otsd: error` in `/health` means (and what it does not)

`/health` probes otsd's homepage and requires the `Best-block` line in the body, not just a 200. otsd commits its 200 status line before making any Bitcoin call, so with Bitcoin RPC dead it still answers 200 — with an empty page. `Best-block` renders only after otsd's `getbestblockhash`/`getblockcount` succeed, so its presence is the only external proof that otsd can reach Bitcoin. `otsd: error` therefore means one of two things: otsd is unreachable, or otsd is up but **Bitcoin-blind** — the gateway log distinguishes them (`otsd unreachable` vs `otsd HTTP up but Bitcoin-blind`).

Not covered: a wedged stamper thread with healthy RPC still renders the page. That class surfaces at outcome level in the `proofs` field (pending-too-long), with hours of latency.

Two different "anchoring isn't happening" signals, and how to tell them apart:

- **Bitcoin-blind** — `/health` shows `otsd: error`, the gateway log says `otsd HTTP up but Bitcoin-blind`, otsd's own log shows `__do_bitcoin() failed` / connection errors. Something broke: fix the Bitcoin RPC path (bitcoind down, credentials rotated, socat/onion bridge dead).
- **Stalled at the fee cap (by design)** — `/health` shows `otsd: ok`, proofs stay pending, otsd's log shows `Maximum txfee reached!`. Nothing broke: the last under-cap transaction is waiting for fees to fall or confirm. The action is patience — or a deliberate decision to raise `--btc-max-fee`.

### Wallet liquidity alarm

The otsd-hot wallet funds anchoring transactions. If it drains, anchoring silently stops — so its balance is checked unattended and surfaced through `/health`.

**How it works:** `ops/wallet-balance-check.sh` (run by a systemd timer every 30 minutes) reads the wallet balance over Bitcoin JSON-RPC (`getbalances`, via `BITCOIN_RPC_SERVICE_URL` from `.env`), compares it against `WALLET_MIN_SATS` (default 50000), and atomically writes a one-line JSON status file (`WALLET_STATUS_PATH`, default `/var/lib/timestamp-gateway/wallet-status`). `/health` reads only that file — the wallet field never comes from the gateway talking to Bitcoin RPC, and the gateway holds no wallet credential for it. (The gateway's one Bitcoin RPC use is fee estimation for the pricing floor: `PRICE_RPC_URL` — which, honest caveat, falls back to the wallet-scoped `BITCOIN_RPC_SERVICE_URL` when unset. Set a dedicated node-level `PRICE_RPC_URL` if you can; see `.env.example`.)

**Install the timer:**

First, make sure `BITCOIN_RPC_SERVICE_URL` is set in `.env` — the check fails with `BITCOIN_RPC_SERVICE_URL not set` (status `unknown`) without it:

```
BITCOIN_RPC_SERVICE_URL=http://<rpc-user>:<rpc-password>@<host:port>/wallet/<wallet-name>
```

The whole URL goes on one line, no quotes.

Then install and start the timer:

```bash
sudo cp ops/systemd/wallet-balance-check.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wallet-balance-check.timer
```

**Before copying any `ops/systemd/*` unit, edit `User=` and the two `/home/gateway` paths** (`WorkingDirectory=` and `ExecStart=`) to match your host — the shipped units assume the reference layout (user `gateway`, repo at `/home/gateway/timestamp-gateway`), the same way the `deploy/*.example` templates do. This applies to all four service units below as well. The failure mode is silent: a unit with a nonexistent `User=` dies with status `217/USER`, the status file it should write never appears, and `/health` reports that check `absent` — which does not degrade health, so nothing flags that the alarm you thought you installed is not running. (Exception: `backup-live-state.service` runs as root on purpose — edit only its paths.)

**Under Docker Compose**, the timers run on the host but `/health` runs in the gateway container — they must share the status directory. Set `GATEWAY_STATE_DIR=/var/lib/timestamp-gateway` in `.env` (and create that directory) so the container mounts the same path the timers write to. Without it the gateway uses a private named volume and reports these checks as `absent`.

**What `/health` reports** in its `wallet` field:

- `ok` — balance at or above `WALLET_MIN_SATS`.
- `low` — balance below the minimum: refill the wallet. Degrades health to 503. Also logged to the journal (`logger -p user.warning`).
- `unknown` — the check ran but could not read the balance (RPC/tunnel down, or malformed status file). Degrades to 503.
- `stale` — the status file is older than `WALLET_STATUS_MAX_AGE_SECONDS` (default 3600 = 2× the timer interval): the timer itself died. Degrades to 503.
- `absent` — no status file: the alarm is not installed. Reported but does **not** degrade health (operators without the calendar profile don't need it).

### Alarm delivery (ntfy)

`ops/health-monitor.sh` (run by `ops/systemd/health-monitor.{service,timer}`, every 5 minutes) polls `/health` and pushes state changes through `ops/notify.sh` to the ntfy topic URL in `NTFY_URL` — set it in `.env` and treat it as a secret (anyone holding the URL can read and post alarms). Unset, `notify.sh` logs and exits non-zero: an unconfigured alarm channel is a failure, not silence. A persisting problem re-alerts after `HEALTH_REALERT_SECONDS` (default 14400); recovery to `ok` pushes once.

The monitor polls `HEALTH_URL` (default `http://127.0.0.1:8000/health`), and that URL must match where the gateway actually listens **as seen from the monitor's host**. The compose default publishes `127.0.0.1:8000` on the Docker host, so the default matches out of the box. If you change the gateway's publish address or port — or bind it elsewhere on bare metal — change `HEALTH_URL` with it, or the monitor alarms against a dead URL while the gateway is fine.

Install:

```bash
sudo cp ops/systemd/health-monitor.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now health-monitor.timer
```

### Other /health fields

`/health` also reports `proofs` — written by `ops/upgrade-all-proofs.sh` (run by `timestamp-gateway-upgrade-proofs.timer`), the sweep that upgrades pending proofs against the local calendar and flags attestation mismatches — and `backup`, written by `ops/backup-live-state.sh` (run by `backup-live-state.timer`; see `ops/BACKUP-RECOVERY.md`). Both follow the wallet pattern: `absent` (not installed) is reported without degrading health; failure and stale states degrade to 503.

---

## Stopping and removing

```bash
docker compose --profile calendar down        # stop; preserve volumes
docker compose --profile calendar down -v     # stop and delete all volumes
                                              # WARNING: destroys onion key (address lost),
                                              # otsd calendar state (proofs unverifiable),
                                              # and the gateway obligation log (pending
                                              # settled payments can no longer be recovered)
```

---

## Running without Docker

For local development or bare-metal deployment:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in all vars
uvicorn main:app --host 0.0.0.0 --port 8000
```

To run this under systemd, start from `deploy/timestamp-gateway.service.example` — adjust its paths and bind address, and keep the monitor's `HEALTH_URL` matching the bind (see Monitoring).

**Create the durable state directory first.** The gateway writes its obligation log (and the `PAUSED` switch) under `OBLIGATIONS_DB_PATH` — default `/var/lib/timestamp-gateway`. On a bare-metal/systemd deployment the service runs as an unprivileged user (e.g. `gateway`) that cannot create a directory under `/var/lib`, and the process **fails loud at startup** if the path is unwritable. Create it once, owned by the service user, before first start:

```bash
sudo mkdir -p /var/lib/timestamp-gateway
sudo chown gateway:gateway /var/lib/timestamp-gateway
```

Substitute your service user for `gateway`. If you point `OBLIGATIONS_DB_PATH` elsewhere, create and chown that directory instead. (Under Docker this is handled automatically by the persistent `gateway_data` volume — see below.)

For a bare-metal otsd:

```bash
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server
cd opentimestamps-server && pip install -r requirements.txt
./otsd --calendar /path/to/calendar-data
```

First run only: the calendar identity must exist or otsd exits at startup — the same three files as under compose, in the calendar data directory (replace both example values with your own):

```bash
echo "https://calendar.example.com/" > /path/to/calendar-data/uri
head -c 32 /dev/urandom > /path/to/calendar-data/hmac-key
echo "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" > /path/to/calendar-data/donation_addr
```

To run the otsd container under systemd instead of bare Python, see `deploy/otsd.service.example`.

For Tor exposure without Docker, add to `/etc/tor/torrc`:

```
HiddenServiceDir /var/lib/tor/timestamp_gateway/
HiddenServicePort 80 127.0.0.1:8000
```

Then restart Tor and read the address:

```bash
sudo cat /var/lib/tor/timestamp_gateway/hostname
```

---

## Operator checklist

- [ ] `OTS_BACKEND_MODE=calendar` is set (not `public`).
- [ ] `OTS_CALENDAR_URL` points to a running otsd instance.
- [ ] `OBLIGATIONS_DB_PATH` is writable by the service user (bare-metal: `mkdir -p` + `chown` it; Docker: the `gateway_data` volume handles this).
- [ ] Bitcoin RPC credentials are configured and otsd can reach Bitcoin Core.
- [ ] A wallet is loaded in Bitcoin Core and has enough BTC to pay anchoring fees.
- [ ] Phoenixd has inbound Lightning liquidity (first-payment channel-open fee caveat — see OPERATOR-NOTES).
- [ ] Invoice macaroon is not committed to any public repository (lnd test-payer / alternative backend only).
- [ ] `.env` is in `.gitignore` and has never been committed.
- [ ] Tor hidden service private key is backed up.
- [ ] `docker compose logs -f otsd` shows otsd starting without errors.
- [ ] A test payment has been completed end-to-end: invoice issued → paid → `.ots` returned.
- [ ] A test proof upgrades (gateway `/upgrade`, or the ops sweep) and `ots verify` passes once anchored (timing: "Proof lifecycle").
- [ ] I understand that Lightning graph exposure (clearnet Lightning node IP) is permanent once published.
