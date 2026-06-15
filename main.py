import hashlib
import io
import logging
import os
import re
import requests
import urllib3
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
from opentimestamps.core.serialize import StreamSerializationContext
from opentimestamps.calendar import RemoteCalendar, DEFAULT_AGGREGATORS


def _parse_config():
    """Parse and validate all required env vars. Raises RuntimeError on misconfiguration."""
    missing = [
        name for name, val in {
            "LND_HOST": os.getenv("LND_HOST"),
            "LND_PORT": os.getenv("LND_PORT"),
            "LND_MACAROON_HEX": os.getenv("LND_MACAROON_HEX"),
            "TOR_PROXY": os.getenv("TOR_PROXY"),
            "GATEWAY_PRICE_SATS": os.getenv("GATEWAY_PRICE_SATS"),
        }.items() if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    try:
        price = int(os.getenv("GATEWAY_PRICE_SATS"))
    except ValueError:
        raise RuntimeError("GATEWAY_PRICE_SATS must be an integer")
    if price <= 0:
        raise RuntimeError(f"GATEWAY_PRICE_SATS must be a positive integer, got {price}")
    return (
        os.getenv("LND_HOST"),
        os.getenv("LND_PORT"),
        os.getenv("LND_MACAROON_HEX"),
        os.getenv("TOR_PROXY"),
        price,
        os.getenv("LND_TLS_VERIFY", "false").lower() == "true",
    )


load_dotenv()
LND_HOST, LND_PORT, LND_MACAROON_HEX, TOR_PROXY, GATEWAY_PRICE_SATS, LND_TLS_VERIFY = _parse_config()
if not LND_TLS_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

AUTH_RE = re.compile(r"^preimage=([0-9a-fA-F]{64})$")

app = FastAPI()
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


class TimestampRequest(BaseModel):
    digest: str

    @field_validator("digest")
    @classmethod
    def must_be_hex(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("digest must be a 64-character hex string (SHA256)")
        return v.lower()


def parse_preimage(auth: str) -> str | None:
    m = AUTH_RE.match(auth)
    return m.group(1) if m else None


def create_invoice(memo: str, amount_sats: int) -> str:
    """Call LND REST API via Tor to create a Lightning invoice. Returns the payment_request string (BOLT11). Raises HTTPException 502 on any failure."""
    proxies = {"https": f"socks5h://{TOR_PROXY}"}
    headers = {"Grpc-Metadata-macaroon": LND_MACAROON_HEX}
    url = f"https://{LND_HOST}:{LND_PORT}/v1/invoices"
    try:
        resp = requests.post(
            url,
            headers=headers,
            json={"memo": memo, "value": amount_sats},
            proxies=proxies,
            verify=LND_TLS_VERIFY,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["payment_request"]
    except Exception:
        logging.exception("LND invoice creation failed")
        raise HTTPException(status_code=502, detail="LND error: could not create invoice")


def stamp_digest(hex_digest: str) -> bytes:
    """Submit a SHA256 digest to the OTS public calendars and return the serialized .ots file as bytes."""
    digest_bytes = bytes.fromhex(hex_digest)
    file_timestamp = DetachedTimestampFile(OpSHA256(), Timestamp(digest_bytes))
    succeeded = 0
    for url in DEFAULT_AGGREGATORS:
        try:
            calendar_timestamp = RemoteCalendar(url).submit(digest_bytes, timeout=10)
            file_timestamp.timestamp.merge(calendar_timestamp)
            succeeded += 1
        except Exception:
            logging.warning("OTS calendar %s failed", url, exc_info=True)
    if succeeded == 0:
        raise RuntimeError("All OTS calendars failed; no timestamp was created")
    buf = io.BytesIO()
    file_timestamp.serialize(StreamSerializationContext(buf))
    return buf.getvalue()


def verify_payment(preimage_hex: str, digest: str) -> bool:
    """SHA256 the preimage to derive the payment hash, fetch the invoice from LND, and confirm
    it is settled, was issued for the specific digest, and the paid amount meets the price."""
    try:
        preimage_bytes = bytes.fromhex(preimage_hex)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid preimage: not a hex string")
    payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
    proxies = {"https": f"socks5h://{TOR_PROXY}"}
    headers = {"Grpc-Metadata-macaroon": LND_MACAROON_HEX}
    url = f"https://{LND_HOST}:{LND_PORT}/v1/invoice/{payment_hash}"
    try:
        resp = requests.get(url, headers=headers, proxies=proxies, verify=LND_TLS_VERIFY, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("settled", False)
            and data.get("memo") == digest
            and int(data.get("amt_paid_sat") or 0) >= GATEWAY_PRICE_SATS
        )
    except Exception:
        logging.exception("LND invoice lookup failed")
        raise HTTPException(status_code=502, detail="LND error: could not verify payment")


@app.get("/")
def root():
    return {"status": "running"}


@app.post("/timestamp")
def timestamp(body: TimestampRequest, request: Request):
    auth = request.headers.get("Authorization", "")
    preimage_hex = parse_preimage(auth) if auth else None

    if auth and not preimage_hex:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    if preimage_hex:
        if not verify_payment(preimage_hex, body.digest):
            raise HTTPException(status_code=402, detail="Payment required or not settled")
        try:
            ots_bytes = stamp_digest(body.digest)
        except Exception:
            logging.exception("OTS stamping failed")
            raise HTTPException(status_code=502, detail="OTS error: stamping failed")
        return Response(
            content=ots_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={body.digest}.ots"},
        )
    else:
        payment_request = create_invoice(body.digest, GATEWAY_PRICE_SATS)
        raise HTTPException(
            status_code=402,
            headers={"WWW-Authenticate": f'LSAT invoice="{payment_request}"'},
            detail={"status": "payment_required", "invoice": payment_request},
        )
