"""Test suite for the L402 gateway.

Covers: startup/config validation, digest validation, L402 header parsing, the
402 challenge, token verify/reject, the paid retry path, phoenixd payment
verification, create_invoice wiring, OTS calendar/public modes with bounded
retry and no public fallback, the health endpoint, reuse semantics, error
discipline (generic public details), the obligation log, the /health file
fields, the float backstop, rate limits, and the retirement diagnostics.
"""

import base64
import re
import hashlib
import io
import json
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

# Set env vars before importing main so module-level config validation passes.
# load_dotenv() does not override existing env vars, so these take precedence.
os.environ["PRICE_PER_PROOF_SATS"] = "500"  # the suite-wide deterministic quote
os.environ["PAYMENT_BACKEND_TYPE"] = "phoenixd"  # the only backend; every payment mock is phoenixd's HTTP API
os.environ["PHOENIXD_URL"] = "http://test-phoenixd:9740"
os.environ["PHOENIXD_HTTP_PASSWORD_LIMITED"] = "test-limited-password"
os.environ["OTS_CALENDAR_URL"] = "http://test-calendar:14788"
os.environ["L402_SECRET_HEX"] = "ab" * 32          # stable, known signing key
os.environ["L402_TOKEN_EXPIRY_SECONDS"] = "3600"
os.environ["OTS_SUBMIT_BACKOFF_SECONDS"] = "0"     # keep retry tests fast
os.environ["OBLIGATIONS_DB_PATH"] = ":memory:"     # overridden per-test by fixture below
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"          # whole suite shares one client IP;
                                                   # rate-limit tests patch the global
# Retired pricing names: pin them empty so a developer's real .env (read by
# main's load_dotenv() at import) cannot fire the legacy startup warning
# mid-suite; the legacy-warning tests set them explicitly. The names retired
# on 2026-09-18 are pinned for the same reason.
for _retired in (
    "GATEWAY_PRICE_SATS", "MIN_GATEWAY_PRICE_SATS", "PRICE_BLIND_SATS",
    "PRICE_BUMP_RESERVE", "PRICE_MARGIN", "PRICE_TX_VSIZE_ESTIMATE",
    "PRICE_CONF_TARGET", "PRICE_RPC_URL", "PRICE_MARKUP",
    # Retired 2026-09-18 (workflow five): billing, the free door, the relay.
    "ANCHOR_BILLING_ENABLED", "PER_RECORD_SATS", "ANCHOR_RECEIPTS_PATH",
    "ANCHOR_BILLS_TOKEN", "L402_ENABLED", "OTS_BACKEND_MODE",
):
    os.environ[_retired] = ""

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from pymacaroons import Macaroon  # noqa: E402
from opentimestamps.calendar import DEFAULT_AGGREGATORS  # noqa: E402
from opentimestamps.core.notary import PendingAttestation, UnknownAttestation  # noqa: E402
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

client = TestClient(app=main.app, raise_server_exceptions=False)

DIGEST = "a" * 64            # valid 64-char lowercase hex
OTHER_DIGEST = "b" * 64      # a different valid digest
PREIMAGE = "11" * 32         # 64-char hex preimage
WRONG_PREIMAGE = "22" * 32   # hashes to something other than PAYMENT_HASH
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
FAKE_INVOICE = "lnbc210n1pfakeinvoicefortesting"
FAKE_OTS = b"ots-proof"
TEST_CALENDAR_URL = "http://test-calendar:14788"


# Helpers
def valid_token(digest=DIGEST, price=21, expiry_ts=None):
    """Mint a valid token via the real minting code (exercises main.mint_l402_token)."""
    if expiry_ts is None:
        expiry_ts = int(time.time()) + 3600
    return main.mint_l402_token(digest, PAYMENT_HASH, price, expiry_ts)


def build_macaroon(digest=DIGEST, payment_hash=PAYMENT_HASH, price=21,
                   capability="timestamp", expiry_ts=None, key=None):
    """Craft a macaroon with arbitrary caveats/key for adversarial token tests."""
    if expiry_ts is None:
        expiry_ts = int(time.time()) + 3600
    if key is None:
        key = main.L402_SECRET
    m = Macaroon(location=main.L402_LOCATION, identifier=payment_hash, key=key)
    m.add_first_party_caveat(f"digest={digest}")
    m.add_first_party_caveat(f"payment_hash={payment_hash}")
    if price is not None:  # None: omit the caveat entirely (adversarial case)
        m.add_first_party_caveat(f"price={price}")
    m.add_first_party_caveat(f"capability={capability}")
    m.add_first_party_caveat(f"expiry={expiry_ts}")
    return m.serialize()


def auth(token, preimage=PREIMAGE):
    return {"Authorization": f"L402 {token}:{preimage}"}


def _get_mock(settled, memo, amt_paid_sat, value=None, expired=False):
    """Mock phoenixd GET /payments/incoming/{hash} (verify_payment): isPaid,
    description (the digest memo), receivedSat (credited), requestedSat (the
    face amount; defaults to receivedSat, paid exactly) and isExpired."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {
        "isPaid": settled,
        "description": memo,
        "receivedSat": amt_paid_sat,
        "requestedSat": value if value is not None else amt_paid_sat,
        "isExpired": expired,
    }
    return m


def _settled_get():
    return _get_mock(True, DIGEST, 21)


def _post_mock():
    """Mock phoenixd POST /createinvoice: the serialized bolt11 and the
    payment hash (PAYMENT_HASH, so the minted token matches)."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {
        "serialized": FAKE_INVOICE,
        "paymentHash": PAYMENT_HASH,
    }
    return m


def _good_calendar_ts():
    """A Timestamp with a PendingAttestation so serialization succeeds."""
    ts = Timestamp(bytes.fromhex(DIGEST))
    ts.attestations.add(PendingAttestation("https://test.calendar.example"))
    return ts


def _ok_otsd():
    # The calendar's healthy status line: best_block is set only after
    # otsd's Bitcoin RPC calls succeed (see the /health probe comment).
    return _otsd_status()


def _blind_otsd(body):
    """A 200 whose body is not the JSON status: an old fork's Bitcoin-blind
    shape (its 200 was committed before any Bitcoin call, so the body was
    empty or lacked the page marker), or a foreign page."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.content = body
    return m


def _fail():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("connection refused")
    return m


# 1. Startup / config validation
@pytest.fixture(autouse=True)
def clear_proof_cache():
    main._proof_cache.clear()
    yield
    main._proof_cache.clear()


@pytest.fixture(autouse=True)
def clear_rate_buckets():
    main._rate_buckets.clear()
    main._verify_rate_buckets.clear()
    yield
    main._rate_buckets.clear()
    main._verify_rate_buckets.clear()


@pytest.fixture(autouse=True)
def clear_health_probe_cache():
    """The otsd probe is cached for HEALTH_PROBE_CACHE_SECONDS; every test
    starts with an empty cache so its own otsd mock is what /health sees."""
    if hasattr(main, "_health_probe_cache"):
        main._health_probe_cache["at"] = None
    yield


@pytest.fixture(autouse=True)
def clear_last_mint():
    """Reset the last-mint record to the fresh-boot state (/health payment
    'unknown') so tests that mint don't leak into later /health assertions."""
    main._last_mint = {"result": None, "at": None, "detail": None}
    yield
    main._last_mint = {"result": None, "at": None, "detail": None}


@pytest.fixture(autouse=True)
def wallet_status_file(tmp_path, monkeypatch):
    """Point WALLET_STATUS_PATH at a per-test path (no file by default, so
    /health reports wallet 'absent') — deterministic regardless of the host."""
    path = tmp_path / "wallet-status"
    monkeypatch.setattr(main, "WALLET_STATUS_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def proofs_status_file(tmp_path, monkeypatch):
    """Point PROOFS_STATUS_PATH at a per-test path (no file by default, so
    /health reports proofs 'absent') — deterministic regardless of the host."""
    path = tmp_path / "proofs-status"
    monkeypatch.setattr(main, "PROOFS_STATUS_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def backup_status_file(tmp_path, monkeypatch):
    """Point BACKUP_STATUS_PATH at a per-test path (no file by default, so
    /health reports backup 'absent') — deterministic regardless of the host."""
    path = tmp_path / "backup-status"
    monkeypatch.setattr(main, "BACKUP_STATUS_PATH", str(path))
    return path


@pytest.fixture(autouse=True)
def obligations_db(tmp_path, monkeypatch):
    """Point the obligation store at a fresh, writable per-test SQLite DB and
    initialize its schema. Keeps the durable-log integration in /timestamp from
    touching the production default path during the whole suite."""
    db_path = tmp_path / "obligations.db"
    monkeypatch.setattr(main, "OBLIGATIONS_DB_PATH", str(db_path))
    main.init_obligation_db()
    yield db_path


def _obligation_row(payment_hash=PAYMENT_HASH):
    """Read a single obligation row as a dict, or None if absent."""
    import sqlite3
    conn = sqlite3.connect(main.OBLIGATIONS_DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT payment_hash, digest, status, attempts, last_attempt_at "
            "FROM obligations WHERE payment_hash=?",
            (payment_hash,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _obligation_count():
    import sqlite3
    conn = sqlite3.connect(main.OBLIGATIONS_DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
    finally:
        conn.close()


def test_missing_calendar_url_fails_at_startup():
    with patch.dict(os.environ, {"OTS_CALENDAR_URL": ""}):
        with pytest.raises(RuntimeError, match="OTS_CALENDAR_URL is required"):
            main._parse_config()


def test_price_per_proof_missing_fails_with_teaching_message():
    # Required with no default, same pattern as L402_SECRET_HEX; the error
    # points the operator at the sizing arithmetic instead of guessing one.
    with patch.dict(os.environ, {"PRICE_PER_PROOF_SATS": ""}):
        with pytest.raises(RuntimeError, match=r"PRICE_PER_PROOF_SATS is required.*operator guide"):
            main._parse_config()


def test_price_per_proof_non_integer_fails():
    with patch.dict(os.environ, {"PRICE_PER_PROOF_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_PER_PROOF_SATS must be an integer"):
            main._parse_config()


def test_price_per_proof_negative_fails():
    with patch.dict(os.environ, {"PRICE_PER_PROOF_SATS": "-5"}):
        with pytest.raises(RuntimeError, match="PRICE_PER_PROOF_SATS must be >= 1"):
            main._parse_config()


def test_price_per_proof_zero_fails():
    # A token minted at 0 could never redeem (the price caveat regexes reject
    # "0"), so zero is refused; there is no free door to point at.
    with patch.dict(os.environ, {"PRICE_PER_PROOF_SATS": "0"}):
        with pytest.raises(RuntimeError, match=r"PRICE_PER_PROOF_SATS must be >= 1.*could never redeem"):
            main._parse_config()


# The two retirements of 2026-09-18 that changed what the gateway was: an
# explicitly configured old value is refused with the way on, never
# silently reinterpreted (workflow five, gate rulings 2 and 5). Every other
# retired name is warned about once and ignored, so a leftover .env boots.

def test_free_door_false_is_refused_with_a_migration_diagnostic():
    with patch.dict(os.environ, {"L402_ENABLED": "false"}):
        with pytest.raises(RuntimeError, match=r"L402_ENABLED=false.*retired on 2026-09-18.*never silently"):
            main._parse_config()
    # Case and whitespace do not smuggle a free door past the refusal.
    with patch.dict(os.environ, {"L402_ENABLED": " False "}):
        with pytest.raises(RuntimeError, match="L402_ENABLED=false"):
            main._parse_config()


def test_free_door_true_is_a_retired_name_warned_and_ignored(caplog):
    with patch.dict(os.environ, {"L402_ENABLED": "true"}):
        with caplog.at_level(logging.WARNING):
            cfg = main._parse_config()
    assert cfg.price_per_proof_sats == 500
    warnings = [r.getMessage() for r in caplog.records if "Retired variables present" in r.getMessage()]
    assert len(warnings) == 1 and "L402_ENABLED" in warnings[0]


def test_public_relay_is_refused_with_a_migration_diagnostic():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "public"}):
        with pytest.raises(RuntimeError, match=r"OTS_BACKEND_MODE=public.*retired on 2026-09-18"):
            main._parse_config()
    # Refused before the calendar URL is even looked at.
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "public", "OTS_CALENDAR_URL": ""}):
        with pytest.raises(RuntimeError, match="OTS_BACKEND_MODE=public"):
            main._parse_config()


def test_backend_mode_calendar_is_a_retired_name_warned_and_ignored(caplog):
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "calendar"}):
        with caplog.at_level(logging.WARNING):
            main._parse_config()
    warnings = [r.getMessage() for r in caplog.records if "Retired variables present" in r.getMessage()]
    assert len(warnings) == 1 and "OTS_BACKEND_MODE" in warnings[0]


def test_billing_names_are_warned_once_and_ignored(caplog):
    retired = {
        "ANCHOR_BILLING_ENABLED": "true",
        "PER_RECORD_SATS": "50",
        "ANCHOR_RECEIPTS_PATH": "/tmp/receipts.jsonl",
        "ANCHOR_BILLS_TOKEN": "t",
        "PRICE_MARKUP": "1.2",
    }
    with patch.dict(os.environ, retired):
        with caplog.at_level(logging.WARNING):
            cfg = main._parse_config()  # boots: a leftover billing .env never fails startup
    assert cfg.price_per_proof_sats == 500
    warnings = [r.getMessage() for r in caplog.records if "Retired variables present" in r.getMessage()]
    assert len(warnings) == 1
    for name in retired:
        assert name in warnings[0]


def test_retired_feature_warning_absent_when_env_clean(caplog):
    with caplog.at_level(logging.WARNING):
        main._parse_config()
    assert not any("Retired variables present" in r.getMessage() for r in caplog.records)


def test_l402_secret_required_without_ephemeral_optin():
    with patch.dict(os.environ, {"L402_SECRET_HEX": "", "L402_ALLOW_EPHEMERAL_SECRET": "false"}):
        with pytest.raises(RuntimeError, match="L402_SECRET_HEX is required"):
            main._parse_config()


def test_ephemeral_secret_opt_in_allowed():
    with patch.dict(os.environ, {"L402_SECRET_HEX": "", "L402_ALLOW_EPHEMERAL_SECRET": "true"}):
        cfg = main._parse_config()
    assert cfg is not None


def test_invalid_l402_secret_hex_fails():
    with patch.dict(os.environ, {"L402_SECRET_HEX": "nothex!!"}):
        with pytest.raises(RuntimeError, match="must be a hex string"):
            main._parse_config()


def test_short_l402_secret_hex_fails():
    with patch.dict(os.environ, {"L402_SECRET_HEX": "abcd"}):  # 2 bytes
        with pytest.raises(RuntimeError, match="at least 16 bytes"):
            main._parse_config()


def test_l402_expiry_non_integer_fails():
    with patch.dict(os.environ, {"L402_TOKEN_EXPIRY_SECONDS": "abc"}):
        with pytest.raises(RuntimeError, match="L402_TOKEN_EXPIRY_SECONDS must be an integer"):
            main._parse_config()


def test_l402_expiry_non_positive_fails():
    with patch.dict(os.environ, {"L402_TOKEN_EXPIRY_SECONDS": "0"}):
        with pytest.raises(RuntimeError, match="L402_TOKEN_EXPIRY_SECONDS must be a positive integer"):
            main._parse_config()


def test_ots_max_attempts_non_integer_fails():
    with patch.dict(os.environ, {"OTS_SUBMIT_MAX_ATTEMPTS": "abc"}):
        with pytest.raises(RuntimeError, match="OTS_SUBMIT_MAX_ATTEMPTS must be an integer"):
            main._parse_config()


def test_ots_max_attempts_below_one_fails():
    with patch.dict(os.environ, {"OTS_SUBMIT_MAX_ATTEMPTS": "0"}):
        with pytest.raises(RuntimeError, match="OTS_SUBMIT_MAX_ATTEMPTS must be >= 1"):
            main._parse_config()


def test_ots_backoff_non_numeric_fails():
    with patch.dict(os.environ, {"OTS_SUBMIT_BACKOFF_SECONDS": "abc"}):
        with pytest.raises(RuntimeError, match="OTS_SUBMIT_BACKOFF_SECONDS must be a number"):
            main._parse_config()


def test_ots_backoff_negative_fails():
    with patch.dict(os.environ, {"OTS_SUBMIT_BACKOFF_SECONDS": "-1"}):
        with pytest.raises(RuntimeError, match="OTS_SUBMIT_BACKOFF_SECONDS must be >= 0"):
            main._parse_config()


# 2. Digest validation
def test_invalid_digest_too_short_returns_422():
    resp = client.post("/timestamp", json={"digest": "abc123"})
    assert resp.status_code == 422


def test_invalid_digest_non_hex_returns_422():
    resp = client.post("/timestamp", json={"digest": "g" * 64})
    assert resp.status_code == 422


def test_must_be_hex_normalizes_to_lowercase():
    assert main.TimestampRequest(digest="A" * 64).digest == "a" * 64


def test_must_be_hex_rejects_non_hex():
    with pytest.raises(ValidationError):
        main.TimestampRequest(digest="g" * 64)


def test_digest_normalized_to_lowercase_in_memo():
    with patch("main.requests.post", return_value=_post_mock()) as patched:
        resp = client.post("/timestamp", json={"digest": "A" * 64})
    assert resp.status_code == 402
    assert patched.call_args.kwargs["data"]["description"] == "a" * 64


# 3. L402 header parsing
def test_parse_l402_auth_accepts_valid_header():
    token = valid_token()  # real macaroon contains url-safe base64 chars
    parsed = main.parse_l402_auth(f"L402 {token}:{PREIMAGE}")
    assert parsed is not None
    assert parsed[0] == token
    assert parsed[1] == PREIMAGE.lower()


def test_parse_l402_auth_lowercases_preimage():
    token = valid_token()
    parsed = main.parse_l402_auth(f"L402 {token}:{PREIMAGE.upper()}")
    assert parsed is not None and parsed[1] == PREIMAGE.lower()


def test_parse_l402_auth_rejects_non_l402_schemes():
    assert main.parse_l402_auth(f"preimage={PREIMAGE}") is None
    assert main.parse_l402_auth("Bearer something") is None
    assert main.parse_l402_auth("L402 onlymacaroon-no-colon") is None


@pytest.mark.parametrize("header", [
    "Bearer abc",
    f"preimage={PREIMAGE}",                 # the old custom scheme must no longer work
    "L402 garbage-no-colon",
    "L402 onlymacaroon",
    "token=abc",
    f"L402 abc:{'z' * 64}",                 # preimage not hex
    "L402 abc:short",                       # preimage wrong length
    f"L402 not-a-macaroon:{PREIMAGE}",      # parses, but macaroon won't deserialize
])
def test_malformed_authorization_returns_401(header):
    resp = client.post("/timestamp", json={"digest": DIGEST}, headers={"Authorization": header})
    assert resp.status_code == 401


# 4. 402 challenge path
def test_unauthenticated_post_returns_402():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402


def test_402_www_authenticate_header_exact_format():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    body = resp.json()["detail"]
    # Exact header: L402 macaroon="<token>", invoice="<bolt11>"  (catches format regressions)
    expected = f'L402 macaroon="{body["macaroon"]}", invoice="{body["invoice"]}"'
    assert resp.headers["www-authenticate"] == expected
    assert body["invoice"] == FAKE_INVOICE


def test_402_json_body_has_status_price_invoice_macaroon_expiry():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    body = resp.json()["detail"]
    assert body["status"] == "payment_required"
    assert body["price_sats"] == 500         # the flat PRICE_PER_PROOF_SATS
    assert body["invoice"] == FAKE_INVOICE
    assert isinstance(body["macaroon"], str) and body["macaroon"]
    assert isinstance(body["expiry"], int) and body["expiry"] > int(time.time())


def test_402_creates_invoice_with_digest_memo_and_configured_price():
    with patch("main.requests.post", return_value=_post_mock()) as p:
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402
    sent = p.call_args.kwargs["data"]
    assert sent["description"] == DIGEST
    assert sent["amountSat"] == 500          # configured PRICE_PER_PROOF_SATS


def test_402_quotes_flat_per_proof_rate():
    # The quote is PRICE_PER_PROOF_SATS and nothing else. Body, invoice
    # amount, and macaroon caveat must all carry the same quote.
    assert main.quoted_price_sats() == 500
    with patch("main.PRICE_PER_PROOF_SATS", 7):
        assert main.quoted_price_sats() == 7
        with patch("main.requests.post", return_value=_post_mock()) as p:
            resp = client.post("/timestamp", json={"digest": DIGEST})
        assert resp.status_code == 402
        body = resp.json()["detail"]
        assert body["price_sats"] == 7
        assert p.call_args.kwargs["data"]["amountSat"] == 7
        assert main.verify_l402_token(body["macaroon"], DIGEST) == (PAYMENT_HASH, 7)


def test_quote_path_is_feerate_independent():
    # Provably no RPC on the quote path: the whole requests module is
    # replaced, so ANY network call would be visible — none occurs. And the
    # feerate machinery itself is gone from the module, not merely bypassed.
    sentinel = MagicMock()
    with patch("main.requests", sentinel):
        assert main.quoted_price_sats() == 500
    assert sentinel.post.call_count == 0
    assert sentinel.get.call_count == 0
    for gone in ("_fetch_feerate_sat_per_vb", "_cached_feerate_sat_per_vb",
                 "_feerate_cache", "_btc_per_kvb_to_sat_per_vb"):
        assert not hasattr(main, gone)


def test_challenge_redeems_after_repricing_down():
    # End to end: challenge minted while the flat rate was 11250, the
    # operator then reprices to the suite's 500. The 11250 invoice paid in
    # full must still redeem — mint-time binding end to end through the real
    # challenge.
    with patch("main.PRICE_PER_PROOF_SATS", 11250):
        with patch("main.requests.post", return_value=_post_mock()):
            challenge = client.post("/timestamp", json={"digest": DIGEST})
    token = challenge.json()["detail"]["macaroon"]
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 11250)):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS
    # And the same token paid only 500 (today's flat rate) must not redeem:
    # payment verification runs before the proof cache, so underpayment
    # against the mint-time amount still fails even after a redemption.
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 500)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


def test_minted_token_verifies_and_carries_mint_time_price():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    token = resp.json()["detail"]["macaroon"]
    # Bound to this digest: verifies and returns the payment hash + mint price.
    assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 500)
    # NOT bound to the live configured price: a token minted at N validates at N
    # forever, so repricing never strands an in-flight invoice.
    with patch("main.PRICE_PER_PROOF_SATS", 99):
        assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 500)


# 5. L402 token verification
def test_verify_token_valid_same_digest_returns_payment_hash_and_price():
    assert main.verify_l402_token(valid_token(), DIGEST) == (PAYMENT_HASH, 21)


def test_verify_token_rejects_wrong_digest():
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(valid_token(), OTHER_DIGEST)
    assert ei.value.status_code == 401


def test_expired_token_still_verifies():
    """Settlement outranks expiry: an expired token passes token verification
    (the expiry caveat is format-checked only); whether it redeems is decided
    by the settlement check, not the clock."""
    token = valid_token(expiry_ts=int(time.time()) - 10)
    assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 21)


def test_settled_but_expired_token_redeems():
    """Pay, come back after expiry, still get the proof: a client who already
    paid is owed it."""
    token = valid_token(expiry_ts=int(time.time()) - 10)
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS
    assert _obligation_row() is not None


def test_expired_unsettled_token_still_pays_nothing():
    """Expiry's remaining meaning: an expired UNPAID token earns nothing.
    The settlement gate refuses it (402), and the invoice itself expired at
    the Lightning layer long ago."""
    token = valid_token(expiry_ts=int(time.time()) - 10)
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 0)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


def test_verify_token_accepts_any_genuinely_signed_price():
    # The price caveat is signed by the gateway at mint; verification trusts it
    # rather than the live config. Payment settlement enforces the amount.
    token = build_macaroon(price=99)
    assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 99)


def test_verify_token_rejects_missing_price_caveat():
    token = build_macaroon(price=None)
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_non_numeric_price_caveat():
    token = build_macaroon(price="21sats")
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_zero_price_caveat():
    token = build_macaroon(price=0)
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_wrong_capability():
    token = build_macaroon(capability="admin")
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_wrong_key_tampered():
    token = build_macaroon(key=b"\x99" * 32)  # signed with a foreign key
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_malformed_with_generic_detail():
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token("not-a-macaroon!!", DIGEST)
    assert ei.value.status_code == 401
    assert ei.value.detail == "Invalid L402 token"  # generic, no internal leakage


# Fuzz-mutated macaroon (seed=0xC0FFEE, verify_l402_token, index 21): it
# deserializes, but its caveat bytes are not valid UTF-8, so the caveat read
# raises. Shared by the 401 regression and the log-shape test below.
UNDECODABLE_CAVEAT_TOKEN = (
    "MDAxZmxvY2F0aW9uIHRpbWVzdGFtcC1nYXRld2F5CjAwNTBpZGVudGlmaWVyIDAyZDQ0OWEzMW3iYjI2N2M4ZjM1"
    "MmU5OTY4YTc5ZTNlNWZjOTVjMWJiZWFhNTAyZmQ2NDU0ZWJkZTVhNGJlZGMKMDA1MGNpZCBkaWdlc3Q9YWFhYWFh"
    "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYQowMDU2Y2lk"
    "IHBheW1lbnRfaGFzaA0wMmQ0NDlhMzFmYmIyNjdjOGYzNTJlOTkkOGE3OWUzZTVmYzk1YzFiYmVhYTUwMmZkNjQ1"
    "NGViZGU1YTRiZWRjCjAwMTFjaWQgcHJpY2U9MjEKMDAxZGNpZCBjYXBhYmlsaXG5PXRpbWVzdGFtcAowMDFhY2lk"
    "IGV4cGlyeT00MTAyNDQ0ODAwCjAwMmZzaWduYXR1cmUg6Y2_anRuD-MzN-VZh4DCrQdyKLCEH2mKjr7kJB6eo0IK"
)


def test_verify_token_rejects_undecodable_caveat_bytes_with_401():
    """Fuzz regression (seed=0xC0FFEE, verify_l402_token, index 21): a mutated
    macaroon that deserializes but whose caveat bytes are not valid UTF-8 used
    to escape as UnicodeDecodeError from pymacaroons' caveat_id access. It must
    be the same generic 401 as any other invalid token."""
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(UNDECODABLE_CAVEAT_TOKEN, DIGEST)
    assert ei.value.status_code == 401
    assert ei.value.detail == "Invalid L402 token"


def test_garbage_token_logs_single_warning_no_traceback(caplog):
    """Both unauthenticated failure stages (deserialize, caveat read) fire on
    arbitrary stranger input, so neither may write ERROR-level records or
    stack traces into the journal — one WARNING line with the exception repr
    is the whole log surface."""
    for stage, token in [
        ("deserialize", "not-a-macaroon!!"),
        ("caveat read", UNDECODABLE_CAVEAT_TOKEN),
    ]:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as ei:
                main.verify_l402_token(token, DIGEST)
        assert ei.value.status_code == 401, stage
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], stage
        assert "Traceback" not in caplog.text, stage
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1, stage


# 6. Paid retry path
def test_valid_paid_retry_returns_raw_ots_bytes_buffered_response():
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    # Fully buffered body (a Response, not a StreamingResponse) with attachment filename.
    assert resp.content == FAKE_OTS
    assert f"attachment; filename={DIGEST}.ots" in resp.headers["content-disposition"]


def test_invalid_token_returns_401():
    bad = build_macaroon(key=b"\x77" * 32)
    resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(bad))
    assert resp.status_code == 401


def test_valid_token_unsettled_invoice_returns_402():
    token = valid_token()
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 0)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


def test_wrong_preimage_rejected_before_backend_lookup():
    token = valid_token()
    mock_get = MagicMock()
    with patch("main.requests.get", mock_get):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"L402 {token}:{WRONG_PREIMAGE}"},
        )
    assert resp.status_code == 401
    mock_get.assert_not_called()  # preimage check happens before any phoenixd call


def test_same_token_preimage_same_digest_reuse_allowed():
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            r1 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
            r2 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert r1.status_code == 200 and r2.status_code == 200


def test_same_token_preimage_different_digest_rejected():
    token = valid_token(digest=DIGEST)
    resp = client.post("/timestamp", json={"digest": OTHER_DIGEST}, headers=auth(token))
    assert resp.status_code == 401


# 7. phoenixd invoice lookup / payment verification
def test_verify_payment_true_when_settled_correct_memo_and_amount():
    with patch("main.requests.get", return_value=_settled_get()):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is True


def test_verify_payment_false_when_unsettled():
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is False


def test_verify_payment_false_when_wrong_memo():
    with patch("main.requests.get", return_value=_get_mock(True, "0" * 64, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is False


def test_verify_payment_false_when_underpaid():
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 5)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is False


def test_verify_payment_enforces_mint_time_price_not_static_config():
    # Face amount 21 against a token minted at 30: insufficient even though
    # the configured price would say less is enough.
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 30) is False
    # Face amount 900 against a token minted at 900: settles even if the
    # configured price has since moved — the binding is to the mint-time
    # amount alone.
    with patch("main.PRICE_PER_PROOF_SATS", 21):
        with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 900)):
            assert main.verify_payment(PAYMENT_HASH, DIGEST, 900) is True


def test_liquidity_fee_netted_receive_still_verifies(caplog):
    # phoenixd nets an ACINQ liquidity fee off the credited
    # amount. bolt11 settlement is atomic — the customer paid the face amount
    # in full; the fee is our cost. Face 21 >= mint price 21 → verified, and
    # the node's first liquidity-fee event documents itself as a WARNING.
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "isPaid": True,
        "requestedSat": 21,
        "receivedSat": 18,
        "description": DIGEST,
    }
    with patch("main.PAYMENT_BACKEND", main.PhoenixdPaymentBackend()):
        with patch("main.requests.get", return_value=resp):
            with caplog.at_level(logging.WARNING):
                assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is True
    assert any("Liquidity fee observed" in r.message for r in caplog.records)


def test_malformed_response_missing_requested_sat_fails_closed():
    # No requestedSat in the response: parses to 0, and 0 >= 21 rejects even
    # though isPaid claims settled.
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"isPaid": True, "receivedSat": 21, "description": DIGEST}
    with patch("main.PAYMENT_BACKEND", main.PhoenixdPaymentBackend()):
        with patch("main.requests.get", return_value=resp):
            assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is False


def test_overpaid_underminted_invoice_rejected():
    # Uniform-rule pin: face amount 10 < price 21 rejects even though the
    # payer overpaid to 30. An invoice minted below its bound price is a
    # gateway mint bug to surface, not a payment to honor.
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 30, value=10)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 21) is False


def test_endpoint_settled_wrong_memo_returns_402():
    token = valid_token()
    with patch("main.requests.get", return_value=_get_mock(True, "0" * 64, 21)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


def test_endpoint_settled_underpaid_returns_402():
    token = valid_token()
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 5)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


def test_endpoint_honors_in_flight_invoice_after_repricing():
    # Challenge quoted 900; by redemption the flat rate has moved on. The
    # invoice paid in full at its mint-time amount must still redeem.
    token = valid_token(price=900)
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 900)):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS


def test_liquidity_fee_payment_yields_obligation_and_proof(obligations_db):
    # End to end: settled with the credited amount netted below the mint
    # price still redeems — 200, real proof bytes, obligation recorded.
    token = valid_token()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "isPaid": True,
        "requestedSat": 21,
        "receivedSat": 18,
        "description": DIGEST,
    }
    with patch("main.PAYMENT_BACKEND", main.PhoenixdPaymentBackend()):
        with patch("main.requests.get", return_value=resp):
            with patch("main.stamp_digest", return_value=FAKE_OTS):
                r = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert r.status_code == 200
    assert r.content == FAKE_OTS
    assert _obligation_row() is not None


def test_verify_payment_lookup_failure_raises_generic_502_and_logs(caplog):
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("boom-internal-detail")
    with patch("main.requests.get", return_value=m):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as ei:
                main.verify_payment(PAYMENT_HASH, DIGEST, 21)
    assert ei.value.status_code == 502
    assert ei.value.detail == "Payment backend error: could not verify payment"
    assert "boom-internal-detail" not in ei.value.detail
    assert any("phoenixd invoice lookup failed" in r.message for r in caplog.records)


# 8. create_invoice() wiring
def test_create_invoice_returns_tuple_and_lowercases_payment_hash():
    ph = "AB" * 32
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"serialized": "lnbc...", "paymentHash": ph}

    def fake_post(url, data=None, auth=None, timeout=None):
        captured.update(url=url, data=data, auth=auth, timeout=timeout)
        return FakeResponse()

    with patch("main.requests.post", fake_post):
        payment_request, payment_hash = main.create_invoice(DIGEST, 21)

    assert payment_request == "lnbc..."
    assert payment_hash == ph.lower() and len(payment_hash) == 64
    assert captured["url"] == f"{main.PHOENIXD_URL}/createinvoice"
    assert captured["auth"] == ("", main.PHOENIXD_HTTP_PASSWORD_LIMITED)
    assert captured["data"]["description"] == DIGEST
    assert captured["data"]["amountSat"] == 21
    assert captured["data"]["externalId"].startswith(DIGEST[:16] + "-")


def test_create_invoice_failure_raises_generic_502_and_logs(caplog):
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("creation-internal-detail")
    with patch("main.requests.post", return_value=m):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as ei:
                main.create_invoice(DIGEST, 21)
    assert ei.value.status_code == 502
    assert ei.value.detail == "Payment backend error: could not create invoice"
    assert "creation-internal-detail" not in ei.value.detail
    assert any("phoenixd invoice creation failed" in r.message for r in caplog.records)


# 9. OTS backend modes
def test_calendar_mode_submits_only_to_calendar_url():
    instance = MagicMock()
    instance.submit.return_value = _good_calendar_ts()
    with patch("main.RemoteCalendar", return_value=instance) as MockCalendar:
        main.stamp_digest(DIGEST)
    assert MockCalendar.call_count == 1
    assert MockCalendar.call_args[0][0] == TEST_CALENDAR_URL


def test_calendar_mode_retries_when_otsd_initially_unavailable():
    instance = MagicMock()
    instance.submit.side_effect = [ConnectionError("otsd not ready"), _good_calendar_ts()]
    with patch("main.RemoteCalendar", return_value=instance) as MockCalendar:
        with patch("main.time.sleep"):
            result = main.stamp_digest(DIGEST)
    assert isinstance(result, bytes) and len(result) > 0
    assert instance.submit.call_count == 2
    assert MockCalendar.call_count == 2


def test_paid_retry_succeeds_after_initial_otsd_failure():
    token = valid_token()
    instance = MagicMock()
    instance.submit.side_effect = [ConnectionError("starting up"), _good_calendar_ts()]
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=instance):
            with patch("main.time.sleep"):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_calendar_mode_exhausts_retries_then_returns_generic_502():
    token = valid_token()
    instance = MagicMock()
    instance.submit.side_effect = ConnectionError("calendar-unreachable-detail")
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=instance) as MockCalendar:
            with patch("main.time.sleep"):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OTS error: stamping failed"
    assert "calendar-unreachable-detail" not in resp.json()["detail"]
    assert MockCalendar.call_count == main.OTS_SUBMIT_MAX_ATTEMPTS


def test_calendar_mode_never_falls_back_to_public_calendars():
    token = valid_token()
    instance = MagicMock()
    instance.submit.side_effect = ConnectionError("calendar unreachable")
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=instance) as MockCalendar:
            with patch("main.time.sleep"):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 502
    for call in MockCalendar.call_args_list:
        called_url = call[0][0]
        assert called_url == TEST_CALENDAR_URL
        for agg in DEFAULT_AGGREGATORS:
            assert called_url != agg, f"fell back to public aggregator {agg}"


def test_calendar_mode_success_returns_ots_bytes():
    token = valid_token()
    instance = MagicMock()
    instance.submit.return_value = _good_calendar_ts()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=instance):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert len(resp.content) > 0


# 10. Health endpoint
def test_health_fresh_boot_payment_unknown_returns_200():
    # Passive payment field: before any real mint there is nothing to vouch
    # for, so a fresh gateway reports "unknown" without degrading (same
    # contract as wallet "absent" / float "inactive").
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "paused": False,
        "payment": "unknown",
        "payment_backend": main.PAYMENT_BACKEND_TYPE,
        "last_mint_at": None,
        "otsd": "ok",
        "wallet": "absent",
        "float": "inactive",
        "proofs": "absent",
        "backup": "absent",
    }


def test_health_otsd_down_in_calendar_mode_returns_503():
    with patch("main.requests.get", side_effect=[_fail()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["otsd"] == "error"


def test_health_otsd_bitcoin_blind_empty_200_returns_503(caplog):
    # An old fork committed its 200 before any Bitcoin call, so a dead
    # Bitcoin RPC yielded an empty 200 body. That must read as error, not
    # ok — and the log line must name the condition.
    with patch("main.requests.get", side_effect=[_blind_otsd(b"")]):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["otsd"] == "error"
    assert any("Bitcoin-blind" in r.message for r in caplog.records)


def test_health_otsd_markerless_200_returns_503():
    # A page that is neither the JSON status nor the retired page with its
    # marker (template drift, partial render) fails loud rather than
    # passing as healthy.
    with patch("main.requests.get",
               side_effect=[_blind_otsd(b"<html>calendar</html>")]):
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["otsd"] == "error"


def test_health_never_raises():
    with patch("main.requests.get", side_effect=RuntimeError("unexpected crash")):
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["otsd"] == "error"


def test_health_otsd_probe_timeout_pairs_with_fork_homepage():
    """The otsd probe's read leg waits for the status to be built in full
    (three Bitcoin RPCs, each allowed a 30s stall by the fork's
    make_proxy(timeout=30)), so a single-stall read can take ~34s. A plain
    timeout=5 marked ~11% of the old page's renders red (2026-07-17). Pin
    (5, 45) so neither side of the pair moves alone."""
    with patch("main.requests.get", side_effect=[_ok_otsd()]) as mock_get:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert mock_get.call_args_list[0].kwargs["timeout"] == (5, 45)


def test_health_payment_reflects_last_real_mint():
    """The payment field is the outcome of the last REAL mint, never a
    reachability probe: fresh boot reads "unknown" (200, reported without
    degrading); a wedged createinvoice (ReadTimeout while getinfo still
    answers) reads "degraded" (503); a successful mint reads
    "ok" (200). last_mint_at exposes the freshness of that verdict."""
    # Fresh state: the autouse fixture reset the record — no mint yet.
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["payment"] == "unknown" and body["last_mint_at"] is None

    # Wedged mint: the L402 challenge path attempts a real createinvoice.
    with patch("main.requests.post",
               side_effect=requests.exceptions.ReadTimeout("read timed out")):
        challenge = client.post("/timestamp", json={"digest": DIGEST})
    assert challenge.status_code == 502
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["payment"] == "degraded" and body["last_mint_at"] is not None

    # Recovered: the next successful mint flips it back to ok.
    with patch("main.requests.post", return_value=_post_mock()):
        challenge = client.post("/timestamp", json={"digest": DIGEST})
    assert challenge.status_code == 402
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["payment"] == "ok" and body["last_mint_at"] is not None


# 11. Error discipline (cross-cutting)
def test_create_error_detail_is_generic_no_leak():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("secret-backend-trace")
    with patch("main.requests.post", return_value=m):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "Payment backend error: could not create invoice"
    assert "secret-backend-trace" not in resp.text


def test_malformed_auth_uses_401():
    resp = client.post("/timestamp", json={"digest": DIGEST}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401


def test_unsettled_uses_402_not_error():
    token = valid_token()
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 0)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


# /verify endpoint
def make_detached_ots_bytes(digest=DIGEST, attestation=None):
    timestamp = main.Timestamp(bytes.fromhex(digest))
    if attestation is None:
        attestation = main.PendingAttestation("http://127.0.0.1:14788")
    timestamp.attestations.add(attestation)
    detached = main.DetachedTimestampFile(main.OpSHA256(), timestamp)
    buf = io.BytesIO()
    detached.serialize(main.StreamSerializationContext(buf))
    return buf.getvalue()


def test_verify_pending_proof_returns_pending_status():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 200
    body = resp.json()
    assert body["digest"] == DIGEST
    assert body["proof_digest"] == DIGEST
    assert body["status"] == "pending"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["attestations"] == [
        {"type": "pending_calendar", "calendar_url": "http://127.0.0.1:14788"}
    ]


def test_verify_mismatched_digest_returns_mismatch_status():
    ots_b64 = base64.b64encode(make_detached_ots_bytes(digest=OTHER_DIGEST)).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "mismatch"
    assert body["valid_ots"] is True
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["proof_digest"] == OTHER_DIGEST


def test_verify_bitcoin_attestation_returns_attestation_present_not_verified():
    ots_b64 = base64.b64encode(
        make_detached_ots_bytes(attestation=main.BitcoinBlockHeaderAttestation(954112))
    ).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "bitcoin_attestation_present"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_attestation_present"] is True
    assert body["bitcoin_anchored"] is True   # the same structural flag, pre-2026-09-15 name
    assert body["verified"] is None           # not checked against Bitcoin here
    assert body["verification"] == "structural"
    assert body["attestations"] == [
        {
            "type": "bitcoin",
            "block_height": 954112,
        }
    ]


def test_verify_invalid_base64_returns_invalid_status():
    resp = client.post("/verify", json={"digest": DIGEST, "ots": "not base64!!!"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["valid_ots"] is False
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False


def test_verify_invalid_ots_bytes_returns_invalid_status():
    ots_b64 = base64.b64encode(b"not an ots proof").decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["valid_ots"] is False
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False


def test_verify_no_recognized_attestations_returns_no_attestations():
    """Case split from "invalid": a well-formed proof whose attestations are
    all unrecognized (an empty timestamp can't serialize) is "no_attestations",
    not "invalid" — the bytes decoded fine and the digest matches."""
    ots = make_detached_ots_bytes(attestation=UnknownAttestation(b"\x99" * 8, b"payload"))
    resp = client.post("/verify", json={"digest": DIGEST, "ots": base64.b64encode(ots).decode()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_attestations"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False


def test_verify_rejects_invalid_digest_with_422():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    resp = client.post("/verify", json={"digest": "g" * 64, "ots": ots_b64})

    assert resp.status_code == 422


def test_verify_rejects_oversized_ots_with_413():
    ots_b64 = base64.b64encode(b"x" * (main.MAX_VERIFY_OTS_BYTES + 1)).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 413


def test_upgrade_invalid_base64_returns_invalid():
    resp = client.post("/upgrade", json={"digest": DIGEST, "ots": "not base64!!!"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["valid_ots"] is False
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] is None


def test_upgrade_invalid_ots_bytes_returns_invalid():
    ots_b64 = base64.b64encode(b"not an ots proof").decode()
    resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "invalid"
    assert body["valid_ots"] is False
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] is None


def test_upgrade_no_recognized_attestations_returns_no_attestations():
    """Same split on the upgrade path: nothing to upgrade, but the proof is
    well-formed — "no_attestations", with the original proof echoed back."""
    ots = make_detached_ots_bytes(attestation=UnknownAttestation(b"\x99" * 8, b"payload"))
    ots_b64 = base64.b64encode(ots).decode()
    resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_attestations"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] == ots_b64  # original proof returned unchanged


def test_upgrade_oversized_ots_returns_413():
    ots_b64 = base64.b64encode(b"x" * (main.MAX_VERIFY_OTS_BYTES + 1)).decode()
    resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 413


def test_upgrade_mismatched_digest_no_calendar_contact():
    ots_b64 = base64.b64encode(make_detached_ots_bytes(digest=OTHER_DIGEST)).decode()
    with patch("main.RemoteCalendar") as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "mismatch"
    assert body["valid_ots"] is True
    assert body["digest_match"] is False
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] == ots_b64
    mock_calendar.assert_not_called()


def test_upgrade_already_attested_no_calendar_contact():
    ots_b64 = base64.b64encode(
        make_detached_ots_bytes(
            attestation=main.BitcoinBlockHeaderAttestation(954112)
        )
    ).decode()
    with patch("main.RemoteCalendar") as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "bitcoin_attestation_present"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_attestation_present"] is True
    assert body["bitcoin_anchored"] is True   # the same structural flag, pre-2026-09-15 name
    assert body["verified"] is None           # not checked against Bitcoin here
    assert body["verification"] == "structural"
    assert body["ots"] == ots_b64
    mock_calendar.assert_not_called()


def test_upgrade_pending_no_calendar_upgrade_returns_pending():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    instance = MagicMock()
    instance.get_timestamp.side_effect = main.CommitmentNotFoundError("commitment not found")
    with patch("main.RemoteCalendar", return_value=instance) as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] == ots_b64
    mock_calendar.assert_called_once_with(main.OTS_CALENDAR_URL)
    assert instance.get_timestamp.called


def test_upgrade_pending_calendar_returns_attestation_present():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    upgraded_ts = main.Timestamp(bytes.fromhex(DIGEST))
    upgraded_ts.attestations.add(main.BitcoinBlockHeaderAttestation(954112))
    instance = MagicMock()
    instance.get_timestamp.return_value = upgraded_ts
    with patch("main.RemoteCalendar", return_value=instance) as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "bitcoin_attestation_present"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_attestation_present"] is True
    assert body["bitcoin_anchored"] is True   # the same structural flag, pre-2026-09-15 name
    assert body["verified"] is None           # not checked against Bitcoin here
    assert body["verification"] == "structural"
    assert body["ots"] is not None
    assert body["ots"] != ots_b64
    assert any(a["type"] == "bitcoin" for a in body["attestations"])
    mock_calendar.assert_called_once_with(main.OTS_CALENDAR_URL)
    assert instance.get_timestamp.called


def test_upgrade_override_allowlist_contacts_only_operator_calendar():
    ots_b64 = base64.b64encode(
        make_detached_ots_bytes(
            attestation=main.PendingAttestation("http://evil.example")
        )
    ).decode()
    instance = MagicMock()
    instance.get_timestamp.side_effect = main.CommitmentNotFoundError("commitment not found")
    with patch("main.RemoteCalendar", return_value=instance) as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is False
    assert body["verified"] is False
    assert body["ots"] == ots_b64
    mock_calendar.assert_called_once_with(main.OTS_CALENDAR_URL)
    assert mock_calendar.call_args.args[0] == main.OTS_CALENDAR_URL
    assert mock_calendar.call_args.args[0] != "http://evil.example"
    assert instance.get_timestamp.called


# Payment backend selection

def test_payment_backend_default_is_phoenixd():
    """With PAYMENT_BACKEND_TYPE unset (or empty: the compose allowlist hands
    an unset variable over as the empty string), the backend is phoenixd."""
    env = {
        "PRICE_PER_PROOF_SATS": "500",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
    }
    with patch.dict(os.environ, env, clear=True):
        assert main._parse_config().payment_backend_type == "phoenixd"
    with patch.dict(os.environ, dict(env, PAYMENT_BACKEND_TYPE=""), clear=True):
        assert main._parse_config().payment_backend_type == "phoenixd"


def test_payment_backend_is_phoenixd():
    assert main.PAYMENT_BACKEND_TYPE == "phoenixd"
    assert isinstance(main.PAYMENT_BACKEND, main.PhoenixdPaymentBackend)


def test_payment_backend_lnd_is_refused_with_a_teaching_message():
    """The LND backend was removed on 2026-09-15 (ruling: not carried unless
    someone actually needs it). An .env still selecting it fails at startup
    naming the removal and the lines to drop, never silently falling back."""
    env = {
        "PAYMENT_BACKEND_TYPE": "lnd",
        "PRICE_PER_PROOF_SATS": "500",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
        "LND_HOST": "x", "LND_PORT": "1", "LND_MACAROON_HEX": "aa",
    }
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match=r"LND backend was removed on 2026-09-15"):
            main._parse_config()
    with pytest.raises(RuntimeError):
        main._make_payment_backend("lnd")
    assert not hasattr(main, "LndPaymentBackend")
    assert not hasattr(main, "LND_HOST") and not hasattr(main, "TOR_PROXY")


def test_make_payment_backend_phoenixd():
    backend = main._make_payment_backend("phoenixd")
    assert isinstance(backend, main.PhoenixdPaymentBackend)


def test_make_payment_backend_invalid():
    with pytest.raises(RuntimeError):
        main._make_payment_backend("invalid")


def test_same_token_preimage_reuse_returns_cached_proof_not_double_stamp(caplog):
    """A replayed valid token returns the cached proof without re-submitting to otsd."""
    token = valid_token()
    submit_calls = 0

    def counting_submit(digest_bytes, timeout=10):
        nonlocal submit_calls
        submit_calls += 1
        return MagicMock(ops={}, attestations=[])

    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", side_effect=lambda d: b"fake-ots-proof") as mock_stamp:
            resp1 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
            with caplog.at_level(logging.INFO):
                resp2 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert mock_stamp.call_count == 1  # stamp_digest only called once despite two requests

    # The routine cached-proof line carries only the 8-hex prefix; the full
    # payment hash never enters routine (INFO) logs.
    cached = [r.message for r in caplog.records if "Returning cached proof" in r.message]
    assert cached and PAYMENT_HASH[:8] + "…" in cached[0]
    assert PAYMENT_HASH not in cached[0]


def test_phoenixd_backend_config_parses():
    """PAYMENT_BACKEND_TYPE=phoenixd with its two variables parses."""
    env = {
        "PAYMENT_BACKEND_TYPE": "phoenixd",
        "PRICE_PER_PROOF_SATS": "500",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
        "PHOENIXD_HTTP_PASSWORD_LIMITED": "testpassword",
    }
    with patch.dict(os.environ, env, clear=True):
        result = main._parse_config()
    assert result is not None


def test_phoenixd_password_pre_rename_alias():
    """The pre-rename PHOENIXD_HTTP_PASSWORD still fills the limited field."""
    env = {
        "PAYMENT_BACKEND_TYPE": "phoenixd",
        "PRICE_PER_PROOF_SATS": "500",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
        "PHOENIXD_HTTP_PASSWORD": "legacyname",
    }
    with patch.dict(os.environ, env, clear=True):
        result = main._parse_config()
    assert result is not None
    assert result.phoenixd_http_password_limited == "legacyname"


def test_parse_config_rejects_invalid_payment_backend():
    with patch.dict(os.environ, {"PAYMENT_BACKEND_TYPE": "invalid"}):
        with pytest.raises(RuntimeError):
            main._parse_config()


def test_phoenixd_create_invoice_uses_form_data():
    backend = main.PhoenixdPaymentBackend()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "serialized": FAKE_INVOICE,
        "paymentHash": PAYMENT_HASH.upper(),
    }
    with patch("main.requests.post", return_value=resp) as post:
        invoice = backend.create_invoice(DIGEST, 21)
    assert invoice.bolt11 == FAKE_INVOICE
    assert invoice.payment_hash == PAYMENT_HASH
    assert post.call_args.args[0].endswith("/createinvoice")
    kwargs = post.call_args.kwargs
    assert "data" in kwargs
    assert "json" not in kwargs
    assert kwargs["data"]["amountSat"] == 21
    assert kwargs["data"]["description"] == DIGEST
    assert kwargs["data"]["externalId"].startswith(DIGEST[:16])


def test_phoenixd_lookup_invoice_maps_neutral_status_only():
    backend = main.PhoenixdPaymentBackend()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "isPaid": True,
        "requestedSat": 21,
        "receivedSat": 21,
        "description": DIGEST,
        "isExpired": False,
        "preimage": "ff" * 32,
        "invoice": FAKE_INVOICE,
    }
    with patch("main.requests.get", return_value=resp) as get:
        status = backend.lookup_invoice(PAYMENT_HASH)
    assert status.settled is True
    assert status.amount_requested_sat == 21
    assert status.amount_received_sat == 21
    assert status.memo == DIGEST
    assert not hasattr(status, "expired")  # isExpired is no longer read (retired with billing, 2026-09-18)
    assert not hasattr(status, "preimage")
    assert not hasattr(status, "invoice")
    assert not hasattr(status, "amount_paid_sat")  # the conflated field is gone
    assert get.call_args.args[0].endswith("/payments/incoming/" + PAYMENT_HASH)


def test_phoenixd_password_not_logged(caplog):
    backend = main.PhoenixdPaymentBackend()
    secret = "phoenix-secret-for-test"
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "serialized": FAKE_INVOICE,
        "paymentHash": PAYMENT_HASH,
    }
    with patch("main.PHOENIXD_HTTP_PASSWORD_LIMITED", secret):
        assert backend._auth() == ("", secret)
        with caplog.at_level(logging.DEBUG):
            with patch("main.requests.post", return_value=resp):
                backend.create_invoice(DIGEST, 21)
    assert secret not in caplog.text


def test_paused_health_returns_503(tmp_path):
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")

    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("requests.get", return_value=_ok_otsd()):
            resp = client.get("/health")

    assert resp.status_code == 503
    assert resp.json()["status"] == "paused"
    assert resp.json()["paused"] is True


def test_paused_timestamp_returns_503(tmp_path):
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")

    with patch("main.PAUSE_FILE", str(pause_file)):
        resp = client.post("/timestamp", json={"digest": DIGEST})

    assert resp.status_code == 503
    assert resp.json()["detail"] == "Gateway is paused by operator"


def test_paused_blocks_paid_redemption_and_unpause_serves_it(tmp_path):
    """PAUSED refuses even a settled token, and the pause takes nothing: the
    same token redeems after unpause."""
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")
    token = valid_token()

    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("main.requests.get", return_value=_settled_get()):
            paused_resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert paused_resp.status_code == 503

    pause_file.unlink()
    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("main.requests.get", return_value=_settled_get()):
            with patch("main.stamp_digest", return_value=FAKE_OTS):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS


def test_paused_blocks_verify_and_upgrade(tmp_path):
    """PAUSED means the gateway answers /health and nothing else — the gate
    sits before routing, so even malformed bodies get the pause answer."""
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")

    with patch("main.PAUSE_FILE", str(pause_file)):
        v = client.post("/verify", json={})
        u = client.post("/upgrade", json={})
    assert v.status_code == 503
    assert u.status_code == 503


def test_sweeper_skips_while_paused(tmp_path):
    """Full-stop: no stamping while PAUSED; rows wait. Control: the same tick
    sweeps once the file is gone."""
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")

    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("main._sweep_obligations_once") as sweep:
            main._sweeper_tick()
    sweep.assert_not_called()

    pause_file.unlink()
    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("main._sweep_obligations_once") as sweep:
            main._sweeper_tick()
    sweep.assert_called_once()


# 12. Durable obligation log
# The obligation store guarantees a settled payment is never lost if calendar
# submission fails. It coexists with _proof_cache: the cache is the instant
# re-serve path, the DB is the durable backstop the sweeper drains. Payment only
# admits a request; the DB is never consulted to validate a proof.

def test_obligation_success_marks_stamped():
    """A fully paid + stamped request leaves the obligation row 'stamped'."""
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    row = _obligation_row()
    assert row is not None
    assert row["status"] == "stamped"
    assert row["digest"] == DIGEST


def test_obligation_stamp_failure_returns_502_and_persists_needs_stamp():
    """Payment settled but stamping fails -> 502, and the obligation is durably
    recorded as 'needs_stamp' so the sweeper can recover it later."""
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", side_effect=RuntimeError("otsd down")):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 502
    row = _obligation_row()
    assert row is not None
    assert row["status"] == "needs_stamp"
    assert row["digest"] == DIGEST


def test_sweeper_completes_pending_obligation_and_bumps_attempts(caplog):
    """The sweeper stamps a needs_stamp row, marks it stamped, bumps attempts,
    and populates _proof_cache (mirroring the endpoint success path)."""
    main.record_obligation(PAYMENT_HASH, DIGEST)
    assert _obligation_row()["status"] == "needs_stamp"

    with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
        with caplog.at_level(logging.INFO):
            main._sweep_obligations_once()

    stamp.assert_called_once_with(DIGEST)
    row = _obligation_row()
    assert row["status"] == "stamped"
    assert row["attempts"] == 1
    assert row["last_attempt_at"] is not None
    assert main._proof_cache[PAYMENT_HASH] == FAKE_OTS

    # Routine recovery line: 8-hex prefix only, never the full payment hash
    # (WARNING-level incident lines keep the full hash for forensics).
    recovered = [r.message for r in caplog.records if "Sweeper: recovered" in r.message]
    assert recovered and PAYMENT_HASH[:8] + "…" in recovered[0]
    assert PAYMENT_HASH not in recovered[0]


def test_sweeper_records_attempt_on_repeated_failure():
    """A still-failing stamp leaves the row needs_stamp but records the attempt,
    so a paid obligation is retried indefinitely, never dropped."""
    main.record_obligation(PAYMENT_HASH, DIGEST)

    with patch("main.stamp_digest", side_effect=RuntimeError("still down")):
        main._sweep_obligations_once()

    row = _obligation_row()
    assert row["status"] == "needs_stamp"
    assert row["attempts"] == 1
    assert row["last_attempt_at"] is not None
    assert PAYMENT_HASH not in main._proof_cache


def test_token_representation_stays_instant_via_proof_cache():
    """Re-presenting a redeemed token serves from _proof_cache without re-stamping
    or re-recording — the instant path is preserved alongside the durable log."""
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
            r1 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
            r2 = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == FAKE_OTS and r2.content == FAKE_OTS
    stamp.assert_called_once()  # second request hit the cache, did not re-stamp
    assert _obligation_count() == 1


def test_obligations_needs_stamp_partial_index_migration_safe():
    """The sweeper's needs_stamp scan is indexed — a partial index that stays
    near-empty in steady state — and an EXISTING database gains it on init
    (CREATE INDEX IF NOT EXISTS): drop it to simulate a pre-index DB, re-run
    init, and the sweeper's exact query plans through it again."""
    import sqlite3
    conn = sqlite3.connect(main.OBLIGATIONS_DB_PATH)
    try:
        conn.execute("DROP INDEX IF EXISTS obligations_needs_stamp")
        conn.commit()
    finally:
        conn.close()
    main.init_obligation_db()  # the migration moment: table exists, index absent
    conn = sqlite3.connect(main.OBLIGATIONS_DB_PATH)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='obligations'")]
        assert "obligations_needs_stamp" in names
        plan = " ".join(r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT payment_hash, digest FROM obligations "
            "WHERE status='needs_stamp'"))
        assert "obligations_needs_stamp" in plan, plan
    finally:
        conn.close()


def test_proof_cache_bounded_fifo(monkeypatch):
    """_proof_cache never exceeds _PROOF_CACHE_MAX: the oldest insertion is
    evicted first (FIFO), and overwriting an existing key neither grows the
    cache nor evicts."""
    monkeypatch.setattr(main, "_PROOF_CACHE_MAX", 3)
    for i in range(3):
        main._proof_cache_put(f"hash{i}", b"proof")
    assert len(main._proof_cache) == 3

    main._proof_cache_put("hash0", b"proof0")  # overwrite: no growth, no eviction
    assert len(main._proof_cache) == 3
    assert main._proof_cache["hash0"] == b"proof0"

    main._proof_cache_put("hash3", b"proof")  # one past the bound
    assert len(main._proof_cache) == 3
    assert "hash0" not in main._proof_cache  # oldest insertion evicted
    assert set(main._proof_cache) == {"hash1", "hash2", "hash3"}

    main._proof_cache_put("hash4", b"proof")
    assert set(main._proof_cache) == {"hash2", "hash3", "hash4"}


def test_duplicate_payment_hash_single_row_no_new_invoice():
    """Two paid calls with the same token yield exactly one obligation row and
    mint no new invoice (INSERT OR IGNORE is idempotent on payment_hash)."""
    token = valid_token()
    with patch("main.requests.post") as post:  # invoice creation must never be called
        with patch("main.requests.get", return_value=_settled_get()):
            with patch("main.stamp_digest", return_value=FAKE_OTS):
                client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
                client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert _obligation_count() == 1
    post.assert_not_called()


def test_unwritable_obligation_db_fails_loud(tmp_path):
    """An unwritable DB path makes init fail loud, so the process refuses to start
    rather than run without a durable obligation log."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n")
    bad_path = blocker / "obligations.db"  # parent is a regular file -> mkdir fails
    with patch("main.OBLIGATIONS_DB_PATH", str(bad_path)):
        with pytest.raises(RuntimeError, match="Cannot initialize obligations DB"):
            main.init_obligation_db()


# 13. Wallet liquidity alarm (/health wallet field)
# /health reads the status file written by ops/wallet-balance-check.sh — no
# Bitcoin RPC from the gateway. "absent" (alarm not installed) does not degrade;
# "low"/"stale"/"unknown" degrade to 503 like a backend failure.

def _write_wallet_status(path, status="ok", checked_at=None, balance_sats=100000):
    if checked_at is None:
        checked_at = int(time.time())
    path.write_text(json.dumps({
        "balance_sats": balance_sats,
        "min_sats": 50000,
        "status": status,
        "checked_at": checked_at,
    }))


def test_health_wallet_ok_returns_200(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="ok")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["wallet"] == "ok"


def test_health_wallet_low_returns_503(wallet_status_file):
    # 30,000: low per the script's WALLET_MIN_SATS (50,000) and a float alarm,
    # but above the 20,000 stop — this test pins the wallet field, not the
    # backstop (section 16 covers the stop).
    _write_wallet_status(wallet_status_file, status="low", balance_sats=30000)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "low"


def test_health_wallet_stale_returns_503(wallet_status_file):
    """An 'ok' file older than WALLET_STATUS_MAX_AGE_SECONDS means the timer
    itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.WALLET_STATUS_MAX_AGE_SECONDS - 60
    _write_wallet_status(wallet_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "stale"


def test_health_wallet_absent_reports_but_stays_healthy(wallet_status_file):
    """No status file = alarm not installed. Reported, but operators without
    the calendar profile must not fail health."""
    assert not wallet_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["wallet"] == "absent"


def test_health_wallet_malformed_returns_503_unknown(wallet_status_file):
    wallet_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "unknown"


# 14. Proof sweep status (/health proofs field)
# /health reads the status file written by ops/upgrade-all-proofs.sh — no
# artifact scanning from the gateway. "absent" (sweep not installed) does not
# degrade; "mismatch"/"attention"/"stale"/"unknown" degrade to 503.

def _write_proofs_status(path, status="ok", checked_at=None):
    if checked_at is None:
        checked_at = int(time.time())
    path.write_text(json.dumps({
        "total": 4,
        "bitcoin_backed": 4,
        "waiting_for_bitcoin": 0,
        "attestation_mismatch": 0,
        "needs_attention": 0,
        "status": status,
        "checked_at": checked_at,
    }))


def test_health_proofs_ok_returns_200(proofs_status_file):
    _write_proofs_status(proofs_status_file, status="ok")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["proofs"] == "ok"


def test_health_proofs_mismatch_returns_503(proofs_status_file):
    """attestation_mismatch is the signal the sweeper repair exists for — the
    calendar holds an attestation an artifact lacks. It must degrade health."""
    _write_proofs_status(proofs_status_file, status="mismatch")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "mismatch"


def test_health_proofs_attention_returns_503(proofs_status_file):
    _write_proofs_status(proofs_status_file, status="attention")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "attention"


def test_health_proofs_stale_returns_503(proofs_status_file):
    """An 'ok' file older than PROOFS_STATUS_MAX_AGE_SECONDS means the sweep
    timer itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.PROOFS_STATUS_MAX_AGE_SECONDS - 60
    _write_proofs_status(proofs_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "stale"


def test_health_proofs_absent_reports_but_stays_healthy(proofs_status_file):
    """No status file = sweep not installed. Reported, but operators without
    the calendar profile must not fail health."""
    assert not proofs_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["proofs"] == "absent"


def test_health_proofs_malformed_returns_503_unknown(proofs_status_file):
    proofs_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "unknown"


def _write_backup_status(path, status="ok", checked_at=None,
                         archive="20260711T000000Z-live-state.tar.gz.age"):
    if checked_at is None:
        checked_at = int(time.time())
    path.write_text(json.dumps({
        "status": status,
        "archive": archive,
        "checked_at": checked_at,
    }))


def test_health_backup_ok_returns_200(backup_status_file):
    _write_backup_status(backup_status_file, status="ok")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["backup"] == "ok"


def test_health_backup_local_only_reports_but_stays_healthy(backup_status_file):
    """local_only is a configuration choice (no BACKUP_REMOTE/recipient set)
    and must not degrade health."""
    _write_backup_status(backup_status_file, status="local_only")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["backup"] == "local_only"


def test_health_backup_attention_returns_503(backup_status_file):
    _write_backup_status(backup_status_file, status="attention")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "attention"


def test_health_backup_failed_returns_503(backup_status_file):
    _write_backup_status(backup_status_file, status="failed")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "failed"


def test_health_backup_stale_returns_503(backup_status_file):
    """An 'ok' file older than BACKUP_STATUS_MAX_AGE_SECONDS means the backup
    timer itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.BACKUP_STATUS_MAX_AGE_SECONDS - 60
    _write_backup_status(backup_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "stale"


def test_health_backup_absent_reports_but_stays_healthy(backup_status_file):
    """No status file = backups not installed. Reported, but boxes without the
    backup timer must not fail health."""
    assert not backup_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["backup"] == "absent"


def test_health_backup_malformed_returns_503_unknown(backup_status_file):
    backup_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "unknown"


# 15. Pricing (flat per-proof rate)
# The quote is PRICE_PER_PROOF_SATS, full stop. The retired floor/feerate
# model warns once at startup and is ignored; STAMPER_FEE_CAP_SATS remains
# only to derive the float backstop thresholds (section 16).


def test_legacy_pricing_vars_warn_once_and_are_ignored(caplog):
    retired = {
        "GATEWAY_PRICE_SATS": "21",
        "MIN_GATEWAY_PRICE_SATS": "1",
        "PRICE_BLIND_SATS": "5000",
        "PRICE_BUMP_RESERVE": "1.5",
        "PRICE_MARGIN": "5",
        "PRICE_TX_VSIZE_ESTIMATE": "150",
        "PRICE_CONF_TARGET": "6",
        "PRICE_RPC_URL": "http://feeuser:feepass@127.0.0.1:18332/",
    }
    with patch.dict(os.environ, retired):
        with caplog.at_level(logging.WARNING):
            cfg = main._parse_config()  # boots — legacy vars never fail startup
    assert cfg.price_per_proof_sats == 500
    warnings = [r.getMessage() for r in caplog.records
                if "Retired pricing variables" in r.getMessage()]
    assert len(warnings) == 1
    for name in retired:
        assert name in warnings[0]


def test_legacy_warning_absent_when_env_clean(caplog):
    with caplog.at_level(logging.WARNING):
        main._parse_config()
    assert not any("Retired pricing variables" in r.getMessage() for r in caplog.records)


def test_price_rpc_url_never_warns_on_bitcoin_rpc_service_url(caplog):
    # BITCOIN_RPC_SERVICE_URL remains legitimately set for otsd; only an
    # explicitly present PRICE_RPC_URL is named.
    env = {"BITCOIN_RPC_SERVICE_URL": "http://u:p@127.0.0.1:8332/wallet/otsd-hot"}
    with patch.dict(os.environ, env):
        with caplog.at_level(logging.WARNING):
            main._parse_config()
    assert not any("PRICE_RPC_URL" in r.getMessage() for r in caplog.records)


def test_stamper_fee_cap_default_is_20000():
    with patch.dict(os.environ):
        os.environ.pop("STAMPER_FEE_CAP_SATS", None)
        assert main._parse_config().stamper_fee_cap_sats == 20000


def test_non_integer_stamper_fee_cap_fails():
    with patch.dict(os.environ, {"STAMPER_FEE_CAP_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="STAMPER_FEE_CAP_SATS must be an integer"):
            main._parse_config()


def test_non_positive_stamper_fee_cap_fails():
    for bad in ("0", "-1"):
        with patch.dict(os.environ, {"STAMPER_FEE_CAP_SATS": bad}):
            with pytest.raises(RuntimeError, match="STAMPER_FEE_CAP_SATS must be a positive"):
                main._parse_config()


# 16. Float backstop
# The anchor wallet's balance is read from the SAME wallet-status file as
# the liquidity alarm — no RPC, no credential. Below
# 5 × STAMPER_FEE_CAP_SATS: alarm, sales continue, /health degrades. Below
# 1 × cap: automatic full stop with PAUSED semantics as the gateway's OWN
# state, clearing itself on recovery. No balance reading: inactive,
# reported, never degrading.


def test_float_ok_at_or_above_five_caps(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="ok", balance_sats=100000)  # exactly 5 × cap
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["float"] == "ok"


def test_float_alarm_below_five_caps_degrades_sales_continue(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="ok", balance_sats=60000)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["float"] == "alarm"
    # Sales continue: the unauthenticated mint path still answers 402.
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402


def test_float_stop_below_one_cap_full_stops(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="low", balance_sats=10000)
    # /health still answers: overall auto_paused — the gateway's own state,
    # not the operator's (paused stays false).
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "auto_paused" and body["float"] == "stop"
    assert body["paused"] is False
    # Everything else is 503 with the auto-pause reason, not the operator's.
    resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 503
    assert "auto-paused" in resp.json()["detail"]
    assert "operator" not in resp.json()["detail"]
    resp = client.post("/verify", json={"digest": DIGEST, "ots": "aGk="})
    assert resp.status_code == 503


def test_float_stop_own_state_distinct_from_operator_pause(tmp_path, wallet_status_file):
    # Both hold: the operator label wins everywhere; the gateway never
    # touches the operator's file, and lifting the manual pause leaves the
    # float stop standing.
    _write_wallet_status(wallet_status_file, status="low", balance_sats=10000)
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("manual\n")
    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch("main.requests.get", side_effect=[_ok_otsd()]):
            resp = client.get("/health")
        body = resp.json()
        assert body["status"] == "paused" and body["float"] == "stop"
        resp = client.post("/timestamp", json={"digest": DIGEST})
        assert "operator" in resp.json()["detail"]
        assert pause_file.exists()
        pause_file.unlink()
        resp = client.post("/timestamp", json={"digest": DIGEST})
        assert resp.status_code == 503
        assert "auto-paused" in resp.json()["detail"]


def test_float_stop_sweeper_skips(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="low", balance_sats=10000)
    with patch("main._sweep_obligations_once") as sweep:
        main._sweeper_tick()
    sweep.assert_not_called()


def test_float_recovery_clears_itself(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="low", balance_sats=10000)
    resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 503
    # The timer writes a recovered balance: no operator action, no restart.
    _write_wallet_status(wallet_status_file, status="ok", balance_sats=150000)
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402
    with patch("main._sweep_obligations_once") as sweep:
        main._sweeper_tick()
    sweep.assert_called_once()


def test_float_stop_blocks_paid_redemption_and_recovery_serves_it(wallet_status_file, obligations_db):
    # Settlement outranks expiry across an auto-pause: a paid token blocked
    # by the float stop redeems after recovery.
    with patch("main.requests.post", return_value=_post_mock()):
        challenge = client.post("/timestamp", json={"digest": DIGEST})
    token = challenge.json()["detail"]["macaroon"]
    _write_wallet_status(wallet_status_file, status="low", balance_sats=5000)
    resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 503
    _write_wallet_status(wallet_status_file, status="ok", balance_sats=150000)
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 500)):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS


def test_float_stale_last_reading_stands(wallet_status_file):
    # Freshness is not consulted: a stale low reading keeps the stop, a
    # stale healthy reading keeps sales; staleness itself alarms through the
    # wallet field.
    old = int(time.time()) - 7200
    _write_wallet_status(wallet_status_file, status="low", balance_sats=10000, checked_at=old)
    resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 503
    _write_wallet_status(wallet_status_file, status="ok", balance_sats=150000, checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    body = resp.json()
    assert body["float"] == "ok" and body["wallet"] == "stale"
    assert body["status"] == "degraded"
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402


def test_float_absent_inactive_backstop_off(wallet_status_file):
    # No file: the backstop is off and says so; nothing degrades, sales run.
    assert not wallet_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["float"] == "inactive" and body["wallet"] == "absent"
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402


def test_float_null_balance_reads_inactive(wallet_status_file):
    # The balance-check script writes balance null when its RPC fails — no
    # reading, no stop; the wallet field alarms that state separately.
    wallet_status_file.write_text(json.dumps({
        "balance_sats": None, "min_sats": 50000, "status": "unknown",
        "checked_at": int(time.time()),
    }))
    assert main._float_state() == "inactive"


# 17. Invoice-mint rate limit
def test_rate_limit_non_integer_fails():
    with patch.dict(os.environ, {"RATE_LIMIT_PER_MINUTE": "many"}):
        with pytest.raises(RuntimeError, match="RATE_LIMIT_PER_MINUTE must be an integer"):
            main._parse_config()


def test_rate_limit_negative_fails():
    with patch.dict(os.environ, {"RATE_LIMIT_PER_MINUTE": "-1"}):
        with pytest.raises(RuntimeError, match="RATE_LIMIT_PER_MINUTE must be >= 0"):
            main._parse_config()


def test_rate_limit_zero_valid_and_default_ten():
    with patch.dict(os.environ, {"RATE_LIMIT_PER_MINUTE": "0"}):
        assert main._parse_config().rate_limit_per_minute == 0
    with patch.dict(os.environ):
        del os.environ["RATE_LIMIT_PER_MINUTE"]
        assert main._parse_config().rate_limit_per_minute == 10


def test_behind_proxy_invalid_value_fails():
    with patch.dict(os.environ, {"GATEWAY_BEHIND_PROXY": "yes"}):
        with pytest.raises(RuntimeError,
                           match="GATEWAY_BEHIND_PROXY must be 'true' or 'false'"):
            main._parse_config()


def test_behind_proxy_defaults_false_and_parses_true():
    with patch.dict(os.environ):
        os.environ.pop("GATEWAY_BEHIND_PROXY", None)
        assert main._parse_config().gateway_behind_proxy is False
    with patch.dict(os.environ, {"GATEWAY_BEHIND_PROXY": "TRUE"}):
        assert main._parse_config().gateway_behind_proxy is True


def test_rate_limit_allows_burst_then_denies(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 3)
    assert [main._rate_limit_retry_after("ip-a") for _ in range(3)] == [None] * 3
    retry = main._rate_limit_retry_after("ip-a")
    assert isinstance(retry, int) and retry >= 1


def test_rate_limit_refills_over_time(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 2)
    # Empty bucket last refilled 30s ago: 30 x 2/60 = 1 token has accrued.
    main._rate_buckets["ip-b"] = (0.0, time.monotonic() - 30)
    assert main._rate_limit_retry_after("ip-b") is None      # spends the accrued token
    assert main._rate_limit_retry_after("ip-b") is not None  # bucket empty again


def test_rate_limit_denied_request_consumes_nothing(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    main._rate_buckets["ip-c"] = (0.5, time.monotonic())
    assert main._rate_limit_retry_after("ip-c") is not None
    tokens, _ = main._rate_buckets["ip-c"]
    assert tokens >= 0.5  # refill only adds; the denial spent nothing


def test_rate_limit_retry_after_reflects_refill_rate(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 60)  # one token per second
    main._rate_buckets["ip-d"] = (0.0, time.monotonic())
    assert main._rate_limit_retry_after("ip-d") == 1


def test_rate_buckets_bounded_fifo(monkeypatch):
    """_rate_buckets never exceeds _RATE_BUCKETS_MAX: the earliest-seen IP is
    evicted to admit a new one — the same bounded-module-dict property as
    _proof_cache, so a spammer rotating source addresses cannot grow memory."""
    monkeypatch.setattr(main, "_RATE_BUCKETS_MAX", 3)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 10)
    for i in range(5):
        main._rate_limit_retry_after(f"ip-{i}")
        assert len(main._rate_buckets) <= 3
    assert set(main._rate_buckets) == {"ip-2", "ip-3", "ip-4"}
    main._rate_limit_retry_after("ip-3")  # existing key: no growth, no eviction
    assert set(main._rate_buckets) == {"ip-2", "ip-3", "ip-4"}


def test_mint_rate_limited_returns_429_and_spares_phoenixd(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    with patch("main.requests.post", return_value=_post_mock()) as p:
        first = client.post("/timestamp", json={"digest": DIGEST})
        second = client.post("/timestamp", json={"digest": DIGEST})
    assert first.status_code == 402
    assert second.status_code == 429
    detail = second.json()["detail"]
    assert detail["status"] == "rate_limited"
    assert isinstance(detail["retry_after_seconds"], int)
    assert detail["retry_after_seconds"] >= 1
    assert second.headers["retry-after"] == str(detail["retry_after_seconds"])
    p.assert_called_once()  # the 429 never reached invoice creation


def test_verify_rate_limit_non_integer_fails():
    with patch.dict(os.environ, {"VERIFY_RATE_LIMIT_PER_MINUTE": "many"}):
        with pytest.raises(RuntimeError, match="VERIFY_RATE_LIMIT_PER_MINUTE must be an integer"):
            main._parse_config()


def test_verify_rate_limit_negative_fails():
    with patch.dict(os.environ, {"VERIFY_RATE_LIMIT_PER_MINUTE": "-1"}):
        with pytest.raises(RuntimeError, match="VERIFY_RATE_LIMIT_PER_MINUTE must be >= 0"):
            main._parse_config()


def test_verify_rate_limit_zero_valid_and_default_thirty():
    with patch.dict(os.environ, {"VERIFY_RATE_LIMIT_PER_MINUTE": "0"}):
        assert main._parse_config().verify_rate_limit_per_minute == 0
    with patch.dict(os.environ):
        os.environ.pop("VERIFY_RATE_LIMIT_PER_MINUTE", None)
        assert main._parse_config().verify_rate_limit_per_minute == 30


def test_verify_and_upgrade_rate_limited_from_one_bucket(monkeypatch):
    # /verify and /upgrade draw one shared budget, and the 429 fires BEFORE
    # the base64 decode — the third request carries invalid base64 and still
    # gets 429, not the 200-invalid body, proving the parse was never paid.
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 2)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    r1 = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})
    r2 = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    r3 = client.post("/verify", json={"digest": DIGEST, "ots": "!!!not-base64!!!"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r3.status_code == 429
    detail = r3.json()["detail"]
    assert detail["status"] == "rate_limited"
    assert isinstance(detail["retry_after_seconds"], int)
    assert detail["retry_after_seconds"] >= 1
    assert r3.headers["retry-after"] == str(detail["retry_after_seconds"])


def test_verify_rate_limit_refills_over_time(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 2)
    # Empty bucket last refilled 30s ago: 30 x 2/60 = 1 token has accrued.
    main._verify_rate_buckets["ip-v"] = (0.0, time.monotonic() - 30)
    assert main._verify_rate_limit_retry_after("ip-v") is None      # spends the accrued token
    assert main._verify_rate_limit_retry_after("ip-v") is not None  # bucket empty again


def test_verify_rate_limit_zero_disables(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 0)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    for _ in range(5):
        assert client.post("/verify", json={"digest": DIGEST, "ots": ots_b64}).status_code == 200


def test_verify_rate_buckets_bounded_fifo(monkeypatch):
    """The second bucket dict holds the same bounded-module-dict property as
    _rate_buckets — enforced by the shared _bucket_retry_after core, pinned
    here so it cannot silently die in one of the two call sites."""
    monkeypatch.setattr(main, "_RATE_BUCKETS_MAX", 3)
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 10)
    for i in range(5):
        main._verify_rate_limit_retry_after(f"vip-{i}")
        assert len(main._verify_rate_buckets) <= 3
    assert set(main._verify_rate_buckets) == {"vip-2", "vip-3", "vip-4"}
    main._verify_rate_limit_retry_after("vip-3")  # existing key: no growth, no eviction
    assert set(main._verify_rate_buckets) == {"vip-2", "vip-3", "vip-4"}


def test_verify_spoofed_forwarded_for_ignored_by_default(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    r1 = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64},
                     headers={"X-Forwarded-For": "1.1.1.1"})
    r2 = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64},
                     headers={"X-Forwarded-For": "2.2.2.2"})
    assert r1.status_code == 200
    assert r2.status_code == 429  # same direct peer, same bucket


def test_verify_forwarded_for_rightmost_used_when_behind_proxy(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "GATEWAY_BEHIND_PROXY", True)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    r1 = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64},
                     headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
    r2 = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64},
                     headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
    r3 = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64},
                     headers={"X-Forwarded-For": "1.1.1.1, 8.8.8.8"})
    assert r1.status_code == 200
    assert r2.status_code == 429  # spoofed leftmost, same real client 9.9.9.9
    assert r3.status_code == 200  # genuinely different client


def test_verify_and_mint_buckets_independent(monkeypatch):
    # The independence property — the point of the separate knob: draining
    # the verify bucket leaves minting untouched, and vice versa.
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    assert client.post("/verify", json={"digest": DIGEST, "ots": ots_b64}).status_code == 200
    assert client.post("/verify", json={"digest": DIGEST, "ots": ots_b64}).status_code == 429
    with patch("main.requests.post", return_value=_post_mock()):
        # Verify bucket empty; the mint bucket still has its full burst.
        assert client.post("/timestamp", json={"digest": DIGEST}).status_code == 402
        # Now the mint bucket is empty too — because of ITS traffic alone.
        assert client.post("/timestamp", json={"digest": DIGEST}).status_code == 429
    # And mint traffic never touched the verify dict's bucket count:
    assert list(main._verify_rate_buckets) == ["testclient"]


def test_paid_redemption_ungated_even_with_empty_verify_bucket(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    ots_b64 = base64.b64encode(FAKE_OTS).decode()
    assert client.post("/verify", json={"digest": DIGEST, "ots": ots_b64}).status_code == 200
    assert client.post("/verify", json={"digest": DIGEST, "ots": ots_b64}).status_code == 429
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200 and resp.content == FAKE_OTS


def test_rate_limit_does_not_gate_paid_redemption(monkeypatch):
    """An exhausted mint bucket must not block redemption: the paid path's cost
    is bounded by payment, and a client who already paid is owed the proof."""
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    with patch("main.requests.post", return_value=_post_mock()):
        assert client.post("/timestamp", json={"digest": DIGEST}).status_code == 402
        assert client.post("/timestamp", json={"digest": DIGEST}).status_code == 429
    token = valid_token()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200 and resp.content == FAKE_OTS


def test_spoofed_forwarded_for_ignored_by_default(monkeypatch):
    """Without GATEWAY_BEHIND_PROXY, X-Forwarded-For must not segregate buckets —
    otherwise any spammer escapes the limit by rotating the header."""
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    with patch("main.requests.post", return_value=_post_mock()):
        r1 = client.post("/timestamp", json={"digest": DIGEST},
                         headers={"X-Forwarded-For": "1.1.1.1"})
        r2 = client.post("/timestamp", json={"digest": DIGEST},
                         headers={"X-Forwarded-For": "2.2.2.2"})
    assert r1.status_code == 402
    assert r2.status_code == 429  # same direct peer, same bucket


def test_forwarded_for_rightmost_used_when_behind_proxy(monkeypatch):
    """Behind a declared proxy the bucket key is the RIGHTMOST X-Forwarded-For
    entry (the one the operator's proxy appended); leftmost entries stay
    client-supplied and must not segregate buckets."""
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "GATEWAY_BEHIND_PROXY", True)
    with patch("main.requests.post", return_value=_post_mock()):
        r1 = client.post("/timestamp", json={"digest": DIGEST},
                         headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
        r2 = client.post("/timestamp", json={"digest": DIGEST},
                         headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
        r3 = client.post("/timestamp", json={"digest": DIGEST},
                         headers={"X-Forwarded-For": "1.1.1.1, 8.8.8.8"})
    assert r1.status_code == 402
    assert r2.status_code == 429  # spoofed leftmost, same real client 9.9.9.9
    assert r3.status_code == 402  # genuinely different client


def test_behind_proxy_without_header_falls_back_to_peer(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "GATEWAY_BEHIND_PROXY", True)
    with patch("main.requests.post", return_value=_post_mock()):
        r1 = client.post("/timestamp", json={"digest": DIGEST})
        r2 = client.post("/timestamp", json={"digest": DIGEST})
    assert r1.status_code == 402 and r2.status_code == 429


def test_rate_limit_zero_disables(monkeypatch):
    monkeypatch.setattr(main, "RATE_LIMIT_PER_MINUTE", 0)
    with patch("main.requests.post", return_value=_post_mock()):
        codes = [client.post("/timestamp", json={"digest": DIGEST}).status_code
                 for _ in range(15)]
    assert codes == [402] * 15
    assert main._rate_buckets == {}  # disabled limiter keeps no state


# 17c. /health probe cache and rate limit (D1 landing 2026-09-08)
# Every unauthenticated GET /health used to render the calendar's homepage
# (four bitcoind RPCs) and hold a worker for up to 50 s; the review starved
# the stamping door with 45 concurrent calls. The otsd probe is now cached
# for HEALTH_PROBE_CACHE_SECONDS behind a single-flight lock, and /health
# draws from the per-peer verify bucket like /verify.

import threading as _threading


def test_health_probe_cache_knob():
    with patch.dict(os.environ):
        os.environ.pop("HEALTH_PROBE_CACHE_SECONDS", None)
        assert main._parse_config().health_probe_cache_seconds == 15
    with patch.dict(os.environ, {"HEALTH_PROBE_CACHE_SECONDS": "0"}):
        assert main._parse_config().health_probe_cache_seconds == 0
    for bad in ("-1", "soon"):
        with patch.dict(os.environ, {"HEALTH_PROBE_CACHE_SECONDS": bad}):
            with pytest.raises(RuntimeError, match="HEALTH_PROBE_CACHE_SECONDS"):
                main._parse_config()


def test_health_probe_single_flight_and_cached():
    calls = []
    def slow_get(*a, **kw):
        calls.append(1)
        time.sleep(0.3)
        return _ok_otsd()
    results = []
    def hit():
        results.append(client.get("/health").status_code)
    with patch("main.requests.get", side_effect=slow_get):
        threads = [_threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        # eight concurrent callers, one homepage render
        assert results == [200] * 8
        assert len(calls) == 1
        # and a later caller inside the TTL still costs nothing
        assert client.get("/health").status_code == 200
        assert len(calls) == 1
        # past the TTL the probe runs again
        main._health_probe_cache["at"] -= main.HEALTH_PROBE_CACHE_SECONDS + 1
        assert client.get("/health").status_code == 200
        assert len(calls) == 2


def test_health_probe_expired_result_is_served_stale_while_one_refresh_runs():
    """Past the TTL a flood must not pile up on the lock: one caller renders,
    the others get the previous answer at once (only a cold start waits)."""
    calls = []
    gate = _threading.Event()
    def slow_get(*a, **kw):
        calls.append(1)
        gate.wait(5)
        return _ok_otsd()
    with patch("main.requests.get", side_effect=slow_get):
        gate.set()
        assert client.get("/health").status_code == 200     # warm the cache
        assert len(calls) == 1
        gate.clear()
        main._health_probe_cache["at"] -= main.HEALTH_PROBE_CACHE_SECONDS + 1
        timings = []
        def hit():
            t0 = time.monotonic()
            code = client.get("/health").status_code
            timings.append((code, time.monotonic() - t0))
        threads = [_threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        # seven answered from the stale result while the eighth still renders
        fast = [t for t in timings if t[1] < 0.4]
        assert len(fast) == 7 and all(code == 200 for code, _ in fast), timings
        assert len(calls) == 2
        gate.set()
        for t in threads:
            t.join(10)
        assert len(timings) == 8 and len(calls) == 2


def test_health_probe_cache_zero_probes_every_call():
    with patch("main.HEALTH_PROBE_CACHE_SECONDS", 0):
        with patch("main.requests.get", return_value=_ok_otsd()) as g:
            client.get("/health")
            client.get("/health")
    assert g.call_count == 2


def test_health_rate_limited_per_peer_like_verify(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    with patch("main.requests.get", return_value=_ok_otsd()):
        first = client.get("/health")
        second = client.get("/health")
    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


# 17a. Request body cap (D2 landing 2026-09-08)
# The largest legitimate body is a /verify or /upgrade proof of 256 KiB in
# base64 inside a small JSON object. Anything bigger is refused with 413 by
# an ASGI middleware BEFORE the body is read — the review measured a 120 MB
# body inflating the process by ~600 MB before the old 413 — and a chunked
# POST (no Content-Length) is refused with 411 on the same routes.

def _run_asgi(scope, body=b""):
    """Drive main.app once and return (status, body, receive_calls)."""
    import asyncio
    calls = []
    async def receive():
        calls.append(1)
        return {"type": "http.request", "body": body, "more_body": False}
    sent = []
    async def send(msg):
        sent.append(msg)
    asyncio.run(main.app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, out, len(calls)


def _scope(path, headers, method="POST"):
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
            "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "client": ("testclient", 1234), "server": ("testserver", 80)}


def test_body_cap_refuses_before_reading():
    big = str(main.MAX_REQUEST_BYTES + 1)
    for path in ("/verify", "/upgrade", "/timestamp"):
        status, out, receives = _run_asgi(_scope(path, [("content-type", "application/json"),
                                                        ("content-length", big)]))
        assert status == 413, path
        assert receives == 0, "the body was read before the 413"
        assert b"too large" in out


def test_body_cap_refuses_chunked_posts():
    status, out, receives = _run_asgi(_scope("/verify", [("content-type", "application/json"),
                                                         ("transfer-encoding", "chunked")]))
    assert status == 411 and receives == 0


def test_body_cap_passes_normal_requests_and_gets():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200 and resp.json()["status"] == "pending"
    # A body just under the cap still reaches the endpoint (and its own 256 KiB rule).
    resp = client.post("/verify", json={"digest": DIGEST, "ots": "A" * (main.MAX_REQUEST_BYTES - 200)})
    assert resp.status_code in (200, 413) and resp.status_code != 411
    with patch("main.requests.get", side_effect=[_ok_otsd()]):
        assert client.get("/health").status_code == 200


def test_body_cap_value():
    assert main.MAX_REQUEST_BYTES == 512 * 1024


# 17b. Upgrade client token (D4 landing 2026-09-08)
# A client that presents UPGRADE_CLIENT_TOKEN as a bearer on /upgrade is not
# drawn from the per-peer verify bucket: a client can hold thousands
# of pending proofs an hour and the client must be able to finish them.
# The token never exempts /verify, a wrong token is simply anonymous, and an
# unset token makes the header inert.

UPGRADE_TOKEN = "upgrade-client-token-for-tests"


def _upgrade_body():
    return {"digest": DIGEST, "ots": base64.b64encode(make_detached_ots_bytes()).decode()}


def test_upgrade_client_token_optional_in_config():
    with patch.dict(os.environ):
        os.environ.pop("UPGRADE_CLIENT_TOKEN", None)
        assert main._parse_config().upgrade_client_token is None
    with patch.dict(os.environ, {"UPGRADE_CLIENT_TOKEN": "t0k"}):
        assert main._parse_config().upgrade_client_token == "t0k"


def test_upgrade_client_token_exempts_bucket(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "UPGRADE_CLIENT_TOKEN", UPGRADE_TOKEN)
    hdr = {"Authorization": f"Bearer {UPGRADE_TOKEN}"}
    instance = MagicMock()
    instance.get_timestamp.side_effect = main.CommitmentNotFoundError("not yet")
    with patch("main.RemoteCalendar", return_value=instance):
        codes = [client.post("/upgrade", json=_upgrade_body(), headers=hdr).status_code
                 for _ in range(5)]
        assert codes == [200] * 5          # never limited
        # The exempt calls spent nothing: the anonymous budget is intact.
        assert client.post("/upgrade", json=_upgrade_body()).status_code == 200
        assert client.post("/upgrade", json=_upgrade_body()).status_code == 429


def test_upgrade_wrong_or_unset_client_token_is_anonymous(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    instance = MagicMock()
    instance.get_timestamp.side_effect = main.CommitmentNotFoundError("not yet")
    with patch("main.RemoteCalendar", return_value=instance):
        monkeypatch.setattr(main, "UPGRADE_CLIENT_TOKEN", UPGRADE_TOKEN)
        wrong = {"Authorization": "Bearer not-the-token"}
        assert client.post("/upgrade", json=_upgrade_body(), headers=wrong).status_code == 200
        assert client.post("/upgrade", json=_upgrade_body(), headers=wrong).status_code == 429
        main._verify_rate_buckets.clear()
        monkeypatch.setattr(main, "UPGRADE_CLIENT_TOKEN", None)
        right = {"Authorization": f"Bearer {UPGRADE_TOKEN}"}
        assert client.post("/upgrade", json=_upgrade_body(), headers=right).status_code == 200
        assert client.post("/upgrade", json=_upgrade_body(), headers=right).status_code == 429


def test_upgrade_client_token_never_exempts_verify(monkeypatch):
    monkeypatch.setattr(main, "VERIFY_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(main, "UPGRADE_CLIENT_TOKEN", UPGRADE_TOKEN)
    hdr = {"Authorization": f"Bearer {UPGRADE_TOKEN}"}
    assert client.post("/verify", json=_upgrade_body(), headers=hdr).status_code == 200
    assert client.post("/verify", json=_upgrade_body(), headers=hdr).status_code == 429


# 18. The calendar's status line. Since fork c1db4dd (2026-09-14) GET / on
# otsd is one JSON object — best_block, needs_attention and the queue — not
# the retired donation page with its "Best-block" marker. The probe reads
# the JSON contract; the retired page is still read, logged as retired,
# while the hosted box waits for the fork (the gateway must take this
# change BEFORE the fork). Anchor billing, which also read anchor_receipts
# here, was retired on 2026-09-18.

def _otsd_status(best_block="00" * 31 + "aa", receipts="on", attention=(), **extra):
    """otsd's status line as the fork now sends it (rpc.py get_status)."""
    import json as _json
    m = MagicMock()
    m.raise_for_status.return_value = None
    body = {"version": "0.7.1+fork", "pending_commitments": 3, "txs_waiting_for_confirmation": 0,
            "most_recent_tx": None, "prior_versions": 0, "tip": None,
            "best_block": best_block, "block_height": 965870 if best_block else None,
            "balance": 212015 if best_block else None,
            "anchor_receipts": receipts, "needs_attention": list(attention)}
    body.update(extra)
    if "needs_attention" in extra and extra["needs_attention"] is None:
        del body["needs_attention"]
    m.content = (_json.dumps(body) + "\n").encode()
    return m


def test_health_otsd_reads_the_json_status():
    with patch("main.requests.get", side_effect=[_otsd_status()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["otsd"] == "ok"
    assert "otsd_attention" not in body


def test_health_otsd_json_bitcoin_blind_is_error(caplog):
    # best_block null: the calendar answered but cannot see Bitcoin.
    with patch("main.requests.get", side_effect=[_otsd_status(best_block=None)]):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["otsd"] == "error"
    assert any("Bitcoin-blind" in r.message for r in caplog.records)


def test_health_otsd_needs_attention_degrades_and_surfaces(caplog):
    finding = "anchor " + "3f" * 32 + " left the chain (confirmations 0, receipted at height 965866)"
    with patch("main.requests.get", side_effect=[_otsd_status(attention=[finding])]):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["otsd"] == "needs_attention"
    assert body["otsd_attention"] == [finding]
    assert any("3f" * 32 in r.message and "attention" in r.message.lower() for r in caplog.records)


def test_health_otsd_attention_outranked_by_bitcoin_blind():
    # A blind calendar cannot be trusted about anything else; the finding is
    # still surfaced in the list.
    finding = "anchor 11aa mined again at height 10, receipted at height 2"
    with patch("main.requests.get", side_effect=[_otsd_status(best_block=None, attention=[finding])]):
        resp = client.get("/health")
    body = resp.json()
    assert body["otsd"] == "error"
    assert body["otsd_attention"] == [finding]


def test_health_otsd_malformed_status_is_error(caplog):
    # Not the JSON status and not the retired page either: broken JSON, a
    # JSON array, an empty body, a foreign page. All read as error, and the
    # log names what came back.
    for body in (b"{", b'["not", "an object"]', b"", b"<html>calendar</html>", b"null"):
        caplog.clear()
        main._health_probe_cache["at"] = None
        with patch("main.requests.get", side_effect=[_blind_otsd(body)]):
            with caplog.at_level(logging.WARNING):
                resp = client.get("/health")
        assert resp.status_code == 503, body
        assert resp.json()["otsd"] == "error", body
        assert any("not the JSON status" in r.message for r in caplog.records), body


def test_health_otsd_status_without_attention_field_is_ok():
    # A fork between the status line and the detector: no field, no finding.
    with patch("main.requests.get", side_effect=[_otsd_status(needs_attention=None)]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["otsd"] == "ok"


def test_health_otsd_attention_must_be_a_list_of_strings():
    # A malformed needs_attention never crashes /health: it reads as unknown.
    for bad in ("a string", 7, {"x": 1}, [1, 2]):
        main._health_probe_cache["at"] = None
        with patch("main.requests.get", side_effect=[_otsd_status(needs_attention=bad)]):
            resp = client.get("/health")
        assert resp.status_code == 200, bad
        assert resp.json()["otsd"] == "ok", bad


def _otsd_legacy_page():
    """The retired donation page (fork before c1db4dd) with its Best-block
    marker: what a hosted box still serves until it takes the fork."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.content = b"<html>Best-block: 00000000abc, height 900000</html>"
    return m


def test_health_otsd_retired_page_still_read_and_named(caplog):
    # Transitional: the hosted box takes this gateway before the fork, so the
    # retired page must still read as healthy — and be named in the log so
    # the operator knows the fork is behind.
    with patch("main.requests.get", return_value=_otsd_legacy_page()):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["otsd"] == "ok"
    assert any("retired" in r.message for r in caplog.records)



# 20. Removed /ui route
# The static payment UI is gone (undocumented, untested, and broken on the
# free door: it treated only 402 as success). The path must 404 like any
# unregistered route.

def test_ui_route_removed_404():
    assert client.get("/ui").status_code == 404
    assert client.get("/ui/").status_code == 404


# ---------------------------------------------------------------------------
# 17d. ops/phoenixd-status.sh matches the daemon by exact process name (D9)
# ---------------------------------------------------------------------------
def test_phoenixd_status_matches_exact_process_name_and_configured_bind(tmp_path):
    """Pre-fix the script ran `pgrep -af phoenixd`, which matched its own
    argv (and any process mentioning phoenixd) so "process: running" could
    never go red, and it looked for 127.0.0.1:9740 whatever PHOENIXD_URL
    said. Now: pgrep -x on PHOENIXD_PROC, and the bind PHOENIXD_URL names."""
    import subprocess
    import stat

    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "pgrep.calls"

    def stub(name, body):
        path = stubs / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    # The stub records its arguments and reports "one daemon" only when
    # asked by exact name (-x: a pid; -xc: a count).
    stub("pgrep", f'echo "$@" >> "{calls}"\n'
                  '[ "$2" = phoenixd ] || exit 1\n'
                  'case "$1" in -x) echo 4242 ;; -xc) echo 1 ;; *) exit 1 ;; esac\n')
    stub("ss", 'echo "LISTEN 0 4096 172.17.0.1:9740 0.0.0.0:*"\nexit 0\n')
    stub("systemctl", 'case "$*" in *is-active*) echo active ;; *is-enabled*) echo enabled ;; *) echo ActiveState=active ;; esac\n')
    stub("curl", 'exit 1\n')

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops", "phoenixd-status.sh")
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(tmp_path),
           "REPO": str(tmp_path / "no-repo"), "PHOENIX_HOME": str(tmp_path / "no-home"),
           "PHOENIXD_URL": "http://172.17.0.1:9740"}
    out = subprocess.run(["/bin/bash", script], env=env, capture_output=True, text=True, timeout=60).stdout

    assert "process: running" in out
    assert "phoenixd_processes: 1" in out
    assert "api: listening (172.17.0.1:9740)" in out
    recorded = calls.read_text().splitlines()
    assert recorded and all("-x" in c for c in recorded), recorded
    assert all("-a" not in c.split() and "-f" not in c.split() and "-af" not in c.split() for c in recorded), recorded

    # The knob: a differently named binary is what gets matched.
    env["PHOENIXD_PROC"] = "phoenixd-arm64"
    out = subprocess.run(["/bin/bash", script], env=env, capture_output=True, text=True, timeout=60).stdout
    assert "process: needs_attention" in out
    assert "phoenixd-arm64" in calls.read_text()



# 19. The 2026-09-15 independent review (gateway findings), as regressions.
# Each test here fails against de352f1 and passes now. Reproductions of the
# review's own kit are kept as close to its shape as the fix allows.

from opentimestamps.core.op import OpAppend as _OpAppend  # noqa: E402


def _forged_bitcoin_proof():
    """A valid OTS encoding attesting DIGEST directly to block 0. The genesis
    header's merkle root is not this digest, so the public library rejects
    it against the real header — a fabricated attestation."""
    return make_detached_ots_bytes(attestation=main.BitcoinBlockHeaderAttestation(0))


def test_review_fabricated_attestation_is_never_reported_verified():
    """Before: both endpoints answered status "anchored", verified true.
    Now the state is structural: bitcoin_attestation_present, verified
    null (not checked here), and the answer says what was and was not
    checked."""
    from bitcoin.core import CBlockHeader
    from opentimestamps.core.notary import VerificationError
    proof = _forged_bitcoin_proof()
    body = {"digest": DIGEST, "ots": base64.b64encode(proof).decode()}
    genesis = CBlockHeader.deserialize(bytes.fromhex(
        "01000000" + "00" * 32 + "3ba3edfd7a7b12b27ac72c3e67768f617fc81bc3888a51323a9fb8aa4b1e5e4a"
        + "29ab5f49ffff001d1dac2b7c"))
    with pytest.raises(VerificationError):
        main.BitcoinBlockHeaderAttestation(0).verify_against_blockheader(bytes.fromhex(DIGEST), genesis)
    for route in ("/verify", "/upgrade"):
        answer = client.post(route, json=body).json()
        assert answer["status"] == "bitcoin_attestation_present", route
        assert answer["verified"] is None, route
        assert answer["verification"] == "structural", route
        assert "not checked against a Bitcoin block header" in answer["verification_note"], route
        assert answer["bitcoin_attestation_present"] is True
        assert "anchored" not in answer["status"]
        assert True not in {answer["verified"]}


def test_review_verified_is_false_for_every_non_attested_state():
    pending = base64.b64encode(make_detached_ots_bytes()).decode()
    other = base64.b64encode(make_detached_ots_bytes(digest=OTHER_DIGEST)).decode()
    for ots, status in ((pending, "pending"), (other, "mismatch"), ("!!not-base64!!", "invalid")):
        answer = client.post("/verify", json={"digest": DIGEST, "ots": ots}).json()
        assert answer["status"] == status
        assert answer["verified"] is False and answer["verification"] == "structural"


def test_review_malformed_token_content_never_reaches_the_log(caplog):
    """The review's kit: an unauthenticated token whose packet carries a
    synthetic patient name and record content. Before, the parse error's
    text — the packet — was logged verbatim at WARNING. Now the log line
    names the exception class and nothing else."""
    payload = b"patient=EXAMPLE_PERSON; diagnosis=EXAMPLE_RECORD_CONTENT"
    packet = b"notakey " + payload + b"\n"
    token = base64.urlsafe_b64encode(f"{len(packet) + 4:04x}".encode() + packet).decode()
    with caplog.at_level(logging.DEBUG):
        response = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert response.status_code == 401
    assert "EXAMPLE_PERSON" not in caplog.text
    assert "EXAMPLE_RECORD_CONTENT" not in caplog.text
    assert "notakey" not in caplog.text
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "could not be" in warnings[0].getMessage()
    # Fixed text plus a class name: the message is one of a closed set.
    assert re.fullmatch(r"L402 macaroon (could not be parsed|caveats could not be read) \([A-Za-z_]+\)",
                        warnings[0].getMessage())


def _proof_with_pending_substamps(count, calendar="http://example.test"):
    tree = main.Timestamp(bytes.fromhex(DIGEST))
    for i in range(count):
        tree.ops.add(_OpAppend(i.to_bytes(2, "big"))).attestations.add(main.PendingAttestation(calendar))
    detached = main.DetachedTimestampFile(main.OpSHA256(), tree)
    buf = io.BytesIO()
    detached.serialize(main.StreamSerializationContext(buf))
    return buf.getvalue()


def test_review_upgrade_calendar_work_is_bounded_per_request():
    """The review's kit: a 3.5 KB proof with 100 pending sub-stamps made
    100 sequential calendar calls (timeout 10 each). Now at most
    UPGRADE_MAX_CALENDAR_QUERIES lookups, each bounded, and the answer
    says the budget ran out."""
    proof = _proof_with_pending_substamps(100)
    calendar = MagicMock()
    calendar.get_timestamp.side_effect = main.CommitmentNotFoundError("not yet")
    with patch("main.RemoteCalendar", return_value=calendar):
        result = client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(proof).decode()})
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "pending"
    assert calendar.get_timestamp.call_count == main.UPGRADE_MAX_CALENDAR_QUERIES == 8
    assert body["upgrade"] == {"calendar_queries": 8, "budget_exhausted": True}
    for call in calendar.get_timestamp.call_args_list:
        assert call.kwargs["timeout"] <= main.UPGRADE_QUERY_TIMEOUT


def test_review_upgrade_elapsed_time_is_bounded(monkeypatch):
    proof = _proof_with_pending_substamps(5)
    clock = [1000.0]

    def slow_lookup(commitment, timeout=None):
        clock[0] += 6.0  # each lookup "takes" six seconds
        raise main.CommitmentNotFoundError("not yet")
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    calendar = MagicMock()
    calendar.get_timestamp.side_effect = slow_lookup
    with patch("main.RemoteCalendar", return_value=calendar):
        body = client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(proof).decode()}).json()
    # 15 s budget, 6 s per lookup: the third would start past 12 s and
    # gets a shortened timeout; nothing starts once 15 s have elapsed.
    assert calendar.get_timestamp.call_count == 3
    assert body["upgrade"]["budget_exhausted"] is True


def test_review_upgrade_deduplicates_commitments_and_stops_at_first_anchor():
    # The same commitment under two branches: one lookup. And once the
    # calendar hands back a Bitcoin attestation, no further lookups.
    tree = main.Timestamp(bytes.fromhex(DIGEST))
    for _ in range(3):
        tree.ops.add(_OpAppend(b"\x01")).attestations.add(main.PendingAttestation("http://example.test"))
    for i in range(4):
        tree.ops.add(_OpAppend(bytes([0x10 + i]))).attestations.add(main.PendingAttestation("http://example.test"))
    detached = main.DetachedTimestampFile(main.OpSHA256(), tree)
    buf = io.BytesIO()
    detached.serialize(main.StreamSerializationContext(buf))
    # OpAppend(b"\x01") added three times is one op in the tree: the walk
    # sees one sub-stamp for it, then the four distinct ones.
    seen = []

    def lookup(commitment, timeout=None):
        seen.append(commitment)
        if len(seen) == 2:
            upgraded = main.Timestamp(commitment)
            upgraded.attestations.add(main.BitcoinBlockHeaderAttestation(954112))
            return upgraded
        raise main.CommitmentNotFoundError("not yet")
    calendar = MagicMock()
    calendar.get_timestamp.side_effect = lookup
    with patch("main.RemoteCalendar", return_value=calendar):
        body = client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(buf.getvalue()).decode()}).json()
    assert body["status"] == "bitcoin_attestation_present"
    assert body["verified"] is None
    assert len(seen) == 2 and len(set(seen)) == 2
    assert body["upgrade"]["calendar_queries"] == 2


def test_review_calendar_unavailable_is_a_503_not_pending():
    """Every lookup failing by transport is the calendar not answering:
    503 calendar_unavailable, never the 200 "pending" that means the
    calendar answered "not anchored yet"."""
    proof = make_detached_ots_bytes()
    calendar = MagicMock()
    calendar.get_timestamp.side_effect = ConnectionError("not reachable")
    with patch("main.RemoteCalendar", return_value=calendar):
        result = client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(proof).decode()})
    assert result.status_code == 503
    body = result.json()
    assert body["status"] == "calendar_unavailable"
    assert body["verified"] is False and body["ots"] == base64.b64encode(proof).decode()
    # One successful "not found" among failures is still a pending answer.
    calendar.get_timestamp.side_effect = [ConnectionError("x"), main.CommitmentNotFoundError("not yet")]
    with patch("main.RemoteCalendar", return_value=calendar):
        result = client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(
            _proof_with_pending_substamps(2)).decode()})
    assert result.status_code == 200 and result.json()["status"] == "pending"


def test_review_calendar_failure_log_names_the_class_only(caplog):
    proof = make_detached_ots_bytes()
    calendar = MagicMock()
    calendar.get_timestamp.side_effect = ConnectionError("http://secret.calendar/with?token=EXAMPLE_SECRET")
    with patch("main.RemoteCalendar", return_value=calendar):
        with caplog.at_level(logging.DEBUG):
            client.post("/upgrade", json={"digest": DIGEST, "ots": base64.b64encode(proof).decode()})
    assert "EXAMPLE_SECRET" not in caplog.text
    assert "calendar query failed (ConnectionError)" in caplog.text



# The body cap against the real HTTP parser: a uvicorn server on loopback
# in this process, spoken to over a raw socket, so the request is framed by
# the pinned h11 exactly as in production.
import contextlib  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402


@contextlib.contextmanager
def _live_gateway():
    import uvicorn
    config = uvicorn.Config(main.app, host="127.0.0.1", port=0, lifespan="off",
                            log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.01)
    assert server.started
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(10)


def _raw_http(port, request: bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        try:
            sock.sendall(request)
        except OSError:
            pass  # the server may answer and close before the body is all sent
        sock.shutdown(socket.SHUT_WR) if False else None
        chunks = []
        while True:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
            if b"\r\n\r\n" in b"".join(chunks):
                head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
                m = re.search(rb"content-length: ([0-9]+)", head, re.I)
                if m and len(rest) >= int(m.group(1)):
                    break
        return b"".join(chunks)


def test_review_body_cap_holds_against_the_real_parser_with_dual_framing():
    """The review's request: Content-Length: 1 beside Transfer-Encoding:
    chunked, carrying a 525 KB JSON body in chunks. Before, h11 framed the
    body by the chunks, the middleware trusted Content-Length, and the
    gateway answered 200. Now: 411 before routing, and the endpoint never
    runs."""
    payload = json.dumps({"digest": DIGEST, "ignored_extra": "x" * (main.MAX_REQUEST_BYTES + 1000)}).encode()
    request = (b"POST /timestamp HTTP/1.1\r\nHost: example\r\nContent-Type: application/json\r\n"
               b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n"
               + f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
    with patch("main.requests.get", return_value=_settled_get()), patch("main.stamp_digest", return_value=b"proof") as stamp:
        with _live_gateway() as port:
            answer = _raw_http(port, request)
            # A plain chunked POST is refused the same way.
            plain = (b"POST /verify HTTP/1.1\r\nHost: example\r\nContent-Type: application/json\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n")
            answer_plain = _raw_http(port, plain)
            # An honest request still reaches the endpoint.
            body = json.dumps({"digest": DIGEST}).encode()
            honest = (b"POST /timestamp HTTP/1.1\r\nHost: example\r\nContent-Type: application/json\r\n"
                      + f"Authorization: L402 {valid_token()}:{PREIMAGE}\r\n".encode()
                      + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            answer_honest = _raw_http(port, honest)
    assert answer.startswith(b"HTTP/1.1 411"), answer[:200]
    assert answer_plain.startswith(b"HTTP/1.1 411"), answer_plain[:200]
    assert answer_honest.startswith(b"HTTP/1.1 200"), answer_honest[:200]
    assert stamp.call_count == 1  # the honest request only


def test_review_body_cap_refuses_ambiguous_content_length_and_over_cap_before_routing():
    with patch("main.requests.get", return_value=_settled_get()), patch("main.stamp_digest", return_value=b"proof") as stamp:
        with _live_gateway() as port:
            body = json.dumps({"digest": DIGEST}).encode()
            # Two disagreeing Content-Length headers: h11 itself refuses
            # the request (400) — it never reaches the app.
            two = (b"POST /timestamp HTTP/1.1\r\nHost: example\r\nContent-Type: application/json\r\n"
                   + f"Content-Length: {len(body)}\r\nContent-Length: 1\r\n\r\n".encode() + body)
            answer_two = _raw_http(port, two)
            big = (b"POST /timestamp HTTP/1.1\r\nHost: example\r\nContent-Type: application/json\r\n"
                   + f"Content-Length: {main.MAX_REQUEST_BYTES + 1}\r\n\r\n".encode())
            answer_big = _raw_http(port, big)
    assert answer_two.startswith(b"HTTP/1.1 400"), answer_two[:200]
    assert answer_big.startswith(b"HTTP/1.1 413"), answer_big[:200]
    assert stamp.call_count == 0


def test_review_body_cap_counts_bytes_the_parser_delivers():
    """Defence in depth at the ASGI layer: whatever the parser frames, the
    app never sees more than the declared length. Driven directly, with a
    receive that hands over more than Content-Length declared."""
    payload = json.dumps({"digest": DIGEST, "ignored_extra": "x" * (main.MAX_REQUEST_BYTES + 1000)}).encode()
    scope = _scope("/timestamp", [("content-type", "application/json"), ("content-length", "1")])
    with patch("main.stamp_digest", return_value=b"proof") as stamp:
        status, out, receives = _run_asgi(scope, body=payload)
    assert status == 413 and b"too large" in out
    assert stamp.call_count == 0
    # The middleware refuses any Transfer-Encoding at the scope level too.
    scope = _scope("/timestamp", [("content-type", "application/json"),
                                  ("content-length", "1"), ("transfer-encoding", "chunked")])
    status, out, receives = _run_asgi(scope, body=payload)
    assert status == 411 and receives == 0
    scope = _scope("/timestamp", [("content-type", "application/json"),
                                  ("content-length", "5"), ("content-length", "7")])
    status, out, receives = _run_asgi(scope, body=b"12345")
    assert status == 400 and receives == 0


# 20. The 2026-09-15 review: ops scripts, the compose allowlist, the units.
# Shell scripts are run as subprocesses with stubbed tools on PATH, exactly
# as the review's kit did; nothing here touches a real wallet or node.
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sqlite3 as _sqlite3  # noqa: E402

_REPO = os.path.dirname(os.path.abspath(__file__))


def _stub(directory, name, body):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o700)
    return path


def _run_script(script, env, cwd=None):
    return subprocess.run(["/bin/bash", os.path.join(_REPO, "ops", script)],
                          env=env, text=True, capture_output=True, cwd=cwd, timeout=120)


def _ops_env(tmp_path, fixture_env, **extra):
    fixture = tmp_path / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / ".env").write_text(fixture_env)
    bins = tmp_path / "bin"
    bins.mkdir(exist_ok=True)
    env = dict(os.environ, REPO=str(fixture), PATH=str(bins) + ":" + os.environ["PATH"])
    env.pop("WALLET_RPC_ENV_FILE", None)
    env.update(extra)
    return fixture, bins, env


def test_review_wallet_alarm_resolves_every_setting_after_env_is_loaded(tmp_path):
    """The review's kit: WALLET_STATUS_PATH set in .env while the
    environment carried an older path. Before, the older path was written
    and the configured file stayed absent (which /health reads as
    healthy). Now .env wins, and WALLET_MIN_SATS from .env is honoured too."""
    expected = tmp_path / "configured-status"
    before = tmp_path / "before-source-status"
    fixture, bins, env = _ops_env(
        tmp_path,
        f"WALLET_STATUS_PATH={expected}\nWALLET_MIN_SATS=2000000\nWALLET_RPC_URL=http://unused.test\n",
        WALLET_STATUS_PATH=str(before), WALLET_MIN_SATS="1")
    _stub(bins, "curl", """printf '%s' '{"result":{"mine":{"trusted":0.01}},"error":null}'""")
    r = _run_script("wallet-balance-check.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert expected.exists() and not before.exists()
    status = json.loads(expected.read_text())
    assert status["balance_sats"] == 1_000_000
    assert status["min_sats"] == 2_000_000 and status["status"] == "low"


def test_review_wallet_alarm_credential_is_its_own_file_not_the_gateway_env(tmp_path):
    """The alarm's RPC user comes from its own file (WALLET_RPC_ENV_FILE);
    the gateway's .env need not — and on the systemd path must not — hold
    any Bitcoin credential. Without either, status is unknown, loudly."""
    status = tmp_path / "wallet-status"
    fixture, bins, env = _ops_env(tmp_path, f"WALLET_STATUS_PATH={status}\n")
    _stub(bins, "curl", """printf '%s' '{"result":{"mine":{"trusted":0.5}},"error":null}'""")
    cred = tmp_path / "wallet-balance-check.env"
    cred.write_text("WALLET_RPC_URL=http://alarm:secret@unused.test\n")
    r = _run_script("wallet-balance-check.sh", dict(env, WALLET_RPC_ENV_FILE=str(cred)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(status.read_text())["status"] == "ok"
    assert "secret" not in r.stdout + r.stderr
    r = _run_script("wallet-balance-check.sh", dict(env, WALLET_RPC_ENV_FILE=str(tmp_path / "absent")))
    assert r.returncode == 0
    assert json.loads(status.read_text()) == {**json.loads(status.read_text()), "status": "unknown",
                                              "error": "WALLET_RPC_URL not set"}


def _proof_scan_fixture(tmp_path, find_body, env_lines=""):
    status = tmp_path / "proofs-status"
    fixture, bins, env = _ops_env(tmp_path, f"PROOFS_STATUS_PATH={status}\n" + env_lines)
    (fixture / "ops").mkdir(exist_ok=True)
    _stub(fixture / "ops", "upgrade-proof.sh", 'echo "state: bitcoin_backed"; echo "bitcoin_block: 1"')
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    _stub(bins, "find", 'case "$1" in --version) echo "find (GNU findutils) 4.9"; exit 0;; esac\n' + find_body)
    env.update(ARTIFACTS=str(artifacts))
    return status, env


def test_review_proof_scan_traversal_failure_is_attention_and_nonzero(tmp_path):
    """The review's kit: find exits 1. Before: exit 0, status ok, total 0,
    scan_complete. Now: exit 1, status attention naming the scan, state
    needs_attention — and the status path comes from .env."""
    status, env = _proof_scan_fixture(tmp_path, "echo permission-denied >&2; exit 1")
    r = _run_script("upgrade-all-proofs.sh", env)
    assert r.returncode == 1, r.stdout + r.stderr
    written = json.loads(status.read_text())
    assert written["status"] == "attention" and "proof scan failed" in written["error"]
    assert "state: needs_attention" in r.stdout and "scan_complete" not in r.stdout


def test_review_proof_scan_distinguishes_no_files_from_failure(tmp_path):
    status, env = _proof_scan_fixture(tmp_path, "exit 0")
    r = _run_script("upgrade-all-proofs.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads(status.read_text())
    assert written["status"] == "ok" and written["total"] == 0 and "error" not in written
    assert "no proofs" in r.stdout and "state: scan_complete" in r.stdout
    # Two proofs listed: both counted.
    proofs = tmp_path / "artifacts"
    for name in ("a", "b"):
        (proofs / name).mkdir()
        (proofs / name / "proof.ots").write_bytes(b"x")
    status, env = _proof_scan_fixture(tmp_path, f'echo "2 {proofs}/a/proof.ots"; echo "1 {proofs}/b/proof.ots"')
    r = _run_script("upgrade-all-proofs.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(status.read_text())["bitcoin_backed"] == 2


def test_review_proof_scan_states_its_gnu_find_dependency(tmp_path):
    status, env = _proof_scan_fixture(tmp_path, "exit 0")
    _stub(tmp_path / "bin", "find", "echo 'find: illegal option -- -' >&2; exit 1")  # BSD find
    r = _run_script("upgrade-all-proofs.sh", env)
    assert r.returncode == 1
    assert "GNU find required" in json.loads(status.read_text())["error"]


def _backup_fixture(tmp_path, with_row=True):
    fixture = tmp_path / "repo"
    fixture.mkdir()
    os.symlink(os.path.join(_REPO, "ops"), fixture / "ops")
    state = tmp_path / "state"
    state.mkdir()
    conn = _sqlite3.connect(state / "obligations.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE obligations(payment_hash TEXT PRIMARY KEY, digest TEXT, created_at INTEGER, status TEXT)")
    if with_row:
        conn.execute("INSERT INTO obligations VALUES (?, ?, 1, 'needs_stamp')", ("ab" * 32, "cd" * 32))
    conn.commit()
    conn.close()
    root = tmp_path / "backups"
    status = tmp_path / "custom-backup-status"
    (fixture / ".env").write_text(f"BACKUP_ROOT={root}\nBACKUP_STATUS_PATH={status}\nBACKUP_KEEP=3\n")
    bins = tmp_path / "bin"
    bins.mkdir()
    _stub(bins, "docker", "exit 1")   # no daemon here: no container path, no compose gateway
    for member in ("phoenix", "calendar", "fork"):
        (tmp_path / member).mkdir()
    env = dict(os.environ, REPO_DIR=str(fixture), STATE_DIR=str(state), PHOENIX_HOME=str(tmp_path / "phoenix"),
               OTSD_CALENDAR_DIR=str(tmp_path / "calendar"), OTSD_FORK_PATH=str(tmp_path / "fork"),
               TOR_KEYS_DIR="", ANCHOR_RECEIPTS_DIR="", ARTIFACTS="", GATEWAY_UNIT="", PHOENIXD_UNIT="", SOCAT_UNIT="",
               PATH=str(bins) + ":" + os.environ["PATH"])
    # This module exports OBLIGATIONS_DB_PATH=":memory:" for the app; the
    # backup honours that setting (2026-09-15/16 review F07) and these
    # tests are about the default path, so it must not leak into the run.
    env.pop("OBLIGATIONS_DB_PATH", None)
    return fixture, state, status, root, bins, env


def _archive_metadata(root):
    archives = sorted(p for p in root.iterdir() if p.name.endswith("-live-state.tar.gz"))
    assert archives, list(root.iterdir())
    out = subprocess.run(["tar", "-xzOf", str(archives[-1]), "--include=*/metadata.txt", "--include=*metadata.txt"],
                         capture_output=True, text=True)
    return out.stdout


def test_review_backup_takes_a_checked_online_snapshot(tmp_path):
    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 CLI not on PATH")
    fixture, state, status, root, bins, env = _backup_fixture(tmp_path)
    r = _run_script("backup-live-state.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads(status.read_text())   # the .env path, honoured
    assert written["status"] == "local_only", written
    assert "snapshot check: usable" in r.stdout
    meta = _archive_metadata(root)
    assert "obligations_newest_payment_hash: " + "ab" * 32 in meta
    assert "obligations_snapshot_check: usable" in meta
    assert "crash-consistent" not in r.stdout + r.stderr


def test_review_backup_without_a_usable_snapshot_is_failed_while_writers_run(tmp_path):
    """The review's point: a raw copy of a live WAL database is not a
    snapshot of any instant. With no usable online snapshot and the
    gateway running, the backup is reported failed (the archive is still
    made); with the gateway stopped it is attention, a stopped-writer copy."""
    fixture, state, status, root, bins, env = _backup_fixture(tmp_path)
    _stub(bins, "sqlite3", "echo 'injected: cannot snapshot' >&2; exit 1")
    _stub(bins, "systemctl", 'case "$1" in is-active) exit 0;; esac; exit 1')   # gateway active
    r = _run_script("backup-live-state.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads(status.read_text())
    assert written["status"] == "failed", written
    assert "NOT a consistent snapshot" in written["detail"]
    assert any(p.name.endswith("-live-state.tar.gz") for p in root.iterdir())
    _stub(bins, "systemctl", 'case "$1" in is-active) exit 3;; esac; exit 1')   # gateway stopped
    r = _run_script("backup-live-state.sh", env)
    assert r.returncode == 0, r.stdout + r.stderr
    written = json.loads(status.read_text())
    assert written["status"] == "attention" and "stopped-writer copy" in written["detail"]


def test_review_snapshot_verifier_reports_an_unusable_copy_as_failed(tmp_path):
    verify = os.path.join(_REPO, "ops", "verify-obligations-snapshot.sh")
    good = tmp_path / "good.db"
    conn = _sqlite3.connect(good)
    conn.execute("CREATE TABLE obligations(payment_hash TEXT PRIMARY KEY, digest TEXT)")
    conn.execute("INSERT INTO obligations VALUES (?, 'x')", ("11" * 32,))
    conn.commit()
    conn.close()
    ok = subprocess.run(["/bin/bash", verify, str(good), "--expect-payment-hash", "11" * 32], capture_output=True, text=True)
    assert ok.returncode == 0 and ok.stdout.startswith("usable:"), ok.stdout + ok.stderr
    missing = subprocess.run(["/bin/bash", verify, str(good), "--expect-payment-hash", "22" * 32], capture_output=True, text=True)
    assert missing.returncode == 1 and "unusable: known obligation row" in missing.stdout
    torn = tmp_path / "torn.db"
    torn.write_bytes(good.read_bytes()[:1500] + b"\x00" * 600)
    bad = subprocess.run(["/bin/bash", verify, str(torn)], capture_output=True, text=True)
    assert bad.returncode == 1 and bad.stdout.startswith("unusable:"), bad.stdout + bad.stderr
    empty = tmp_path / "empty.db"
    _sqlite3.connect(empty).close()
    notable = subprocess.run(["/bin/bash", verify, str(empty)], capture_output=True, text=True)
    assert notable.returncode == 1 and "obligations table missing" in notable.stdout


def test_review_compose_gateway_environment_is_an_allowlist(tmp_path):
    """The review's kit, inverted: `docker compose config` with a dummy
    RPC credential in .env. The gateway service's environment must not
    carry it, otsd's must, and every variable main.py reads must be in
    the gateway's allowlist (so a config addition cannot silently strand
    the container)."""
    probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.skip("docker compose not available")
    shutil.copy(os.path.join(_REPO, "docker-compose.yml"), tmp_path / "docker-compose.yml")
    (tmp_path / "otsd").mkdir()
    (tmp_path / "tor").mkdir()
    (tmp_path / ".env").write_text(
        "BITCOIN_RPC_SERVICE_URL=http://example_user:example_wallet_password@127.0.0.1:8332/wallet/test\n"
        "PRICE_PER_PROOF_SATS=500\n")
    r = subprocess.run(["docker", "compose", "--project-directory", str(tmp_path), "--profile", "calendar",
                        "config", "--format", "json"], text=True, capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    services = json.loads(r.stdout)["services"]
    gateway, otsd = services["gateway"]["environment"], services["otsd"]["environment"]
    assert "env_file" not in services["gateway"]
    assert "BITCOIN_RPC_SERVICE_URL" not in gateway and "BITCOIN_RPC_ONION" not in gateway
    assert "example_wallet_password" not in json.dumps(gateway)
    assert "example_wallet_password" in otsd["BITCOIN_RPC_SERVICE_URL"]
    assert gateway["PRICE_PER_PROOF_SATS"] == "500"
    assert gateway["PAUSE_FILE"] == ""   # absent from .env: empty, which main reads as unset
    source = open(os.path.join(_REPO, "main.py")).read()
    read_by_main = set(re.findall(r'_env\(\s*"([A-Z0-9_]+)"', source)) | set(re.findall(r'os\.getenv\("([A-Z0-9_]+)"', source))
    retired = {"GATEWAY_PRICE_SATS", "MIN_GATEWAY_PRICE_SATS", "PRICE_BLIND_SATS", "PRICE_BUMP_RESERVE",
               "PRICE_MARGIN", "PRICE_TX_VSIZE_ESTIMATE", "PRICE_CONF_TARGET", "PRICE_RPC_URL", "PRICE_MARKUP"}
    missing = read_by_main - retired - set(gateway)
    assert not missing, f"read by main.py but not in the compose allowlist: {sorted(missing)}"
    assert not (set(gateway) - read_by_main), f"in the allowlist but never read: {sorted(set(gateway) - read_by_main)}"


def test_review_systemd_templates_separate_the_gateway_identity_from_docker_and_the_credential():
    gateway_unit = open(os.path.join(_REPO, "deploy", "timestamp-gateway.service.example")).read()
    otsd_unit = open(os.path.join(_REPO, "deploy", "otsd.service.example")).read()
    alarm_unit = open(os.path.join(_REPO, "ops", "systemd", "wallet-balance-check.service")).read()
    assert "InaccessiblePaths=-/var/run/docker.sock" in gateway_unit
    assert "User=gateway" in gateway_unit and "docker group" in gateway_unit
    assert "User=otsd" in otsd_unit and "User=gateway" not in otsd_unit
    assert "EnvironmentFile=-/etc/systemd/system/wallet-balance-check.env" in alarm_unit
    assert "lnd" not in gateway_unit.lower()


# 21. Workflow five (2026-09-18): the surviving findings of the 2026-09-15
# review and the gate's rulings, red-first. Each test here failed against
# 713269e before its fix (the step 1 close-out records the red output).
import http.server as _http_server  # noqa: E402


def test_review_upgrade_wall_clock_deadline_holds_against_a_trickling_calendar(monkeypatch):
    """F18. The per-lookup timeout reaches the socket as an inactivity
    timeout, so a calendar that trickles its answer in gaps shorter than it
    kept one lookup running past the advertised total (the review's loopback
    demonstrator: 13 bytes in 1.42 s gaps took 17.07 s against a 15 s
    budget). The same server shape drives a real request here with the
    budget scaled down: the answer must arrive within the budget, the
    lookup counted as failed and the budget reported exhausted."""
    attested = main.Timestamp(bytes.fromhex(DIGEST))
    attested.attestations.add(main.BitcoinBlockHeaderAttestation(900000))
    buf = io.BytesIO()
    attested.serialize(main.StreamSerializationContext(buf))
    answer = buf.getvalue()
    budget, per_query = 1.0, 0.5
    gap = 3.0 / (len(answer) - 1)   # the whole answer takes ~3 s; every gap is under per_query
    assert gap < per_query
    delivered = []

    class Trickle(_http_server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(answer)))
            self.end_headers()
            for index, byte in enumerate(answer):
                if index:
                    time.sleep(gap)
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except OSError:
                    break
                delivered.append(index)

    server = _http_server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(main, "UPGRADE_MAX_SECONDS", budget)
    monkeypatch.setattr(main, "UPGRADE_QUERY_TIMEOUT", per_query)
    try:
        with patch.object(main, "OTS_CALENDAR_URL", f"http://127.0.0.1:{server.server_port}"):
            started = time.monotonic()
            resp = client.post("/upgrade", json={"digest": DIGEST,
                                                 "ots": base64.b64encode(make_detached_ots_bytes()).decode()})
            elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert delivered, "the trickling server never served: the boundary was not reached"
    assert elapsed < budget + 0.75, f"{elapsed:.2f}s for a {budget}s budget"
    body = resp.json()
    assert resp.status_code == 503 and body["status"] == "calendar_unavailable"
    assert body["upgrade"]["calendar_queries"] == 1 and body["upgrade"]["budget_exhausted"] is True


def test_review_phoenixd_status_reads_its_settings_from_the_env_file(tmp_path):
    """F23. The review's probe: PHOENIXD_URL only in .env, naming
    172.17.0.1:9740; the script probed 127.0.0.1:9740 and reported the
    configured listener absent. Now every setting is resolved after
    ops/lib/env.sh, .env winning over an older value in the environment."""
    fixture, bins, env = _ops_env(
        tmp_path, "PHOENIXD_URL=http://172.17.0.1:9740\nPHOENIXD_HTTP_PASSWORD_LIMITED=invented-test-value\n",
        PHOENIXD_URL="http://127.0.0.1:1", PHOENIX_HOME=str(tmp_path / "absent-wallet"))
    _stub(bins, "systemctl", "echo active")
    _stub(bins, "pgrep", 'case "$1" in -x) echo 1 ;; -xc) echo 1 ;; *) exit 1 ;; esac')
    _stub(bins, "ss", 'echo "LISTEN 0 128 172.17.0.1:9740 0.0.0.0:*"')
    _stub(bins, "curl", "sed -n '/^url = /p'")
    out = _run_script("phoenixd-status.sh", env).stdout
    assert "api: listening (172.17.0.1:9740)" in out, out
    assert 'url = "http://172.17.0.1:9740/getinfo"' in out, out
    assert "nothing listening" not in out


def test_review_otsd_status_reads_its_settings_from_the_env_file(tmp_path):
    """F23. The calendar directory comes from .env (OTSD_CALENDAR_DIR) and
    the container from OTSD_CONTAINER or, unset, from compose's own name for
    the otsd service, never a literal `otsd` and a hard-wired host path."""
    calendar = tmp_path / "cal"
    calendar.mkdir()
    (calendar / "journal").write_bytes(b"x")
    fixture, bins, env = _ops_env(tmp_path, f"OTSD_CALENDAR_DIR={calendar}\n")
    calls = tmp_path / "docker.calls"
    _stub(bins, "docker", f'echo "$@" >> "{calls}"\n'
          'case "$*" in\n'
          '  compose*ps*otsd*) echo tg-otsd-1 ;;\n'
          '  ps*) echo tg-otsd-1 ;;\n'
          '  inspect*--format*.Name*) echo /tg-otsd-1 ;;\n'
'  inspect*) echo \'["python3","otsd","--calendar","/calendar","--btc-conf-target","12"]\' ;;\n'
          '  *) exit 0 ;;\n'
          'esac')
    _stub(bins, "find", 'echo "find $*"')
    out = _run_script("otsd-status.sh", env).stdout
    assert "container: tg-otsd-1" in out, out
    assert f"calendar_host_path: {calendar}" in out, out
    assert f"find {calendar} " in out, out


def test_review_status_script_resolves_settings_after_the_env_file(tmp_path):
    """F23. GATEWAY_URL, ARTIFACTS, PRICE_PER_PROOF_SATS and PAUSE_FILE come
    from .env through the shared loader, .env winning over an older value in
    the environment; nothing is grepped out of .env by hand."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "proof-marker.txt").write_text("x")
    fixture, bins, env = _ops_env(
        tmp_path, f"GATEWAY_URL=http://10.0.0.5:8000\nARTIFACTS={art}\nPRICE_PER_PROOF_SATS=777\nPAUSE_FILE={tmp_path}/PAUSED\n",
        GATEWAY_URL="http://127.0.0.1:1", ARTIFACTS=str(tmp_path / "elsewhere"))
    _stub(bins, "systemctl", "exit 0")
    _stub(bins, "curl", 'echo "curl $*"')
    _stub(bins, "docker", "exit 1")
    _stub(bins, "pgrep", "exit 1")
    _stub(bins, "git", "echo stubbed")
    out = _run_script("status.sh", env).stdout
    assert "curl -sS --max-time 5 http://10.0.0.5:8000/health" in out, out
    assert "price_per_proof_sats: 777" in out, out
    assert "proof-marker.txt" in out, out
    assert "paused: false" in out, out


def test_review_phoenixd_unit_runs_as_its_own_user_with_a_home_the_gateway_cannot_read():
    """F06. Both shipped units ran as User=gateway, so the web process could
    read the wallet's seed and full password by ownership. Now phoenixd has
    its own user and home, the gateway unit keeps its own user, and the
    backup, recovery and status paths name the wallet home under that user."""
    phoenixd_unit = open(os.path.join(_REPO, "deploy", "phoenixd.service.example")).read()
    gateway_unit = open(os.path.join(_REPO, "deploy", "timestamp-gateway.service.example")).read()
    backup = open(os.path.join(_REPO, "ops", "backup-live-state.sh")).read()
    status = open(os.path.join(_REPO, "ops", "phoenixd-status.sh")).read()
    recovery = open(os.path.join(_REPO, "ops", "BACKUP-RECOVERY.md")).read()
    assert "User=phoenixd" in phoenixd_unit and "User=gateway" not in phoenixd_unit
    assert "useradd" in phoenixd_unit
    # The unit body (after the header, which may name the old path in its
    # migration note) runs nothing from, and writes nothing under, the
    # gateway user's home.
    assert "/home/gateway" not in phoenixd_unit.split("[Unit]", 1)[1]
    assert "User=gateway" in gateway_unit
    assert 'PHOENIX_HOME="${PHOENIX_HOME:-/var/lib/phoenixd/.phoenix}"' in backup
    assert 'PHOENIX_HOME="${PHOENIX_HOME:-/var/lib/phoenixd/.phoenix}"' in status
    assert "/var/lib/phoenixd/.phoenix" in recovery and "/home/gateway/phoenixd" not in recovery


# Interruption and handled-failure tests around the paid door's transitions
# (the step 1 brief): each injected boundary asserted reached, recovery
# interrupted again where it can be, convergence shown, exception unwinding
# distinguished from process death (the backup module covers G9; the payer's
# suite covers P0 to P5).

def _post_paid(token, stamp_result=FAKE_OTS):
    """A paid presentation; a server exception surfaces as its class or as a
    500 depending on the test client's setting, so both are read as 500."""
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=stamp_result) as stamp:
            try:
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
                status, content = resp.status_code, resp.content
            except _sqlite3.OperationalError:
                status, content = 500, b""
    return status, content, stamp.call_count


def test_review_g3_a_stop_after_the_stamp_and_before_the_mark_is_converged_by_cache_and_sweeper():
    """G3, exception unwinding between (3) the cache and (4) the mark: the
    calendar holds the commitment, the bytes are cached, the row still says
    needs_stamp and the client got no answer. The client's next presentation
    is served from the cache without a second stamp; the sweeper's next tick
    resubmits once (the calendar dedupes) and marks the row; the tick after
    does nothing."""
    token = valid_token()
    with patch("main.mark_obligation_stamped", side_effect=_sqlite3.OperationalError("disk I/O error")) as mark:
        status, _, stamps = _post_paid(token)
    assert mark.called, "the injected boundary was not reached"
    assert status == 500 and stamps == 1
    assert _obligation_row()["status"] == "needs_stamp"
    assert main._proof_cache[PAYMENT_HASH] == FAKE_OTS
    status, content, stamps = _post_paid(token)
    assert status == 200 and content == FAKE_OTS and stamps == 0
    with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
        main._sweep_obligations_once()
        main._sweep_obligations_once()
    assert stamp.call_count == 1
    row = _obligation_row()
    assert row["status"] == "stamped" and row["attempts"] == 1


def test_review_g3_a_failed_obligation_write_makes_no_calendar_contact_and_the_next_presentation_converges():
    """G3 (1): the row's commit fails. No stamp is attempted and nothing is
    cached: the client still owns the duty through its token, and its next
    presentation records and stamps."""
    token = valid_token()
    with patch("main.record_obligation", side_effect=_sqlite3.OperationalError("disk I/O error")) as record:
        status, _, stamps = _post_paid(token)
    assert record.called, "the injected boundary was not reached"
    assert status == 500 and stamps == 0
    assert _obligation_row() is None and PAYMENT_HASH not in main._proof_cache
    status, content, stamps = _post_paid(token)
    assert status == 200 and content == FAKE_OTS and stamps == 1
    assert _obligation_row()["status"] == "stamped"


def test_review_g4_a_sweeper_stopped_after_the_stamp_converges_on_the_next_tick():
    """G4: the mark fails after the stamp. The tick ends with the row still
    owed and its attempt counted; the next tick stamps again (bounded by the
    calendar's dedupe) and marks it."""
    main.record_obligation(PAYMENT_HASH, DIGEST)
    with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
        with patch("main.mark_obligation_stamped", side_effect=_sqlite3.OperationalError("disk I/O error")) as mark:
            with pytest.raises(_sqlite3.OperationalError):
                main._sweep_obligations_once()
    assert mark.called and stamp.call_count == 1
    row = _obligation_row()
    assert row["status"] == "needs_stamp" and row["attempts"] == 1
    assert main._proof_cache[PAYMENT_HASH] == FAKE_OTS
    with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
        main._sweep_obligations_once()
    row = _obligation_row()
    assert row["status"] == "stamped" and row["attempts"] == 2 and stamp.call_count == 1


def test_review_g8_a_restart_forgets_the_cache_but_never_the_obligation():
    """Process death between G3 (1) and (4), then a restart: the row is on
    disk, the cache is not. The first tick after the restart stamps the row;
    a token presented after the restart re-stamps once (the calendar
    dedupes) and is then cached; the log holds one row throughout."""
    main.record_obligation(PAYMENT_HASH, DIGEST)     # what the dead process left
    main._proof_cache.clear()                         # what the restart forgets
    main.init_obligation_db()                         # the next start, on the existing log
    assert _obligation_row()["status"] == "needs_stamp"
    with patch("main.stamp_digest", return_value=FAKE_OTS) as stamp:
        main._sweep_obligations_once()                # the first tick after the restart
    assert stamp.call_count == 1 and _obligation_row()["status"] == "stamped"
    main._proof_cache.clear()
    status, content, stamps = _post_paid(valid_token())
    assert status == 200 and content == FAKE_OTS and stamps == 1
    assert _obligation_count() == 1
