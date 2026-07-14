import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
import requests
import urllib3
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from pymacaroons import Macaroon, Verifier
from pymacaroons.exceptions import MacaroonException
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp
from opentimestamps.core.serialize import StreamSerializationContext, StreamDeserializationContext
from opentimestamps.calendar import RemoteCalendar, DEFAULT_AGGREGATORS
from opentimestamps.core.notary import PendingAttestation, BitcoinBlockHeaderAttestation


# L402 token constants. The capability names the endpoint a token authorizes, so a
# token minted for one action cannot be replayed against another.
L402_LOCATION = "timestamp-gateway"
L402_CAPABILITY = "timestamp"


@dataclass(frozen=True)
class GatewayConfig:
    """Validated gateway configuration. One field per module global, named as
    the lowercase of the global it populates. Built only by _parse_config()."""
    lnd_host: str | None
    lnd_port: str | None
    lnd_macaroon_hex: str | None
    tor_proxy: str | None
    gateway_price_sats: int
    min_gateway_price_sats: int
    pause_file: str
    lnd_tls_verify: bool
    ots_backend_mode: str
    ots_calendar_url: str | None
    lnd_readonly_macaroon_hex: str | None
    l402_secret: bytes
    l402_token_expiry_seconds: int
    ots_submit_max_attempts: int
    ots_submit_backoff_seconds: float
    payment_backend_type: str
    phoenixd_url: str
    phoenixd_http_password: str | None
    obligations_db_path: str
    obligation_sweep_interval: int
    wallet_status_path: str
    wallet_status_max_age_seconds: int
    proofs_status_path: str
    proofs_status_max_age_seconds: int
    backup_status_path: str
    backup_status_max_age_seconds: int
    price_rpc_url: str | None
    price_tx_vsize_estimate: int
    price_bump_reserve: float
    price_margin: float
    price_conf_target: int
    rate_limit_per_minute: int
    gateway_behind_proxy: bool


def _parse_config() -> GatewayConfig:
    """Parse and validate all required env vars. Raises RuntimeError on misconfiguration."""
    # Determine payment backend type early so we know which vars are required.
    # phoenixd is the live default; lnd is the test payer / alternative backend.
    payment_backend_type_early = os.getenv("PAYMENT_BACKEND_TYPE", "phoenixd").lower()

    required = {
        "GATEWAY_PRICE_SATS": os.getenv("GATEWAY_PRICE_SATS"),
        "OTS_BACKEND_MODE": os.getenv("OTS_BACKEND_MODE"),
    }
    if payment_backend_type_early == "lnd":
        required["LND_HOST"] = os.getenv("LND_HOST")
        required["LND_PORT"] = os.getenv("LND_PORT")
        required["LND_MACAROON_HEX"] = os.getenv("LND_MACAROON_HEX")

    missing = [name for name, val in required.items() if not val]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    try:
        price = int(os.getenv("GATEWAY_PRICE_SATS"))
    except ValueError:
        raise RuntimeError("GATEWAY_PRICE_SATS must be an integer")
    if price <= 0:
        raise RuntimeError(f"GATEWAY_PRICE_SATS must be a positive integer, got {price}")

    try:
        min_price = int(os.getenv("MIN_GATEWAY_PRICE_SATS", "1"))
    except ValueError:
        raise RuntimeError("MIN_GATEWAY_PRICE_SATS must be an integer")
    if min_price <= 0:
        raise RuntimeError(
            f"MIN_GATEWAY_PRICE_SATS must be a positive integer, got {min_price}"
        )
    if price < min_price:
        raise RuntimeError(
            f"GATEWAY_PRICE_SATS must be >= MIN_GATEWAY_PRICE_SATS "
            f"({price} < {min_price})"
        )

    pause_file = os.getenv("PAUSE_FILE", "/var/lib/timestamp-gateway/PAUSED")

    mode = os.getenv("OTS_BACKEND_MODE").lower()
    if mode not in ("calendar", "public"):
        raise RuntimeError(
            f"OTS_BACKEND_MODE must be 'calendar' or 'public', got {mode!r}"
        )

    calendar_url = os.getenv("OTS_CALENDAR_URL") or None
    if mode == "calendar" and not calendar_url:
        raise RuntimeError(
            "OTS_CALENDAR_URL is required when OTS_BACKEND_MODE=calendar"
        )
    if mode == "public" and calendar_url:
        raise RuntimeError(
            "OTS_CALENDAR_URL must not be set when OTS_BACKEND_MODE=public; "
            "set OTS_BACKEND_MODE=calendar to use a specific calendar backend"
        )

    # ── L402 token signing key ────────────────────────────────────────────────
    # Root key used to sign and verify L402 macaroons. It is required in
    # production: a stable key means a paid-but-not-yet-redeemed token still
    # verifies after a gateway restart. A random per-process key is allowed only
    # as an explicit development opt-out, because it would invalidate such tokens
    # on every restart.
    secret_hex = os.getenv("L402_SECRET_HEX") or None
    allow_ephemeral = os.getenv("L402_ALLOW_EPHEMERAL_SECRET", "false").lower() == "true"
    if secret_hex:
        try:
            l402_secret = bytes.fromhex(secret_hex)
        except ValueError:
            raise RuntimeError("L402_SECRET_HEX must be a hex string")
        if len(l402_secret) < 16:
            raise RuntimeError("L402_SECRET_HEX must decode to at least 16 bytes")
    elif allow_ephemeral:
        l402_secret = secrets.token_bytes(32)
        logging.warning(
            "L402_SECRET_HEX is not set and L402_ALLOW_EPHEMERAL_SECRET=true; using a "
            "random per-process key. Paid-but-unredeemed tokens will not verify after a "
            "restart. Development only."
        )
    else:
        raise RuntimeError(
            "L402_SECRET_HEX is required. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"`. "
            "For development only, set L402_ALLOW_EPHEMERAL_SECRET=true to use a "
            "random per-process key instead."
        )

    try:
        l402_expiry = int(os.getenv("L402_TOKEN_EXPIRY_SECONDS", "3600"))
    except ValueError:
        raise RuntimeError("L402_TOKEN_EXPIRY_SECONDS must be an integer")
    if l402_expiry <= 0:
        raise RuntimeError("L402_TOKEN_EXPIRY_SECONDS must be a positive integer")

    # ── OTS submission retry (otsd-not-ready resilience) ──────────────────────
    try:
        ots_max_attempts = int(os.getenv("OTS_SUBMIT_MAX_ATTEMPTS", "5"))
    except ValueError:
        raise RuntimeError("OTS_SUBMIT_MAX_ATTEMPTS must be an integer")
    if ots_max_attempts < 1:
        raise RuntimeError("OTS_SUBMIT_MAX_ATTEMPTS must be >= 1")

    try:
        ots_backoff = float(os.getenv("OTS_SUBMIT_BACKOFF_SECONDS", "2"))
    except ValueError:
        raise RuntimeError("OTS_SUBMIT_BACKOFF_SECONDS must be a number")
    if ots_backoff < 0:
        raise RuntimeError("OTS_SUBMIT_BACKOFF_SECONDS must be >= 0")

    payment_backend_type = os.getenv("PAYMENT_BACKEND_TYPE", "phoenixd").lower()
    if payment_backend_type not in ("lnd", "phoenixd"):
        raise RuntimeError("PAYMENT_BACKEND_TYPE must be 'lnd' or 'phoenixd'")
    phoenixd_url = os.getenv("PHOENIXD_URL", "http://127.0.0.1:9740")
    phoenixd_http_password = os.getenv("PHOENIXD_HTTP_PASSWORD") or None

    # ── Durable obligation log ────────────────────────────────────────────────
    # Path to the SQLite obligation store and how often the backstop sweeper
    # retries obligations left in needs_stamp. The store is what guarantees a
    # settled payment is never lost if calendar submission fails.
    obligations_db_path = os.getenv(
        "OBLIGATIONS_DB_PATH", "/var/lib/timestamp-gateway/obligations.db"
    )
    try:
        obligation_sweep_interval = int(os.getenv("OBLIGATION_SWEEP_INTERVAL", "1800"))
    except ValueError:
        raise RuntimeError("OBLIGATION_SWEEP_INTERVAL must be an integer")
    if obligation_sweep_interval <= 0:
        raise RuntimeError("OBLIGATION_SWEEP_INTERVAL must be a positive integer")

    # ── Wallet liquidity alarm (file-mediated; NO Bitcoin RPC from the gateway)
    # /health reads the status file written by ops/wallet-balance-check.sh.
    # The gateway never holds wallet credentials — the rails stay separate.
    # One carve-out (the pricing floor): the gateway may hold a fee-estimation
    # credential (PRICE_RPC_URL, node-level), never a spending one.
    wallet_status_path = os.getenv(
        "WALLET_STATUS_PATH", "/var/lib/timestamp-gateway/wallet-status"
    )
    try:
        wallet_status_max_age = int(os.getenv("WALLET_STATUS_MAX_AGE_SECONDS", "3600"))
    except ValueError:
        raise RuntimeError("WALLET_STATUS_MAX_AGE_SECONDS must be an integer")
    if wallet_status_max_age <= 0:
        raise RuntimeError("WALLET_STATUS_MAX_AGE_SECONDS must be a positive integer")

    # /health reads the status file written by ops/upgrade-all-proofs.sh (the
    # proof sweep). Same file-mediated pattern as the wallet alarm.
    proofs_status_path = os.getenv(
        "PROOFS_STATUS_PATH", "/var/lib/timestamp-gateway/proofs-status"
    )
    try:
        proofs_status_max_age = int(os.getenv("PROOFS_STATUS_MAX_AGE_SECONDS", "3600"))
    except ValueError:
        raise RuntimeError("PROOFS_STATUS_MAX_AGE_SECONDS must be an integer")
    if proofs_status_max_age <= 0:
        raise RuntimeError("PROOFS_STATUS_MAX_AGE_SECONDS must be a positive integer")

    # /health reads the status file written by ops/backup-live-state.sh (the
    # backup timer). Same file-mediated pattern as the wallet alarm. Max age
    # defaults to 2x the daily timer, the established convention.
    backup_status_path = os.getenv(
        "BACKUP_STATUS_PATH", "/var/lib/timestamp-gateway/backup-status"
    )
    try:
        backup_status_max_age = int(os.getenv("BACKUP_STATUS_MAX_AGE_SECONDS", "172800"))
    except ValueError:
        raise RuntimeError("BACKUP_STATUS_MAX_AGE_SECONDS must be an integer")
    if backup_status_max_age <= 0:
        raise RuntimeError("BACKUP_STATUS_MAX_AGE_SECONDS must be a positive integer")

    # ── Pricing solvency floor (fee-estimation RPC — see the wallet note above)
    # The 402 quote is max(GATEWAY_PRICE_SATS, floor); the floor prices one
    # anchoring transaction at the live fee market. PRICE_RPC_URL should carry
    # a node-level (wallet-less) credential; it falls back to
    # BITCOIN_RPC_SERVICE_URL so a single-credential deployment floats prices
    # without new setup. Unset entirely: the gateway quotes the static price
    # and warns — degraded pricing must never take payments down.
    price_rpc_url = (
        os.getenv("PRICE_RPC_URL") or os.getenv("BITCOIN_RPC_SERVICE_URL") or None
    )

    try:
        price_tx_vsize = int(os.getenv("PRICE_TX_VSIZE_ESTIMATE", "150"))
    except ValueError:
        raise RuntimeError("PRICE_TX_VSIZE_ESTIMATE must be an integer")
    if price_tx_vsize <= 0:
        raise RuntimeError(
            f"PRICE_TX_VSIZE_ESTIMATE must be a positive integer, got {price_tx_vsize}"
        )

    try:
        price_bump_reserve = float(os.getenv("PRICE_BUMP_RESERVE", "1.5"))
    except ValueError:
        raise RuntimeError("PRICE_BUMP_RESERVE must be a number")
    if price_bump_reserve < 1:
        raise RuntimeError(
            f"PRICE_BUMP_RESERVE must be >= 1 (a reserve below the unbumped fee "
            f"underprices anchoring), got {price_bump_reserve}"
        )

    try:
        price_margin = float(os.getenv("PRICE_MARGIN", "5"))
    except ValueError:
        raise RuntimeError("PRICE_MARGIN must be a number")
    if price_margin < 1:
        raise RuntimeError(
            f"PRICE_MARGIN must be >= 1 (below 1 quotes anchoring at a loss), "
            f"got {price_margin}"
        )

    try:
        price_conf_target = int(os.getenv("PRICE_CONF_TARGET", "6"))
    except ValueError:
        raise RuntimeError("PRICE_CONF_TARGET must be an integer")
    if not 1 <= price_conf_target <= 1008:
        raise RuntimeError(
            f"PRICE_CONF_TARGET must be within estimatesmartfee's accepted range "
            f"1-1008, got {price_conf_target}"
        )

    # ── Invoice-mint rate limit ───────────────────────────────────────────────
    # Per-IP token bucket on the unauthenticated 402 path. Every anonymous
    # request makes phoenixd sign AND durably store an invoice, so minting is
    # the one request whose backend cost is not bounded by payment. 0 disables.
    try:
        rate_limit_per_minute = int(os.getenv("RATE_LIMIT_PER_MINUTE", "10"))
    except ValueError:
        raise RuntimeError("RATE_LIMIT_PER_MINUTE must be an integer")
    if rate_limit_per_minute < 0:
        raise RuntimeError(
            f"RATE_LIMIT_PER_MINUTE must be >= 0 (0 disables the limit), "
            f"got {rate_limit_per_minute}"
        )

    # Whether a reverse proxy the OPERATOR controls sits in front of the
    # gateway. Only then is X-Forwarded-For consulted for the client address —
    # the header is client-forgeable and must never be trusted by default.
    # Strict parse: a typo silently treated as false would bucket every client
    # under the proxy's address and rate-limit them collectively.
    behind_proxy_raw = os.getenv("GATEWAY_BEHIND_PROXY", "false").lower()
    if behind_proxy_raw not in ("true", "false"):
        raise RuntimeError(
            f"GATEWAY_BEHIND_PROXY must be 'true' or 'false', got {behind_proxy_raw!r}"
        )
    gateway_behind_proxy = behind_proxy_raw == "true"

    return GatewayConfig(
        lnd_host=os.getenv("LND_HOST"),
        lnd_port=os.getenv("LND_PORT"),
        lnd_macaroon_hex=os.getenv("LND_MACAROON_HEX"),
        tor_proxy=os.getenv("TOR_PROXY") or None,  # optional; None = direct connection
        gateway_price_sats=price,
        min_gateway_price_sats=min_price,
        pause_file=pause_file,
        lnd_tls_verify=os.getenv("LND_TLS_VERIFY", "false").lower() == "true",
        ots_backend_mode=mode,
        ots_calendar_url=calendar_url,
        # optional; falls back to LND_MACAROON_HEX
        lnd_readonly_macaroon_hex=os.getenv("LND_READONLY_MACAROON_HEX") or None,
        l402_secret=l402_secret,
        l402_token_expiry_seconds=l402_expiry,
        ots_submit_max_attempts=ots_max_attempts,
        ots_submit_backoff_seconds=ots_backoff,
        payment_backend_type=payment_backend_type,
        phoenixd_url=phoenixd_url,
        phoenixd_http_password=phoenixd_http_password,
        obligations_db_path=obligations_db_path,
        obligation_sweep_interval=obligation_sweep_interval,
        wallet_status_path=wallet_status_path,
        wallet_status_max_age_seconds=wallet_status_max_age,
        proofs_status_path=proofs_status_path,
        proofs_status_max_age_seconds=proofs_status_max_age,
        backup_status_path=backup_status_path,
        backup_status_max_age_seconds=backup_status_max_age,
        price_rpc_url=price_rpc_url,
        price_tx_vsize_estimate=price_tx_vsize,
        price_bump_reserve=price_bump_reserve,
        price_margin=price_margin,
        price_conf_target=price_conf_target,
        rate_limit_per_minute=rate_limit_per_minute,
        gateway_behind_proxy=gateway_behind_proxy,
    )


load_dotenv()
_CONFIG = _parse_config()

# Module globals mirror the config fields one-to-one. Kept as globals (not
# attribute reads at call sites) so tests can patch individual values via
# patch("main.<NAME>", ...) — the established seam throughout the suite.
LND_HOST = _CONFIG.lnd_host
LND_PORT = _CONFIG.lnd_port
LND_MACAROON_HEX = _CONFIG.lnd_macaroon_hex
TOR_PROXY = _CONFIG.tor_proxy
GATEWAY_PRICE_SATS = _CONFIG.gateway_price_sats
MIN_GATEWAY_PRICE_SATS = _CONFIG.min_gateway_price_sats
PAUSE_FILE = _CONFIG.pause_file
LND_TLS_VERIFY = _CONFIG.lnd_tls_verify
OTS_BACKEND_MODE = _CONFIG.ots_backend_mode
OTS_CALENDAR_URL = _CONFIG.ots_calendar_url
LND_READONLY_MACAROON_HEX = _CONFIG.lnd_readonly_macaroon_hex
L402_SECRET = _CONFIG.l402_secret
L402_TOKEN_EXPIRY_SECONDS = _CONFIG.l402_token_expiry_seconds
OTS_SUBMIT_MAX_ATTEMPTS = _CONFIG.ots_submit_max_attempts
OTS_SUBMIT_BACKOFF_SECONDS = _CONFIG.ots_submit_backoff_seconds
PAYMENT_BACKEND_TYPE = _CONFIG.payment_backend_type
PHOENIXD_URL = _CONFIG.phoenixd_url
PHOENIXD_HTTP_PASSWORD = _CONFIG.phoenixd_http_password
OBLIGATIONS_DB_PATH = _CONFIG.obligations_db_path
OBLIGATION_SWEEP_INTERVAL = _CONFIG.obligation_sweep_interval
WALLET_STATUS_PATH = _CONFIG.wallet_status_path
WALLET_STATUS_MAX_AGE_SECONDS = _CONFIG.wallet_status_max_age_seconds
PROOFS_STATUS_PATH = _CONFIG.proofs_status_path
PROOFS_STATUS_MAX_AGE_SECONDS = _CONFIG.proofs_status_max_age_seconds
BACKUP_STATUS_PATH = _CONFIG.backup_status_path
BACKUP_STATUS_MAX_AGE_SECONDS = _CONFIG.backup_status_max_age_seconds
PRICE_RPC_URL = _CONFIG.price_rpc_url
PRICE_TX_VSIZE_ESTIMATE = _CONFIG.price_tx_vsize_estimate
PRICE_BUMP_RESERVE = _CONFIG.price_bump_reserve
PRICE_MARGIN = _CONFIG.price_margin
PRICE_CONF_TARGET = _CONFIG.price_conf_target
RATE_LIMIT_PER_MINUTE = _CONFIG.rate_limit_per_minute
GATEWAY_BEHIND_PROXY = _CONFIG.gateway_behind_proxy

if not LND_TLS_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# "L402 <macaroon>:<preimage>" — the macaroon is base64 (urlsafe or standard,
# padded or not) and never contains a colon; the preimage is 64 hex chars.
L402_AUTH_RE = re.compile(r"^L402\s+([A-Za-z0-9+/=_-]+):([0-9a-fA-F]{64})$")

# Process-level cache mapping payment_hash -> ots_bytes.
# Prevents the same paid token from submitting the same digest to otsd multiple times
# within the token expiry window. Resets on process restart (acceptable: the invoice
# is still settled in Phoenixd so the client can re-present the token after restart).
# Bounded at _PROOF_CACHE_MAX entries with FIFO eviction so a long-lived process
# cannot grow it without limit. Eviction never loses a paid obligation: a
# re-presented token that misses the cache just re-stamps (the obligation log is
# the durable record), at the cost of one redundant otsd submission.
_PROOF_CACHE_MAX = 10_000
_proof_cache: dict[str, bytes] = {}


def _proof_cache_put(payment_hash: str, ots_bytes: bytes) -> None:
    """Insert into _proof_cache, evicting the oldest entry once the cache is full
    (dicts iterate in insertion order). Overwriting an existing key never evicts."""
    if payment_hash not in _proof_cache and len(_proof_cache) >= _PROOF_CACHE_MAX:
        del _proof_cache[next(iter(_proof_cache))]
    _proof_cache[payment_hash] = ots_bytes


# ── Invoice-mint rate limiter ─────────────────────────────────────────────────
# Per-IP token bucket guarding the unauthenticated mint path. Every anonymous
# POST /timestamp makes phoenixd sign a bolt11 and persist an invoice row in its
# own database, so minting is the one request whose backend cost is not bounded
# by payment. Same bounded module-dict shape as _proof_cache: FIFO eviction (by
# first sighting) at _RATE_BUCKETS_MAX. Evicting a bucket refills it — a spammer
# spread across that many addresses is beyond what a per-IP limit can bound
# anyway. Lock-free like _proof_cache: a concurrent get/set race can only
# under-count a request or two, never corrupt the dict.
_RATE_BUCKETS_MAX = 10_000
# ip -> (tokens remaining, time.monotonic() at the last refill)
_rate_buckets: dict[str, tuple[float, float]] = {}


def _client_ip(request: Request) -> str:
    """The address the rate limiter buckets on. The direct peer address, unless
    the operator has declared a reverse proxy in front (GATEWAY_BEHIND_PROXY=true)
    — then the rightmost X-Forwarded-For entry, the one appended by the
    operator's own proxy. Leftmost entries are client-supplied and never used."""
    if GATEWAY_BEHIND_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_retry_after(ip: str) -> int | None:
    """Take one token from ip's bucket. Returns None when the request is
    allowed, else the whole seconds (>= 1) until a token accrues. Capacity and
    refill rate are both RATE_LIMIT_PER_MINUTE, so a full bucket is one
    minute's allowance of burst. A denied request consumes nothing."""
    if RATE_LIMIT_PER_MINUTE <= 0:
        return None
    now = time.monotonic()
    tokens, last_refill = _rate_buckets.get(ip, (float(RATE_LIMIT_PER_MINUTE), now))
    tokens = min(
        float(RATE_LIMIT_PER_MINUTE),
        tokens + (now - last_refill) * RATE_LIMIT_PER_MINUTE / 60,
    )
    if tokens >= 1:
        tokens -= 1
        retry_after = None
    else:
        retry_after = math.ceil((1 - tokens) * 60 / RATE_LIMIT_PER_MINUTE)
    if ip not in _rate_buckets and len(_rate_buckets) >= _RATE_BUCKETS_MAX:
        del _rate_buckets[next(iter(_rate_buckets))]
    _rate_buckets[ip] = (tokens, now)
    return retry_after


# ── Durable obligation log ────────────────────────────────────────────────────
# A SQLite table recording each settled payment as an obligation to stamp. The
# in-memory _proof_cache is the instant re-serve path; this DB is the durable
# backstop. When a payment settles but stamping fails (e.g. otsd down past the
# retry window), the row is left in 'needs_stamp' and the background sweeper
# retries it until it is stamped — a paid obligation is never dropped.
#
# The store is NEVER consulted to validate a proof. Payment only admits a
# request; a finished proof verifies with zero dependency on this DB.
#
# Every operation uses a fresh, short-lived connection (the sweeper runs in a
# separate thread; sharing one sqlite3 connection across threads is unsafe).


def _obligation_connect() -> sqlite3.Connection:
    """Open a fresh connection to the obligation store. Reads the module global
    at call time so tests can repoint OBLIGATIONS_DB_PATH."""
    conn = sqlite3.connect(OBLIGATIONS_DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_obligation_db() -> None:
    """Create the obligations table if it does not exist. Fails loud (RuntimeError)
    if the path is unwritable, so the process refuses to start rather than silently
    running without a durable obligation log."""
    path = OBLIGATIONS_DB_PATH
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = _obligation_connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS obligations (
                    payment_hash    TEXT PRIMARY KEY,
                    digest          TEXT NOT NULL,
                    created_at      INTEGER NOT NULL,
                    status          TEXT NOT NULL
                                    CHECK (status IN ('needs_stamp', 'stamped')),
                    attempts        INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at INTEGER
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        raise RuntimeError(f"Cannot initialize obligations DB at {path}: {e}")


def record_obligation(payment_hash: str, digest: str) -> None:
    """Durably record a paid obligation as 'needs_stamp' before stamping.
    INSERT OR IGNORE keyed on payment_hash: a duplicate paid token is idempotent
    (single row, no state change, no new invoice)."""
    conn = _obligation_connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO obligations "
            "(payment_hash, digest, created_at, status, attempts) "
            "VALUES (?, ?, ?, 'needs_stamp', 0)",
            (payment_hash, digest, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def mark_obligation_stamped(payment_hash: str) -> None:
    """Mark an obligation complete after a successful stamp."""
    conn = _obligation_connect()
    try:
        conn.execute(
            "UPDATE obligations SET status='stamped' WHERE payment_hash=?",
            (payment_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def _sweep_obligations_once() -> None:
    """Retry every 'needs_stamp' obligation once. Each row uses short, independent
    transactions so a mid-run crash is safe and the sweeper never holds a long lock.
    attempts/last_attempt_at are always bumped; on success the row is marked
    'stamped' and _proof_cache is populated (mirroring the endpoint's success path).
    On failure the row stays 'needs_stamp' for the next sweep — a paid obligation is
    retried indefinitely, never capped-and-dropped."""
    conn = _obligation_connect()
    try:
        rows = conn.execute(
            "SELECT payment_hash, digest FROM obligations WHERE status='needs_stamp'"
        ).fetchall()
    finally:
        conn.close()

    for payment_hash, digest in rows:
        conn = _obligation_connect()
        try:
            conn.execute(
                "UPDATE obligations SET attempts=attempts+1, last_attempt_at=? "
                "WHERE payment_hash=?",
                (int(time.time()), payment_hash),
            )
            conn.commit()
        finally:
            conn.close()

        try:
            ots_bytes = stamp_digest(digest)
        except Exception:
            logging.warning(
                "Sweeper: stamping still failing for %s; leaving needs_stamp",
                payment_hash, exc_info=True,
            )
            continue

        _proof_cache_put(payment_hash, ots_bytes)
        mark_obligation_stamped(payment_hash)
        logging.info("Sweeper: recovered obligation %s", payment_hash)


def _sweeper_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _sweep_obligations_once()
        except Exception:
            logging.exception("Obligation sweep failed; will retry next interval")
        stop_event.wait(OBLIGATION_SWEEP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loud on an unwritable DB path before serving any request.
    init_obligation_db()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_sweeper_loop, args=(stop_event,),
        name="obligation-sweeper", daemon=True,
    )
    thread.start()
    app.state.sweeper_stop = stop_event
    app.state.sweeper_thread = thread
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=10)


app = FastAPI(lifespan=lifespan)
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


def is_paused() -> bool:
    return bool(PAUSE_FILE and Path(PAUSE_FILE).exists())


def _wallet_status() -> str:
    """Classify the wallet-status file written by ops/wallet-balance-check.sh.

    File-mediated like the PAUSED switch: the gateway never talks to Bitcoin
    RPC and never holds wallet credentials — it only reads the file the timer
    leaves behind. Returns one of:
      ok       — balance at or above the minimum
      low      — balance below the minimum (wallet draining; anchoring at risk)
      unknown  — the check ran but could not read the balance, or the file is
                 malformed
      stale    — the file is older than WALLET_STATUS_MAX_AGE_SECONDS (the
                 timer itself died)
      absent   — no file (alarm not installed)
    Never raises — /health must never crash."""
    try:
        raw = Path(WALLET_STATUS_PATH).read_text()
    except FileNotFoundError:
        return "absent"
    except Exception:
        return "unknown"
    try:
        data = json.loads(raw)
        status = data["status"]
        checked_at = int(data["checked_at"])
    except Exception:
        return "unknown"
    if status not in ("ok", "low", "unknown"):
        return "unknown"
    if time.time() - checked_at > WALLET_STATUS_MAX_AGE_SECONDS:
        return "stale"
    return status


def _proofs_status() -> str:
    """Classify the proofs-status file written by ops/upgrade-all-proofs.sh.

    File-mediated like the wallet alarm: the sweep timer scans and upgrades the
    proof artifacts; /health only reads the file it leaves behind. Returns one of:
      ok        — every proof is anchored or honestly waiting for Bitcoin
      mismatch  — the calendar holds an attestation some artifact lacks
      attention — a proof is in a state the sweep cannot classify
      unknown   — the file is unreadable or malformed
      stale     — the file is older than PROOFS_STATUS_MAX_AGE_SECONDS (the
                  sweep timer itself died)
      absent    — no file (sweep not installed)
    Never raises — /health must never crash."""
    try:
        raw = Path(PROOFS_STATUS_PATH).read_text()
    except FileNotFoundError:
        return "absent"
    except Exception:
        return "unknown"
    try:
        data = json.loads(raw)
        status = data["status"]
        checked_at = int(data["checked_at"])
    except Exception:
        return "unknown"
    if status not in ("ok", "mismatch", "attention"):
        return "unknown"
    if time.time() - checked_at > PROOFS_STATUS_MAX_AGE_SECONDS:
        return "stale"
    return status


def _backup_status() -> str:
    """Classify the backup-status file written by ops/backup-live-state.sh.

    File-mediated like the wallet alarm: the backup timer archives, encrypts
    and pushes; /health only reads the file it leaves behind. Returns one of:
      ok         — archive created, encrypted, pushed off-box
      local_only — archive created but kept on this box (a configuration
                   choice, honestly reported; does not degrade health)
      attention  — a backup exists but degraded (its detail field says why)
      failed     — the run produced no usable archive
      unknown    — the file is unreadable or malformed
      stale      — the file is older than BACKUP_STATUS_MAX_AGE_SECONDS (the
                   backup timer itself died)
      absent     — no file (backups not installed)
    Never raises — /health must never crash."""
    try:
        raw = Path(BACKUP_STATUS_PATH).read_text()
    except FileNotFoundError:
        return "absent"
    except Exception:
        return "unknown"
    try:
        data = json.loads(raw)
        status = data["status"]
        checked_at = int(data["checked_at"])
    except Exception:
        return "unknown"
    if status not in ("ok", "local_only", "attention", "failed"):
        return "unknown"
    if time.time() - checked_at > BACKUP_STATUS_MAX_AGE_SECONDS:
        return "stale"
    return status


class TimestampRequest(BaseModel):
    digest: str

    @field_validator("digest")
    @classmethod
    def must_be_hex(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("digest must be a 64-character hex string (SHA256)")
        return v.lower()


class VerifyRequest(BaseModel):
    digest: str
    ots: str

    @field_validator("digest")
    @classmethod
    def must_be_hex(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("digest must be a 64-character hex string (SHA256)")
        return v.lower()


MAX_VERIFY_OTS_BYTES = 256 * 1024


def _extract_attestations(timestamp) -> list:
    """Walk a timestamp's attestations into a JSON-serializable list. Shared by
    /verify and /upgrade so both report attestations identically."""
    attestations = []
    for _msg, attestation in timestamp.all_attestations():
        if isinstance(attestation, PendingAttestation):
            attestations.append(
                {
                    "type": "pending_calendar",
                    "calendar_url": attestation.uri,
                }
            )
        elif isinstance(attestation, BitcoinBlockHeaderAttestation):
            height = attestation.height
            attestations.append(
                {
                    "type": "bitcoin",
                    "block_height": height,
                    "mempool_block_height_url": f"https://mempool.space/block-height/{height}",
                }
            )
        else:
            attestations.append(
                {
                    "type": "unknown",
                    "description": repr(attestation),
                }
            )
    return attestations


def _verify_ots_bytes(digest: str, ots_bytes: bytes) -> dict:
    try:
        ctx = StreamDeserializationContext(io.BytesIO(ots_bytes))
        detached = DetachedTimestampFile.deserialize(ctx)
    except Exception:
        logging.info("Verify failed: invalid OTS proof", exc_info=True)
        return {
            "digest": digest,
            "proof_digest": None,
            "status": "invalid",
            "valid_ots": False,
            "digest_match": False,
            "bitcoin_anchored": False,
            "verified": False,
            "attestations": [],
        }

    proof_digest = detached.file_digest.hex()
    digest_match = proof_digest == digest
    attestations = _extract_attestations(detached.timestamp)

    bitcoin_anchored = any(a["type"] == "bitcoin" for a in attestations)
    has_pending = any(a["type"] == "pending_calendar" for a in attestations)

    if not digest_match:
        status = "mismatch"
    elif bitcoin_anchored:
        status = "anchored"
    elif has_pending:
        status = "pending"
    else:
        # Well-formed OTS proof, digest matches, but no recognized
        # (bitcoin/pending) attestations — distinct from "invalid"
        # (undecodable bytes). An empty timestamp can't serialize, so in
        # practice this means only unknown attestation types are present.
        status = "no_attestations"

    return {
        "digest": digest,
        "proof_digest": proof_digest,
        "status": status,
        "valid_ots": True,
        "digest_match": digest_match,
        "bitcoin_anchored": bitcoin_anchored,
        "verified": status == "anchored",
        "attestations": attestations,
    }


def _upgrade_pending_against_operator(timestamp, timeout) -> None:
    calendar = RemoteCalendar(OTS_CALENDAR_URL)
    def walk(stamp):
        yield stamp
        for sub in stamp.ops.values():
            yield from walk(sub)
    for sub_stamp in walk(timestamp):
        if not any(isinstance(a, PendingAttestation) for a in sub_stamp.attestations):
            continue
        try:
            upgraded = calendar.get_timestamp(sub_stamp.msg, timeout=timeout)
        except Exception:
            logging.info("Upgrade: no operator attestation available", exc_info=True)
            continue
        try:
            sub_stamp.merge(upgraded)
        except Exception:
            logging.warning("Upgrade: failed to merge operator attestation", exc_info=True)


def _upgrade_ots_bytes(digest: str, ots_bytes: bytes) -> dict:
    try:
        ctx = StreamDeserializationContext(io.BytesIO(ots_bytes))
        detached = DetachedTimestampFile.deserialize(ctx)
    except Exception:
        logging.info("Upgrade failed: invalid OTS proof", exc_info=True)
        return {
            "digest": digest,
            "proof_digest": None,
            "status": "invalid",
            "valid_ots": False,
            "digest_match": False,
            "bitcoin_anchored": False,
            "verified": False,
            "ots": None,
            "attestations": [],
        }
    proof_digest = detached.file_digest.hex()
    digest_match = proof_digest == digest
    original_b64 = base64.b64encode(ots_bytes).decode()
    def result(status: str, ots_b64: str | None) -> dict:
        attestations = _extract_attestations(detached.timestamp)
        bitcoin_anchored = any(a["type"] == "bitcoin" for a in attestations)
        return {
            "digest": digest,
            "proof_digest": proof_digest,
            "status": status,
            "valid_ots": True,
            "digest_match": digest_match,
            "bitcoin_anchored": bitcoin_anchored,
            "verified": status == "anchored",
            "ots": ots_b64,
            "attestations": attestations,
        }
    if not digest_match:
        return result("mismatch", original_b64)
    attestations = _extract_attestations(detached.timestamp)
    bitcoin_anchored = any(a["type"] == "bitcoin" for a in attestations)
    has_pending = any(a["type"] == "pending_calendar" for a in attestations)
    if bitcoin_anchored:
        return result("anchored", original_b64)
    if not has_pending:
        # Well-formed proof, digest matches, but no recognized (bitcoin/
        # pending) attestations to upgrade — distinct from "invalid"
        # (undecodable bytes).
        return result("no_attestations", original_b64)
    if OTS_CALENDAR_URL:
        _upgrade_pending_against_operator(detached.timestamp, timeout=10)
    now_anchored = any(
        a["type"] == "bitcoin" for a in _extract_attestations(detached.timestamp)
    )
    if now_anchored:
        buf = io.BytesIO()
        detached.serialize(StreamSerializationContext(buf))
        upgraded_b64 = base64.b64encode(buf.getvalue()).decode()
        return result("anchored", upgraded_b64)
    return result("pending", original_b64)


# ── L402 token (macaroon) ────────────────────────────────────────────────────

def _caveat_text(caveat_id) -> str:
    """pymacaroons stores caveat ids as str or bytes depending on version; normalize."""
    if isinstance(caveat_id, bytes):
        return caveat_id.decode("utf-8", "replace")
    return caveat_id


def mint_l402_token(digest: str, payment_hash: str, price: int, expiry_ts: int) -> str:
    """Mint an L402 macaroon bound to a specific digest, payment hash, price,
    capability, and expiry. Returned base64-serialized for the WWW-Authenticate header."""
    m = Macaroon(location=L402_LOCATION, identifier=payment_hash, key=L402_SECRET)
    m.add_first_party_caveat(f"digest={digest}")
    m.add_first_party_caveat(f"payment_hash={payment_hash}")
    m.add_first_party_caveat(f"price={price}")
    m.add_first_party_caveat(f"capability={L402_CAPABILITY}")
    m.add_first_party_caveat(f"expiry={expiry_ts}")
    return m.serialize()


def _caveat_value(m: Macaroon, key: str) -> str | None:
    prefix = f"{key}="
    for caveat in m.first_party_caveats():
        text = _caveat_text(caveat.caveat_id)
        if text.startswith(prefix):
            return text[len(prefix):]
    return None


def _expiry_satisfier(caveat_id) -> bool:
    text = _caveat_text(caveat_id)
    if not text.startswith("expiry="):
        return False
    try:
        return time.time() < int(text[len("expiry="):])
    except ValueError:
        return False


def _payment_hash_satisfier(caveat_id) -> bool:
    return re.fullmatch(r"payment_hash=[0-9a-f]{64}", _caveat_text(caveat_id)) is not None


def _price_satisfier(caveat_id) -> bool:
    return re.fullmatch(r"price=[1-9][0-9]*", _caveat_text(caveat_id)) is not None


def verify_l402_token(macaroon_b64: str, digest: str) -> tuple[str, int]:
    """Verify an L402 macaroon against the request digest and the server root key.

    Checks token integrity (signature), the digest binding, the capability
    binding, and the expiry. The price is read from the macaroon's own signed
    caveat rather than compared to the current configured price: a token minted
    at price N validates at N forever, so repricing between challenge and
    payment never strands an in-flight invoice. The HMAC prevents a client
    from lowering the caveat; what the mint-time price must buy is enforced
    against the settled invoice in verify_payment. On success returns
    (payment_hash, mint_time_price_sats). Any invalid, expired, tampered, or
    wrong-digest token raises HTTPException 401 — an authorization failure,
    not a payment one."""
    try:
        m = Macaroon.deserialize(macaroon_b64)
    except Exception:
        logging.exception("L402 macaroon could not be parsed")
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    payment_hash = _caveat_value(m, "payment_hash")
    if not payment_hash or not re.fullmatch(r"[0-9a-f]{64}", payment_hash):
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    price_str = _caveat_value(m, "price")
    if not price_str or not re.fullmatch(r"[1-9][0-9]*", price_str):
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    verifier = Verifier()
    verifier.satisfy_exact(f"digest={digest}")
    verifier.satisfy_exact(f"capability={L402_CAPABILITY}")
    verifier.satisfy_general(_payment_hash_satisfier)
    verifier.satisfy_general(_price_satisfier)
    verifier.satisfy_general(_expiry_satisfier)

    try:
        verifier.verify(m, L402_SECRET)
    except MacaroonException:
        raise HTTPException(status_code=401, detail="Invalid, expired, or wrong-digest L402 token")
    except Exception:
        logging.exception("L402 verification raised unexpectedly")
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    return payment_hash, int(price_str)


def parse_l402_auth(auth: str) -> tuple[str, str] | None:
    """Parse 'L402 <macaroon>:<preimage>'. Returns (macaroon_b64, preimage_hex)
    or None if the header is not a well-formed L402 authorization."""
    m = L402_AUTH_RE.match(auth.strip())
    if not m:
        return None
    return m.group(1), m.group(2).lower()


# ── Pricing solvency floor ───────────────────────────────────────────────────
# The 402 challenge quotes max(GATEWAY_PRICE_SATS, floor): the floor prices one
# anchoring transaction at the live fee market times the bump reserve and the
# margin, so the gateway is structurally unable to quote below anchoring cost.
# Any missing feerate (no RPC URL, timeout, RPC error, fresh node with no
# estimate yet) falls back to the static price with a warning — degraded
# pricing must never take payments down, and must never be silent. The RPC
# round-trip crosses socat→Tor→a remote node and can take seconds, so results
# (including failures) are cached and the hard timeout keeps a dead node from
# stalling the challenge path.

_PRICE_RPC_TIMEOUT_SECONDS = 5
_FEERATE_CACHE_TTL_SECONDS = 60

# Single-entry module-level cache, same simplicity as _proof_cache:
# (feerate in sat/vB, or None if the last fetch failed; monotonic fetch time).
_feerate_cache: tuple[float | None, float] | None = None


def _btc_per_kvb_to_sat_per_vb(btc_per_kvb: float) -> float:
    """estimatesmartfee quotes BTC/kvB; sat/vB = BTC/kvB × 1e8 sat/BTC ÷ 1000 vB/kvB.
    bitcoind serializes BTC to 8 decimals, so sat/vB has exactly 3 meaningful
    decimals; rounding there is lossless and stops binary-float noise
    (0.00001 × 1e8 ÷ 1000 = 1.0000000000000002) from inflating ceil() by a sat."""
    return round(btc_per_kvb * 1e8 / 1000, 3)


def _fetch_feerate_sat_per_vb() -> float | None:
    """estimatesmartfee(PRICE_CONF_TARGET) against PRICE_RPC_URL, in sat/vB.
    Returns None — always with a warning — when no URL is configured, the call
    fails or exceeds the timeout, or the node has no estimate. Exceptions are
    reduced to their class name: requests errors can echo the URL, which
    carries credentials."""
    if not PRICE_RPC_URL:
        logging.warning(
            "Pricing floor: neither PRICE_RPC_URL nor BITCOIN_RPC_SERVICE_URL "
            "is set; quoting the static price"
        )
        return None
    try:
        resp = requests.post(
            PRICE_RPC_URL,
            json={
                "jsonrpc": "1.0",
                "id": "gateway-price-floor",
                "method": "estimatesmartfee",
                "params": [PRICE_CONF_TARGET],
            },
            timeout=_PRICE_RPC_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        result = resp.json().get("result") or {}
        feerate_btc_kvb = result.get("feerate")
    except Exception as exc:
        logging.warning(
            "Pricing floor: estimatesmartfee failed (%s); quoting the static price",
            type(exc).__name__,
        )
        return None
    if not isinstance(feerate_btc_kvb, (int, float)) or feerate_btc_kvb <= 0:
        logging.warning(
            "Pricing floor: node has no feerate estimate for conf target %d; "
            "quoting the static price",
            PRICE_CONF_TARGET,
        )
        return None
    return _btc_per_kvb_to_sat_per_vb(feerate_btc_kvb)


def _cached_feerate_sat_per_vb() -> float | None:
    """The fetched feerate, refreshed at most once per TTL. Failures are cached
    too: a dead node costs one timed-out call per TTL, not one per request."""
    global _feerate_cache
    now = time.monotonic()
    if _feerate_cache is not None and now - _feerate_cache[1] < _FEERATE_CACHE_TTL_SECONDS:
        return _feerate_cache[0]
    feerate = _fetch_feerate_sat_per_vb()
    _feerate_cache = (feerate, now)
    return feerate


def quoted_price_sats() -> int:
    """The price the 402 challenge quotes: the static price or the solvency
    floor, whichever is higher. Floats with the fee market; never refuses."""
    feerate = _cached_feerate_sat_per_vb()
    if feerate is None:
        return GATEWAY_PRICE_SATS
    floor = math.ceil(
        PRICE_TX_VSIZE_ESTIMATE * feerate * PRICE_BUMP_RESERVE * PRICE_MARGIN
    )
    return max(GATEWAY_PRICE_SATS, floor)


# ── Payment backend abstraction ──────────────────────────────────────────────
# Typed result objects and the operations the gateway depends on from a Lightning
# node, so endpoint logic doesn't depend on any one node implementation.


@dataclass(frozen=True)
class Invoice:
    """A newly created Lightning invoice."""
    bolt11: str
    payment_hash: str


@dataclass(frozen=True)
class InvoiceStatus:
    """The current state of a previously created invoice, as reported by the node.

    ``expired`` is ``None`` when the backend exposes no expiry signal,
    distinguishing "not expired" from "unknown".
    """
    settled: bool
    amount_paid_sat: int
    memo: str | None
    expired: bool | None


class PaymentBackend(Protocol):
    """Lightning payment operations the gateway depends on."""

    def create_invoice(self, digest: str, amount_sats: int) -> Invoice:
        """Create an invoice for ``amount_sats``, bound to ``digest`` (as the memo)."""
        ...

    def lookup_invoice(self, payment_hash: str) -> InvoiceStatus:
        """Look up the current state of the invoice for ``payment_hash``."""
        ...

    def health(self) -> bool:
        """Return True iff the backing node is reachable and responding."""
        ...


class LndPaymentBackend:
    """PaymentBackend backed by the LND REST API.

    Reads the ``LND_*`` / ``TOR_PROXY`` / ``LND_TLS_VERIFY`` module globals at call
    time (not construction) so configuration stays patchable and the backend never
    holds a stale copy of connection settings. Wire behavior is preserved exactly
    from the prior module-level functions.
    """

    def _proxies(self):
        return {"https": f"socks5h://{TOR_PROXY}"} if TOR_PROXY else None

    def create_invoice(self, digest: str, amount_sats: int) -> Invoice:
        headers = {"Grpc-Metadata-macaroon": LND_MACAROON_HEX}
        url = f"https://{LND_HOST}:{LND_PORT}/v1/invoices"
        try:
            resp = requests.post(
                url,
                headers=headers,
                json={"memo": digest, "value": amount_sats, "private": True},
                proxies=self._proxies(),
                verify=LND_TLS_VERIFY,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            payment_request = data["payment_request"]
            # LND returns r_hash as standard base64 over REST; normalize to hex.
            payment_hash = base64.b64decode(data["r_hash"]).hex()
            return Invoice(bolt11=payment_request, payment_hash=payment_hash)
        except Exception:
            logging.exception("LND invoice creation failed")
            raise HTTPException(status_code=502, detail="LND error: could not create invoice")

    def lookup_invoice(self, payment_hash: str) -> InvoiceStatus:
        headers = {"Grpc-Metadata-macaroon": LND_MACAROON_HEX}
        url = f"https://{LND_HOST}:{LND_PORT}/v1/invoice/{payment_hash}"
        try:
            resp = requests.get(
                url, headers=headers, proxies=self._proxies(), verify=LND_TLS_VERIFY, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logging.exception("LND invoice lookup failed")
            raise HTTPException(status_code=502, detail="LND error: could not verify payment")
        return InvoiceStatus(
            settled=bool(data.get("settled", False)),
            amount_paid_sat=int(data.get("amt_paid_sat") or 0),
            memo=data.get("memo"),
            expired=None,
        )

    def health(self) -> bool:
        headers = {"Grpc-Metadata-macaroon": LND_READONLY_MACAROON_HEX or LND_MACAROON_HEX}
        try:
            resp = requests.get(
                f"https://{LND_HOST}:{LND_PORT}/v1/getinfo",
                headers=headers,
                proxies=self._proxies(),
                verify=LND_TLS_VERIFY,
                timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception:
            logging.warning("Health check: LND unreachable")
            return False


class PhoenixdPaymentBackend:
    """PaymentBackend backed by the phoenixd HTTP API."""

    def _auth(self):
        return ("", PHOENIXD_HTTP_PASSWORD) if PHOENIXD_HTTP_PASSWORD else None

    def create_invoice(self, digest: str, amount_sats: int) -> Invoice:
        external_id = f"{digest[:16]}-{uuid.uuid4().hex}"
        try:
            resp = requests.post(
                f"{PHOENIXD_URL}/createinvoice",
                data={
                    "amountSat": amount_sats,
                    "description": digest,
                    "externalId": external_id,
                },
                auth=self._auth(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return Invoice(
                bolt11=data["serialized"],
                payment_hash=data["paymentHash"].lower(),
            )
        except Exception:
            logging.exception("phoenixd invoice creation failed")
            raise HTTPException(
                status_code=502,
                detail="Payment backend error: could not create invoice",
            )

    def lookup_invoice(self, payment_hash: str) -> InvoiceStatus:
        try:
            resp = requests.get(
                f"{PHOENIXD_URL}/payments/incoming/{payment_hash}",
                auth=self._auth(),
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logging.exception("phoenixd invoice lookup failed")
            raise HTTPException(
                status_code=502,
                detail="Payment backend error: could not verify payment",
            )

        return InvoiceStatus(
            settled=bool(data.get("isPaid", False)),
            amount_paid_sat=int(data.get("receivedSat") or 0),
            memo=data.get("description"),
            expired=data.get("isExpired") if "isExpired" in data else None,
        )

    def health(self) -> bool:
        try:
            resp = requests.get(
                f"{PHOENIXD_URL}/getinfo",
                auth=self._auth(),
                timeout=5,
            )
            resp.raise_for_status()
            return True
        except Exception:
            logging.warning("Health check: phoenixd unreachable")
            return False


def _make_payment_backend(backend_type: str) -> PaymentBackend:
    if backend_type == "lnd":
        return LndPaymentBackend()
    if backend_type == "phoenixd":
        return PhoenixdPaymentBackend()
    raise RuntimeError(f"Unknown PAYMENT_BACKEND_TYPE: {backend_type!r}")


# Phoenixd is the live default backend; PAYMENT_BACKEND_TYPE=lnd selects the
# LND test payer / alternative backend.
PAYMENT_BACKEND: PaymentBackend = _make_payment_backend(PAYMENT_BACKEND_TYPE)


def create_invoice(memo: str, amount_sats: int) -> tuple[str, str]:
    """Call LND REST API to create a Lightning invoice. Returns
    (payment_request, payment_hash_hex). Raises HTTPException 502 on any failure."""
    invoice = PAYMENT_BACKEND.create_invoice(memo, amount_sats)
    return invoice.bolt11, invoice.payment_hash


def stamp_digest(hex_digest: str) -> bytes:
    """Submit a SHA256 digest to the configured OTS backend and return the serialized .ots bytes.

    OTS_BACKEND_MODE=calendar  — submit to the operator-controlled OTS calendar at
                                  OTS_CALENDAR_URL, retrying up to OTS_SUBMIT_MAX_ATTEMPTS
                                  times with a backoff so a paid request does not fail just
                                  because otsd is still starting. No fallback to public
                                  calendars. Persistent failure -> RuntimeError.
    OTS_BACKEND_MODE=public    — submit to DEFAULT_AGGREGATORS (compatibility/testing only).
                                  Succeeds if at least one aggregator responds.
    """
    digest_bytes = bytes.fromhex(hex_digest)
    file_timestamp = DetachedTimestampFile(OpSHA256(), Timestamp(digest_bytes))

    if OTS_BACKEND_MODE == "calendar":
        last_error = None
        for attempt in range(1, OTS_SUBMIT_MAX_ATTEMPTS + 1):
            try:
                calendar_timestamp = RemoteCalendar(OTS_CALENDAR_URL).submit(
                    digest_bytes, timeout=10
                )
                file_timestamp.timestamp.merge(calendar_timestamp)
                break
            except Exception as e:
                last_error = e
                logging.warning(
                    "OTS calendar submit attempt %d/%d to %s failed: %s",
                    attempt, OTS_SUBMIT_MAX_ATTEMPTS, OTS_CALENDAR_URL, e,
                )
                if attempt < OTS_SUBMIT_MAX_ATTEMPTS:
                    time.sleep(OTS_SUBMIT_BACKOFF_SECONDS)
        else:
            logging.error(
                "OTS calendar backend %s failed after %d attempts: %s",
                OTS_CALENDAR_URL, OTS_SUBMIT_MAX_ATTEMPTS, last_error,
            )
            raise RuntimeError("OTS calendar backend failed")
    else:
        # public — compatibility/testing mode only; do not use as the real backend
        succeeded = 0
        for url in DEFAULT_AGGREGATORS:
            try:
                calendar_timestamp = RemoteCalendar(url).submit(digest_bytes, timeout=10)
                file_timestamp.timestamp.merge(calendar_timestamp)
                succeeded += 1
            except Exception:
                logging.warning("OTS public calendar %s failed", url, exc_info=True)
        if succeeded == 0:
            raise RuntimeError("All OTS public calendars failed; no timestamp was created")

    buf = io.BytesIO()
    file_timestamp.serialize(StreamSerializationContext(buf))
    return buf.getvalue()


def verify_payment(payment_hash: str, digest: str, price_sats: int) -> bool:
    """Fetch the invoice for a payment hash from LND and confirm it is settled, was
    issued for the specific digest (memo), and the paid amount meets ``price_sats``
    — the mint-time price carried in the token's signed caveat, not the current
    configured price, so a repriced gateway still honors in-flight invoices. The
    caller must already have proven that the presented preimage hashes to this
    payment hash."""
    status = PAYMENT_BACKEND.lookup_invoice(payment_hash)
    return (
        status.settled
        and status.memo == digest
        and status.amount_paid_sat >= price_sats
    )


@app.get("/")
def root():
    return {"status": "running"}


@app.get("/health")
def health():
    paused = is_paused()
    payment_status = "ok" if PAYMENT_BACKEND.health() else "error"

    if OTS_CALENDAR_URL:
        otsd_status = "ok"
        try:
            resp = requests.get(OTS_CALENDAR_URL, timeout=5)
            resp.raise_for_status()
        except Exception:
            logging.warning("Health check: otsd unreachable at %s", OTS_CALENDAR_URL)
            otsd_status = "error"
    else:
        otsd_status = "n/a"

    # Wallet liquidity alarm: read the status file left by the balance-check
    # timer. "absent" (alarm not installed) reports but does not degrade;
    # "low"/"unknown"/"stale" degrade like a backend failure.
    wallet_status = _wallet_status()

    # Proof sweep status: same file-mediated pattern. "absent" (sweep not
    # installed) reports but does not degrade; mismatch/attention/unknown/stale
    # degrade like a backend failure.
    proofs_status = _proofs_status()

    # Backup status: same file-mediated pattern. "absent" (backups not
    # installed) and "local_only" (a configuration choice, honestly reported)
    # do not degrade; attention/failed/unknown/stale degrade like a backend
    # failure.
    backup_status = _backup_status()

    if paused:
        overall = "paused"
    else:
        overall = (
            "ok"
            if payment_status == "ok"
            and otsd_status in ("ok", "n/a")
            and wallet_status in ("ok", "absent")
            and proofs_status in ("ok", "absent")
            and backup_status in ("ok", "local_only", "absent")
            else "degraded"
        )

    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "paused": paused,
            "payment": payment_status,
            "payment_backend": PAYMENT_BACKEND_TYPE,
            "otsd": otsd_status,
            "wallet": wallet_status,
            "proofs": proofs_status,
            "backup": backup_status,
        },
    )


@app.post("/verify")
def verify(body: VerifyRequest):
    try:
        ots_bytes = base64.b64decode(body.ots, validate=True)
    except Exception:
        return JSONResponse(
            status_code=200,
            content={
                "digest": body.digest,
                "proof_digest": None,
                "status": "invalid",
                "valid_ots": False,
                "digest_match": False,
                "bitcoin_anchored": False,
                "verified": False,
                "attestations": [],
            },
        )

    if len(ots_bytes) > MAX_VERIFY_OTS_BYTES:
        raise HTTPException(status_code=413, detail="OTS proof too large")

    return JSONResponse(content=_verify_ots_bytes(body.digest, ots_bytes))


@app.post("/upgrade")
def upgrade(body: VerifyRequest):
    try:
        ots_bytes = base64.b64decode(body.ots, validate=True)
    except Exception:
        return JSONResponse(
            status_code=200,
            content={
                "digest": body.digest,
                "proof_digest": None,
                "status": "invalid",
                "valid_ots": False,
                "digest_match": False,
                "bitcoin_anchored": False,
                "verified": False,
                "ots": None,
                "attestations": [],
            },
        )
    if len(ots_bytes) > MAX_VERIFY_OTS_BYTES:
        raise HTTPException(status_code=413, detail="OTS proof too large")
    return JSONResponse(content=_upgrade_ots_bytes(body.digest, ots_bytes))


@app.post("/timestamp")
def timestamp(body: TimestampRequest, request: Request):
    if is_paused():
        raise HTTPException(
            status_code=503,
            detail="Gateway is paused by operator",
        )

    auth = request.headers.get("Authorization", "")

    if auth:
        parsed = parse_l402_auth(auth)
        if not parsed:
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header; expected 'L402 <macaroon>:<preimage>'",
            )
        macaroon_b64, preimage_hex = parsed

        # 1. Token must be valid and bound to THIS digest (401 otherwise).
        payment_hash, mint_price_sats = verify_l402_token(macaroon_b64, body.digest)

        # 2. The presented preimage must hash to the token's payment hash.
        derived = hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest()
        if derived != payment_hash:
            raise HTTPException(status_code=401, detail="Preimage does not match token payment hash")

        # 3. The invoice must be settled, for this digest, at the mint-time amount.
        if not verify_payment(payment_hash, body.digest, mint_price_sats):
            raise HTTPException(status_code=402, detail="Payment required or not settled")

        # 4. Return cached proof if this payment_hash was already redeemed.
        if payment_hash in _proof_cache:
            logging.info("Returning cached proof for payment_hash %s", payment_hash)
            ots_bytes = _proof_cache[payment_hash]
            return Response(
                content=ots_bytes,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={body.digest}.ots"},
            )

        # 5. Durably record the paid obligation BEFORE stamping. If stamping fails
        #    below, the row stays 'needs_stamp' and the sweeper recovers it — the
        #    settled payment is never lost. Idempotent on payment_hash.
        record_obligation(payment_hash, body.digest)

        try:
            ots_bytes = stamp_digest(body.digest)
        except Exception:
            logging.exception("OTS stamping failed")
            raise HTTPException(status_code=502, detail="OTS error: stamping failed")

        # 6. Stamped: populate the instant re-serve cache AND close the obligation.
        _proof_cache_put(payment_hash, ots_bytes)
        mark_obligation_stamped(payment_hash)
        return Response(
            content=ots_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={body.digest}.ots"},
        )

    # No authorization — mint an invoice and an L402 token bound to it. The
    # quote floats with the solvency floor; invoice, macaroon, and body all
    # carry the same mint-time amount, which is what redemption later enforces.
    # Rate-limit before touching any backend: a 429 costs neither phoenixd nor
    # the fee-estimation RPC anything.
    retry_after = _rate_limit_retry_after(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={"status": "rate_limited", "retry_after_seconds": retry_after},
        )
    quoted = quoted_price_sats()
    payment_request, payment_hash = create_invoice(body.digest, quoted)
    expiry_ts = int(time.time()) + L402_TOKEN_EXPIRY_SECONDS
    token = mint_l402_token(body.digest, payment_hash, quoted, expiry_ts)
    raise HTTPException(
        status_code=402,
        headers={"WWW-Authenticate": f'L402 macaroon="{token}", invoice="{payment_request}"'},
        detail={
            "status": "payment_required",
            "price_sats": quoted,
            "invoice": payment_request,
            "macaroon": token,
            "expiry": expiry_ts,
        },
    )
