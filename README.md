# timestamp-gateway

timestamp-gateway is portable paid OpenTimestamps calendar-node software. It accepts a SHA-256 digest, charges a configured Lightning price, submits the paid digest to the operator's own OpenTimestamps calendar backend, and returns a raw .ots proof. It stores no files, requires no accounts, and does not need to be trusted after the proof is returned.

This is not a hosted service. It is software for running a Lightning-gated OpenTimestamps calendar node.

```
client / Flagpole
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
- Log digests, preimages, or client identities.
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

- Docker Engine 24+ with Docker Compose v2.17+ (BuildKit)
- A running phoenixd instance (the live payment backend) — or an LND node with REST API and invoice macaroon if using the LND test-payer / alternative backend
- An OpenTimestamps calendar backend (otsd) — bundled in the Compose stack or external
- A Bitcoin Core node reachable by otsd, with a wallet loaded and funded for anchoring transactions
- Inbound Lightning liquidity on the payment backend node (see [Inbound liquidity](#inbound-liquidity))

---

## Quick start

```bash
# Two repos: the gateway, and the calendar code it mounts into otsd.
git clone https://github.com/ab21tor/timestamp-gateway
git clone -b calendar-ops https://github.com/ab21tor/opentimestamps-server
cd timestamp-gateway
cp .env.example .env
# Edit .env — set PHOENIXD_HTTP_PASSWORD (live default backend; LND_* only
# if using the lnd test payer / alternative backend) and
# BITCOIN_RPC_SERVICE_URL for otsd (see .env.example for the three shapes).

# First run only: give the calendar its identity — the URI callers will see
# in pending attestations, the HMAC key, and a donation address its web page
# displays. otsd refuses to start without them. Replace both example values
# with your own (the address below is the BIP173 example, not yours).
docker compose --profile calendar run --rm otsd sh -c \
  'echo "https://calendar.example.com/" > /calendar/uri \
   && head -c 32 /dev/urandom > /calendar/hmac-key \
   && echo "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4" > /calendar/donation_addr'

docker compose --profile calendar up -d
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
| `PHOENIXD_URL` | No | `http://127.0.0.1:9740` | phoenixd HTTP API endpoint (live default backend); from the compose stack use `http://host.docker.internal:9740` |
| `PHOENIXD_HTTP_PASSWORD` | No | — | phoenixd HTTP API password (`http-password` in `phoenix.conf`) |
| `LND_HOST` | When `lnd` | — | Hostname, IP, or `.onion` address of your LND REST API (test payer / alternative) |
| `LND_PORT` | When `lnd` | — | LND REST port, typically `8080` (test payer / alternative) |
| `LND_MACAROON_HEX` | When `lnd` | — | Hex-encoded invoice macaroon (test payer / alternative) |
| `GATEWAY_PRICE_SATS` | Yes | — | Satoshis charged per timestamp |
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

The gateway forwards paid digests to the operator's own otsd instance. otsd aggregates submissions and anchors the aggregate root in Bitcoin once per block cycle. This is the intended production mode.

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

```bash
docker compose --profile calendar up -d
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname
```

**Trade-off:** Tor adds latency. Tor-only Lightning nodes have harder inbound routing.

### Hybrid (practical self-hosted)

Gateway onion. LND (test payer / alternative backend) clearnet or hybrid. otsd on the same host.

```
LND_HOST=192.168.1.x
TOR_PROXY=              # blank — direct LND connection
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

**Trade-off:** A clearnet Lightning node links its pubkey to an IP on the Lightning graph permanently.

### Clearnet

The gateway publishes `127.0.0.1:8000` by default (reachable from the Docker
host only). For clearnet, edit the mapping in `docker-compose.yml`:

```yaml
ports:
  - "8000:8000"
```

---

## Connecting to LND (test payer / alternative backend only)

This section applies only when `PAYMENT_BACKEND_TYPE=lnd` (test payer / alternative). The live default backend is Phoenixd, which needs only `PHOENIXD_URL` and `PHOENIXD_HTTP_PASSWORD`.

The gateway needs an invoice macaroon — it authorises creating and reading invoices, nothing else.

```bash
# Convert the macaroon to hex
xxd -p -c 256 ~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

Paste the output as `LND_MACAROON_HEX`.

| LND location | `LND_HOST` value | `TOR_PROXY` |
|---|---|---|
| Same Docker host | `host.docker.internal` | blank |
| Remote LAN machine | LAN IP | blank |
| Onion address | `.onion` address | `tor:9050` |
| Umbrel | `umbrel.local` | blank |

---

## OTS calendar backend (otsd)

otsd is the OpenTimestamps calendar server. It is the proof engine. The gateway is the paid front door.

**What otsd needs:**
- A Bitcoin Core node with RPC enabled (pruned is acceptable for the submission role).
- A wallet loaded in Bitcoin Core with enough BTC to pay for periodic OP_RETURN anchoring transactions.
- A persistent data directory for calendar state.

**Transaction cost:** otsd submits approximately one Bitcoin transaction per block cycle, containing an OP_RETURN with the Merkle root of all digests aggregated since the last anchoring. Normal on-chain fees apply. A small wallet (50k–100k sats) is sufficient for extended low-volume operation.

**Initial vs anchored proof:** When a digest is first submitted, otsd returns a receipt with a pending attestation pointing to the calendar URL. This is not yet Bitcoin-anchored. After Bitcoin confirms the anchoring block (~1 hour), the proof can be upgraded to a full Bitcoin-anchored `.ots` file using:

```bash
ots upgrade proof.ots
ots verify proof.ots
```

The initial `.ots` file returned by the gateway is a valid pending receipt, not a finalized proof. This is normal and expected behaviour.

**Bitcoin RPC config** (set in gitignored `.env` only — full URL including credentials):

```
BITCOIN_RPC_SERVICE_URL=http://rpcuser:rpcpassword@host.docker.internal:18332/wallet/otsd-hot
```

For an onion-only node, the bundled `--profile onion-rpc` bridge forwards `rpc-bridge:18332` to your node over Tor; the systemd path uses a host socat bridge instead (see the operator guide for both).

**otsd is not publicly exposed.** It publishes no port; the gateway reaches it at `http://otsd:14788` on the compose network. Clients have no direct access to otsd; they interact only with the gateway.

The calendar code is the `opentimestamps-server` fork:
`https://github.com/ab21tor/opentimestamps-server` (branch `calendar-ops`, fork of `opentimestamps/opentimestamps-server`).

---

## Inbound liquidity

To receive Lightning payments, the payment backend (Phoenixd by default) must have inbound liquidity. Other nodes must be able to route payments to your node. Note Phoenixd's first-payment channel-open fee caveat — see `ops/OPERATOR-NOTES.md`.

**This is a Lightning network and operator issue, not a gateway or OTS issue.**

Options:
- **Boltz submarine swap** — push sats from local channel balance to the remote side, creating inbound capacity without opening a new channel.
- **Receive a channel** — ask a well-connected node (Loop, Bitrefill Thor, ACINQ, Amboss Magma) to open a channel to you.
- **Lightning Terminal / Pool** — purchase inbound liquidity from the market.

**Tor-only nodes have harder routing.** Many nodes will not route payments to Tor-only endpoints. Options: accept lower reliability, use a hybrid node, or run the payment node on a VPS.

---

## Privacy trade-offs

| Scenario | What is publicly visible |
|---|---|
| Tor-only gateway + Tor-only Lightning node | No clearnet footprint |
| Tor gateway + clearnet Lightning node | Node pubkey and IP on Lightning graph |
| Clearnet gateway | Gateway IP is public; Lightning node depends on config |

Lightning graph exposure is permanent. If your Lightning node advertises a clearnet IP, that association is recorded by Lightning explorers and cannot be undone.

The gateway does not log digests, client IPs, or payment preimages beyond normal uvicorn access logs.

---

## Running on home hardware and node boxes

| Platform | Notes |
|---|---|
| Raspberry Pi / ARM64 | Works. All base images are multi-arch. |
| Home Linux / mini PC | Standard Docker Compose. |
| Umbrel | Run `docker compose --profile calendar up -d`. If using the lnd test-payer / alternative backend, point `LND_HOST` at Umbrel LND REST. |
| Start9 | Run as a Docker Compose stack. If using the lnd test-payer / alternative backend, retrieve the macaroon from the LND app. |
| VPS / data-centre | Standard deployment. Consider the clearnet privacy trade-offs. |
| NGO / university / journalist | Tor-only mode recommended. No clearnet exposure required. |

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in payment backend vars and set OTS_BACKEND_MODE
uvicorn main:app --reload
```

Run the test suite (no payment backend, Tor, or otsd required — all network calls are mocked):

```bash
pytest -q
```

---

## Verifying a proof

After receiving a `.ots` file, the proof is pending calendar confirmation. After Bitcoin confirms the anchoring block (~1 hour):

```bash
ots upgrade proof.ots   # fetches the Bitcoin anchoring from the calendar
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
