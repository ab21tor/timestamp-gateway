# Operator guide

This document covers running a timestamp-gateway node in detail.

---

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- An LND node with REST API enabled and the invoice macaroon available
- Inbound Lightning liquidity on that node

You do not need a VPS. You do not need a static IP. You do not need to expose any clearnet ports if you use the Tor-only mode.

---

## Getting the invoice macaroon

The invoice macaroon is a credential that allows the gateway to create and look up invoices. It cannot spend funds, open channels, or do anything else.

### LND CLI

```bash
lncli bakemacaroon \
  invoices:read \
  invoices:write \
  address:read \
  offchain:read \
  --save_to invoice.macaroon
xxd -p -c 256 invoice.macaroon
```

This creates a minimal macaroon with only the permissions the gateway needs. Paste the hex output as `LND_MACAROON_HEX`.

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

The Umbrel LND macaroon is at:

```
~/umbrel/app-data/lightning/data/lnd/data/chain/bitcoin/mainnet/invoice.macaroon
```

The LND REST host is `umbrel.local` (or the Umbrel IP) and port `8080`.

### Start9

Retrieve the invoice macaroon from the Embassy LND app's Properties page, or via SSH:

```bash
# Path varies by Embassy OS version; check LND app documentation
```

### RaspiBlitz

```bash
cat /mnt/hdd/lnd/data/chain/bitcoin/mainnet/invoice.macaroon | xxd -p -c 256
```

---

## First-run checklist

1. Clone the repository and copy `.env.example` to `.env`.
2. Fill in `LND_HOST`, `LND_PORT`, `LND_MACAROON_HEX`.
3. Set `TOR_PROXY=tor:9050` if `LND_HOST` is a `.onion` address; leave blank otherwise.
4. Run `docker compose up -d`.
5. Check logs: `docker compose logs -f`.
6. Retrieve onion address: `docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname`.
7. Test the endpoint with `curl` (see README quick start).

---

## Tor hidden service keys

Tor generates a private key for your hidden service on first start. It is stored in the `tor_keys` Docker volume. If you destroy this volume, your `.onion` address changes permanently.

**Back up the key:**

```bash
docker compose exec tor cat /var/lib/tor/timestamp_gateway/hs_ed25519_secret_key | base64
```

Store the output somewhere safe. To restore it, copy it back into the volume before starting the stack.

**Never share the secret key.** Anyone with it can impersonate your hidden service.

---

## Updating

```bash
git pull
docker compose build
docker compose up -d
```

The `tor_keys` volume is preserved across updates. Your `.onion` address stays the same.

---

## Inbound liquidity in detail

### Why it matters

A Lightning invoice can only be paid if the network can route sats to your node. For routing to work, there must be a channel where the remote side has sats to push toward you — this is inbound capacity.

Opening a channel gives you outbound capacity. To get inbound capacity, you need someone to open a channel to you, or to move your own sats to the remote side of an existing channel.

### Practical options

**Boltz submarine swap** (no new channel required):

1. Create a Lightning invoice on your node.
2. Submit it to Boltz. Boltz pays the invoice over Lightning (giving your node inbound capacity on that channel) and gives you on-chain sats in return, minus a small fee.
3. Result: your existing channel now has inbound capacity.

```bash
# Example: create a 30,000-sat invoice with memo "boltz-swap"
lncli addinvoice --memo boltz-swap --amt 30000
# Submit the invoice on boltz.exchange
```

**Open a channel from a well-connected node:**

Services like ACINQ (Phoenix), Bitrefill Thor, or Amboss Magma will open a channel to your node for a fee. This gives you immediate inbound capacity.

**Lightning Terminal (Loop In):**

Submarine swap via Terminal to push sats from your local channel balance to the remote side.

### Tor-only routing difficulty

If your LND node is Tor-only, it cannot be reached by nodes that are clearnet-only or that have disabled Tor routing. This reduces the number of potential routing paths to you significantly.

Options:
- **Accept the trade-off.** Lower routing reliability in exchange for better privacy. Suitable for low-volume personal use.
- **Run a hybrid node.** Advertise both a Tor address and a clearnet address in your LND configuration. This improves routing reliability at the cost of linking your pubkey to a clearnet IP.
- **Use a clearnet VPS.** If you want reliable inbound routing without exposing a home IP, run LND on a VPS and operate the gateway from there or from a home machine connecting to the VPS LND.

---

## Clearnet exposure (optional)

To make the gateway reachable over clearnet in addition to Tor, edit `docker-compose.yml` and uncomment:

```yaml
services:
  gateway:
    ports:
      - "8000:8000"
```

You can also put the gateway behind a reverse proxy (nginx, Caddy) for TLS termination and a custom domain.

Example Caddy config:

```caddyfile
timestamp.yourdomain.com {
    reverse_proxy localhost:8000
}
```

---

## Monitoring and logs

```bash
docker compose logs -f gateway   # gateway logs
docker compose logs -f tor       # Tor logs
```

The gateway logs one line per request to stdout (uvicorn access log) and logs warnings/errors for LND and OTS failures. It does not log digests or preimages.

---

## Stopping and removing

```bash
docker compose down              # stop; preserve volumes
docker compose down -v           # stop and delete volumes (destroys onion key — address lost)
```

---

## Running without Docker

For local development or bare-metal deployment:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in vars
uvicorn main:app --host 0.0.0.0 --port 8000
```

For Tor exposure without Docker, install Tor from your system package manager and add to `/etc/tor/torrc`:

```
HiddenServiceDir /var/lib/tor/timestamp_gateway/
HiddenServicePort 80 127.0.0.1:8000
```

Then restart Tor and read the onion address:

```bash
sudo cat /var/lib/tor/timestamp_gateway/hostname
```

---

## Privacy checklist for operators

- [ ] I understand that advertising a clearnet LND address permanently links my node pubkey to that IP on the Lightning graph.
- [ ] I have decided whether I want Tor-only, hybrid, or clearnet Lightning.
- [ ] If I am using a home IP for clearnet Lightning, I understand this is a permanent and public record.
- [ ] I have backed up my Tor hidden service private key.
- [ ] My invoice macaroon is not committed to any public repository.
- [ ] I understand that the `.ots` proofs are independently verifiable and do not require trusting this gateway after issuance.
