# Notarie Receipt Format v1

This document defines the local receipt shape that a notarie-style client saves beside a timestamped file.

The gateway remains dumb:

- no files
- no accounts
- no claims
- no authorship
- no custody
- no truth assertions
- digest in
- paid proof out

## Files saved locally

For an input file named example.pdf, notarie saves:

- example.pdf.sha256
- example.pdf.ots
- example.pdf.notarie.json

## .sha256

Plain lowercase SHA-256 hex digest of the source file.

Example:

87571e11ca692ab728b4ae7897fc7fd164a176b508f786abe5e6b9f3d8534c5a

## .ots

Raw OpenTimestamps detached timestamp proof bytes returned by the gateway.

The .ots file is portable and independently verifiable with OpenTimestamps tooling.

## .notarie.json

Machine-readable local receipt metadata.

Minimum v1 shape:

{
  "schema": "notarie.receipt.v1",
  "created_utc": "2026-06-26T12:00:00Z",
  "source": {
    "file_name": "example.pdf",
    "digest_algorithm": "sha256",
    "digest": "87571e11ca692ab728b4ae7897fc7fd164a176b508f786abe5e6b9f3d8534c5a"
  },
  "gateway": {
    "url": "http://100.98.161.106:8000",
    "endpoint": "/timestamp",
    "auth": "L402"
  },
  "payment": {
    "paid": true,
    "amount_sats": 500
  },
  "proof": {
    "ots_file": "example.pdf.ots",
    "ots_sha256": "<sha256-of-ots-file>",
    "bytes": 133,
    "status": "waiting_for_bitcoin",
    "bitcoin": null
  }
}

When upgraded to Bitcoin, proof.status becomes bitcoin_backed and proof.bitcoin is filled:

{
  "block": 955477,
  "txid": "f22a6256f64a735dd16d02c6a7e0cbe3f7f34e18bcb336d482ddd539f94b2465"
}

## Sensitive data exclusion

The receipt MUST NOT store:

- L402 macaroon
- payment preimage
- raw Lightning invoice unless explicitly requested for debugging
- wallet credentials
- gateway secrets
- source file contents

The receipt MAY store the paid amount and gateway URL.

## Semantics

A receipt means:

This SHA-256 digest received an OpenTimestamps proof from the configured gateway.

If Bitcoin-backed, it additionally means:

This digest is anchored through the OTS proof path into the stated Bitcoin block.

A receipt does not prove:

- file authorship
- file truth
- file legality
- consent
- originality
- chain of custody
- that the gateway saw the file
- that the operator reviewed the content
