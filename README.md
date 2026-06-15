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

## How to run

```bash
source .venv/bin/activate
uvicorn main:app --reload
```
