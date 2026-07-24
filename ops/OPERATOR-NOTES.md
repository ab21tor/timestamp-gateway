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

- Client wallets pay Phoenixd invoices directly over the Lightning network
- LND is not involved in the payment flow
- LND may be removed or replaced in a future deployment

Do not confuse LND's presence with it being the active payment backend.

## Phoenixd first payment warning

On a fresh Phoenixd node with no open channel, the first received payment triggers an automatic channel open by ACINQ.

ACINQ deducts a liquidity fee from the received amount. The figures are ACINQ's, they change, and this file does not record them — current schedule: https://phoenix.acinq.co/server/liquidity (pointer recorded 2026-07-21). Shape of the fee: a mining-fee component plus a service percentage of the liquidity purchased, paid upfront out of the payment that triggers it.

The arithmetic that matters is not the exact figures:

    received = invoiced − liquidity fee

The fee can exceed the whole margin of a small first payment — or the payment itself.

What the gateway does about it (pinned by the H3 tests in test_main.py): verify_payment checks the invoice's FACE amount (requestedSat) against the mint-time price, never the credited amount — a settled bolt11 is atomic, so settlement proves the payer paid the face amount in full. A settled first payment gets its proof; the liquidity fee nets the OPERATOR's credit and logs a WARNING ("Liquidity fee observed: requested N sat, received M sat"). The customer is never refused over ACINQ's fee.

The remaining failure mode is upstream of the gateway: a payment too small to carry the fee fails to settle at the Lightning layer — no sats move, the invoice stays unpaid, and a retry after pre-funding succeeds. (phoenixd liquidity-policy behaviour; not yet exercised on this deployment — unverified.)

Mitigations:
- Pre-fund the Phoenixd node by receiving a payment before going live — concrete walkthrough: operator guide, "First payment: pre-fund before going live"
- Price so the first-receive fee cannot dominate your margin — the fee is your cost, not the customer's shortfall

Once a channel is open, subsequent payments arrive at full value with no deduction.

## otsd boundary

The local calendar is your own `otsd`, not the public OpenTimestamps calendars.

The live container's exact shape — image, mounts, network, env, command —
is recorded once, in BACKUP-RECOVERY.md "Current Docker shape" (the
restore record). Since 2026-07-21 the installed unit matches the repo
template (`deploy/otsd.service.example`).

Policy on this box:

- Fee cap LIVE since 2026-07-21: the installed unit was updated to the
  shipped template — `--btc-max-fee 0.0002` in force and `-v` gone (INFO
  is the production log level), verified by docker inspect. Credentials
  ride `/etc/systemd/system/otsd.env`, owned by the service user, mode
  600. When hand-editing the cap: the flag takes BTC, 0.0002 BTC =
  20,000 sats — never "fix" 0.0002 to 20000, that would mean 20,000 BTC.
- Rotation lesson (durable): a rotation is pull+restart and never touches
  the installed unit — a unit change lands only through an explicit
  install + daemon-reload, as on 2026-07-21.
- Cap semantics (what one anchor cycle can spend across its RBF ladder):
  operator guide, "Bitcoin transaction cost". The cap-stall signal —
  `Maximum txfee reached!` in otsd's log while `/health` shows `otsd: ok`
  and proofs stay pending — and how to tell it from a broken Bitcoin
  path: operator guide, "Monitoring".
- `--btc-conf-target 12` — anchors at a 12-block fee target (in the
  shipped run commands; otsd's own default is 1008).
- Batch interval and confirmation depth run at the shipped defaults:
  operator guide, "Bitcoin transaction cost" and "Proof lifecycle".

## Proof states

Use simple states where possible:

- `waiting_for_payment`
- `receipt_issued`
- `waiting_for_bitcoin`
- `bitcoin_backed`
- `needs_attention`

These are the ops-side lifecycle states (what the ops scripts emit). The
client-facing API vocabulary is different and lives in the README
("`/verify` and `/upgrade` status vocabulary"). Mapping:

| Ops state | API `/verify` / `/upgrade` status |
|---|---|
| `waiting_for_payment` | — (no proof exists yet; the client holds only a 402 challenge) |
| `receipt_issued` | `pending` |
| `waiting_for_bitcoin` | `pending` (the API cannot distinguish these two — the split is ops-side: receipt just issued vs. anchor transaction awaiting confirmations) |
| `bitcoin_backed` | `anchored` |
| `needs_attention` | `mismatch`, `no_attestations`, or `invalid` — or `pending` for longer than the anchoring policy explains |

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
