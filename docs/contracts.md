# Contracts

What each part of the paid door promises, who owns unfinished work at
each handoff, what durable evidence lets a part forget, and which records
are authoritative. Every statement here is made by code named by file and
line in the tree as changed on 2026-09-18 for workflow five, step 1, on
top of revision 713269e (branch `l402`). A row labelled *current behaviour* describes that code. A
row labelled *required postcondition* states what must hold; where the
code does not make it hold, a *known defect* row beside it says so and
names the finding of the 2026-09-15/16 review. A row labelled *operating
assumption* is something the code relies on and does not check. Nothing
here describes a change that has not been made. The step 0 version of
this document, the gate's eight rulings and the step 1 close-out are the
operator's session records of 2026-09-18, kept outside the repository.

The form follows `docs/contracts.md` of the calendar fork
(`opentimestamps-server` at 4bd1018): its sections 1 to 7 are the model,
and its section 10 is where the backup of this deployment is already
described from the calendar's side (R1, current defect (c)).

The words used throughout:

- **Digest**: the 32-byte SHA-256 a client posts as 64 hex characters.
  The gateway never sees the bytes it is a digest of.
- **Challenge**: the 402 answer to an unauthenticated `POST /timestamp`:
  a Lightning invoice and a macaroon, both for one digest at one price.
- **Token**: the macaroon. Its signed caveats name the digest, the
  invoice's payment hash, the price the challenge was minted at, the
  capability `timestamp`, and an advisory expiry.
- **Invoice**: the bolt11 the wallet (phoenixd) minted for the challenge.
  Its memo is the digest; its face amount is the price.
- **Preimage**: the 32 bytes whose SHA-256 is the invoice's payment hash.
  Holding it is the client's proof of payment.
- **Settlement**: phoenixd's own record that the invoice was paid
  (`isPaid` on `GET /payments/incoming/<hash>`).
- **Obligation**: one row of the `obligations` table: a settled payment
  the gateway has accepted the duty to stamp, keyed by payment hash.
- **Proof**: the `.ots` bytes the calendar returns for a digest. It is
  *pending* while its attestation names the calendar, and *attested*
  once it carries a Bitcoin attestation node. "Verified" is a claim
  nothing in this repository makes.
- **The calendar**: the operator's `otsd`, the calendar fork. Its own
  contract is the fork's `docs/contracts.md`; this document cites it as
  "fork C1" and so on.
- **The wallet**: phoenixd, reached with the limited password. It holds
  every invoice and every settlement; the gateway holds neither.
- **Retired on 2026-09-18** (workflow five, gate rulings 2 to 6): anchor
  billing, the free door and the public-calendar relay. The door is
  paid, the calendar is the operator's, and nothing here describes them
  further than section 8's note.

## 1. What an HTTP answer promises

| Request | Answer | The promise | What it does not promise |
|---|---|---|---|
| `POST /timestamp`, no `Authorization` | 402, `WWW-Authenticate: L402 macaroon="…", invoice="…"`, JSON body with `price_sats`, `invoice`, `macaroon`, `expiry` | An invoice for exactly `PRICE_PER_PROOF_SATS` with this digest as memo exists at the wallet, and the token binds that invoice's payment hash, this digest and this price under the root key (`main.py:1924-1949`, `mint_l402_token` 1272-1282) | Nothing is owed and nothing is recorded in the gateway: an unpaid challenge is not an obligation. Not that the invoice stays payable: its lifetime is the wallet's default expiry, and the token's `expiry` caveat is advisory (`_expiry_satisfier`, 1293-1299) |
| `POST /timestamp` with `Authorization: L402 <macaroon>:<preimage>` | 200, body = the proof, `Content-Disposition: attachment` | The token verified under the root key for this digest; the preimage hashes to the token's payment hash; the wallet reported the invoice settled, with this digest as memo and a face amount at or above the token's price; an obligation row for this payment hash exists and is `stamped`, or the proof came from the process cache of an earlier stamp (1874-1922) | Not that the proof is anchored: it is pending. Not that the same digest was submitted to the calendar once: a re-presented token after a restart stamps again (section 4, G3) |
| the same | 401 | The header did not parse, or the token failed signature, digest, capability or caveat shape, or the preimage does not hash to the payment hash (`verify_l402_token` 1309-1364, 1883-1891). One fixed detail string; the log names an exception class and nothing else | Nothing about payment: a 401 is never a payment verdict |
| the same | 402, detail `Payment required or not settled`, no new challenge | The wallet answered, and it did not report this invoice settled for this digest at the mint-time price (`verify_payment` 1568-1597, 1893-1894) | Not that the invoice is unpaid: a payment in flight reads as not settled until it settles. The client keeps its token and retries |
| the same | 502, detail `Payment backend error: could not verify payment` | The wallet did not answer (1490-1504) | Nothing about payment either way. **A timeout is not evidence of non-payment**: the client retries with the same token |
| the same | 502, detail `OTS error: stamping failed` | The payment verified and the obligation row is on disk as `needs_stamp`; every calendar submission attempt failed (1908-1915, `stamp_digest` 1531-1565) | Not that the digest never reached the calendar: an attempt may have been accepted after the gateway stopped waiting. The sweeper and the client's next presentation both resubmit (G3, G4) |
| `POST /timestamp`, no `Authorization` | 429 with `Retry-After` | The peer's mint bucket is empty; nothing was minted and the wallet was not contacted (1928-1934) | — |
| any route but `/health` | 503, `Gateway is paused by operator` or `auto-paused: anchor wallet below the stamper fee cap` | The pause file exists, or the wallet-status file shows a balance below one fee cap (`_pause_gate` 827-843). Nothing is dropped: tokens redeem after the pause and obligations wait | — |
| `POST /verify` | 200, JSON | The proof's encoding, its digest and its attestation nodes were read by the public library (`_verify_ots_bytes` 1121-1138). `status` is one of `bitcoin_attestation_present`, `pending`, `mismatch`, `no_attestations`, `invalid`; `verified` is `null` for the first and `false` otherwise, never `true` | Nothing about Bitcoin: the attested root was not checked against any block header (section 6) |
| `POST /upgrade` | 200, JSON with `ots` | As `/verify`, and: for a `pending` proof the calendar was asked for each distinct pending commitment, within the bounds of G5, and the returned bytes carry the Bitcoin attestation if one was obtained (`_upgrade_ots_bytes` 1228-1262) | Not that "pending" means the calendar still holds the entry: a `404` from the calendar is read as not yet anchored, with no deadline (fork R2, current defects) |
| `POST /upgrade` | 503, `status: calendar_unavailable` | Every lookup failed by transport: the calendar did not answer, or did not answer within the request's budget (1863-1867; G5) | — |
| `GET /health` | 200 `ok`, or 503 `degraded` / `paused` / `auto_paused` | Each field is the reading of one file or one probe, in the vocabulary of section 4, G7, and the overall status is the rule at 1767-1782 | Not that the service is correct: `payment` is the outcome of the last real mint, `otsd` is one status read, the file-mediated fields are what the timers last wrote |

**Ambiguous outcomes across the door.** Every answer above is sent after
the writes it describes. A client that times out before reading a 200
holds a token whose obligation is on disk and, if the stamp completed,
whose proof is in the process cache; it presents the token again and gets
the cached bytes, or a fresh stamp after a restart. No ambiguous outcome
deletes anything: the obligations table only grows (rows are never
deleted by code), and the wallet's records are the wallet's.

## 2. Records: authoritative, evidence, rebuildable

| Record | Kind | What it is authoritative for | Loss or damage |
|---|---|---|---|
| `obligations` table (SQLite, WAL, at `OBLIGATIONS_DB_PATH`; schema `main.py:581-590`) | authoritative | Every settled payment the gateway accepted, and whether it was stamped. A `needs_stamp` row is owed | A lost `needs_stamp` row is a settled payment the gateway no longer knows it owes; the client's token and the wallet's settlement still exist, so the client's next presentation recreates the row (`INSERT OR IGNORE`, 608-623). `stamped` rows are history only; the operator guide says they may be purged |
| the wallet's invoices and settlements (phoenixd's database) | external, authoritative | What was minted, for which digest (the memo), and what settled. Every redeem asks it (1490-1511) | The gateway cannot verify a payment the wallet has forgotten: a 502 while the wallet is down, a 402 if its record is gone. The wallet's backup is the operator's (`ops/BACKUP-RECOVERY.md`, "Recovery is by seed") |
| the root key `L402_SECRET_HEX` | configuration, authoritative for token verification | Whether any token ever minted verifies (1309-1364) | A changed or lost key makes every unredeemed token a 401. The wallet still holds the settlement and the memo names the digest, so the obligation can be met by hand; nothing in code does it. Operating assumption: the key is kept and never rotated while tokens are outstanding |
| `_proof_cache` (memory, 465-475; 10,000 entries, FIFO) | rebuildable | Nothing: the instant re-serve path for a token already stamped in this process | Empty after a restart or eviction: a re-presented token stamps again |
| `_rate_buckets`, `_verify_rate_buckets`, `_last_mint`, `_health_probe_cache` (memory) | rebuildable | Nothing across a restart | A restart refills every bucket and reads `payment: unknown` until the first mint |
| the pause file (`PAUSE_FILE`) | operator's | Whether the door is closed by hand | — |
| `wallet-status`, `proofs-status`, `backup-status` (one JSON line each, written by the ops timers) | evidence, read only here | What the timer last saw and when. Classified at 845-993; `absent` is reported, not degraded | A stale file degrades `/health` after its maximum age; the float backstop keeps the last balance reading whatever its age (890-904) |
| the calendar's `journal`, `db/`, identity files, receipts | the calendar's (fork section 2) | The proof itself, and what the pending attestation can be upgraded to | The gateway holds no copy of any proof. A calendar that loses its state strands every pending proof it issued (README, "What the client must keep") |
| `anchor_bills` table in an existing `obligations.db` | operator data, not read | Nothing: anchor billing was retired on 2026-09-18 (section 8). The table is never dropped or migrated by code | — |
| the backup archive (`ops/backup-live-state.sh`) | a copy: the obligations snapshot checked; the calendar directory at a boundary the run established or verified, else a hot copy the run calls failed; everything else hot | Section 4, G9 | — |

## 3. Who owns unfinished work

| Handoff | Before | After | Owner of the work in between | Evidence that lets the previous owner forget |
|---|---|---|---|---|
| client → gateway (challenge) | the client holds a digest | an invoice at the wallet and a token in the client's hands | nobody: nothing is owed | none needed |
| client → wallet (payment) | an unpaid invoice | settlement at the wallet; the preimage with the client | the Lightning network, then the wallet | the preimage; the wallet's settlement record |
| client → gateway (redeem) | token and preimage | the obligation row committed (`needs_stamp`) | the client, until the `INSERT` commits: a stop before it leaves the client with a token it presents again | the commit at 1908; from here the gateway owns the duty |
| obligation → calendar | `needs_stamp` | the calendar's 200 (its journal fsynced: fork C1), then `stamped` | the row; the sweeper is its retry | the row marked `stamped` (1917, 681) |
| gateway → client | stamped | the proof bytes with the client | the client: the gateway keeps no proof | the client's copy (README, "What the client must keep") |
| pending → attested | a pending proof | the same proof carrying a Bitcoin attestation | the client, who asks `/upgrade`; the calendar answers for as long as its journal holds the entry (fork R2) | the upgraded file |
| live state → backup | the live obligations log and the calendar | an archive with a checked obligations snapshot and a hot copy of the calendar | the timer, then the operator | `backup-status`, which is success only when every member it names was taken at its boundary (G9) |
| operator → pause | running | the pause file exists | the operator | — |

## 4. The paid door: challenge → payment → obligation → proof → upgrade

One table per transition. Each row carries its label.

### G1. The challenge

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the wallet's invoice row. Nothing in the gateway | current behaviour |
| Owner of unfinished work | nobody: an unpaid challenge is not an obligation | current behaviour |
| Preconditions | the door is paid, always: an explicitly configured `L402_ENABLED=false` is refused at startup with a migration diagnostic, never read as a paid door (`_parse_config` 89-98; `test_free_door_false_is_refused_with_a_migration_diagnostic`); the pause gate open; the body a 64-hex digest (422 otherwise); the peer's mint bucket has a token (`_rate_limit_retry_after`, 536-539; the peer is the direct address, or the rightmost `X-Forwarded-For` entry only with `GATEWAY_BEHIND_PROXY=true`, 496-506) | current behaviour |
| Side effects, in order | (1) one token taken from the peer's bucket (memory); (2) `POST /createinvoice` at the wallet with `amountSat` = the flat price, `description` = the digest, a random `externalId` (1461-1488); (3) `_last_mint` replaced (1428-1438); (4) the macaroon minted with caveats `digest=`, `payment_hash=`, `price=`, `capability=timestamp`, `expiry=now+L402_TOKEN_EXPIRY_SECONDS` (1272-1282); (5) the 402 | current behaviour |
| Visibility and durability | the invoice is durable at the wallet before the 402; nothing is written in the gateway | current behaviour |
| Acknowledgement | the 402 | current behaviour |
| Ambiguous outcomes | a 502 from `createinvoice`: the wallet may or may not hold an invoice nobody has a token for (unpaid, expires); a client timeout after the 402: the client asks again and holds two challenges, both unpaid | current behaviour |
| Recovery | none needed; unpaid invoices expire at the wallet | current behaviour |
| Intended guarantee | an unpaid challenge costs the gateway nothing durable and creates no obligation; the price in the body, the invoice and the token are the same number | required postcondition (met) |
| Operating assumptions | the wallet's invoice expiry is its default; the token's expiry caveat is format-checked only and never enforced, so settlement, not the clock, gates redemption | operating assumption |
| Tests | `test_unauthenticated_post_returns_402`, `test_402_www_authenticate_header_exact_format`, `test_402_json_body_has_status_price_invoice_macaroon_expiry`, `test_402_creates_invoice_with_digest_memo_and_configured_price`, `test_mint_rate_limited_returns_429_and_spares_phoenixd`, `test_create_invoice_failure_raises_generic_502_and_logs`, `test_rate_limit_*` | current behaviour |

### G2. Payment verification against the mint-time price

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the token's signed caveats (payment hash, price, digest) and the wallet's incoming-payment record (`isPaid`, `requestedSat`, `receivedSat`, `description`) | current behaviour |
| Owner of unfinished work | the client, who holds the token and the preimage; this transition is a gate and writes nothing | current behaviour |
| Preconditions | the header parses as `L402 <base64>:<64 hex>` (`parse_l402_auth` 1366-1373; 401 otherwise); the pause gate open | current behaviour |
| Side effects, in order | (1) deserialize the macaroon and read its caveats; a failure is one WARNING naming the exception class and a 401 (1324-1347); (2) verify `digest=<this digest>`, `capability=timestamp`, the shapes of `payment_hash=`, `price=` (a positive integer), `expiry=`, and the signature under the root key (1349-1362); (3) `sha256(preimage) == payment_hash`, 401 otherwise (1889-1891); (4) `GET /payments/incoming/<hash>` at the wallet, 502 on any failure (1490-1504); (5) settled, memo equal to the digest, and `requestedSat >= price` from the token (1593-1597); a liquidity fee (`receivedSat < requestedSat`) is logged and never charged to the client (1586-1592) | current behaviour |
| Visibility and durability | no write. Every redeem repeats (4), cache hits included: the lookup at 1893 precedes the cache read at 1896 | current behaviour |
| Acknowledgement | none: the request proceeds to G3 | current behaviour |
| Ambiguous outcomes | the wallet did not answer: 502, no verdict; not settled: 402, which a payment in flight also gets until it settles; an invoice that expired unpaid can never settle and the client needs a new challenge (nothing is owed) | current behaviour |
| Recovery | the client retries with the same token | current behaviour |
| Intended guarantees | a token minted at price N validates at N for as long as its settled invoice backs it, never re-bound to the live price; settlement outranks the token's expiry; the wallet's own cut never counts against the client; a wallet that does not answer is a 502, never a 402 | required postcondition (met) |
| Tests | `test_verify_token_*` (13 tests), `test_expired_token_still_verifies`, `test_settled_but_expired_token_redeems`, `test_expired_unsettled_token_still_pays_nothing`, `test_wrong_preimage_rejected_before_backend_lookup`, `test_verify_payment_*`, `test_verify_payment_enforces_mint_time_price_not_static_config`, `test_challenge_redeems_after_repricing_down`, `test_endpoint_honors_in_flight_invoice_after_repricing`, `test_liquidity_fee_netted_receive_still_verifies`, `test_overpaid_underminted_invoice_rejected`, `test_verify_payment_lookup_failure_raises_generic_502_and_logs`, `test_garbage_token_logs_single_warning_no_traceback`, `test_review_malformed_token_content_never_reaches_the_log` | current behaviour |

### G3. Redemption: obligation, stamp, proof

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the `obligations` row for this payment hash; the calendar's journal once its submit returns (fork C1) | current behaviour |
| Owner of unfinished work | the row, from its commit until it is `stamped`; the sweeper (G4) is its retry, the client's next presentation the other | current behaviour |
| Preconditions | G2 passed | current behaviour |
| Side effects, in order | (0) a cache hit for the payment hash returns the bytes with no write (1896-1903); otherwise (1) `INSERT OR IGNORE` the row as `needs_stamp`, committed (1908, 608-623); (2) `stamp_digest`: up to `OTS_SUBMIT_MAX_ATTEMPTS` submits to `OTS_CALENDAR_URL`, `OTS_SUBMIT_BACKOFF_SECONDS` apart, 10 s each, no fallback to any other calendar (1531-1565); (3) the bytes into the cache (1916); (4) `UPDATE … status='stamped'`, committed (1917, 625-635); (5) the 200 | current behaviour |
| Visibility and durability | (1) is durable before any calendar contact; the calendar's 200 means its journal is fsynced (fork C1); (4) is durable before the response is built | current behaviour |
| Acknowledgement | the 200 with the bytes | current behaviour |
| Ambiguous outcomes | a stop between (1) and (2): the row is owed and nothing was submitted; a stop between (2) and (4): the calendar holds the commitment and the row still says `needs_stamp`; a stop between (4) and (5): stamped, the client has no bytes; (2) failing every attempt: 502 with the row `needs_stamp`. In every case the sweeper resubmits the digest and the client, presenting the token again, gets the cache or a fresh stamp. A resubmission inside the calendar's dedupe horizon is the same commitment; past it, or across a calendar restart, a second commitment and a second record (fork C1, "Ambiguous outcomes"). The client's charge is the flat price either way | current behaviour |
| Recovery | the sweeper's next tick; the client's next presentation. Both are idempotent on the row | current behaviour |
| Intended guarantees | a settled payment is never lost: from (1) the gateway owns the duty and only a `stamped` row or a served proof discharges it; a paid token presented twice never mints a second invoice and never makes a second row; an unpaid replay never reaches the cache: the order is token, preimage, settlement, then cache | required postcondition (met) |
| Operating assumptions | the calendar's dedupe horizon (one hour, in memory, 65,536 entries: fork README "Anchor receipts") bounds the over-count from resubmission; one gateway process serves one obligations database | operating assumption |
| Tests | `test_review_g3_a_stop_after_the_stamp_and_before_the_mark_is_converged_by_cache_and_sweeper` (a failed (4): the cache serves the next presentation, the sweeper converges the row, one resubmission), `test_review_g3_a_failed_obligation_write_makes_no_calendar_contact_and_the_next_presentation_converges` (a failed (1)), `test_review_g8_a_restart_forgets_the_cache_but_never_the_obligation` (process death after (1), then a restart), `test_obligation_success_marks_stamped`, `test_obligation_stamp_failure_returns_502_and_persists_needs_stamp`, `test_token_representation_stays_instant_via_proof_cache`, `test_same_token_preimage_reuse_returns_cached_proof_not_double_stamp`, `test_duplicate_payment_hash_single_row_no_new_invoice`, `test_paid_retry_succeeds_after_initial_otsd_failure`, `test_calendar_mode_exhausts_retries_then_returns_generic_502`, `test_calendar_mode_never_falls_back_to_public_calendars`, `test_paused_blocks_paid_redemption_and_unpause_serves_it`, `test_float_stop_blocks_paid_redemption_and_recovery_serves_it`, `test_proof_cache_bounded_fifo`, `test_unwritable_obligation_db_fails_loud` | current behaviour |

### G4. The sweeper

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the `obligations` table | current behaviour |
| Owner of unfinished work | each `needs_stamp` row | current behaviour |
| Preconditions | the thread started at lifespan (707-723); a tick at start and every `OBLIGATION_SWEEP_INTERVAL` seconds (697-704); the tick is skipped while paused or float-stopped (685-695) | current behaviour |
| Side effects, per row, in order | (1) `attempts+1`, `last_attempt_at`, committed (661-668); (2) `stamp_digest` (671-678); (3) the cache (680); (4) `stamped`, committed (681). Each row is its own short transaction; a fresh connection per operation (561-567) | current behaviour |
| Visibility and durability | (1) and (4) durable; the cache entry lets a later presentation of the token be served without a second stamp | current behaviour |
| Acknowledgement | none outside the process: an INFO line naming the first 8 hex of the hash on success; a WARNING with the full hash and the traceback on failure | current behaviour |
| Ambiguous outcomes | a stop after (2) before (4): resubmitted at the next tick under the calendar's dedupe rule; a failed (4) ends that tick (the rows after it wait for the next one) with the row still owed and its attempt counted; a stamp that keeps failing: retried every tick, forever | current behaviour |
| Recovery | the next tick | current behaviour |
| Intended guarantee | a `needs_stamp` row is retried until stamped, never capped and dropped; the sweeper never mints and never contacts the wallet | required postcondition (met) |
| Tests | `test_review_g4_a_sweeper_stopped_after_the_stamp_converges_on_the_next_tick`, `test_sweeper_completes_pending_obligation_and_bumps_attempts`, `test_sweeper_records_attempt_on_repeated_failure`, `test_sweeper_skips_while_paused`, `test_float_stop_sweeper_skips`, `test_obligations_needs_stamp_partial_index_migration_safe` | current behaviour |

### G5. `/upgrade`: pending → attested

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the calendar's database (fork section 2); the gateway holds nothing and writes nothing | current behaviour |
| Owner of unfinished work | the client, who holds the pending proof and asks; the calendar owes the path for as long as its journal holds the entry | current behaviour |
| Preconditions | the pause gate open; the peer's verify bucket has a token, or the request carries `UPGRADE_CLIENT_TOKEN` as a bearer (1829-1840, 1845-1855); the body at most 512 KiB (`_BodyCap`, 748-821), the proof at most 256 KiB (1859-1860); the bytes deserialize (else `invalid`, 200), the digest matches (else `mismatch`), some attestation is pending and none is Bitcoin (else no calendar contact: 1235-1243) | current behaviour |
| Side effects, in order | per distinct pending commitment, in walk order: one `get_timestamp` at `OTS_CALENDAR_URL`, at most `UPGRADE_MAX_CALENDAR_QUERIES` (8) lookups, each run in a worker thread with `timeout=min(5, 15 − elapsed)` and joined for what remains of the 15 s budget; a worker still reading when the budget runs out is abandoned (it ends at its own socket timeout, at most 5 s of inactivity later), counted as a failed lookup, and the walk stops with the budget reported exhausted; the walk also stops once a Bitcoin attestation is merged (1151-1226; the worker at 1183-1205). Not found counts as a successful lookup; any other exception as a failure, logged by class name only | current behaviour |
| Visibility and durability | nothing durable. The answer's `upgrade` field reports `calendar_queries` and `budget_exhausted` | current behaviour |
| Acknowledgement | 200 with `status` and `ots` (the original bytes unless a Bitcoin attestation was obtained); 503 `calendar_unavailable` when every lookup failed by transport or by the budget (1252-1259, 1863-1867) | current behaviour |
| Ambiguous outcomes | `pending` covers both "not anchored yet" and "the calendar no longer holds the entry" (fork R2, current defects): the answer has no deadline and nothing here can tell them apart | current behaviour; operating assumption |
| Recovery | the client asks again later | current behaviour |
| Intended guarantee | one `/upgrade` makes at most eight lookups and takes at most fifteen seconds in all, on the wall clock; a calendar that did not answer, in time or at all, is a 503, never `pending` | required postcondition (met since 2026-09-18) |
| Was (F18) | until 2026-09-18 the budget was checked only between lookups and the per-lookup `timeout` reached the library as a socket inactivity timeout, so a calendar trickling bytes in gaps under it kept one lookup running past the budget: the review's loopback demonstrator delivered 13 bytes in 1.42 s gaps in 17.07 s against a 15 s limit (`repro/gateway/slow_calendar.py`). The same server shape, with the budget scaled down, is now the test | current behaviour |
| Tests | `test_review_upgrade_wall_clock_deadline_holds_against_a_trickling_calendar` (a real loopback server trickling a valid answer over three times the budget, every gap under the per-lookup timeout: the request ends within the budget, 503, one failed lookup, budget exhausted), `test_review_upgrade_calendar_work_is_bounded_per_request`, `test_review_upgrade_elapsed_time_is_bounded` (a mocked clock advanced between lookups), `test_review_upgrade_deduplicates_commitments_and_stops_at_first_anchor`, `test_review_calendar_unavailable_is_a_503_not_pending`, `test_review_calendar_failure_log_names_the_class_only`, `test_upgrade_*` (9 tests), `test_upgrade_client_token_*` | current behaviour |

### G6. `/verify`: the structural check

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the bytes the client sent; nothing of the gateway's | current behaviour |
| Preconditions | the verify bucket; the body cap; the proof cap | current behaviour |
| Side effects | none | current behaviour |
| Acknowledgement | 200 with the one answer shape (`_proof_answer`, 1068-1098): `status`, `valid_ots`, `digest_match`, `bitcoin_attestation_present` (and `bitcoin_anchored`, the same flag under its old name), `verified` null or false, `verification: "structural"`, the note, the attestation list | current behaviour |
| Intended guarantee | `/verify` and `/upgrade` never say `verified`; a fabricated attestation naming a real height is reported `bitcoin_attestation_present` with `verified: null`, exactly as a genuine one | required postcondition (met) |
| Tests | `test_verify_*` (9 tests), `test_review_fabricated_attestation_is_never_reported_verified`, `test_review_verified_is_false_for_every_non_attested_state`, `test_review_body_cap_*` | current behaviour |

### G7. `/health`: the structural check of the operation

| Row | Statement | Label |
|---|---|---|
| Authoritative record | none: each field is one reading. `payment` is `_last_mint` (ok / degraded / unknown); `otsd` is one read of the calendar's JSON status line, cached `HEALTH_PROBE_CACHE_SECONDS` behind a single-flight lock, a stale answer served while one refresh runs (`_probe_otsd` 1605-1688, `_otsd_probe_cached` 1694-1724); `wallet`, `proofs`, `backup` are the timers' files; `float` is the balance in the wallet file against `STAMPER_FEE_CAP_SATS` (1726-1804) | current behaviour |
| Preconditions | the peer's verify bucket (1730-1738); `/health` is the one route the pause gate lets through | current behaviour |
| Side effects | the probe cache | current behaviour |
| Acknowledgement | 200 only when every field is in its healthy set: `payment` ok or unknown; `otsd` ok; `wallet` ok or absent; `proofs` ok or absent; `backup` ok, local_only or absent; `float` ok or inactive (1767-1782); 503 otherwise, and `paused` / `auto_paused` outrank the rest | current behaviour |
| Ambiguous outcomes | a `503` says which field; `unknown` is the reading for a file that is present but not readable as its shape, never `ok`; `absent` is a timer not installed and does not degrade | current behaviour |
| Intended guarantee | `/health` never raises, never crashes, and answers `ok` only from readings in the healthy set; `otsd: ok` requires `best_block` set, not merely a 200 (a Bitcoin-blind calendar is `error`); the calendar's `needs_attention` findings are surfaced verbatim | required postcondition (met) |
| Operating assumptions | the calendar answers the JSON status line of fork c1db4dd or later (the retired page is read through its markers and named in the log as behind); the timers write their files atomically | operating assumption |
| Limits | `otsd: ok` proves RPC reachability from the calendar, not that its stamper thread is unwedged: the health monitor's stall alarm (`ops/health-monitor.sh`, 62-116) covers that class from outside | current behaviour |
| Tests | `test_health_*` (section 10, 13, 14, 16 of `test_main.py`), `test_health_otsd_reads_the_json_status`, `test_health_otsd_json_bitcoin_blind_is_error`, `test_health_otsd_needs_attention_degrades_and_surfaces`, `test_health_probe_single_flight_and_cached`, `test_health_rate_limited_per_peer_like_verify`, `test_health_never_raises` | current behaviour |

### G8. Start and stop

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the environment (`_parse_config`, 83-395, at import); the obligations database | current behaviour |
| Preconditions for serving | every required variable present and well-formed, else `RuntimeError` at import with a message that names the variable (section 7); an explicitly configured `L402_ENABLED=false` or `OTS_BACKEND_MODE=public` refused with a migration diagnostic naming the retirement and the way on, the other retired names (`PRICE_MARKUP`, the four billing names, `L402_ENABLED=true`, `OTS_BACKEND_MODE=calendar`) warned about once and ignored (89-107, 143-166); `init_obligation_db` succeeds: the directory made, WAL set, the table and the partial index created (569-606), else `RuntimeError` and no listener | current behaviour |
| Side effects, in order | (1) config parsed; (2) the module globals set from it (428-451); (3) at lifespan start, the database initialised, then the sweeper thread started (707-717); (4) uvicorn binds the listener after the lifespan startup returns | current behaviour |
| Acknowledgement | the listener bound; `/health` answering `payment: unknown` until the first mint | current behaviour |
| Stopping | the pause file: every route but `/health` answers 503 and the sweeper skips; the float stop: the same from the wallet file's balance, clearing itself on a newer reading; lifespan shutdown sets the stop event and joins the sweeper for up to 10 s (719-722): a stamp in flight past that is abandoned with its row `needs_stamp`, which the next start's first tick retries | current behaviour |
| Intended guarantee | nothing is served before the obligations log is writable; a pause or a stop loses nothing | required postcondition (met) |
| Operating assumption | one process, one worker: the caches, buckets and the probe cache are per process, and a multi-worker launch would give each worker its own (the shipped launch paths run one) | operating assumption |
| Tests | section 1 of `test_main.py` (config, with `test_free_door_false_is_refused_with_a_migration_diagnostic`, `test_public_relay_is_refused_with_a_migration_diagnostic`, `test_billing_names_are_warned_once_and_ignored`), `test_unwritable_obligation_db_fails_loud`, `test_review_g8_a_restart_forgets_the_cache_but_never_the_obligation`, `test_paused_*`, `test_float_*` | current behaviour |

### G9. The obligation log's backup, and the archive around it

| Row | Statement | Label |
|---|---|---|
| Authoritative record | the live obligations log, until a restore of a checked snapshot has started elsewhere; the archive's `metadata.txt` names the snapshot's path, its check verdict and the newest row's payment hash | current behaviour |
| Owner of unfinished work | the timer's run (`ops/backup-live-state.sh`, as root), then the operator: nothing in code restores | current behaviour |
| Preconditions | `.env` at `REPO_DIR` (else `failed`); every setting resolved after the shared loader; `OBLIGATIONS_DB_PATH` resolved once to the host path of the configured log: a configured path that is not a file on the host fails the member, never a default beside it (review F07, fixed at 044ad43); `CALENDAR_BACKUP_BOUNDARY` unset, `stop`, `stopped` or `snapshot`, any other value refused before anything is stopped | current behaviour |
| Side effects, in order | (1) `metadata.txt`, the status scripts' outputs, `docker inspect`, the units; (2) the newest obligation row's payment hash read from the live log; (3) the online snapshot: `sqlite3 .backup`, or the same API through the gateway container; (4) the snapshot checked: `PRAGMA integrity_check`, the table, the known row (`ops/verify-obligations-snapshot.sh`); an unusable snapshot is deleted, and without one the run is `failed` while a gateway writer runs and `attention` when none does; (5) the calendar boundary (`calendar_writer`, the `CALENDAR_BACKUP_BOUNDARY` case): with `stop`, the calendar writer (the systemd unit `OTSD_SERVICE` or the compose service `otsd`) is stopped and seen stopped, else the member is failed; with `stopped`, no writer running is verified, else failed; with `snapshot`, `OTSD_CALENDAR_SNAPSHOT_DIR` (a directory holding a `journal`) replaces the live directory as the member, else failed; unset, no writer running counts as stopped and a running one fails the member; the boundary used is written to `metadata.txt` as `calendar_backup_boundary`; (6) one `tar` of every present member: `.env`, the units, `PHOENIX_HOME`, the calendar member, `TOR_KEYS_DIR`, `ANCHOR_RECEIPTS_DIR`, `STATE_DIR`, the fork checkout, `ARTIFACTS`, the run directory, and the configured log with its `-wal`/`-shm` when outside `STATE_DIR`; (7) a writer this run stopped is restarted as soon as the `tar` returns (`restart_calendar_writer`), and also from the error trap if anything fails before that; a writer that does not come back is a failed member, named; (8) encryption to `BACKUP_AGE_RECIPIENT`, the push to `BACKUP_REMOTE`, the prune to `BACKUP_KEEP`; (9) `backup-status` written atomically: `ok`, `local_only`, `attention` or `failed` | current behaviour |
| Visibility and durability | the archive is nothing until `tar` returns; the status file is the one thing `/health` reads | current behaviour |
| Acknowledgement | the status file; `ok` and `local_only` are answered only when the calendar member was taken at a boundary | current behaviour |
| Ambiguous outcomes | any unhandled failure is `failed` by the `ERR` trap, which restarts a writer this run stopped first; a member missing from the host is `attention`, naming it; a member declared empty is skipped silently by design; a stop after the `tar` and before the restart leaves the calendar stopped and the status unwritten: the timer's next run, or the operator, starts it (the failed status the trap wrote says so) | current behaviour |
| What the obligations member guarantees | the restored snapshot is a SQLite copy of one instant that holds the newest row the run saw; `ops/BACKUP-RECOVERY.md`'s restore uses it and never the raw trio | required postcondition (met since 044ad43) |
| Intended guarantee | a calendar backup succeeds only at a stopped-writer or snapshot boundary: `otsd` stopped, and seen to be stopped, for the whole copy of its directory, or one filesystem snapshot that covers the set (fork R1, "Stopped copy"); without such a boundary the run refuses success (neither `ok` nor `local_only`) and its status and metadata say why; the script establishes the boundary itself or verifies one it is told of and never infers one (gate ruling 3) | required postcondition (met since 2026-09-18) |
| Was | until 2026-09-18 the script asked only whether a gateway writer ran, put the calendar directory into the one `tar` while `otsd` wrote, and ended `ok` or `local_only` on the strength of the obligations snapshot, the encryption and the push; the fork's contract named it so (section 10, R1, current defect (c)). What a hot copy of the calendar starts as, or refuses as, is the fork's R1 and R2 | current behaviour |
| Operating assumptions | the timer runs as root; the destination keeps the bytes; a filesystem snapshot declared to the script is one (the script checks that the directory exists and holds a journal, not that it was taken atomically); the restore is rehearsed (fork R1, side effect (5)) before the archive is called a backup | operating assumption |
| Tests | `test_ops_backup.py` (10 tests: the configured database (2); a running calendar with no boundary is `failed` and still archived; `stop` stops, sees stopped, copies, restarts, in that order; a writer that does not stop, and one that does not restart, are `failed`; `stopped` with a running writer is `failed` and stops nothing; no writer running satisfies the boundary; the declared snapshot replaces the live directory, and a missing one is `failed`; an unknown value is refused before anything is stopped), `test_review_backup_takes_a_checked_online_snapshot`, `test_review_backup_without_a_usable_snapshot_is_failed_while_writers_run`, `test_review_snapshot_verifier_reports_an_unusable_copy_as_failed` | current behaviour |

## 5. Invariants

### The three payment invariants, stated explicitly

1. **A timeout is not evidence of non-payment.** Where it holds: a wallet
   lookup that fails is a 502 with no verdict (G2), never the 402 that
   means "not settled"; a stamp that fails after a verified payment leaves
   the obligation row and answers 502 (G3); the client's token stays
   valid across every retry. Where it is checked:
   `test_verify_payment_lookup_failure_raises_generic_502_and_logs`,
   `test_obligation_stamp_failure_returns_502_and_persists_needs_stamp`.
2. **Expiry never erases a paid or unresolved obligation.** Where it
   holds: the token's expiry caveat is format-checked and never enforced
   (`_expiry_satisfier`), so a settled invoice redeems after it; the
   obligations table has no expiry and no deletion path; a pause and a
   float stop leave every row and every token as they were. An invoice
   that expires *unpaid* at the wallet has nothing to erase: no
   obligation exists before settlement. Where it is checked:
   `test_settled_but_expired_token_redeems`,
   `test_expired_unsettled_token_still_pays_nothing`,
   `test_paused_blocks_paid_redemption_and_unpause_serves_it`.
3. **A retry reconciles the existing purchase before creating another
   payable attempt.** Where it holds: an authenticated retry never mints
   (the challenge path is the unauthenticated branch only, 1924); a
   presented token is checked against the wallet's record of *its*
   invoice, and a duplicate presentation is idempotent on the row; the
   gateway never asks a client to pay twice for one digest under one
   token. Where it is checked:
   `test_duplicate_payment_hash_single_row_no_new_invoice`,
   `test_same_token_preimage_reuse_returns_cached_proof_not_double_stamp`.
   The client's half of this invariant (asking the wallet what became of
   an earlier payment before paying again) is the payer's contract
   (`auto-anchor/docs/contracts.md`, P1 and P3) and the adapter's
   (`api-endpoint/docs/contracts.md`, A5, unwritten: findings F04, F15,
   F16 are its).

### The seven invariants

| Invariant | Where it holds here | Where it is checked |
|---|---|---|
| Conservation of obligations | a settled payment is owned by the obligations row from its commit until `stamped`; before the commit the client owns it through the token and the wallet's settlement | G3, G4 tests |
| Ambiguity is a state | a wallet that does not answer is a 502, not a verdict; a calendar that does not answer on `/upgrade`, in time or at all, is `calendar_unavailable`, not `pending`; a calendar copied without a boundary is a `failed` backup, not a backup; an unreadable status file is `unknown`, not `ok`; a malformed token is a 401 with one log line naming a class; the backup without a usable snapshot is `failed` | G2, G5, G7, G9 tests |
| Recovery is interruptible | the sweeper's four writes per row are each a short transaction and the row is re-runnable from any of them; a client's re-presentation is idempotent; a restart finds the row and not the cache; the backup's status is written last and atomically, and the writer it stopped is restarted from the error trap too | `test_review_g3_*`, `test_review_g4_*`, `test_review_g8_*`, `test_ops_backup.py` |
| Concurrency preserves decisions | the obligations row is keyed by payment hash with `INSERT OR IGNORE`; `busy_timeout` 30 s on every connection; the probe cache is single-flight; the caches and buckets tolerate a race by under-counting, never by corrupting | `test_health_probe_single_flight_and_cached`, `test_duplicate_payment_hash_single_row_no_new_invoice` |
| Safety includes progress | a failed stamp is retried every tick; an unreachable wallet blocks only redemption, never `/verify`, `/upgrade` or `/health`; one failing row never blocks the next | G4 tests |
| External effects have retry semantics | a challenge's identity is its payment hash; a resubmission to the calendar is deduped inside its horizon and counted twice past it (the fork's stated over-count, at the operator's cost, never the client's); an invoice mint that fails may leave an unpaid orphan at the wallet | G1, G3 |
| Time, capacity, observation | the mint and verify buckets are per peer with FIFO eviction at 10,000; the body cap holds before routing; one `/upgrade` is bounded on the wall clock; `/health` is one reading per field with a stated vocabulary; `process alive` is not `service healthy` | section 17 of `test_main.py`, `test_review_body_cap_*`, `test_review_upgrade_wall_clock_deadline_holds_against_a_trickling_calendar`, G7 |
| Anchored is not irreversible | the gateway holds no Bitcoin view and reports the calendar's `needs_attention` findings verbatim; nothing here re-anchors or repairs a proof | `test_health_otsd_needs_attention_degrades_and_surfaces` |

Counting uncertainty never increases a client's charge (rule 6): the
client pays the flat price once per challenge; nothing in this repository
charges per record or per resubmission; the calendar's over-count from a
resubmission is a cost on the operator's side of the ledger.

## 6. The proof reader: three claims kept apart

The gateway carries no proof parser of its own: `/verify` and `/upgrade`
deserialize with the public library (`opentimestamps` 0.4.5,
`DetachedTimestampFile.deserialize`), which consumes every byte, checks
every attestation payload to its end (`assert_eof` after each payload,
`notary.py:91`) and refuses trailing bytes (`TrailingGarbageError`). Of
the three claims the fork's section 6 names, the gateway makes (1),
*parses*, by the library's rules, and (2), *contains a Bitcoin
attestation*, as `bitcoin_attestation_present`; it never makes (3),
*verifies against Bitcoin*. The stamping path builds the proof from the
calendar's answer with the same library and returns what it serialises.
The review's parser finding F03 is therefore not this repository's: its
two readers are the adapter's (fixed at api-endpoint fd970a9) and the
payer's `check-proof.py` (the payer's contract, section 6).

## 7. Configuration and locking

Configuration reaches the gateway one way: the environment, read once at
import through `_env` (75-86), which treats an empty value as unset
because the compose file hands the container an explicit allowlist
interpolated name by name from `.env` (`docker-compose.yml`, 48-82) and a
name absent there arrives empty. Under systemd `.env` is the unit's
`EnvironmentFile`. `test_review_compose_gateway_environment_is_an_allowlist`
checks that every name `main.py` reads is in the allowlist and the
Bitcoin RPC credential is in the calendar's environment only. Every ops
script reads `.env` through `ops/lib/env.sh` after its defaults, `.env`
winning over an older value in the environment (review F23, fixed
2026-09-18: `ops/status.sh`, `ops/phoenixd-status.sh` and
`ops/otsd-status.sh` resolved theirs before it, or never; `otsd-status.sh`
now takes the calendar directory from `OTSD_CALENDAR_DIR` and the
container from `OTSD_CONTAINER` or compose's own name for the service;
`test_review_phoenixd_status_reads_its_settings_from_the_env_file`,
`test_review_otsd_status_reads_its_settings_from_the_env_file`,
`test_review_status_script_resolves_settings_after_the_env_file`).
Two retired names, `L402_ENABLED` and `OTS_BACKEND_MODE`, stay in the
compose allowlist on purpose: a refused value must reach the process to
be refused.

Exclusion:

| Tool | Used for | Where |
|---|---|---|
| SQLite's own locking, `busy_timeout` 30 s, one fresh connection per operation | the obligations table between the request threads and the sweeper thread | `_obligation_connect` 561-567 |
| a lock around one probe, non-blocking after the first fill | the calendar status read behind `/health` | `_health_probe_lock` 1690-1724 |
| a worker thread joined for the remaining budget, abandoned past it | one calendar lookup inside `/upgrade` (G5) | `_upgrade_pending_against_operator` 1183-1205 |
| a whole-run stop of the calendar writer, restarted from the error trap too | the backup's calendar member with `CALENDAR_BACKUP_BOUNDARY=stop` (G9) | `ops/backup-live-state.sh`, `stop_calendar_writer`, `restart_calendar_writer` |
| no lock | the caches and buckets (a race under-counts, never corrupts); one process per obligations database (operating assumption, G8) | 465-545 |

Files written by the ops scripts are written under a temporary name in
the same directory and renamed (`write_status` in each script).

## 8. Anchor billing: retired

Anchor billing (bills minted from the calendar's receipts and paid by a
standing payer), the free door (`L402_ENABLED=false`) and the
public-calendar relay (`OTS_BACKEND_MODE=public`) were retired on
2026-09-18 under gate rulings 2 to 6 of workflow five. The code, its
configuration and its tests are gone from this tree; an `.env` that still
carries the names boots with one warning, and one that still configures
`L402_ENABLED=false` or `OTS_BACKEND_MODE=public` is refused at startup
with a diagnostic naming the way on (G8). What stays: the calendar's
receipts, its `records` count and its markers (the fork's, section 2), the
`anchor_bills` table in any existing `obligations.db` (operator data,
never dropped by code), and the payer's own records on its box. The
legacy-bill procedure (ruling 4: reconcile by wallet lookup, write off the
definitively unpaid, keep the unresolved, mint nothing) is in the operator guide's
"Retired: anchor billing (2026-09-18)"; the step 0 gate's record is the
operator's, kept outside the repository. `InvoiceStatus.expired` went with
its last consumer (ruling 6). The review's F12 was in the retired code and
was not repaired.

## 9. Assumptions and limits

- No live Lightning payment was made for this document; every payment
  path is pinned against a mocked wallet. The live proofs on record are
  `LIVE_PROOF.md`.
- No power loss is simulated anywhere: SQLite's WAL and the ops scripts'
  rename are what a power cut relies on.
- No Linux run: the suite ran on macOS (step 0 report, baseline).
- The gateway trusts the calendar's 200 to mean what fork C1 says, and
  trusts phoenixd's `isPaid`, `requestedSat` and `description` to mean
  what its sources at v0.8.0 and v0.9.1 say (`InvoiceStatus`,
  1400-1413).
- The gateway holds no Bitcoin view and no spending credential by design:
  the limited phoenixd password (create, look up, `getinfo`), never the
  full one; no Bitcoin RPC at all. The deployment's other boundary, that
  the gateway process cannot read the wallet's files, is the host's:
  since 2026-09-18 the shipped phoenixd unit runs as its own user with
  its home under `/var/lib/phoenixd` (mode 700), the gateway unit as
  `gateway` (`deploy/phoenixd.service.example`,
  `deploy/timestamp-gateway.service.example`;
  `test_review_phoenixd_unit_runs_as_its_own_user_with_a_home_the_gateway_cannot_read`);
  until then both ran as `gateway` and the web process reached the seed
  and the full password by ownership (review F06). Applying it to a box
  deployed before is the operator's migration, in the unit's header.
- `pending` from `/upgrade` has no deadline: a proof whose journal entry
  is gone is reported pending forever (fork R2, current defects).
- An abandoned `/upgrade` lookup worker lives on until its own socket
  timeout, at most `UPGRADE_QUERY_TIMEOUT` of inactivity: bounded, and
  outside the request it belonged to.
- A declared filesystem snapshot (`CALENDAR_BACKUP_BOUNDARY=snapshot`) is
  taken at the operator's word: the script checks that the directory is
  there and holds a journal, not how it was taken.
