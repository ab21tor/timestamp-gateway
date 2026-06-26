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
os.environ["PAYMENT_BACKEND_TYPE"] = "lnd"
os.environ["OTS_BACKEND_MODE"] = "calendar"
os.environ["OTS_CALENDAR_URL"] = "http://test-calendar:14788"
os.environ["L402_SECRET_HEX"] = "ab" * 32          # stable, known signing key
os.environ["L402_TOKEN_EXPIRY_SECONDS"] = "3600"
os.environ["OTS_SUBMIT_BACKOFF_SECONDS"] = "0"     # keep retry tests fast

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from pymacaroons import Macaroon  # noqa: E402
from opentimestamps.calendar import DEFAULT_AGGREGATORS  # noqa: E402
from opentimestamps.core.notary import PendingAttestation  # noqa: E402
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
    m.add_first_party_caveat(f"price={price}")
    m.add_first_party_caveat(f"capability={capability}")
    m.add_first_party_caveat(f"expiry={expiry_ts}")
    return m.serialize()


def auth(token, preimage=PREIMAGE):
    return {"Authorization": f"L402 {token}:{preimage}"}


def _get_mock(settled, memo, amt_paid_sat):
    """Mock LND invoice-lookup response (verify_payment)."""
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"settled": settled, "memo": memo, "amt_paid_sat": str(amt_paid_sat)}
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
    return m


def _fail():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("connection refused")
    return m


# ══ 1. Startup / config validation ════════════════════════════════════════════

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


def test_402_json_body_has_status_invoice_macaroon_expiry():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    body = resp.json()["detail"]
    assert body["status"] == "payment_required"
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


def test_minted_token_verifies_and_is_bound_to_digest_and_price():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    token = resp.json()["detail"]["macaroon"]
    # Bound to this digest: verifies and returns the payment hash.
    assert main.verify_l402_token(token, DIGEST) == PAYMENT_HASH
    # Bound to the configured price: fails if the gateway price changes.
    with patch("main.GATEWAY_PRICE_SATS", 99):
        with pytest.raises(HTTPException) as ei:
            main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


# ══ 5. L402 token verification ═════════════════════════════════════════════════

def test_verify_token_valid_same_digest_returns_payment_hash():
    assert main.verify_l402_token(valid_token(), DIGEST) == PAYMENT_HASH


def test_verify_token_rejects_wrong_digest():
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(valid_token(), OTHER_DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_expired():
    token = valid_token(expiry_ts=int(time.time()) - 10)
    with pytest.raises(HTTPException) as ei:
        main.verify_l402_token(token, DIGEST)
    assert ei.value.status_code == 401


def test_verify_token_rejects_wrong_price():
    token = build_macaroon(price=99)
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
        assert main.verify_payment(PAYMENT_HASH, DIGEST) is True


def test_verify_payment_false_when_unsettled():
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST) is False


def test_verify_payment_false_when_wrong_memo():
    with patch("main.requests.get", return_value=_get_mock(True, "0" * 64, 21)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST) is False


def test_verify_payment_false_when_underpaid():
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 5)):
        assert main.verify_payment(PAYMENT_HASH, DIGEST) is False


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


def test_verify_payment_lookup_failure_raises_generic_502_and_logs(caplog):
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("boom-internal-detail")
    with patch("main.requests.get", return_value=m):
        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as ei:
                main.verify_payment(PAYMENT_HASH, DIGEST)
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

def test_payment_backend_default_is_lnd():
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


def test_phoenixd_backend_does_not_require_lnd_vars():
    """PAYMENT_BACKEND_TYPE=phoenixd must not require LND_HOST/PORT/MACAROON."""
    env = {
        "PAYMENT_BACKEND_TYPE": "phoenixd",
        "GATEWAY_PRICE_SATS": "500",
        "MIN_GATEWAY_PRICE_SATS": "500",
        "OTS_BACKEND_MODE": "calendar",
        "OTS_CALENDAR_URL": "http://127.0.0.1:14788",
        "L402_SECRET_HEX": "ab" * 16,
        "PHOENIXD_HTTP_PASSWORD": "testpassword",
    }
    with patch.dict(os.environ, env, clear=True):
        result = main._parse_config()
    assert result is not None


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
        "receivedSat": 21,
        "description": DIGEST,
        "isExpired": False,
        "preimage": "ff" * 32,
        "invoice": FAKE_INVOICE,
    }
    with patch("main.requests.get", return_value=resp) as get:
        status = backend.lookup_invoice(PAYMENT_HASH)
    assert status.settled is True
    assert status.amount_paid_sat == 21
    assert status.memo == DIGEST
    assert status.expired is False
    assert not hasattr(status, "preimage")
    assert not hasattr(status, "invoice")
    assert get.call_args.args[0].endswith("/payments/incoming/" + PAYMENT_HASH)


def test_phoenixd_lookup_invoice_expired_none_when_absent():
    backend = main.PhoenixdPaymentBackend()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "isPaid": False,
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
    with patch("main.PHOENIXD_HTTP_PASSWORD", secret):
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
