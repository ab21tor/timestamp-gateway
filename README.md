# timestamp-gateway

This service lets a client submit a SHA-256 digest for Bitcoin-anchored timestamping through OpenTimestamps, gated by a small Lightning payment. The client sends only the digest, receives a Lightning invoice, pays it, retries with the payment preimage, and receives the resulting .ots timestamp proof. The original file never needs to leave the client's machine, and all communication with the remote LND node is routed through Tor.

## Requirements

- Python 3.10+
- Tor running locally on port 9050
- A reachable LND node with REST API enabled

## Configuration

Fill in the values in `.env`. The five required variables are:

| Variable | Description |
|---|---|
| `LND_HOST` | Hostname or onion address of the LND REST API |
| `LND_PORT` | Port the LND REST API listens on (typically `8080`) |
| `LND_MACAROON_HEX` | Hex-encoded LND macaroon with invoice write permissions |
| `TOR_PROXY` | Address of the local Tor SOCKS5 proxy |
| `GATEWAY_PRICE_SATS` | Amount in satoshis to charge per timestamp request |
| `LND_TLS_VERIFY` | Set to `true` to verify LND TLS certificate; defaults to `false` |

### .env.example

```
LND_HOST=your-node.onion
LND_PORT=8080
LND_MACAROON_HEX=0201...
TOR_PROXY=127.0.0.1:9050
GATEWAY_PRICE_SATS=21
LND_TLS_VERIFY=false
```

## How to run

```bash
source .venv/bin/activate
uvicorn main:app --reload
```

## Usage

Timestamp a digest in two steps.

**Step 1 — submit digest, receive invoice:**

```bash
curl -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -d '{"digest": "a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef"}'
```

Response (402):

```json
{
  "status": "payment_required",
  "invoice": "lnbc210n1p..."
}
```

Pay the invoice with a Lightning wallet and note the payment preimage.

**Step 2 — retry with preimage, receive .ots file:**

```bash
curl -X POST http://localhost:8000/timestamp \
  -H "Content-Type: application/json" \
  -H "Authorization: preimage=<64-char-hex-preimage>" \
  -d '{"digest": "a3f5c2d1e9b087640000000000000000000000000000000000000000deadbeef"}' \
  -o proof.ots
```

A successful payment returns the `.ots` file as `application/octet-stream`. Submit it to an OpenTimestamps verifier once the timestamp has been anchored in a Bitcoin block (~1 hour).

## Privacy

Only the SHA-256 digest is submitted to the gateway — the original file never leaves the client machine. To produce the digest locally:

```bash
sha256sum myfile.pdf       # Linux
shasum -a 256 myfile.pdf   # macOS
```

The browser UI (`/ui`) hashes the file locally using the Web Crypto API before sending anything to the server.
