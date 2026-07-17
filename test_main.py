"""Test suite for the L402 gateway.

Full coverage of the agreed "at least" list: startup/config validation, digest
validation, L402 header parsing, the 402 challenge, token verify/reject, the paid
retry path, LND payment verification, create_invoice wiring, OTS calendar/public
modes with bounded retry and no public fallback, the health endpoint, reuse
semantics, and error discipline (generic public details).
"""

import base64
import hashlib
import io
import json
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest

# Set env vars before importing main so module-level config validation passes.
# load_dotenv() does not override existing env vars, so these take precedence.
os.environ["LND_HOST"] = "test.onion"
os.environ["LND_PORT"] = "8080"
os.environ["LND_MACAROON_HEX"] = "deadbeef" * 8
os.environ["TOR_PROXY"] = "127.0.0.1:9050"
os.environ["GATEWAY_PRICE_SATS"] = "21"
os.environ["MIN_GATEWAY_PRICE_SATS"] = "1"
# Pinned to the static price so quote assertions stay 21 (the suite runs with
# no feerate); blind-price behavior is tested by patching main.PRICE_BLIND_SATS.
os.environ["PRICE_BLIND_SATS"] = "21"
os.environ["PAYMENT_BACKEND_TYPE"] = "lnd"  # explicit: suite mocks LND REST (the test payer)
os.environ["OTS_BACKEND_MODE"] = "calendar"
os.environ["OTS_CALENDAR_URL"] = "http://test-calendar:14788"
os.environ["L402_SECRET_HEX"] = "ab" * 32          # stable, known signing key
os.environ["L402_TOKEN_EXPIRY_SECONDS"] = "3600"
os.environ["OTS_SUBMIT_BACKOFF_SECONDS"] = "0"     # keep retry tests fast
os.environ["OBLIGATIONS_DB_PATH"] = ":memory:"     # overridden per-test by fixture below
os.environ["RATE_LIMIT_PER_MINUTE"] = "0"          # whole suite shares one client IP;
                                                   # rate-limit tests patch the global

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


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _get_mock(settled, memo, amt_paid_sat, value=None):
    """Mock LND invoice-lookup response (verify_payment). ``value`` is the
    invoice face amount; defaults to amt_paid_sat (paid exactly)."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {
        "settled": settled,
        "memo": memo,
        "amt_paid_sat": str(amt_paid_sat),
        "value": str(value if value is not None else amt_paid_sat),
    }
    return m


def _settled_get():
    return _get_mock(True, DIGEST, 21)


def _post_mock():
    """Mock LND invoice-creation response: includes r_hash so create_invoice can
    decode the payment hash (base64 of PAYMENT_HASH so the minted token matches)."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {
        "payment_request": FAKE_INVOICE,
        "r_hash": base64.b64encode(bytes.fromhex(PAYMENT_HASH)).decode(),
    }
    return m


def _good_calendar_ts():
    """A Timestamp with a PendingAttestation so serialization succeeds."""
    ts = Timestamp(bytes.fromhex(DIGEST))
    ts.attestations.add(PendingAttestation("https://test.calendar.example"))
    return ts


def _ok_lnd():
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"alias": "test-node"}
    return m


def _ok_otsd():
    m = MagicMock()
    m.raise_for_status.return_value = None
    # Minimal healthy homepage: the Best-block marker renders only after
    # otsd's Bitcoin RPC calls succeed (see the /health probe comment).
    m.content = b"<html>Best-block: 00000000abc, height 900000</html>"
    return m


def _blind_otsd(body):
    """otsd's Bitcoin-blind shape: the 200 was committed before any Bitcoin
    call, so the body is empty (or at least missing the Best-block marker)."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.content = body
    return m


def _fail():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("connection refused")
    return m


# ══ 1. Startup / config validation ════════════════════════════════════════════

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


def test_missing_required_env_var_fails_at_startup():
    with patch.dict(os.environ, {"LND_HOST": ""}):
        with pytest.raises(RuntimeError, match="Missing required environment variables"):
            main._parse_config()


def test_non_integer_gateway_price_fails():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="GATEWAY_PRICE_SATS must be an integer"):
            main._parse_config()


def test_zero_gateway_price_fails():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "0"}):
        with pytest.raises(RuntimeError, match="positive integer"):
            main._parse_config()


def test_negative_gateway_price_fails():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "-5"}):
        with pytest.raises(RuntimeError, match="positive integer"):
            main._parse_config()


def test_invalid_ots_backend_mode_fails():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "invalid"}):
        with pytest.raises(RuntimeError, match="OTS_BACKEND_MODE must be"):
            main._parse_config()


def test_calendar_mode_requires_calendar_url():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "calendar", "OTS_CALENDAR_URL": ""}):
        with pytest.raises(RuntimeError, match="OTS_CALENDAR_URL is required"):
            main._parse_config()


def test_public_mode_rejects_calendar_url():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "public"}):
        with pytest.raises(RuntimeError, match="OTS_CALENDAR_URL must not be set"):
            main._parse_config()


def test_public_mode_does_not_require_calendar_url():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "public", "OTS_CALENDAR_URL": ""}):
        cfg = main._parse_config()
    assert cfg is not None


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


# ══ 2. Digest validation ══════════════════════════════════════════════════════

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
    assert patched.call_args.kwargs["json"]["memo"] == "a" * 64


# ══ 3. L402 header parsing ═════════════════════════════════════════════════════

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


# ══ 4. 402 challenge path ══════════════════════════════════════════════════════

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
    assert body["price_sats"] == 21          # no feerate in test env; blind floor pinned to 21 here
    assert body["invoice"] == FAKE_INVOICE
    assert isinstance(body["macaroon"], str) and body["macaroon"]
    assert isinstance(body["expiry"], int) and body["expiry"] > int(time.time())


def test_402_creates_invoice_with_digest_memo_and_configured_price():
    with patch("main.requests.post", return_value=_post_mock()) as p:
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402
    sent = p.call_args.kwargs["json"]
    assert sent["memo"] == DIGEST
    assert sent["value"] == 21               # configured GATEWAY_PRICE_SATS
    assert sent["private"] is True


def test_402_quotes_solvency_floor_when_above_static():
    # Fee market at 10 sat/vB: floor = ceil(150 x 10 x 1.5 x 5) = 11250 > 21.
    # Body, invoice amount, and macaroon caveat must all carry the same quote.
    main._feerate_cache = (10.0, time.monotonic())
    try:
        with patch("main.requests.post", return_value=_post_mock()) as p:
            resp = client.post("/timestamp", json={"digest": DIGEST})
        assert resp.status_code == 402
        body = resp.json()["detail"]
        assert body["price_sats"] == 11250
        assert p.call_args.kwargs["json"]["value"] == 11250
        token = body["macaroon"]
        assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 11250)
    finally:
        main._feerate_cache = None


def test_402_challenge_never_refuses_on_feerate_failure():
    # A dead fee-estimation RPC degrades the quote to static; the challenge
    # path itself must stay up.
    main._feerate_cache = None
    try:
        with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
            with patch(
                "main.requests.post",
                side_effect=[main.requests.exceptions.Timeout(), _post_mock()],
            ):
                resp = client.post("/timestamp", json={"digest": DIGEST})
        assert resp.status_code == 402
        assert resp.json()["detail"]["price_sats"] == 21
    finally:
        main._feerate_cache = None


def test_floated_challenge_redeems_after_repricing_down():
    # Full circle: challenge quoted at the 11250 floor, fee market then falls
    # (cache now empty → static 21). The 11250 invoice paid in full must
    # still redeem — mint-time binding end to end through the real challenge.
    main._feerate_cache = (10.0, time.monotonic())
    try:
        with patch("main.requests.post", return_value=_post_mock()):
            challenge = client.post("/timestamp", json={"digest": DIGEST})
        token = challenge.json()["detail"]["macaroon"]
        main._feerate_cache = (None, time.monotonic())  # repriced: static again
        with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 11250)):
            with patch("main.stamp_digest", return_value=FAKE_OTS):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
        assert resp.status_code == 200
        assert resp.content == FAKE_OTS
        # And the same token paid only 21 (the static price) must not redeem:
        # payment verification runs before the proof cache, so underpayment
        # against the mint-time amount still fails even after a redemption.
        with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 21)):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
        assert resp.status_code == 402
    finally:
        main._feerate_cache = None


def test_minted_token_verifies_and_carries_mint_time_price():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    token = resp.json()["detail"]["macaroon"]
    # Bound to this digest: verifies and returns the payment hash + mint price.
    assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 21)
    # NOT bound to the live configured price: a token minted at N validates at N
    # forever, so repricing never strands an in-flight invoice.
    with patch("main.GATEWAY_PRICE_SATS", 99):
        assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 21)


# ══ 5. L402 token verification ═════════════════════════════════════════════════

def test_verify_token_valid_same_digest_returns_payment_hash_and_price():
    assert main.verify_l402_token(valid_token(), DIGEST) == (PAYMENT_HASH, 21)


def test_verify_token_rejects_wrong_digest():
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(valid_token(), OTHER_DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_expired():
    token = valid_token(expiry_ts=int(time.time()) - 10)
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


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


# ══ 6. Paid retry path ═════════════════════════════════════════════════════════

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


def test_wrong_preimage_rejected_before_lnd_lookup():
    token = valid_token()
    mock_get = MagicMock()
    with patch("main.requests.get", mock_get):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"L402 {token}:{WRONG_PREIMAGE}"},
        )
    assert resp.status_code == 401
    mock_get.assert_not_called()  # preimage check happens before any LND call


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


# ══ 7. LND invoice lookup / payment verification ═══════════════════════════════

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
    # the static price says 21 is enough.
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST, 30) is False
    # Face amount 900 against a token minted at 900: settles even if the
    # static price has since moved — the binding is to the mint-time amount
    # alone.
    with patch("main.GATEWAY_PRICE_SATS", 21):
        with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 900)):
            assert main.verify_payment(PAYMENT_HASH, DIGEST, 900) is True


def test_h3_liquidity_fee_netted_receive_still_verifies(caplog):
    # H3 regression: phoenixd nets an ACINQ liquidity fee off the credited
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


def test_lnd_overpaid_underminted_invoice_rejected():
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
    # Challenge quoted 900; by redemption the live price is back at 21. The
    # invoice paid in full at its mint-time amount must still redeem.
    token = valid_token(price=900)
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 900)):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS


def test_h3_liquidity_fee_payment_yields_obligation_and_proof(obligations_db):
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
    assert ei.value.detail == "LND error: could not verify payment"
    assert "boom-internal-detail" not in ei.value.detail
    assert any("LND invoice lookup failed" in r.message for r in caplog.records)


# ══ 8. create_invoice() wiring ═════════════════════════════════════════════════

def test_create_invoice_returns_tuple_and_decodes_rhash_to_hex():
    ph = "ab" * 32
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"payment_request": "lnbc...", "r_hash": base64.b64encode(bytes.fromhex(ph)).decode()}

    def fake_post(url, headers=None, json=None, proxies=None, verify=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, proxies=proxies, verify=verify)
        return FakeResponse()

    with patch("main.requests.post", fake_post):
        payment_request, payment_hash = main.create_invoice(DIGEST, 21)

    assert payment_request == "lnbc..."
    assert payment_hash == ph and len(payment_hash) == 64
    assert captured["url"].endswith("/v1/invoices")
    assert captured["headers"]["Grpc-Metadata-macaroon"] == main.LND_MACAROON_HEX
    assert captured["verify"] == main.LND_TLS_VERIFY
    assert captured["proxies"] == {"https": f"socks5h://{main.TOR_PROXY}"}  # TOR_PROXY respected
    assert captured["json"]["memo"] == DIGEST
    assert captured["json"]["value"] == 21
    assert captured["json"]["private"] is True


def test_create_invoice_lnd_failure_raises_generic_502_and_logs(caplog):
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("creation-internal-detail")
    with patch("main.requests.post", return_value=m):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as ei:
                main.create_invoice(DIGEST, 21)
    assert ei.value.status_code == 502
    assert ei.value.detail == "LND error: could not create invoice"
    assert "creation-internal-detail" not in ei.value.detail
    assert any("LND invoice creation failed" in r.message for r in caplog.records)


# ══ 9. OTS backend modes ═══════════════════════════════════════════════════════

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


def test_public_mode_submits_to_all_default_aggregators():
    instance = MagicMock()
    instance.submit.return_value = _good_calendar_ts()
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch("main.RemoteCalendar", return_value=instance) as MockCalendar:
            result = main.stamp_digest(DIGEST)
    assert isinstance(result, bytes) and len(result) > 0
    assert MockCalendar.call_count == len(DEFAULT_AGGREGATORS)


def test_public_mode_succeeds_if_at_least_one_aggregator_responds():
    fail = MagicMock()
    fail.submit.side_effect = ConnectionError("unreachable")
    ok = MagicMock()
    ok.submit.return_value = _good_calendar_ts()
    instances = [fail] + [ok] * (len(DEFAULT_AGGREGATORS) - 1)
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch("main.RemoteCalendar", side_effect=instances):
            result = main.stamp_digest(DIGEST)
    assert isinstance(result, bytes) and len(result) > 0


def test_public_mode_fails_only_if_all_aggregators_fail():
    token = valid_token()
    instance = MagicMock()
    instance.submit.side_effect = ConnectionError("unreachable")
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch("main.requests.get", return_value=_settled_get()):
            with patch("main.RemoteCalendar", return_value=instance):
                resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OTS error: stamping failed"


# ══ 10. Health endpoint ════════════════════════════════════════════════════════

def test_health_both_ok_returns_200():
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "paused": False,
        "payment": "ok",
        "payment_backend": main.PAYMENT_BACKEND_TYPE,
        "otsd": "ok",
        "wallet": "absent",
        "proofs": "absent",
        "backup": "absent",
    }


def test_health_lnd_down_returns_503():
    with patch("main.requests.get", side_effect=[_fail(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["payment"] == "error"


def test_health_otsd_down_in_calendar_mode_returns_503():
    with patch("main.requests.get", side_effect=[_ok_lnd(), _fail()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["otsd"] == "error"


def test_health_otsd_bitcoin_blind_empty_200_returns_503(caplog):
    # otsd commits its 200 before any Bitcoin call, so a dead Bitcoin RPC
    # yields an empty 200 body. That must read as error, not ok — and the
    # log line must name the condition.
    with patch("main.requests.get", side_effect=[_ok_lnd(), _blind_otsd(b"")]):
        with caplog.at_level(logging.WARNING):
            resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["otsd"] == "error"
    assert any("Bitcoin-blind" in r.message for r in caplog.records)


def test_health_otsd_markerless_200_returns_503():
    # A rendered body without the Best-block marker (template drift, partial
    # render) fails loud rather than passing as healthy.
    with patch("main.requests.get",
               side_effect=[_ok_lnd(), _blind_otsd(b"<html>calendar</html>")]):
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["otsd"] == "error"


def test_health_otsd_na_in_public_mode():
    with patch("main.OTS_CALENDAR_URL", None):
        with patch("main.requests.get", return_value=_ok_lnd()):
            resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "paused": False,
        "payment": "ok",
        "payment_backend": main.PAYMENT_BACKEND_TYPE,
        "otsd": "n/a",
        "wallet": "absent",
        "proofs": "absent",
        "backup": "absent",
    }


def test_health_uses_readonly_macaroon_when_set():
    readonly = "cd" * 32
    with patch("main.LND_READONLY_MACAROON_HEX", readonly):
        with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]) as mock_get:
            resp = client.get("/health")
    assert resp.status_code == 200
    assert mock_get.call_args_list[0].kwargs["headers"]["Grpc-Metadata-macaroon"] == readonly


def test_health_falls_back_to_invoice_macaroon_when_readonly_absent():
    with patch("main.LND_READONLY_MACAROON_HEX", None):
        with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]) as mock_get:
            resp = client.get("/health")
    assert resp.status_code == 200
    assert mock_get.call_args_list[0].kwargs["headers"]["Grpc-Metadata-macaroon"] == main.LND_MACAROON_HEX


def test_health_never_raises():
    with patch("main.requests.get", side_effect=RuntimeError("unexpected crash")):
        resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.json()["payment"] == "error"


def test_health_otsd_probe_timeout_pairs_with_fork_homepage():
    """The otsd probe's read leg races otsd's FULL homepage render (headers
    commit instantly; the body arrives in one write after ~4 Bitcoin RPCs over
    Tor, each allowed a 30s stall by the fork's make_proxy(timeout=30)), so a
    single-stall render can honestly take ~34s. A plain timeout=5 marked ~11%
    of honest renders red (2026-07-17). Pin (5, 45) so neither side of the
    pair moves alone."""
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]) as mock_get:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert mock_get.call_args_list[1].kwargs["timeout"] == (5, 45)


# ══ 11. Error discipline (cross-cutting) ═══════════════════════════════════════

def test_lnd_create_error_detail_is_generic_no_leak():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("secret-lnd-trace")
    with patch("main.requests.post", return_value=m):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not create invoice"
    assert "secret-lnd-trace" not in resp.text


def test_malformed_auth_uses_401():
    resp = client.post("/timestamp", json={"digest": DIGEST}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401


def test_unsettled_uses_402_not_error():
    token = valid_token()
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 0)):
        resp = client.post("/timestamp", json={"digest": DIGEST}, headers=auth(token))
    assert resp.status_code == 402


# ── /verify endpoint ─────────────────────────────────────────────────────────

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


def test_verify_bitcoin_attestation_returns_anchored_status():
    ots_b64 = base64.b64encode(
        make_detached_ots_bytes(attestation=main.BitcoinBlockHeaderAttestation(954112))
    ).decode()
    resp = client.post("/verify", json={"digest": DIGEST, "ots": ots_b64})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "anchored"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is True
    assert body["verified"] is True
    assert body["attestations"] == [
        {
            "type": "bitcoin",
            "block_height": 954112,
            "mempool_block_height_url": "https://mempool.space/block-height/954112",
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


def test_upgrade_already_anchored_no_calendar_contact():
    ots_b64 = base64.b64encode(
        make_detached_ots_bytes(
            attestation=main.BitcoinBlockHeaderAttestation(954112)
        )
    ).decode()
    with patch("main.RemoteCalendar") as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "anchored"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is True
    assert body["verified"] is True
    assert body["ots"] == ots_b64
    mock_calendar.assert_not_called()


def test_upgrade_pending_no_calendar_upgrade_returns_pending():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    instance = MagicMock()
    instance.get_timestamp.side_effect = Exception("commitment not found")
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


def test_upgrade_pending_calendar_returns_bitcoin_anchored():
    ots_b64 = base64.b64encode(make_detached_ots_bytes()).decode()
    upgraded_ts = main.Timestamp(bytes.fromhex(DIGEST))
    upgraded_ts.attestations.add(main.BitcoinBlockHeaderAttestation(954112))
    instance = MagicMock()
    instance.get_timestamp.return_value = upgraded_ts
    with patch("main.RemoteCalendar", return_value=instance) as mock_calendar:
        resp = client.post("/upgrade", json={"digest": DIGEST, "ots": ots_b64})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "anchored"
    assert body["valid_ots"] is True
    assert body["digest_match"] is True
    assert body["bitcoin_anchored"] is True
    assert body["verified"] is True
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
    instance.get_timestamp.side_effect = Exception("commitment not found")
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


# PAYMENT BACKEND TESTS

def test_payment_backend_default_is_phoenixd():
    """With PAYMENT_BACKEND_TYPE unset, the default is phoenixd (the live
    backend) — and a bare config must parse without any LND vars."""
    env = {
        "GATEWAY_PRICE_SATS": "500",
        "OTS_BACKEND_MODE": "calendar",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
    }
    with patch.dict(os.environ, env, clear=True):
        result = main._parse_config()
    assert result.payment_backend_type == "phoenixd"


def test_payment_backend_env_var_selects_lnd():
    """The suite runs with PAYMENT_BACKEND_TYPE=lnd set explicitly (LND is the
    test payer; all payment mocks are LND REST). Explicit selection must win."""
    assert main.PAYMENT_BACKEND_TYPE == "lnd"
    assert isinstance(main.PAYMENT_BACKEND, main.LndPaymentBackend)


def test_make_payment_backend_lnd():
    backend = main._make_payment_backend("lnd")
    assert isinstance(backend, main.LndPaymentBackend)


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


def test_phoenixd_backend_does_not_require_lnd_vars():
    """PAYMENT_BACKEND_TYPE=phoenixd must not require LND_HOST/PORT/MACAROON."""
    env = {
        "PAYMENT_BACKEND_TYPE": "phoenixd",
        "GATEWAY_PRICE_SATS": "500",
        "MIN_GATEWAY_PRICE_SATS": "500",
        "OTS_BACKEND_MODE": "calendar",
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
        "GATEWAY_PRICE_SATS": "500",
        "MIN_GATEWAY_PRICE_SATS": "500",
        "OTS_BACKEND_MODE": "calendar",
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
    assert status.expired is False
    assert not hasattr(status, "preimage")
    assert not hasattr(status, "invoice")
    assert not hasattr(status, "amount_paid_sat")  # the conflated field is gone
    assert get.call_args.args[0].endswith("/payments/incoming/" + PAYMENT_HASH)


def test_phoenixd_lookup_invoice_expired_none_when_absent():
    backend = main.PhoenixdPaymentBackend()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "isPaid": False,
        "requestedSat": 21,
        "receivedSat": 0,
        "description": DIGEST,
    }
    with patch("main.requests.get", return_value=resp):
        status = backend.lookup_invoice(PAYMENT_HASH)
    assert status.expired is None


def test_phoenixd_health_success_and_failure():
    backend = main.PhoenixdPaymentBackend()
    ok = MagicMock()
    ok.raise_for_status.return_value = None
    with patch("main.requests.get", return_value=ok):
        assert backend.health() is True
    with patch("main.requests.get", side_effect=Exception("boom")):
        assert backend.health() is False


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


def test_gateway_price_below_minimum_fails():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "20", "MIN_GATEWAY_PRICE_SATS": "21"}):
        with pytest.raises(RuntimeError, match="GATEWAY_PRICE_SATS must be >= MIN_GATEWAY_PRICE_SATS"):
            main._parse_config()


def test_non_integer_min_gateway_price_fails():
    with patch.dict(os.environ, {"MIN_GATEWAY_PRICE_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="MIN_GATEWAY_PRICE_SATS must be an integer"):
            main._parse_config()


def test_paused_health_returns_503(tmp_path):
    pause_file = tmp_path / "PAUSED"
    pause_file.write_text("paused\n")

    with patch("main.PAUSE_FILE", str(pause_file)):
        with patch.object(main.PAYMENT_BACKEND, "health", return_value=True), patch("requests.get", return_value=_ok_otsd()):
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


# ══ 12. Durable obligation log ═════════════════════════════════════════════════
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


# ══ 13. Wallet liquidity alarm (/health wallet field) ═══════════════════════════
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
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["wallet"] == "ok"


def test_health_wallet_low_returns_503(wallet_status_file):
    _write_wallet_status(wallet_status_file, status="low", balance_sats=1000)
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "low"


def test_health_wallet_stale_returns_503(wallet_status_file):
    """An 'ok' file older than WALLET_STATUS_MAX_AGE_SECONDS means the timer
    itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.WALLET_STATUS_MAX_AGE_SECONDS - 60
    _write_wallet_status(wallet_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "stale"


def test_health_wallet_absent_reports_but_stays_healthy(wallet_status_file):
    """No status file = alarm not installed. Reported, but operators without
    the calendar profile must not fail health."""
    assert not wallet_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["wallet"] == "absent"


def test_health_wallet_malformed_returns_503_unknown(wallet_status_file):
    wallet_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["wallet"] == "unknown"


# ══ 14. Proof sweep status (/health proofs field) ════════════════════════════════
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
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["proofs"] == "ok"


def test_health_proofs_mismatch_returns_503(proofs_status_file):
    """attestation_mismatch is the signal the sweeper repair exists for — the
    calendar holds an attestation an artifact lacks. It must degrade health."""
    _write_proofs_status(proofs_status_file, status="mismatch")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "mismatch"


def test_health_proofs_attention_returns_503(proofs_status_file):
    _write_proofs_status(proofs_status_file, status="attention")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "attention"


def test_health_proofs_stale_returns_503(proofs_status_file):
    """An 'ok' file older than PROOFS_STATUS_MAX_AGE_SECONDS means the sweep
    timer itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.PROOFS_STATUS_MAX_AGE_SECONDS - 60
    _write_proofs_status(proofs_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["proofs"] == "stale"


def test_health_proofs_absent_reports_but_stays_healthy(proofs_status_file):
    """No status file = sweep not installed. Reported, but operators without
    the calendar profile must not fail health."""
    assert not proofs_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["proofs"] == "absent"


def test_health_proofs_malformed_returns_503_unknown(proofs_status_file):
    proofs_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
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
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["backup"] == "ok"


def test_health_backup_local_only_reports_but_stays_healthy(backup_status_file):
    """local_only is a configuration choice (no BACKUP_REMOTE/recipient set),
    honestly reported — it must not degrade health."""
    _write_backup_status(backup_status_file, status="local_only")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok" and body["backup"] == "local_only"


def test_health_backup_attention_returns_503(backup_status_file):
    _write_backup_status(backup_status_file, status="attention")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "attention"


def test_health_backup_failed_returns_503(backup_status_file):
    _write_backup_status(backup_status_file, status="failed")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "failed"


def test_health_backup_stale_returns_503(backup_status_file):
    """An 'ok' file older than BACKUP_STATUS_MAX_AGE_SECONDS means the backup
    timer itself died — that must degrade health, not pass as ok."""
    old = int(time.time()) - main.BACKUP_STATUS_MAX_AGE_SECONDS - 60
    _write_backup_status(backup_status_file, status="ok", checked_at=old)
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "stale"


def test_health_backup_absent_reports_but_stays_healthy(backup_status_file):
    """No status file = backups not installed. Reported, but boxes without the
    backup timer must not fail health."""
    assert not backup_status_file.exists()
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["backup"] == "absent"


def test_health_backup_malformed_returns_503_unknown(backup_status_file):
    backup_status_file.write_text("not json{{{")
    with patch("main.requests.get", side_effect=[_ok_lnd(), _ok_otsd()]):
        resp = client.get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded" and body["backup"] == "unknown"


# ══ 15. Pricing solvency floor ═══════════════════════════════════════════════════

PRICE_TEST_URL = "http://feeuser:feepass@127.0.0.1:18332/"


def _feerate_post_mock(result):
    """Mock Bitcoin JSON-RPC estimatesmartfee response."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"result": result, "error": None, "id": "gateway-price-floor"}
    return m


@pytest.fixture
def fresh_feerate_cache():
    main._feerate_cache = None
    yield
    main._feerate_cache = None


# ── Config validation ──


def test_price_rpc_url_falls_back_to_bitcoin_rpc_service_url():
    env = {"PRICE_RPC_URL": "", "BITCOIN_RPC_SERVICE_URL": PRICE_TEST_URL}
    with patch.dict(os.environ, env):
        assert main._parse_config().price_rpc_url == PRICE_TEST_URL


def test_price_rpc_url_wins_over_fallback():
    env = {"PRICE_RPC_URL": PRICE_TEST_URL, "BITCOIN_RPC_SERVICE_URL": "http://other:18332/"}
    with patch.dict(os.environ, env):
        assert main._parse_config().price_rpc_url == PRICE_TEST_URL


def test_price_rpc_url_unset_is_none_and_boots():
    with patch.dict(os.environ, {"PRICE_RPC_URL": "", "BITCOIN_RPC_SERVICE_URL": ""}):
        assert main._parse_config().price_rpc_url is None


def test_price_floor_defaults():
    cfg = main._parse_config()
    assert cfg.price_tx_vsize_estimate == 150
    assert cfg.price_bump_reserve == 1.5
    assert cfg.price_margin == 5.0
    assert cfg.price_conf_target == 6


def test_non_integer_price_tx_vsize_fails():
    with patch.dict(os.environ, {"PRICE_TX_VSIZE_ESTIMATE": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_TX_VSIZE_ESTIMATE must be an integer"):
            main._parse_config()


def test_zero_price_tx_vsize_fails():
    with patch.dict(os.environ, {"PRICE_TX_VSIZE_ESTIMATE": "0"}):
        with pytest.raises(RuntimeError, match="PRICE_TX_VSIZE_ESTIMATE must be a positive"):
            main._parse_config()


def test_non_numeric_price_bump_reserve_fails():
    with patch.dict(os.environ, {"PRICE_BUMP_RESERVE": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_BUMP_RESERVE must be a number"):
            main._parse_config()


def test_sub_one_price_bump_reserve_fails():
    with patch.dict(os.environ, {"PRICE_BUMP_RESERVE": "0.9"}):
        with pytest.raises(RuntimeError, match="PRICE_BUMP_RESERVE must be >= 1"):
            main._parse_config()


def test_non_numeric_price_margin_fails():
    with patch.dict(os.environ, {"PRICE_MARGIN": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_MARGIN must be a number"):
            main._parse_config()


def test_sub_one_price_margin_fails():
    with patch.dict(os.environ, {"PRICE_MARGIN": "0.5"}):
        with pytest.raises(RuntimeError, match="PRICE_MARGIN must be >= 1"):
            main._parse_config()


def test_non_integer_price_conf_target_fails():
    with patch.dict(os.environ, {"PRICE_CONF_TARGET": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_CONF_TARGET must be an integer"):
            main._parse_config()


def test_out_of_range_price_conf_target_fails():
    for bad in ("0", "1009"):
        with patch.dict(os.environ, {"PRICE_CONF_TARGET": bad}):
            with pytest.raises(RuntimeError, match="1-1008"):
                main._parse_config()


# ── BTC/kvB → sat/vB conversion (explicit, per spec) ──


def test_btc_per_kvb_to_sat_per_vb_conversion():
    # 0.00001 BTC/kvB = 1000 sat / 1000 vB = exactly 1 sat/vB. Exact equality
    # is deliberate: binary-float noise here would leak through ceil() and
    # inflate the quoted floor by a sat.
    assert main._btc_per_kvb_to_sat_per_vb(0.00001) == 1.0
    assert main._btc_per_kvb_to_sat_per_vb(0.0001) == 10.0
    assert main._btc_per_kvb_to_sat_per_vb(0.00012345) == 12.345


def test_conversion_noise_does_not_inflate_floor(fresh_feerate_cache):
    # A 1 sat/vB estimate must price ceil(150 x 1.0 x 1.5 x 5) = 1125, not the
    # 1126 that raw float conversion (1.0000000000000002 sat/vB) would give.
    main._feerate_cache = (main._btc_per_kvb_to_sat_per_vb(0.00001), time.monotonic())
    assert main.quoted_price_sats() == 1125


# ── Feerate fetch: success and every fallback path ──


def test_fetch_feerate_success_calls_rpc_and_converts(fresh_feerate_cache):
    mock_post = MagicMock(return_value=_feerate_post_mock({"feerate": 0.0001, "blocks": 6}))
    with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
        with patch("main.requests.post", mock_post):
            assert main._fetch_feerate_sat_per_vb() == 10.0
    args, kwargs = mock_post.call_args
    assert args[0] == PRICE_TEST_URL
    assert kwargs["json"]["method"] == "estimatesmartfee"
    assert kwargs["json"]["params"] == [main.PRICE_CONF_TARGET]
    assert kwargs["timeout"] == 5  # hard timeout: Tor path can hang for seconds


def test_fetch_feerate_none_without_url_warns(caplog):
    with patch("main.PRICE_RPC_URL", None):
        with caplog.at_level(logging.WARNING):
            assert main._fetch_feerate_sat_per_vb() is None
    assert any("quoting the static price" in r.message for r in caplog.records)


def test_fetch_feerate_timeout_falls_back_and_never_logs_credentials(caplog):
    # The exception message deliberately echoes the URL — the log must not.
    boom = main.requests.exceptions.ConnectionError(f"{PRICE_TEST_URL} refused")
    with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
        with patch("main.requests.post", side_effect=boom):
            with caplog.at_level(logging.WARNING):
                assert main._fetch_feerate_sat_per_vb() is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("estimatesmartfee failed (ConnectionError)" in m for m in messages)
    assert all("feepass" not in m for m in messages)


def test_fetch_feerate_no_estimate_falls_back(caplog):
    # Fresh node: estimatesmartfee returns errors and no feerate key.
    mock_post = MagicMock(
        return_value=_feerate_post_mock({"errors": ["Insufficient data"], "blocks": 0})
    )
    with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
        with patch("main.requests.post", mock_post):
            with caplog.at_level(logging.WARNING):
                assert main._fetch_feerate_sat_per_vb() is None
    assert any("no feerate estimate" in r.message for r in caplog.records)


# ── Cache: one fetch per TTL, failures cached too ──


def test_feerate_cache_serves_within_ttl_and_refetches_after(fresh_feerate_cache):
    mock_post = MagicMock(return_value=_feerate_post_mock({"feerate": 0.0001, "blocks": 6}))
    with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
        with patch("main.requests.post", mock_post):
            assert main._cached_feerate_sat_per_vb() == 10.0
            assert main._cached_feerate_sat_per_vb() == 10.0
            assert mock_post.call_count == 1
            # Age the entry past the TTL; the next call must refetch.
            main._feerate_cache = (10.0, time.monotonic() - 61)
            assert main._cached_feerate_sat_per_vb() == 10.0
            assert mock_post.call_count == 2


def test_feerate_cache_caches_failures_too(fresh_feerate_cache):
    mock_post = MagicMock(side_effect=main.requests.exceptions.Timeout())
    with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
        with patch("main.requests.post", mock_post):
            assert main._cached_feerate_sat_per_vb() is None
            assert main._cached_feerate_sat_per_vb() is None
    assert mock_post.call_count == 1  # a dead node is hit once per TTL, not per request


# ── Quote: max(static, floor), ceil, fallback ──


def test_quoted_price_uses_floor_when_above_static(fresh_feerate_cache):
    # ceil(150 vB x 10 sat/vB x 1.5 x 5) = 11250 > static 21.
    main._feerate_cache = (10.0, time.monotonic())
    assert main.quoted_price_sats() == 11250


def test_quoted_price_stays_static_when_floor_below(fresh_feerate_cache):
    # ceil(150 x 0.001 x 1.5 x 5) = 2 < static 21.
    main._feerate_cache = (0.001, time.monotonic())
    assert main.quoted_price_sats() == 21


def test_quoted_price_floor_rounds_up(fresh_feerate_cache):
    # 150 x 1.234 x 1.5 x 5 = 1388.25 → ceil → 1389, never rounded down.
    main._feerate_cache = (1.234, time.monotonic())
    assert main.quoted_price_sats() == 1389


def test_quoted_price_blind_floor_on_feerate_fallback(fresh_feerate_cache):
    # Was test_quoted_price_static_on_feerate_fallback: blind now quotes
    # max(static, PRICE_BLIND_SATS), never the bare static price.
    main._feerate_cache = (None, time.monotonic())
    with patch("main.PRICE_BLIND_SATS", 5000):
        assert main.quoted_price_sats() == 5000


def test_blind_price_when_rpc_url_unset(fresh_feerate_cache):
    with patch("main.PRICE_BLIND_SATS", 5000):
        with patch("main.PRICE_RPC_URL", None):
            assert main.quoted_price_sats() == 5000


def test_blind_price_on_rpc_timeout(fresh_feerate_cache):
    with patch("main.PRICE_BLIND_SATS", 5000):
        with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
            with patch("main.requests.post", MagicMock(side_effect=main.requests.exceptions.Timeout())):
                assert main.quoted_price_sats() == 5000


def test_blind_price_on_rpc_error(fresh_feerate_cache):
    with patch("main.PRICE_BLIND_SATS", 5000):
        with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
            with patch("main.requests.post", MagicMock(side_effect=main.requests.exceptions.ConnectionError())):
                assert main.quoted_price_sats() == 5000


def test_blind_price_when_node_has_no_estimate(fresh_feerate_cache):
    no_estimate = _feerate_post_mock({"errors": ["Insufficient data or no feerate found"], "blocks": 0})
    with patch("main.PRICE_BLIND_SATS", 5000):
        with patch("main.PRICE_RPC_URL", PRICE_TEST_URL):
            with patch("main.requests.post", MagicMock(return_value=no_estimate)):
                assert main.quoted_price_sats() == 5000


def test_blind_floor_below_static_keeps_static(fresh_feerate_cache):
    # max() holds: a blind floor below the static price never lowers the quote.
    main._feerate_cache = (None, time.monotonic())
    with patch("main.PRICE_BLIND_SATS", 5):
        assert main.quoted_price_sats() == 21


def test_price_blind_sats_default_is_5000():
    with patch.dict(os.environ):
        os.environ.pop("PRICE_BLIND_SATS", None)
        assert main._parse_config().price_blind_sats == 5000


def test_non_integer_price_blind_sats_fails():
    with patch.dict(os.environ, {"PRICE_BLIND_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="PRICE_BLIND_SATS must be an integer"):
            main._parse_config()


def test_zero_price_blind_sats_fails():
    with patch.dict(os.environ, {"PRICE_BLIND_SATS": "0"}):
        with pytest.raises(RuntimeError, match="PRICE_BLIND_SATS must be a positive"):
            main._parse_config()


def test_negative_price_blind_sats_fails():
    with patch.dict(os.environ, {"PRICE_BLIND_SATS": "-5"}):
        with pytest.raises(RuntimeError, match="PRICE_BLIND_SATS must be a positive"):
            main._parse_config()


def test_token_minted_at_blind_price_validates_at_blind_price(fresh_feerate_cache):
    # Mint-time binding invariant, blind edition: a token minted while blind
    # carries the blind price and validates at it even after the market
    # becomes visible again and the floor moves.
    main._feerate_cache = (None, time.monotonic())
    with patch("main.PRICE_BLIND_SATS", 5000):
        price = main.quoted_price_sats()
        assert price == 5000
        token = valid_token(price=price)
    main._feerate_cache = (10.0, time.monotonic())  # market back; floor now 11250
    assert main.verify_l402_token(token, DIGEST) == (PAYMENT_HASH, 5000)


# ══ 16. Invoice-mint rate limit ══════════════════════════════════════════════════

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
