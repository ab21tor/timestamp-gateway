# Live Mainnet Proof Records

---

## Proof 3 — Stranger run: docs-only install, unattended sales (2026-07-27)

Between 24 and 27 July 2026, a stranger run took a fresh VPS from nothing to a live gateway using the documentation alone, and the resulting island made six sales over the onion unattended. The run passed: proofs were anchored in Bitcoin, anchoring amortized across sales as designed, and a customer-side upgrade produced a proof independently verifiable against Bitcoin.

- **Date:** 2026-07-27 (run window 2026-07-24 → 2026-07-27)
- **Install:** docs-only, fresh VPS
- **Sales:** six over the onion, 6,918 sats total
- **First anchor transaction:** `8b558dfc...` — confirmed in block 959459, fee 308 sats after one bump
- **Amortization:** five proofs on one 153-sat anchor — 30.6 sats/proof
- **Customer-side verification:** `/upgrade` returned an anchored proof, independently verifiable against Bitcoin
- **Net:** +6,457 sats, unattended

### Findings register (stranger run)

Five findings, all resolved in the docs on 2026-07-27:

1. **Prerequisites incomplete.** git missing from the prerequisites; the README's pytest instruction broken as written (test-only packages absent from `requirements.txt`). Fixed: prerequisites in both docs; README "Local development".
2. **Docker floor without an acquisition route.** The version floor named no way to obtain a compliant Docker on a fresh box. Fixed: operator guide, "Prerequisites".
3. **Same-host bitcoind undocumented.** rpcbind/rpcallowip for the compose stack, credentials, and wallet-load persistence — without `load_on_startup`, a bitcoind restart leaves otsd Bitcoin-blind (`JSONRPCError -18`). Fixed: operator guide, "Same-host bitcoind under compose".
4. **Funding: the toll, and the refused Lightning pre-fund.** The on-chain swap-in deposit is the proven pre-fund rail (31,232 sats in → 21,561 toll → 9,671 remaining; ~2M-sat inbound channel); the Lightning pre-fund was refused upstream in three attempts from a Phoenix mobile sender (`UpdateFailHtlc` in ~1 second, receiver never contacted; the prescribed payer-wallet sender untested); a payer-side phoenixd pays the identical toll under defaults (81,736 in → 21,561 toll → 60,175 spendable). Fixed: operator guide, pre-fund walkthrough, "Funding the payer side (phoenixd as payer)", and "Inbound liquidity".
5. **Calendar URI for a domainless island.** Dissolved by the README's existing calendar-URI note (the uri need not resolve). Residue: recommend the onion address as the natural uri identity (README, "Calendar URI note") and verify the onion answers after minting (operator guide, first-run checklist step 10).

---

## Proof 2 — First calendar-mode proof (2026-06-17)

On 17 June 2026, timestamp-gateway completed its first proof in calendar mode (`OTS_BACKEND_MODE=calendar`). The gateway forwarded the paid digest to the operator-controlled otsd instance, which submitted an anchoring transaction to Bitcoin mainnet via Start9 Bitcoin Core over Tor.

- **Date:** 2026-06-17
- **Mode:** calendar (`OTS_BACKEND_MODE=calendar`)
- **Digest:** b94f6f125c79e3a5ffaa826f584c10d52ada669e6762051b826b55776d05a152
- **Gateway invoice paid via:** Phoenix (1000 sats)
- **otsd anchor transaction:** b0ec0468ed7579e6b7e62c793b4c9a3c33f38d3c0cb5254f1e5496d047ea1107
- **Proof returned:** 133 bytes, application/octet-stream
- **Stack:** timestamp-gateway → LND → Phoenix payment → otsd → Start9 Bitcoin Core over Tor → otsd-hot wallet → Bitcoin mainnet

---

## Proof 1 — First live mainnet proof (2026-06-16)

On 16 June 2026, timestamp-gateway completed its first live mainnet proof in public mode: a real Lightning payment was settled, the payment preimage was used to unlock the request, and the gateway returned a 926-byte OpenTimestamps proof for the submitted SHA-256 digest.

- **Date:** 2026-06-16
- **Digest:** b94f6f125c79e3a5ffaa826f584c10d52ada669e6762051b826b55776d05a152
- **Payment hash:** 9e72b549d5203ab9ce648af7019648ea5da34349c181588864561de64d71ead0
- **Proof:** 926 bytes, application/octet-stream
- **Commit:** 9236d1f
