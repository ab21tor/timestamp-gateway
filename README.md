# timestamp-gateway

A FastAPI service that accepts a SHA-256 digest over HTTP and returns a Lightning invoice for a small payment. Intended as a gateway between a client application and an LND node, with all LND communication routed through Tor.

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
