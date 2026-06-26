# timestamp-gateway operator notes

## Live services

Gateway service:

- `timestamp-gateway.service`

Payment backend:

- Phoenixd
- service: `phoenixd.service`
- API: `127.0.0.1:9740`
- enabled on boot

Local OpenTimestamps calendar:

- Docker container: `otsd`
- host calendar path: `/var/lib/otsd/calendar`
- container calendar path: `/calendar`

## Operator commands

Run from:

`/home/gateway/timestamp-gateway`

Commands:

`./ops/status.sh`

`./ops/phoenixd-status.sh`

`./ops/otsd-status.sh`

`./ops/list-proofs.sh`

`./ops/proof-status.sh`

`./ops/upgrade-proof.sh`

## Phoenixd boundary

Phoenixd is the live Lightning payment backend.

It is managed by systemd.

It listens only on localhost.

The gateway uses Phoenixd through `.env`.

Do not print or paste the Phoenixd password.

Phoenixd state lives here:

`/home/gateway/phoenixd/home/.phoenix`

Important files:

- `phoenix.conf`
- `seed.dat`
- `phoenix.mainnet.*.db`
- `phoenix.mainnet.*.db-wal`
- `phoenix.mainnet.*.db-shm`
- `phoenix.log`

`seed.dat` is critical. Treat it as secret wallet material.

## LND role in this deployment

LND is present on this VPS but is NOT the active payment backend.

Current payment backend: Phoenixd

LND is used only as a test payer in operator scripts:

- ops/l402-paid-proof.sh uses lncli to pay Phoenixd invoices for testing
- This creates a local loop: LND pays → Phoenixd receives

This is a testing artifact. In production:

- Client wallets pay Phoenixd invoices over the Lightning network
- LND is not involved in the payment flow
- LND may be removed or replaced in a future deployment

Do not confuse LND's presence with it being the active payment backend.

## Phoenixd first payment warning

On a fresh Phoenixd node with no open channel, the first received payment triggers an automatic channel open by ACINQ.

ACINQ deducts a liquidity fee from the received amount:

- mining fee: ~137-411 sats (depends on mempool)
- service fee: ~1,000-21,000 sats (depends on amount received)

This means the first payment may arrive with less than the invoiced amount.

The gateway verify_payment check requires amount_paid >= GATEWAY_PRICE_SATS.

If the liquidity fee causes the received amount to fall below the price floor, the proof will be refused with 402.

Mitigations:
- Pre-fund the Phoenixd node by receiving a payment before going live
- Set GATEWAY_PRICE_SATS high enough to absorb the worst-case liquidity fee on first receive
- Accept that the first proof on a fresh node may fail and require the client to retry

Once a channel is open, subsequent payments arrive at full value with no deduction.

## otsd boundary

The local calendar is your own `otsd`, not the public OpenTimestamps calendars.

Current command:

`python3 otsd --calendar /calendar --btc-conf-target 2 -v`

Current policy:

- batch up to 6 hours by default
- when anchoring, aim for about 2-block Bitcoin confirmation
- save the Bitcoin proof after 6 confirmations by default

The 6-hour default comes from:

`--btc-min-tx-interval default: 21600 seconds`

## Proof states

Use simple states where possible:

- `waiting_for_payment`
- `receipt_issued`
- `waiting_for_bitcoin`
- `bitcoin_backed`
- `needs_attention`

## Product boundary

No files.

No accounts.

No claims.

No truth.

No custody.

The gateway accepts digests and returns portable `.ots` receipts.

A receipt proves the digest existed no later than the time supported by the OpenTimestamps proof path.

It does not prove document truth, authorship, consent, legality, originality, completeness, or content review.

## notarie boundary

Do not build notarie until the proof machine is boring.

notarie should only be a local watcher, hasher, and receipt saver.

It should not parse documents, upload files, judge content, make claims, or expose Lightning/OpenTimestamps internals to the user.
