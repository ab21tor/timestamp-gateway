import os
from unittest.mock import MagicMock, patch

import pytest

# Set env vars before importing main so module-level validation passes.
# load_dotenv() does not override existing env vars, so these take precedence.
os.environ["LND_HOST"] = "test.onion"
os.environ["LND_PORT"] = "8080"
os.environ["LND_MACAROON_HEX"] = "deadbeef" * 8
os.environ["TOR_PROXY"] = "127.0.0.1:9050"
os.environ["GATEWAY_PRICE_SATS"] = "21"

from fastapi.testclient import TestClient  # noqa: E402
from main import app, _parse_config, stamp_digest  # noqa: E402
from opentimestamps.core.notary import PendingAttestation  # noqa: E402
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

DIGEST = "a" * 64          # valid 64-char lowercase hex
PREIMAGE = "b" * 64        # valid 64-char hex preimage
FAKE_INVOICE = "lnbc210n1pfakeinvoicefortesting"
FAKE_OTS = b"\x00\x01\x02\x03opentimestamps"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_mock(settled, memo, amt_paid_sat):
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"settled": settled, "memo": memo, "amt_paid_sat": str(amt_paid_sat)}
    return m


def _post_mock():
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"payment_request": FAKE_INVOICE}
    return m


def _settled_get():
    return _get_mock(True, DIGEST, 21)


# ── 1. Input validation ───────────────────────────────────────────────────────

def test_valid_digest_no_auth_returns_402_with_invoice():
    with patch("main.requests.post", return_value=_post_mock()):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 402
    body = resp.json()
    assert body["detail"]["status"] == "payment_required"
    assert body["detail"]["invoice"] == FAKE_INVOICE


def test_invalid_digest_too_short_returns_422():
    resp = client.post("/timestamp", json={"digest": "abc123"})
    assert resp.status_code == 422


def test_invalid_digest_non_hex_returns_422():
    resp = client.post("/timestamp", json={"digest": "g" * 64})
    assert resp.status_code == 422


def test_digest_normalized_to_lowercase_in_memo():
    with patch("main.requests.post", return_value=_post_mock()) as patched:
        resp = client.post("/timestamp", json={"digest": "A" * 64})
    assert resp.status_code == 402
    assert patched.call_args.kwargs["json"]["memo"] == "a" * 64


# ── 2. Startup config validation ──────────────────────────────────────────────

def test_non_integer_gateway_price_fails_at_startup():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "abc"}):
        with pytest.raises(RuntimeError, match="GATEWAY_PRICE_SATS must be an integer"):
            _parse_config()


def test_zero_gateway_price_fails_at_startup():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "0"}):
        with pytest.raises(RuntimeError, match="positive integer"):
            _parse_config()


def test_negative_gateway_price_fails_at_startup():
    with patch.dict(os.environ, {"GATEWAY_PRICE_SATS": "-5"}):
        with pytest.raises(RuntimeError, match="positive integer"):
            _parse_config()


# ── 3. Authorization header handling ─────────────────────────────────────────

def test_malformed_authorization_returns_401():
    resp = client.post(
        "/timestamp",
        json={"digest": DIGEST},
        headers={"Authorization": "Bearer not-a-preimage"},
    )
    assert resp.status_code == 401


def test_bearer_token_auth_returns_401():
    resp = client.post(
        "/timestamp",
        json={"digest": DIGEST},
        headers={"Authorization": "Bearer " + "a" * 64},
    )
    assert resp.status_code == 401


def test_arbitrary_non_preimage_auth_returns_401():
    resp = client.post(
        "/timestamp",
        json={"digest": DIGEST},
        headers={"Authorization": "token=abc"},
    )
    assert resp.status_code == 401


def test_uppercase_preimage_hex_is_accepted():
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": "preimage=" + PREIMAGE.upper()},
            )
    assert resp.status_code == 200


# ── 4. Payment verification ───────────────────────────────────────────────────

def test_valid_preimage_unpaid_returns_402():
    with patch("main.requests.get", return_value=_get_mock(False, DIGEST, 0)):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 402
    assert "invoice" not in resp.json().get("detail", "")


def test_settled_wrong_memo_returns_402():
    with patch("main.requests.get", return_value=_get_mock(True, "0" * 64, 21)):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 402


def test_settled_correct_memo_underpaid_returns_402():
    with patch("main.requests.get", return_value=_get_mock(True, DIGEST, 5)):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 402


# ── 5. LND errors ─────────────────────────────────────────────────────────────

def test_lnd_invoice_creation_http_error_returns_502():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("503 Service Unavailable")
    with patch("main.requests.post", return_value=m):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not create invoice"


def test_lnd_invoice_creation_missing_payment_request_returns_502():
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {}  # no payment_request key → KeyError → 502
    with patch("main.requests.post", return_value=m):
        resp = client.post("/timestamp", json={"digest": DIGEST})
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not create invoice"


def test_lnd_lookup_http_error_returns_502():
    m = MagicMock()
    m.raise_for_status.side_effect = Exception("503 Service Unavailable")
    with patch("main.requests.get", return_value=m):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not verify payment"


def test_lnd_lookup_malformed_json_returns_502():
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.side_effect = ValueError("not valid json")
    with patch("main.requests.get", return_value=m):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not verify payment"


def test_lnd_lookup_non_integer_amt_paid_sat_returns_502():
    m = MagicMock()
    m.raise_for_status.return_value = None
    m.json.return_value = {"settled": True, "memo": DIGEST, "amt_paid_sat": "notanumber"}
    with patch("main.requests.get", return_value=m):
        resp = client.post(
            "/timestamp",
            json={"digest": DIGEST},
            headers={"Authorization": f"preimage={PREIMAGE}"},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "LND error: could not verify payment"


# ── 6. OTS ────────────────────────────────────────────────────────────────────

def test_ots_all_calendars_fail_returns_502():
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("unreachable")
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=fail_instance):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OTS error: stamping failed"


def test_ots_partial_calendar_failure_still_returns_bytes():
    # First calendar down; rest succeed — result must be valid .ots bytes.
    # The Timestamp returned by a calendar must carry at least one attestation
    # so that serialization does not raise "An empty timestamp can't be serialized".
    good_ts = Timestamp(bytes.fromhex(DIGEST))
    good_ts.attestations.add(PendingAttestation("https://test.calendar.example"))
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("unreachable")
    ok_instance = MagicMock()
    ok_instance.submit.return_value = good_ts
    # DEFAULT_AGGREGATORS has 4 entries; provide enough for all of them.
    with patch("main.RemoteCalendar", side_effect=[fail_instance, ok_instance, ok_instance, ok_instance]):
        result = stamp_digest(DIGEST)
    assert isinstance(result, bytes)
    assert len(result) > 0


# ── 7. Happy path ─────────────────────────────────────────────────────────────

def test_successful_response_has_octet_stream_content_type():
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"


def test_successful_response_returns_ots_bytes_with_filename():
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.stamp_digest", return_value=FAKE_OTS):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 200
    assert resp.content == FAKE_OTS
    assert f"{DIGEST}.ots" in resp.headers["content-disposition"]
