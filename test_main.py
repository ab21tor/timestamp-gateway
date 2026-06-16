import main
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
os.environ["OTS_BACKEND_MODE"] = "calendar"
os.environ["OTS_CALENDAR_URL"] = "http://test-calendar:14788"

from fastapi.testclient import TestClient  # noqa: E402
from main import app, _parse_config, stamp_digest  # noqa: E402
from opentimestamps.calendar import DEFAULT_AGGREGATORS  # noqa: E402
from opentimestamps.core.notary import PendingAttestation  # noqa: E402
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)

DIGEST = "a" * 64          # valid 64-char lowercase hex
PREIMAGE = "b" * 64        # valid 64-char hex preimage
FAKE_INVOICE = "lnbc210n1pfakeinvoicefortesting"
FAKE_OTS = b"\x00\x01\x02\x03opentimestamps"
TEST_CALENDAR_URL = "http://test-calendar:14788"


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


def _good_calendar_ts():
    """A Timestamp with a PendingAttestation so serialization succeeds."""
    ts = Timestamp(bytes.fromhex(DIGEST))
    ts.attestations.add(PendingAttestation("https://test.calendar.example"))
    return ts


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


# ── 2. Startup config validation — LND ───────────────────────────────────────

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


# ── 3. Startup config validation — OTS backend ───────────────────────────────

def test_invalid_ots_backend_mode_fails_at_startup():
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "invalid"}):
        with pytest.raises(RuntimeError, match="OTS_BACKEND_MODE must be"):
            _parse_config()


def test_calendar_mode_requires_calendar_url():
    # Empty OTS_CALENDAR_URL with calendar mode must fail.
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "calendar", "OTS_CALENDAR_URL": ""}):
        with pytest.raises(RuntimeError, match="OTS_CALENDAR_URL is required"):
            _parse_config()


def test_public_mode_rejects_calendar_url():
    # OTS_CALENDAR_URL is set in module-level env; switching to public mode must fail.
    with patch.dict(os.environ, {"OTS_BACKEND_MODE": "public"}):
        with pytest.raises(RuntimeError, match="OTS_CALENDAR_URL must not be set"):
            _parse_config()


# ── 4. Authorization header handling ─────────────────────────────────────────

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


# ── 5. Payment verification ───────────────────────────────────────────────────

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


# ── 6. LND errors ─────────────────────────────────────────────────────────────

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


# ── 7. OTS backend — calendar mode ───────────────────────────────────────────

def test_calendar_mode_submits_only_to_calendar_url():
    """Calendar mode must call RemoteCalendar with OTS_CALENDAR_URL, not an aggregator."""
    calendar_instance = MagicMock()
    calendar_instance.submit.return_value = _good_calendar_ts()
    with patch("main.RemoteCalendar") as MockCalendar:
        MockCalendar.return_value = calendar_instance
        stamp_digest(DIGEST)
    MockCalendar.assert_called_once_with(TEST_CALENDAR_URL)


def test_calendar_mode_does_not_call_default_aggregators():
    """Calendar mode must not touch DEFAULT_AGGREGATORS under any circumstance."""
    calendar_instance = MagicMock()
    calendar_instance.submit.return_value = _good_calendar_ts()
    with patch("main.RemoteCalendar") as MockCalendar:
        MockCalendar.return_value = calendar_instance
        stamp_digest(DIGEST)
    assert MockCalendar.call_count == 1
    called_url = MockCalendar.call_args[0][0]
    for agg_url in DEFAULT_AGGREGATORS:
        assert called_url != agg_url, f"calendar mode called aggregator {agg_url}"


def test_calendar_backend_fails_returns_502():
    """A calendar backend failure must return 502 with a generic error message."""
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("calendar unreachable")
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=fail_instance):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OTS error: stamping failed"


def test_calendar_mode_no_fallback_to_public_on_failure():
    """When the calendar backend fails, the gateway must not fall back to public aggregators."""
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("calendar unreachable")
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar") as MockCalendar:
            MockCalendar.return_value = fail_instance
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 502
    # Only one call should have been made — to the calendar URL, not to any aggregator.
    assert MockCalendar.call_count == 1
    called_url = MockCalendar.call_args[0][0]
    for agg_url in DEFAULT_AGGREGATORS:
        assert called_url != agg_url, f"fell back to public aggregator {agg_url}"


def test_calendar_mode_success_returns_ots_bytes():
    """Successful calendar-mode flow returns raw .ots bytes with octet-stream content type."""
    calendar_instance = MagicMock()
    calendar_instance.submit.return_value = _good_calendar_ts()
    with patch("main.requests.get", return_value=_settled_get()):
        with patch("main.RemoteCalendar", return_value=calendar_instance):
            resp = client.post(
                "/timestamp",
                json={"digest": DIGEST},
                headers={"Authorization": f"preimage={PREIMAGE}"},
            )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert len(resp.content) > 0


# ── 8. OTS backend — public mode (compatibility/testing) ─────────────────────

def test_public_mode_submits_to_all_default_aggregators():
    """Public mode (compatibility/testing) must submit to all DEFAULT_AGGREGATORS."""
    ok_instance = MagicMock()
    ok_instance.submit.return_value = _good_calendar_ts()
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch("main.RemoteCalendar") as MockCalendar:
            MockCalendar.return_value = ok_instance
            result = stamp_digest(DIGEST)
    assert isinstance(result, bytes) and len(result) > 0
    assert MockCalendar.call_count == len(DEFAULT_AGGREGATORS)
    called_urls = [call[0][0] for call in MockCalendar.call_args_list]
    for url in DEFAULT_AGGREGATORS:
        assert url in called_urls, f"public mode did not submit to aggregator {url}"


def test_public_mode_partial_calendar_failure_still_returns_bytes():
    """Public mode: first aggregator fails, rest succeed — result must be valid .ots bytes."""
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("unreachable")
    ok_instance = MagicMock()
    ok_instance.submit.return_value = _good_calendar_ts()
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch(
            "main.RemoteCalendar",
            side_effect=[fail_instance, ok_instance, ok_instance, ok_instance],
        ):
            result = stamp_digest(DIGEST)
    assert isinstance(result, bytes) and len(result) > 0


def test_public_mode_all_aggregators_fail_returns_502():
    """Public mode: all aggregators failing must return 502 — same contract as calendar mode."""
    fail_instance = MagicMock()
    fail_instance.submit.side_effect = ConnectionError("unreachable")
    with patch("main.OTS_BACKEND_MODE", "public"):
        with patch("main.requests.get", return_value=_settled_get()):
            with patch("main.RemoteCalendar", return_value=fail_instance):
                resp = client.post(
                    "/timestamp",
                    json={"digest": DIGEST},
                    headers={"Authorization": f"preimage={PREIMAGE}"},
                )
    assert resp.status_code == 502
    assert resp.json()["detail"] == "OTS error: stamping failed"


# ── 9. Happy path ─────────────────────────────────────────────────────────────

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

def test_create_invoice_requests_private_invoice(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self): pass
        def json(self): return {"payment_request": "lnbc..."}

    def fake_post(url, headers=None, json=None, proxies=None, verify=None, timeout=None):
        captured["json"] = json
        return FakeResponse()

    monkeypatch.setattr("main.requests.post", fake_post)
    invoice = main.create_invoice("a" * 64, 21)
    assert invoice == "lnbc..."
    assert captured["json"]["memo"] == "a" * 64
    assert captured["json"]["value"] == 21
    assert captured["json"]["private"] is True
