# timestamp-gateway

timestamp-gateway treats Tor as an optional deployment layer rather than a hard requirement. The gateway can run as a portable node package with Docker Compose, where the FastAPI service is paired with a Tor container that can expose the gateway as an onion service and provide a SOCKS proxy for outbound onion connections. If TOR_PROXY is set, LND requests are routed through Tor; if TOR_PROXY is unset, the gateway connects to LND directly. This means operators can choose their own mode: Tor-only gateway with Tor-only LND, onion gateway with clearnet or hybrid LND, or clearnet/onion public infrastructure. LND is deliberately not bundled; operators bring their own backend from Start9, Umbrel, RaspiBlitz, local LND, remote LND, or other infrastructure. The onion service key is persisted in a Docker volume so the onion address survives restarts, while clearnet exposure is commented out by default. Tor is supported, but not mandatory. VPS is supported, but not mandatory. Tor-only is possible, but not imposed.

---

## How it works

1. Client sends `POST /timestamp` with `{"digest": "<sha256-hex>"}` — no auth header.
2. Gateway calls LND, creates a Lightning invoice, returns HTTP 402 with the BOLT11 string.
3. Client pays the invoice and records the payment preimage.
4. Client resends `POST /timestamp` with `Authorization: preimage=<64-hex-preimage>`.
5. Gateway verifies the payment against LND, submits the digest to the OpenTimestamps public calendars, and returns a raw `.ots` proof file.
6. Client verifies the proof independently once it is anchored in a Bitcoin block (~1 hour).

The original file never touches the gateway. Only the digest is transmitted.

---

## What you need

- Docker and Docker Compose
- An existing LND node with the REST API enabled
- An invoice macaroon for that node
- Inbound Lightning liquidity (see [Inbound liquidity](#inbound-liquidity))

---

## Quick start

```bash
git clone https://github.com/ab21tor/timestamp-gateway
cd timestamp-gateway
cp .env.example .env
# Edit .env — fill in LND_HOST, LND_PORT, LND_MACAROON_HEX
docker compose up -d
```

Get your onion address:

```bash
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname
```

Test the endpoint (replace with your onion address or use localhost if you exposed port 8000):

```bash
curl -s -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -d '{"digest": "a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef"}'
```

A working gateway returns HTTP 402 with a Lightning invoice. Pay it, then:

```bash
curl -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -H "Authorization: preimage=<64-char-hex-preimage>" \
  -d '{"digest": "a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef"}' \
  -o proof.ots
```

A successful payment returns the `.ots` file.

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `LND_HOST` | Yes | — | Hostname, IP, or `.onion` address of your LND REST API |
| `LND_PORT` | Yes | — | LND REST port (typically `8080`) |
| `LND_MACAROON_HEX` | Yes | — | Hex-encoded invoice macaroon |
| `GATEWAY_PRICE_SATS` | Yes | — | Satoshis charged per timestamp |
| `TOR_PROXY` | No | — | SOCKS5h proxy for LND connections (e.g. `tor:9050`). Required if `LND_HOST` is a `.onion` address. Leave unset for direct clearnet LND. |
| `LND_TLS_VERIFY` | No | `false` | Set `true` only for CA-signed LND TLS certs. Almost no home node qualifies. |

---

## Deployment modes

### Tor-only (maximum privacy)

Gateway exposed as a Tor hidden service. LND reachable at a `.onion` address. No clearnet ports exposed.

```
LND_HOST=yourlnd.onion
LND_PORT=8080
LND_MACAROON_HEX=<hex>
TOR_PROXY=tor:9050
GATEWAY_PRICE_SATS=21
LND_TLS_VERIFY=false
```

```bash
docker compose up -d
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname
```

Clients reach the gateway at `http://<your-onion>.onion/` over Tor (port 80 maps to the gateway's port 8000).

**Trade-off:** Tor adds latency. Inbound Lightning liquidity over Tor-only nodes is harder to obtain — see [Inbound liquidity](#inbound-liquidity).

---

### Hybrid (practical self-hosted)

Gateway exposed on Tor. LND is clearnet or hybrid.

```
LND_HOST=192.168.1.x    # or clearnet hostname
LND_PORT=8080
LND_MACAROON_HEX=<hex>
TOR_PROXY=              # leave blank — direct connection to clearnet LND
GATEWAY_PRICE_SATS=21
LND_TLS_VERIFY=false
```

**Trade-off:** If your LND node advertises a clearnet address, your node pubkey is permanently linked to that IP on the public Lightning graph. Do not expose a home IP casually.

---

### Clearnet

Gateway port exposed directly. Tor container still runs for the onion address, but clients can also reach the gateway over clearnet.

In `docker-compose.yml`, uncomment the ports block under `gateway`:

```yaml
ports:
  - "8000:8000"
```

---

## Connecting to LND

The gateway needs an invoice macaroon: it authorises creating and reading invoices, nothing else.

**Find the macaroon on your node:**

```bash
# LND default location
~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

**Convert to hex:**

```bash
xxd -p -c 256 ~/.lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

Paste the output as `LND_MACAROON_HEX` in `.env`.

**LND on the same Docker host:**

```
LND_HOST=host.docker.internal   # Mac/Windows Docker Desktop
# or the host's LAN IP on Linux
```

**LND on a remote machine (LAN):**

```
LND_HOST=192.168.1.x
TOR_PROXY=              # blank — direct connection
```

**LND behind a `.onion` address:**

```
LND_HOST=yourlnd.onion
TOR_PROXY=tor:9050
```

---

## Inbound liquidity

To receive Lightning payments, the LND backend must have inbound liquidity: capacity must exist on the remote side of a channel so other nodes can route payments to you.

**This is a Lightning network and operator issue, not a FastAPI or OpenTimestamps issue.** The gateway itself has no control over routing.

Options for getting inbound capacity:

- **Submarine swap:** Use [Boltz](https://boltz.exchange/) to swap sats from your local channel balance to the remote side, creating inbound capacity without opening a new channel.
- **Receive a channel:** Ask a well-connected node (Loop, Bitrefill Thor, ACINQ, Amboss Magma) to open a channel to you.
- **Lightning Terminal / Lightning Pool:** Purchase inbound liquidity from the market.

**Tor-only nodes have additional routing challenges.** Many Lightning nodes will not route payments to Tor-only endpoints because they cannot reliably reach them. Options:

- Accept lower payment reliability in exchange for better privacy.
- Run a hybrid Lightning node that advertises both a clearnet and a Tor address, but understand the privacy implications below.

---

## Privacy trade-offs

| Scenario | What is visible publicly |
|---|---|
| Tor-only gateway, Tor-only LND | Nothing — no clearnet footprint |
| Tor gateway, clearnet LND | Your LND node pubkey and IP are on the Lightning graph |
| Clearnet gateway | Your gateway IP is public; your LND depends on config |

**Lightning graph exposure is permanent.** If your LND node advertises a clearnet IP, that IP and your node pubkey are recorded by Lightning explorers (1ML, Amboss, Mempool) and archived. This cannot be undone after the fact.

**Recommendations:**

- Do not expose a home IP on the Lightning network unless you understand and accept the consequence.
- Use a VPS or hosted server if you want a public, well-connected node without exposing a home address.
- A Tor-only node trades routing reliability for privacy. This is a valid choice for lower-volume personal use.
- The gateway itself does not log digests, client IPs, or payment preimages beyond what uvicorn writes to stdout.

---

## Running on home hardware and node boxes

timestamp-gateway runs anywhere Docker runs.

| Platform | Notes |
|---|---|
| Raspberry Pi / ARM64 | Works. `python:3.13-slim` and `debian:bookworm-slim` are multi-arch. |
| Home Linux / mini PC | Standard Docker Compose. |
| Umbrel | Run `docker compose up -d` in the gateway directory. Point `LND_HOST` at the Umbrel LND REST address (usually `umbrel.local:8080`). |
| Start9 | Run as a Docker Compose stack. Embassy OS can manage arbitrary compose stacks. Retrieve the macaroon from the Embassy LND app. |
| VPS / data-centre | Standard deployment. See clearnet and privacy trade-offs above. |
| NGO / university / journalist infrastructure | Tor-only mode recommended. No clearnet exposure required. |

---

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in LND vars
uvicorn main:app --reload
```

Run the test suite (no LND or Tor required — all network calls are mocked):

```bash
pytest -q
```

---

## Browser UI

The gateway includes a minimal single-page UI at `/ui`. It:

- Accepts a file or a manual digest input.
- Hashes the file locally using the Web Crypto API (the file never leaves the browser).
- Guides the user through the payment state.
- Auto-downloads the `.ots` file on success.

---

## Verifying a timestamp

Once the `.ots` file is returned, the digest is pending calendar confirmation. After Bitcoin confirms the anchoring block (~1 hour):

```bash
ots verify proof.ots -f yourfile
```

Or use the [OpenTimestamps web verifier](https://opentimestamps.org). The proof is independently verifiable against Bitcoin without trusting the gateway.
