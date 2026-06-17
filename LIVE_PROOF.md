# Live Mainnet Proof Records

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
- **Commit:** 94eb530
