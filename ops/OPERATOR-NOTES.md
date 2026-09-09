# timestamp-gateway operator notes

## Live services

Gateway:

- compose service `gateway` (the compose path), or `timestamp-gateway.service` under systemd

Payment backend:

- Phoenixd
- host service (`phoenixd.service` where systemd-managed)
- API: `127.0.0.1:9740` by default; the shipped `deploy/phoenixd.service.example` binds `172.17.0.1:9740` (the docker0 bridge) so a container-run gateway can reach it — password-gated, reachable from every container on the host (operator guide, "Neighbours on the bridge")

Local OpenTimestamps calendar:

- compose service `otsd` (or the standalone `otsd` container under systemd)
- calendar state: the host path `OTSD_CALENDAR_DIR` names, mounted at `/calendar`

## Operator commands

Run from the repo checkout:

`./ops/status.sh`

`./ops/phoenixd-status.sh`

`./ops/otsd-status.sh`

`./ops/list-proofs.sh`

`./ops/proof-status.sh`

`./ops/upgrade-proof.sh`

## Phoenixd boundary

Phoenixd is the live Lightning payment backend.

It is managed by systemd.

It listens on loopback by default, or on the docker0 bridge address (`172.17.0.1`) under the shipped unit — never on a public interface.

The gateway uses Phoenixd through `.env`.

Do not print or paste the Phoenixd password.

Phoenixd state lives in the directory `PHOENIX_HOME` names (a `.phoenix`
directory — e.g. `~/.phoenix` beside the phoenixd binary's user).

Important files:

- `phoenix.conf`
- `seed.dat`
- `phoenix.mainnet.*.db`
- `phoenix.mainnet.*.db-wal`
- `phoenix.mainnet.*.db-shm`
- `phoenix.log`

`seed.dat` is critical. Treat it as secret wallet material. Recovery is
by seed — a restored phoenixd is never started from copied state
(ops/BACKUP-RECOVERY.md, "Recovery is by seed").

## Phoenixd first payment warning

On a fresh Phoenixd node with no open channel, the first received payment triggers an automatic channel open by ACINQ.

ACINQ deducts a liquidity fee from the received amount. The figures are ACINQ's, they change, and this file does not record them — current schedule: https://phoenix.acinq.co/server/liquidity. Shape of the fee: a mining-fee component plus a service percentage of the liquidity purchased, paid upfront out of the payment that triggers it.

The arithmetic that matters is not the exact figures:

    received = invoiced − liquidity fee

The fee can exceed the whole margin of a small first payment — or the payment itself.

What the gateway does about it (pinned by `test_liquidity_fee_netted_receive_still_verifies` and `test_liquidity_fee_payment_yields_obligation_and_proof` in test_main.py): verify_payment checks the invoice's FACE amount (requestedSat) against the mint-time price, never the credited amount — a settled bolt11 is atomic, so settlement proves the payer paid the face amount in full. A settled first payment gets its proof; the liquidity fee nets the OPERATOR's credit and logs a WARNING ("Liquidity fee observed: requested N sat, received M sat"). The customer is never refused over ACINQ's fee.

The remaining failure mode is upstream of the gateway: a payment too small to carry the fee fails to settle at the Lightning layer — no sats move, the invoice stays unpaid, and a retry after pre-funding succeeds. (phoenixd liquidity-policy behaviour; not yet exercised on this deployment — unverified.)

Mitigations:
- Pre-fund the Phoenixd node by receiving a payment before going live — concrete walkthrough: operator guide, "First payment: pre-fund before going live"
- Price so the first-receive fee cannot dominate your margin — the fee is your cost, not the customer's shortfall

Once a channel is open, subsequent payments arrive at full value with no deduction.

## otsd boundary

The local calendar is your own `otsd`, not the public OpenTimestamps calendars.

The container's exact shape — image, mounts, network, env, command — is
recorded once, in BACKUP-RECOVERY.md "Docker shape" (the restore record);
the installed unit matches `deploy/otsd.service.example`.

Policy:

- Fee cap: `--btc-max-fee 0.0002` in the shipped template, no `-v` (INFO
  is the production log level). Credentials ride
  `/etc/systemd/system/otsd.env`, owned by the service user, mode 600.
  When hand-editing the cap: the flag takes BTC, 0.0002 BTC = 20,000 sats.
- A code rotation is pull + restart and never touches the installed unit;
  a unit change lands only through an explicit install + daemon-reload.
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

No files, no accounts, no claims, no truth, no custody (README, "What the gateway does and does not do").

The gateway accepts digests and returns portable `.ots` receipts. A receipt proves the digest existed no later than the time supported by the OpenTimestamps proof path. It does not prove document truth, authorship, consent, legality, originality, completeness, or content review.
