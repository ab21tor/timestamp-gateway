# Operator guide

timestamp-gateway is portable paid OpenTimestamps calendar-node software. The gateway is the paid front door. The OTS calendar backend (otsd) is the proof engine. This document covers both.

---

## Architecture

```
client / Flagpole
  → Lightning-gated gateway       (this repo — collects payment, forwards digest)
  → operator-controlled OTS calendar  (otsd — aggregates, anchors in Bitcoin)
  → Bitcoin anchoring             (one OP_RETURN transaction per block cycle)
  → .ots                          (returned to client as pending receipt)
```

The gateway cannot produce Bitcoin-anchored proofs on its own. It requires a running OTS calendar backend (otsd). The initial `.ots` returned to the client is a pending receipt. After Bitcoin confirms the anchoring block (~1 hour), the proof can be upgraded using `ots upgrade`.

---

## Prerequisites

- Docker Engine 24+ and Docker Compose v2
- An LND node with REST API enabled and the invoice macaroon available
- Inbound Lightning liquidity on the LND node
- An OTS calendar backend (otsd) — bundled via `--profile calendar` or external
- A Bitcoin Core node reachable by otsd, with a wallet loaded and funded

You do not need a VPS. You do not need a static IP. You do not need to expose any clearnet ports if you use Tor-only mode.

---

## First-run checklist

1. Clone the repository and copy `.env.example` to `.env`.
2. Fill in `LND_HOST`, `LND_PORT`, `LND_MACAROON_HEX`.
3. Set `TOR_PROXY=tor:9050` if `LND_HOST` is a `.onion` address; leave blank otherwise.
4. Set `OTS_BACKEND_MODE=calendar` and `OTS_CALENDAR_URL=http://otsd:14788`.
5. Fill in `BITCOIN_RPC_HOST`, `BITCOIN_RPC_USER`, `BITCOIN_RPC_PASSWORD` for otsd.
6. Start the full stack: `docker compose --profile calendar up -d`.
7. Check logs: `docker compose logs -f`.
8. Retrieve onion address: `docker compose exec tor cat /var/lib/tor/timestamp_gateway/hostname`.
9. Test the endpoint with `curl` (see README quick start).

---

## OTS backend modes

### calendar (real mode — use this in production)

```
OTS_BACKEND_MODE=calendar
OTS_CALENDAR_URL=http://otsd:14788
```

The gateway forwards paid digests to the operator's own otsd instance. otsd aggregates submissions and anchors the Merkle root in Bitcoin once per block cycle. This is the only production mode.

**There is no silent fallback.** If the calendar backend fails, the gateway returns 502. It does not retry against the public OpenTimestamps aggregators.

### public (compatibility/testing only — not for production)

```
OTS_BACKEND_MODE=public
# OTS_CALENDAR_URL must NOT be set
```

The gateway forwards paid digests to the four public OpenTimestamps aggregators (`a.pool.opentimestamps.org`, etc.). This mode is provided so you can test the payment flow without running otsd. It is not the real target.

**Do not use `public` mode in production.** In public mode, the gateway is a paid relay to other operators' infrastructure — not an independent calendar node. This was the original architectural mistake; calendar mode is the correction.

---

## OTS calendar backend (otsd)

### What otsd is

otsd is the OpenTimestamps calendar server. It accepts raw digest bytes over HTTP (`POST /digest`), aggregates them into a Merkle tree, and submits one Bitcoin transaction per block cycle containing an OP_RETURN output with the Merkle root. The resulting proof links any submitted digest to the block height of that Bitcoin transaction.

### What otsd needs

- A Bitcoin Core node reachable via JSON-RPC.
- A wallet loaded in Bitcoin Core (use `bitcoin-cli loadwallet` or `createwallet`).
- Enough BTC in the wallet to pay for OP_RETURN transaction fees. A small wallet (50k–100k sats) is sufficient for extended low-volume operation. otsd does not spend to the wallet — it only draws from it to fund transactions.
- A persistent data directory for the calendar state (`/data` in the container).

### Bitcoin transaction cost

otsd submits approximately one Bitcoin transaction per block cycle (~10 minutes under normal conditions). Each transaction contains a single OP_RETURN output. Transaction cost depends on prevailing on-chain fee rates. At normal fee rates (5–20 sat/vbyte), a single anchoring transaction costs 500–2000 sats. A wallet of 100,000 sats sustains several months of uninterrupted operation at typical rates.

otsd will stop anchoring if the wallet is empty. Proofs submitted during a gap are eventually anchored when the wallet is refunded, but the gap delays proof finalisation.

### Bitcoin RPC configuration

Pass via `.env` or the `environment:` block in `docker-compose.yml`:

```
BITCOIN_RPC_HOST=       # IP or hostname of your Bitcoin Core node
BITCOIN_RPC_PORT=8332   # default Bitcoin Core RPC port
BITCOIN_RPC_USER=       # rpcuser from bitcoin.conf
BITCOIN_RPC_PASSWORD=   # rpcpassword from bitcoin.conf
```

If Bitcoin Core is on the same Docker host as otsd (not inside the Compose stack), use the host's Docker bridge IP (e.g. `172.17.0.1`) rather than `127.0.0.1` or `localhost`.

If Bitcoin Core is on a remote machine, ensure RPC is bound to an accessible address (`rpcbind` in bitcoin.conf) and that the address is in `rpcallowip`.

**Pruned nodes:** A pruned Bitcoin Core node is acceptable for otsd's transaction submission role. otsd does not need to download the full chain — it only submits transactions and reads the current tip.

### Starting with the bundled otsd profile

```bash
docker compose --profile calendar up -d
```

This starts `gateway`, `tor`, and `otsd`. otsd is not publicly exposed — it runs on the internal `ts_net` network only. The gateway reaches it at `http://otsd:14788`.

### Pointing to an external otsd

If you run otsd on a separate host or VM:

```
OTS_CALENDAR_URL=http://<host>:<port>
```

Do not expose otsd on a public port. It has no authentication. Access should be restricted to the gateway only.

### Proof lifecycle

1. **Immediate:** The gateway submits the digest to otsd and receives a receipt with a `PendingAttestation` pointing to the calendar URL. This is the `.ots` file returned to the client. It is not yet Bitcoin-anchored.
2. **~1 hour:** otsd submits a Bitcoin transaction anchoring the Merkle root for this aggregation window. The transaction is confirmed in a block.
3. **Upgrade:** The client runs `ots upgrade proof.ots` to fetch the Bitcoin anchoring from the calendar. The proof is now a full Bitcoin-anchored `.ots` file.
4. **Verify:** The client runs `ots verify proof.ots` to verify the proof against the Bitcoin blockchain independently.

The `.ots` file returned immediately by the gateway is a valid receipt. It is not incomplete or broken. It simply has not been finalized yet because Bitcoin blocks take time.

---

## Getting the invoice macaroon

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

LND REST host: `umbrel.local`, port `8080`.

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

To receive Lightning payments, the LND backend must have inbound capacity — channels where the remote peer has sats to push toward you.

**This is a Lightning network problem, not a gateway or OTS problem.** The gateway issues valid invoices regardless; routing failures happen before the invoice is ever paid.

### Options

**Boltz submarine swap** (no new channel needed):

```bash
lncli addinvoice --memo boltz-swap --amt 30000
# Submit invoice at boltz.exchange
```

Boltz pays the invoice over Lightning (creating inbound capacity on that channel) and gives you on-chain BTC in return, minus a fee.

**Receive a channel from a well-connected node:**

Services like ACINQ, Bitrefill Thor, or Amboss Magma open a channel to your node for a fee. Gives you immediate inbound capacity.

**Lightning Terminal (Loop In):**

Submarine swap via Terminal to move sats from your local channel balance to the remote side, creating inbound capacity.

### Tor-only routing difficulty

If your LND node is Tor-only, nodes that have disabled Tor routing cannot route payments to you. This reduces the routing path count significantly.

Options:
- **Accept lower reliability.** Suitable for low-volume personal or testing use.
- **Hybrid node.** Advertise both a Tor and a clearnet address. Better routing at the cost of linking your pubkey to a clearnet IP permanently.
- **VPS LND.** Run LND on a VPS for reliable clearnet routing. Run the gateway anywhere.

---

## Updating

```bash
git pull
docker compose --profile calendar build
docker compose --profile calendar up -d
```

The `tor_keys` volume and `otsd_data` volume are preserved across updates. Your `.onion` address and calendar state are retained.

---

## Clearnet exposure (optional)

To expose the gateway on clearnet in addition to Tor, edit `docker-compose.yml` and uncomment:

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

---

## Monitoring

```bash
docker compose logs -f gateway   # gateway + uvicorn access log
docker compose logs -f tor       # Tor process
docker compose logs -f otsd      # OTS calendar server
```

The gateway logs one line per request (uvicorn access log) and logs warnings/errors for LND and OTS backend failures at `WARNING`/`ERROR` level. It does not log digests or preimages.

---

## Stopping and removing

```bash
docker compose --profile calendar down        # stop; preserve volumes
docker compose --profile calendar down -v     # stop and delete all volumes
                                              # WARNING: destroys onion key (address lost)
                                              # and otsd calendar state (proofs unverifiable)
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

For a bare-metal otsd:

```bash
pip install opentimestamps-server
otsd --path /path/to/calendar-data
```

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
- [ ] Bitcoin RPC credentials are configured and otsd can reach Bitcoin Core.
- [ ] A wallet is loaded in Bitcoin Core and has enough BTC to pay anchoring fees.
- [ ] LND node has inbound Lightning liquidity.
- [ ] Invoice macaroon is not committed to any public repository.
- [ ] `.env` is in `.gitignore` and has never been committed.
- [ ] Tor hidden service private key is backed up.
- [ ] `docker compose logs -f otsd` shows otsd starting without errors.
- [ ] A test payment has been completed end-to-end: invoice issued → paid → `.ots` returned.
- [ ] `ots upgrade` and `ots verify` work on a test proof after ~1 hour.
- [ ] I understand that Lightning graph exposure (clearnet LND IP) is permanent once published.
