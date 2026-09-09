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
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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
    price_per_proof_sats: int | None
    stamper_fee_cap_sats: int
    pause_file: str
    lnd_tls_verify: bool
    ots_backend_mode: str
    ots_calendar_url: str | None
    l402_enabled: bool
    l402_secret: bytes | None
    l402_token_expiry_seconds: int
    ots_submit_max_attempts: int
    ots_submit_backoff_seconds: float
    payment_backend_type: str
    phoenixd_url: str
    phoenixd_http_password_limited: str | None
    obligations_db_path: str
    obligation_sweep_interval: int
    wallet_status_path: str
    wallet_status_max_age_seconds: int
    proofs_status_path: str
    proofs_status_max_age_seconds: int
    backup_status_path: str
    backup_status_max_age_seconds: int
    rate_limit_per_minute: int
    verify_rate_limit_per_minute: int
    gateway_behind_proxy: bool
    anchor_billing_enabled: bool
    per_record_sats: int | None
    anchor_receipts_path: str | None
    anchor_bills_token: str | None
    upgrade_client_token: str | None
    health_probe_cache_seconds: int


def _parse_config() -> GatewayConfig:
    """Parse and validate all required env vars. Raises RuntimeError on misconfiguration."""
    # Determine payment backend type early so we know which vars are required.
    # phoenixd is the live default; lnd is the test payer / alternative backend.
    payment_backend_type_early = os.getenv("PAYMENT_BACKEND_TYPE", "phoenixd").lower()

    required = {
        "OTS_BACKEND_MODE": os.getenv("OTS_BACKEND_MODE"),
    }
    if payment_backend_type_early == "lnd":
        required["LND_HOST"] = os.getenv("LND_HOST")
        required["LND_PORT"] = os.getenv("LND_PORT")
        required["LND_MACAROON_HEX"] = os.getenv("LND_MACAROON_HEX")

    missing = [name for name, val in required.items() if not val]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    # Retired pricing model (one warning, never a failure)
    # The quote is the flat PRICE_PER_PROOF_SATS; the gateway reads no
    # feerates for any purpose. Old-model variables found in the environment
    # are named once and ignored, so no operator's env goes silently inert.
    # PRICE_RPC_URL fires only when itself explicitly set — never on
    # BITCOIN_RPC_SERVICE_URL, which remains legitimately set for otsd.
    retired_present = [
        name
        for name in (
            "GATEWAY_PRICE_SATS",
            "MIN_GATEWAY_PRICE_SATS",
            "PRICE_BLIND_SATS",
            "PRICE_BUMP_RESERVE",
            "PRICE_MARGIN",
            "PRICE_TX_VSIZE_ESTIMATE",
            "PRICE_CONF_TARGET",
            "PRICE_RPC_URL",
        )
        if os.getenv(name) not in (None, "")
    ]
    if retired_present:
        logging.warning(
            "Retired pricing variables present and ignored: %s. The quote is "
            "now the flat PRICE_PER_PROOF_SATS; the gateway reads no feerates. "
            "Remove them from .env.",
            ", ".join(retired_present),
        )

    # PRICE_MARKUP retired separately: its successor is the anchor-bill rate,
    # not the quote, so the feerate-era message above would misname it. Same
    # mechanics — fires only when itself explicitly set, never a failure.
    if os.getenv("PRICE_MARKUP") not in (None, ""):
        logging.warning(
            "PRICE_MARKUP present and ignored: anchor bills are now "
            "records × PER_RECORD_SATS. Remove it from .env."
        )

    # L402 door switch. true (default): /timestamp charges the flat per-proof
    # rate. false: records are stamped free and the per-record cost lands on
    # the anchor bill (records × PER_RECORD_SATS per confirmed anchor, when
    # billing is on). Strict true/false: a typo silently read as a mode would
    # change what the door charges.
    l402_enabled_raw = os.getenv("L402_ENABLED", "true").lower()
    if l402_enabled_raw not in ("true", "false"):
        raise RuntimeError(
            f"L402_ENABLED must be 'true' or 'false', got {l402_enabled_raw!r}"
        )
    l402_enabled = l402_enabled_raw == "true"

    # Flat per-proof price. Required with no default when the door is on: the
    # price is an operator decision. With the door off it is never read — an
    # .env legitimately holds both modes' vars. Zero is refused: a token
    # minted at 0 could never redeem (the price caveat regexes reject "0");
    # the free door is L402_ENABLED=false.
    price_per_proof: int | None = None
    if l402_enabled:
        price_per_proof_raw = os.getenv("PRICE_PER_PROOF_SATS")
        if price_per_proof_raw is None or price_per_proof_raw == "":
            raise RuntimeError(
                "PRICE_PER_PROOF_SATS is required — the flat price in sats every "
                "hash pays at submission. Size it with the operator guide's "
                '"Pricing" arithmetic. To run a free door instead, set '
                "L402_ENABLED=false."
            )
        try:
            price_per_proof = int(price_per_proof_raw)
        except ValueError:
            raise RuntimeError("PRICE_PER_PROOF_SATS must be an integer")
        if price_per_proof < 1:
            raise RuntimeError(
                f"PRICE_PER_PROOF_SATS must be >= 1, got {price_per_proof}. "
                "For a free door set L402_ENABLED=false."
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

    # L402 token signing key
    # Root key used to sign and verify L402 macaroons. It is required in
    # production: a stable key means a paid-but-not-yet-redeemed token still
    # verifies after a gateway restart. A random per-process key is allowed only
    # as an explicit development opt-out, because it would invalidate such tokens
    # on every restart. Never read with the door off — no token is minted or
    # verified there.
    l402_secret: bytes | None = None
    if l402_enabled:
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

    # OTS submission retry (otsd-not-ready resilience)
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
    phoenixd_http_password_limited = (
        os.getenv("PHOENIXD_HTTP_PASSWORD_LIMITED")
        # Pre-rename alias: an existing .env keeps working until swapped.
        or os.getenv("PHOENIXD_HTTP_PASSWORD")
        or None
    )

    # Durable obligation log
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

    # Wallet liquidity alarm (file-mediated; NO Bitcoin RPC from the gateway)
    # /health reads the status file written by ops/wallet-balance-check.sh.
    # The gateway never holds a wallet credential and makes no Bitcoin RPC
    # calls at all. Its one scoped credential is PHOENIXD_HTTP_PASSWORD_LIMITED
    # (old name PHOENIXD_HTTP_PASSWORD read as a fallback), which must be
    # phoenixd's http-password-limited-access key — the gateway calls only
    # createinvoice, payments/incoming, and getinfo (PhoenixdPaymentBackend),
    # all covered by the limited key, which cannot reach /payinvoice.
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
    # defaults to 2x the daily timer.
    backup_status_path = os.getenv(
        "BACKUP_STATUS_PATH", "/var/lib/timestamp-gateway/backup-status"
    )
    try:
        backup_status_max_age = int(os.getenv("BACKUP_STATUS_MAX_AGE_SECONDS", "172800"))
    except ValueError:
        raise RuntimeError("BACKUP_STATUS_MAX_AGE_SECONDS must be an integer")
    if backup_status_max_age <= 0:
        raise RuntimeError("BACKUP_STATUS_MAX_AGE_SECONDS must be a positive integer")

    # Stamper fee cap (float backstop thresholds only)
    # Mirrors otsd's --btc-max-fee flag: the most one anchor cycle can spend.
    # The flag takes BTC and this takes sats (0.0002 BTC = 20,000 sats; keep
    # the two in sync). The gateway uses it only
    # to derive the float backstop thresholds; it makes no Bitcoin RPC calls.
    try:
        stamper_fee_cap = int(os.getenv("STAMPER_FEE_CAP_SATS", "20000"))
    except ValueError:
        raise RuntimeError("STAMPER_FEE_CAP_SATS must be an integer")
    if stamper_fee_cap <= 0:
        raise RuntimeError(
            f"STAMPER_FEE_CAP_SATS must be a positive integer, got {stamper_fee_cap}"
        )

    # Invoice-mint rate limit
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

    # The free proof endpoints (/verify, /upgrade) get their own budget: they
    # cost gateway CPU and private-calendar round-trips, not phoenixd storage,
    # and a client legitimately polls /upgrade while an anchor pends — free
    # traffic must never be able to starve the paid mint path.
    try:
        verify_rate_limit_per_minute = int(os.getenv("VERIFY_RATE_LIMIT_PER_MINUTE", "30"))
    except ValueError:
        raise RuntimeError("VERIFY_RATE_LIMIT_PER_MINUTE must be an integer")
    if verify_rate_limit_per_minute < 0:
        raise RuntimeError(
            f"VERIFY_RATE_LIMIT_PER_MINUTE must be >= 0 (0 disables the limit), "
            f"got {verify_rate_limit_per_minute}"
        )

    # Upgrade client token (optional). A client presenting it as a bearer on
    # /upgrade is not drawn from the per-peer verify bucket: the free door
    # can hand out thousands of pending proofs an hour, and the client that
    # owns them must be able to finish them. It exempts /upgrade only — a
    # wrong token is simply anonymous, and unset makes the header inert.
    upgrade_client_token = os.getenv("UPGRADE_CLIENT_TOKEN") or None

    # /health's calendar probe is cached for this many seconds behind a
    # single-flight lock: however many callers hit /health at once, the
    # calendar renders its homepage (four bitcoind RPCs) at most once per
    # window. 0 disables the cache (every call probes).
    try:
        health_probe_cache_seconds = int(os.getenv("HEALTH_PROBE_CACHE_SECONDS", "15"))
    except ValueError:
        raise RuntimeError("HEALTH_PROBE_CACHE_SECONDS must be an integer")
    if health_probe_cache_seconds < 0:
        raise RuntimeError("HEALTH_PROBE_CACHE_SECONDS must be >= 0 (0 disables the cache)")

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

    # Anchor billing (part two of the pricing model)
    # Off by default: disabled means the three billing vars are never read and
    # every behavior stays byte-identical. Strict true/false: a typo silently
    # treated as false would turn billing off without a word.
    anchor_billing_raw = os.getenv("ANCHOR_BILLING_ENABLED", "false").lower()
    if anchor_billing_raw not in ("true", "false"):
        raise RuntimeError(
            f"ANCHOR_BILLING_ENABLED must be 'true' or 'false', got {anchor_billing_raw!r}"
        )
    anchor_billing_enabled = anchor_billing_raw == "true"

    per_record_sats: int | None = None
    anchor_receipts_path: str | None = None
    anchor_bills_token: str | None = None
    if anchor_billing_enabled:
        # All three are operator decisions with no sane default. Every missing
        # one is named in a single error so the operator fixes them in one pass.
        missing_billing = []
        if not os.getenv("PER_RECORD_SATS"):
            missing_billing.append(
                "PER_RECORD_SATS — the price in sats per record inside each "
                "anchor (a bill is records × this rate)"
            )
        if not os.getenv("ANCHOR_RECEIPTS_PATH"):
            missing_billing.append(
                "ANCHOR_RECEIPTS_PATH — the anchor-receipts JSONL file the "
                "calendar fork writes (the path its OTSD_ANCHOR_RECEIPTS names)"
            )
        if not os.getenv("ANCHOR_BILLS_TOKEN"):
            missing_billing.append(
                "ANCHOR_BILLS_TOKEN — the bearer token guarding GET "
                "/anchor-bills; operational history is not public"
            )
        if missing_billing:
            raise RuntimeError(
                "ANCHOR_BILLING_ENABLED=true requires: " + "; ".join(missing_billing)
            )
        # Same rule as PRICE_PER_PROOF_SATS: integer >= 1; zero is refused.
        try:
            per_record_sats = int(os.getenv("PER_RECORD_SATS"))
        except ValueError:
            raise RuntimeError("PER_RECORD_SATS must be an integer")
        if per_record_sats < 1:
            raise RuntimeError(
                f"PER_RECORD_SATS must be >= 1, got {per_record_sats}. "
                "To bill nothing set ANCHOR_BILLING_ENABLED=false."
            )
        anchor_receipts_path = os.getenv("ANCHOR_RECEIPTS_PATH")
        anchor_bills_token = os.getenv("ANCHOR_BILLS_TOKEN")

    # Mode announcement (free door)
    # One line at startup naming what charges. Free door with billing on is
    # INFO: the anchor bill carries the per-record cost. Free door with
    # billing off means nothing charges anywhere — a valid choice (a
    # subsidising operator), named once at WARNING, never silent; the warning
    # replaces the INFO line so each mode is announced exactly once.
    if not l402_enabled:
        if anchor_billing_enabled:
            logging.info(
                "L402_ENABLED=false: free door — records are stamped free of "
                "charge; anchor billing carries the charges (records × "
                "PER_RECORD_SATS per confirmed anchor)."
            )
        else:
            logging.warning(
                "L402_ENABLED=false and ANCHOR_BILLING_ENABLED=false: nothing "
                "charges anywhere — the door is free and no anchor bills are "
                "minted."
            )

    return GatewayConfig(
        lnd_host=os.getenv("LND_HOST"),
        lnd_port=os.getenv("LND_PORT"),
        lnd_macaroon_hex=os.getenv("LND_MACAROON_HEX"),
        tor_proxy=os.getenv("TOR_PROXY") or None,  # optional; None = direct connection
        price_per_proof_sats=price_per_proof,
        stamper_fee_cap_sats=stamper_fee_cap,
        pause_file=pause_file,
        lnd_tls_verify=os.getenv("LND_TLS_VERIFY", "false").lower() == "true",
        ots_backend_mode=mode,
        ots_calendar_url=calendar_url,
        l402_enabled=l402_enabled,
        l402_secret=l402_secret,
        l402_token_expiry_seconds=l402_expiry,
        ots_submit_max_attempts=ots_max_attempts,
        ots_submit_backoff_seconds=ots_backoff,
        payment_backend_type=payment_backend_type,
        phoenixd_url=phoenixd_url,
        phoenixd_http_password_limited=phoenixd_http_password_limited,
        obligations_db_path=obligations_db_path,
        obligation_sweep_interval=obligation_sweep_interval,
        wallet_status_path=wallet_status_path,
        wallet_status_max_age_seconds=wallet_status_max_age,
        proofs_status_path=proofs_status_path,
        proofs_status_max_age_seconds=proofs_status_max_age,
        backup_status_path=backup_status_path,
        backup_status_max_age_seconds=backup_status_max_age,
        rate_limit_per_minute=rate_limit_per_minute,
        verify_rate_limit_per_minute=verify_rate_limit_per_minute,
        gateway_behind_proxy=gateway_behind_proxy,
        anchor_billing_enabled=anchor_billing_enabled,
        per_record_sats=per_record_sats,
        anchor_receipts_path=anchor_receipts_path,
        anchor_bills_token=anchor_bills_token,
        upgrade_client_token=upgrade_client_token,
        health_probe_cache_seconds=health_probe_cache_seconds,
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
PRICE_PER_PROOF_SATS = _CONFIG.price_per_proof_sats
STAMPER_FEE_CAP_SATS = _CONFIG.stamper_fee_cap_sats
PAUSE_FILE = _CONFIG.pause_file
LND_TLS_VERIFY = _CONFIG.lnd_tls_verify
OTS_BACKEND_MODE = _CONFIG.ots_backend_mode
OTS_CALENDAR_URL = _CONFIG.ots_calendar_url
L402_ENABLED = _CONFIG.l402_enabled
L402_SECRET = _CONFIG.l402_secret
L402_TOKEN_EXPIRY_SECONDS = _CONFIG.l402_token_expiry_seconds
OTS_SUBMIT_MAX_ATTEMPTS = _CONFIG.ots_submit_max_attempts
OTS_SUBMIT_BACKOFF_SECONDS = _CONFIG.ots_submit_backoff_seconds
PAYMENT_BACKEND_TYPE = _CONFIG.payment_backend_type
PHOENIXD_URL = _CONFIG.phoenixd_url
PHOENIXD_HTTP_PASSWORD_LIMITED = _CONFIG.phoenixd_http_password_limited
OBLIGATIONS_DB_PATH = _CONFIG.obligations_db_path
OBLIGATION_SWEEP_INTERVAL = _CONFIG.obligation_sweep_interval
WALLET_STATUS_PATH = _CONFIG.wallet_status_path
WALLET_STATUS_MAX_AGE_SECONDS = _CONFIG.wallet_status_max_age_seconds
PROOFS_STATUS_PATH = _CONFIG.proofs_status_path
PROOFS_STATUS_MAX_AGE_SECONDS = _CONFIG.proofs_status_max_age_seconds
BACKUP_STATUS_PATH = _CONFIG.backup_status_path
BACKUP_STATUS_MAX_AGE_SECONDS = _CONFIG.backup_status_max_age_seconds
RATE_LIMIT_PER_MINUTE = _CONFIG.rate_limit_per_minute
VERIFY_RATE_LIMIT_PER_MINUTE = _CONFIG.verify_rate_limit_per_minute
GATEWAY_BEHIND_PROXY = _CONFIG.gateway_behind_proxy
ANCHOR_BILLING_ENABLED = _CONFIG.anchor_billing_enabled
PER_RECORD_SATS = _CONFIG.per_record_sats
ANCHOR_RECEIPTS_PATH = _CONFIG.anchor_receipts_path
ANCHOR_BILLS_TOKEN = _CONFIG.anchor_bills_token
UPGRADE_CLIENT_TOKEN = _CONFIG.upgrade_client_token
HEALTH_PROBE_CACHE_SECONDS = _CONFIG.health_probe_cache_seconds

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


# Invoice-mint rate limiter
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
# Same shape for the free proof endpoints (/verify, /upgrade): a separate
# budget so free traffic can never starve the paid mint path. Same
# _RATE_BUCKETS_MAX — past 10k rotating source addresses a per-IP limiter
# is the wrong tool regardless of endpoint.
_verify_rate_buckets: dict[str, tuple[float, float]] = {}


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


def _bucket_retry_after(
    buckets: dict[str, tuple[float, float]], ip: str, per_minute: int
) -> int | None:
    """Take one token from ip's bucket. Returns None when the request is
    allowed, else the whole seconds (>= 1) until a token accrues. Capacity and
    refill rate are both per_minute, so a full bucket is one minute's
    allowance of burst. A denied request consumes nothing. One implementation
    for every bucket dict, so the FIFO eviction bound cannot silently die in
    a copy."""
    if per_minute <= 0:
        return None
    now = time.monotonic()
    tokens, last_refill = buckets.get(ip, (float(per_minute), now))
    tokens = min(
        float(per_minute),
        tokens + (now - last_refill) * per_minute / 60,
    )
    if tokens >= 1:
        tokens -= 1
        retry_after = None
    else:
        retry_after = math.ceil((1 - tokens) * 60 / per_minute)
    if ip not in buckets and len(buckets) >= _RATE_BUCKETS_MAX:
        del buckets[next(iter(buckets))]
    buckets[ip] = (tokens, now)
    return retry_after


def _rate_limit_retry_after(ip: str) -> int | None:
    """Invoice-mint bucket: bounds what a spammer can make phoenixd do."""
    return _bucket_retry_after(_rate_buckets, ip, RATE_LIMIT_PER_MINUTE)


def _verify_rate_limit_retry_after(ip: str) -> int | None:
    """/verify + /upgrade bucket (one budget for both — they contend for the
    same resources: gateway CPU and private-calendar round-trips)."""
    return _bucket_retry_after(_verify_rate_buckets, ip, VERIFY_RATE_LIMIT_PER_MINUTE)


# Durable obligation log
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
            # The sweeper's scan (WHERE status='needs_stamp') must not decay
            # into a full-table walk as years of stamped history accumulate.
            # A partial index holds only un-swept rows — near-empty in steady
            # state — and IF NOT EXISTS is the whole migration on an existing
            # database: the index builds once at the next startup.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS obligations_needs_stamp "
                "ON obligations(payment_hash) WHERE status='needs_stamp'"
            )
            # Gated on the flag, not created unconditionally: with billing
            # disabled the gateway must stay byte-identical, including the
            # bytes of a disabled operator's obligations.db.
            if ANCHOR_BILLING_ENABLED:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS anchor_bills (
                        txid               TEXT PRIMARY KEY,
                        fee_sats           INTEGER NOT NULL,
                        commitments        INTEGER NOT NULL,
                        confirmed_height   INTEGER NOT NULL,
                        confirmed_at       INTEGER NOT NULL,
                        records            INTEGER,
                        amount_sats        INTEGER NOT NULL,
                        payment_hash       TEXT,
                        bolt11             TEXT,
                        invoice_created_at INTEGER,
                        status             TEXT NOT NULL DEFAULT 'unpaid'
                                           CHECK (status IN ('unpaid', 'paid')),
                        paid_at            INTEGER
                    )
                    """
                )
                # Additive upgrade for a markup-era table: records is NULL on
                # rows billed under the old formula — their stored amounts
                # stay untouched (nullable column, no rewrite).
                cols = [
                    row[1]
                    for row in conn.execute("PRAGMA table_info(anchor_bills)")
                ]
                if "records" not in cols:
                    conn.execute(
                        "ALTER TABLE anchor_bills ADD COLUMN records INTEGER"
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
    conn = _obligation_connect()
    try:
        conn.execute(
            "UPDATE obligations SET status='stamped' WHERE payment_hash=?",
            (payment_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def _hash8(payment_hash: str) -> str:
    """Routine-log form of a payment hash: first 8 hex + ellipsis. Enough to
    correlate lines within a session; not enough to identify the payment on
    the Lightning network. WARNING-level incident lines keep the full hash."""
    return payment_hash[:8] + "…"


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
        logging.info("Sweeper: recovered obligation %s", _hash8(payment_hash))


def _sweeper_tick() -> None:
    """One sweeper cycle. PAUSED and the float auto-pause both skip it;
    'needs_stamp' rows wait for unpause or recovery — nothing is dropped."""
    if is_paused():
        logging.info("Sweeper: gateway paused; skipping sweep")
        return
    if is_float_stopped():
        logging.info("Sweeper: float backstop auto-pause; skipping sweep")
        return
    _sweep_obligations_once()


def _sweeper_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            _sweeper_tick()
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


# Request body cap
# The largest legitimate body is a /verify or /upgrade proof: 256 KiB of
# proof, base64-encoded, inside a small JSON object. Everything else is a
# 64-hex digest. Without a cap, uvicorn buffers whatever a client sends
# before pydantic sees it (a 120 MB body inflated the process by ~600 MB
# before the 413 in the 2026-09-08 review), so the cap is answered by a pure
# ASGI middleware before a single body byte is read: 413 above the cap, and
# 411 for a body that declares no length (chunked) on the POST routes.
MAX_REQUEST_BYTES = 512 * 1024


class _BodyCap:
    def __init__(self, app):
        self.app = app

    async def _answer(self, send, status: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method") in ("POST", "PUT", "PATCH"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            length = headers.get("content-length")
            if length is None or not re.fullmatch(r"[0-9]+", length):
                await self._answer(send, 411, "Content-Length is required; chunked bodies are not accepted")
                return
            if int(length) > MAX_REQUEST_BYTES:
                await self._answer(send, 413, "Request body too large")
                return
        await self.app(scope, receive, send)


app.add_middleware(_BodyCap)


def is_paused() -> bool:
    return bool(PAUSE_FILE and Path(PAUSE_FILE).exists())


@app.middleware("http")
async def _pause_gate(request, call_next):
    # PAUSED (the operator's file) and the float auto-pause both stop
    # everything except /health. Nothing is dropped: paid tokens redeem after
    # unpause and recorded obligations wait in the log. The auto-pause never
    # touches the operator's file and clears itself when the wallet-status
    # file shows the balance recovered.
    if request.url.path != "/health":
        if is_paused():
            return JSONResponse(status_code=503, content={"detail": "Gateway is paused by operator"})
        if is_float_stopped():
            return JSONResponse(
                status_code=503,
                content={"detail": "Gateway auto-paused: anchor wallet below the stamper fee cap"},
            )
    return await call_next(request)


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


# Float backstop
# The anchor wallet funds anchoring. Its balance is read from the SAME
# wallet-status file the liquidity alarm uses (ops/wallet-balance-check.sh) —
# the gateway holds no wallet credential and makes no Bitcoin RPC calls.
# Thresholds derive from STAMPER_FEE_CAP_SATS (the most one anchor cycle can
# spend): below 5 × cap the float is an alarm; below 1 × cap the next anchor
# cycle may be unaffordable, so the gateway stops itself.

_FLOAT_ALARM_CAP_MULTIPLE = 5
_FLOAT_STOP_CAP_MULTIPLE = 1


def _wallet_balance_sats() -> int | None:
    """The last balance reading from the wallet-status file, or None when no
    reading is available (file absent, unreadable, or balance null — the
    script writes null when its RPC fails). Freshness is deliberately not
    consulted: the last reading stands until replaced; staleness alarms
    separately through the wallet field."""
    try:
        data = json.loads(Path(WALLET_STATUS_PATH).read_text())
    except Exception:
        return None
    balance = data.get("balance_sats") if isinstance(data, dict) else None
    if isinstance(balance, bool) or not isinstance(balance, int):
        return None
    return balance


def _float_state() -> str:
    """Float backstop classification:
      ok       — balance at or above 5 × STAMPER_FEE_CAP_SATS
      alarm    — below 5 × cap: refill; sales continue, /health degrades
      stop     — below 1 × cap: automatic full stop (own state, distinct from
                 the operator's PAUSED file; clears itself on recovery)
      inactive — no balance reading available (e.g. the ops timers are not
                 installed): backstop off, reported, never degrades
    """
    balance = _wallet_balance_sats()
    if balance is None:
        return "inactive"
    if balance < _FLOAT_STOP_CAP_MULTIPLE * STAMPER_FEE_CAP_SATS:
        return "stop"
    if balance < _FLOAT_ALARM_CAP_MULTIPLE * STAMPER_FEE_CAP_SATS:
        return "alarm"
    return "ok"


def is_float_stopped() -> bool:
    return _float_state() == "stop"


def _proofs_status() -> str:
    """Classify the proofs-status file written by ops/upgrade-all-proofs.sh.

    File-mediated like the wallet alarm: the sweep timer scans and upgrades the
    proof artifacts; /health only reads the file it leaves behind. Returns one of:
      ok        — every proof is anchored or still waiting for Bitcoin
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
                   choice; reported, does not degrade health)
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
            attestations.append({"type": "bitcoin", "block_height": height})
        else:
            attestations.append(
                {
                    "type": "unknown",
                    "description": repr(attestation),
                }
            )
    return attestations


def _undecodable_result(digest: str, upgrade: bool = False) -> dict:
    """The /verify (and, with upgrade=True, /upgrade) answer for an ``ots``
    field that is not even base64: the same "invalid" shape the parsers
    return for malformed bytes, so a client sees one vocabulary."""
    result = {
        "digest": digest,
        "proof_digest": None,
        "status": "invalid",
        "valid_ots": False,
        "digest_match": False,
        "bitcoin_anchored": False,
        "verified": False,
    }
    if upgrade:
        result["ots"] = None
    result["attestations"] = []
    return result


def _verify_ots_bytes(digest: str, ots_bytes: bytes) -> dict:
    try:
        ctx = StreamDeserializationContext(io.BytesIO(ots_bytes))
        detached = DetachedTimestampFile.deserialize(ctx)
    except Exception:
        logging.info("Verify failed: invalid OTS proof")
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
            logging.info("Upgrade: no operator attestation available")
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
        logging.info("Upgrade failed: invalid OTS proof")
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


# L402 token (macaroon)
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
    # Settlement outranks expiry: a client who paid is owed the proof. The
    # expiry caveat is advisory — it mirrors the unpaid invoice's Lightning
    # lifetime — and redemption is gated by the settlement check, never this
    # clock. Format-only, like _payment_hash_satisfier.
    return re.fullmatch(r"expiry=[0-9]+", _caveat_text(caveat_id)) is not None


def _payment_hash_satisfier(caveat_id) -> bool:
    return re.fullmatch(r"payment_hash=[0-9a-f]{64}", _caveat_text(caveat_id)) is not None


def _price_satisfier(caveat_id) -> bool:
    return re.fullmatch(r"price=[1-9][0-9]*", _caveat_text(caveat_id)) is not None


def verify_l402_token(macaroon_b64: str, digest: str) -> tuple[str, int]:
    """Verify an L402 macaroon against the request digest and the server root key.

    Checks token integrity (signature), the digest binding, the capability
    binding, and the expiry. The price is read from the macaroon's own signed
    caveat rather than compared to the current configured price: a token minted
    at price N validates at N whenever its settled invoice backs it, so
    repricing between challenge and payment never strands an in-flight
    invoice. The HMAC prevents a client
    from lowering the caveat; what the mint-time price must buy is enforced
    against the settled invoice in verify_payment. On success returns
    (payment_hash, mint_time_price_sats). Any invalid, tampered, or
    wrong-digest token raises HTTPException 401 — an authorization failure,
    not a payment one."""
    try:
        m = Macaroon.deserialize(macaroon_b64)
    except Exception as exc:
        # Unauthenticated input: one WARNING, no traceback — a stranger must not
        # be able to write ERROR-level stack traces into the journal at will.
        logging.warning("L402 macaroon could not be parsed: %r", exc)
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    try:
        payment_hash = _caveat_value(m, "payment_hash")
        price_str = _caveat_value(m, "price")
    except Exception as exc:
        logging.warning("L402 macaroon caveats could not be read: %r", exc)
        raise HTTPException(status_code=401, detail="Invalid L402 token")

    if not payment_hash or not re.fullmatch(r"[0-9a-f]{64}", payment_hash):
        raise HTTPException(status_code=401, detail="Invalid L402 token")

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
        raise HTTPException(status_code=401, detail="Invalid or wrong-digest L402 token")
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


# Pricing
# Flat rate: every hash pays PRICE_PER_PROOF_SATS at submission. The quote
# consults nothing else — no feerate, no estimator, no floor logic; the
# gateway makes no Bitcoin RPC calls. Anchoring costs are the operator's side
# of the ledger; the operator guide's "Pricing" section carries the sizing
# arithmetic.


def quoted_price_sats() -> int:
    """The price the 402 challenge quotes: the flat per-proof rate."""
    return PRICE_PER_PROOF_SATS


# Payment backend abstraction
# Typed result objects and the operations the gateway depends on from a Lightning
# node, so endpoint logic doesn't depend on any one node implementation.


@dataclass(frozen=True)
class Invoice:
    bolt11: str
    payment_hash: str


@dataclass(frozen=True)
class InvoiceStatus:
    """The current state of a previously created invoice, as reported by the node.

    ``expired`` is ``None`` when the backend exposes no expiry signal,
    distinguishing "not expired" from "unknown".

    Two amounts, not one: phoenixd reports the credited amount net of any
    ACINQ liquidity fee, so a fully paid invoice would look underpaid if the
    face amount and the credited amount were conflated.
    """
    settled: bool
    # The invoice's face amount — what the gateway itself minted at quote
    # time. phoenixd: requestedSat. LND: value.
    amount_requested_sat: int
    # Sats actually credited to our wallet. phoenixd: receivedSat (net of any
    # ACINQ liquidity fee). LND: amt_paid_sat (gross; LND has no receive-side
    # deduction, so credited = paid).
    amount_received_sat: int
    memo: str | None
    expired: bool | None


# The last real invoice mint the gateway attempted, recorded inside both
# backends' create_invoice (the L402 challenge and anchor-bill minting both
# land there). /health derives its payment field from this record, not from
# a reachability probe: getinfo can answer ok while createinvoice is wedged,
# and minting is the capability the field vouches for. Passive — no prober,
# no timers — so a freshly started gateway reports "unknown" until the first
# real mint. Replaced wholesale on every attempt (never mutated in place) so
# /health always reads one consistent snapshot.
_last_mint = {"result": None, "at": None, "detail": None}


def _record_mint(result: str, detail: str | None = None) -> None:
    """Record the outcome of a real mint attempt: result "ok"/"failed", a UTC
    timestamp, and a short failure reason (exception class name; the full
    traceback is already in the log)."""
    global _last_mint
    _last_mint = {
        "result": result,
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": detail,
    }


class PaymentBackend(Protocol):
    """Lightning payment operations the gateway depends on."""

    def create_invoice(self, digest: str, amount_sats: int) -> Invoice:
        """Create an invoice for ``amount_sats``, bound to ``digest`` (as the memo)."""
        ...

    def lookup_invoice(self, payment_hash: str) -> InvoiceStatus:
        """Look up the current state of the invoice for ``payment_hash``."""
        ...


class LndPaymentBackend:
    """PaymentBackend backed by the LND REST API.

    Reads the ``LND_*`` / ``TOR_PROXY`` / ``LND_TLS_VERIFY`` module globals at call
    time (not construction) so configuration stays patchable and the backend never
    holds a stale copy of connection settings.
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
            invoice = Invoice(bolt11=payment_request, payment_hash=payment_hash)
            _record_mint("ok")
            return invoice
        except Exception as exc:
            _record_mint("failed", type(exc).__name__)
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
            amount_requested_sat=int(data.get("value") or 0),
            amount_received_sat=int(data.get("amt_paid_sat") or 0),
            memo=data.get("memo"),
            expired=None,
        )


class PhoenixdPaymentBackend:
    def _auth(self):
        return (
            ("", PHOENIXD_HTTP_PASSWORD_LIMITED)
            if PHOENIXD_HTTP_PASSWORD_LIMITED
            else None
        )

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
            invoice = Invoice(
                bolt11=data["serialized"],
                payment_hash=data["paymentHash"].lower(),
            )
            _record_mint("ok")
            return invoice
        except Exception as exc:
            _record_mint("failed", type(exc).__name__)
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
            amount_requested_sat=int(data.get("requestedSat") or 0),
            amount_received_sat=int(data.get("receivedSat") or 0),
            memo=data.get("description"),
            expired=data.get("isExpired") if "isExpired" in data else None,
        )


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
    """Create a Lightning invoice via the configured payment backend. Returns
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
    """Fetch the invoice for a payment hash and confirm it is settled, was
    issued for the specific digest (memo), and its FACE amount meets
    ``price_sats`` — the mint-time price carried in the token's signed caveat,
    not the current configured price, so a repriced gateway still honors
    in-flight invoices. The caller must already have proven that the presented
    preimage hashes to this payment hash.

    The check is against amount_requested_sat, never amount_received_sat: a
    settled bolt11 invoice is atomic, so settled=True proves the face amount
    was paid in full, and the face amount is what the gateway itself minted at
    quote time. The provider's cut (phoenixd nets an ACINQ liquidity fee off
    the credited amount) is our cost, never the customer's shortfall. The
    overpayment case this drops can only arise from an invoice minted below
    its bound price — a gateway mint bug — and passing on a payer's accidental
    overpayment would mask it rather than honor a payment."""
    status = PAYMENT_BACKEND.lookup_invoice(payment_hash)
    if status.settled and status.amount_received_sat < status.amount_requested_sat:
        logging.warning(
            "Liquidity fee observed on %s: requested %d sat, received %d sat",
            _hash8(payment_hash),
            status.amount_requested_sat,
            status.amount_received_sat,
        )
    return (
        status.settled
        and status.memo == digest
        and status.amount_requested_sat >= price_sats
    )


# Anchor billing (part two of the pricing model)
# The calendar fork writes a receipt line per confirmed anchor into an
# append-only JSONL file (its OTSD_ANCHOR_RECEIPTS), including "records" —
# the number of digest submissions inside that anchor's tree (0 = the fork
# could not prove a count; it errs low by design). With billing enabled, the
# gateway ingests those receipts into the anchor_bills table and bills the
# standing payer records × PER_RECORD_SATS per anchor. Invoices are minted on
# poll of GET /anchor-bills, never at ingestion; settlement is checked on the
# same poll through the existing payment backend. Billing state gates
# nothing: sales, stamping, and redemption never consult this table.

# A bill is overdue for /health once unpaid for 24h past its anchor's own
# confirmed_at — a hardcoded bookkeeping alarm, not an enforcement clock.
# Enabling billing over a receipts file with old anchors alarms immediately:
# those anchors ARE unbilled operational history, so that is intended.
_ANCHOR_BILL_OVERDUE_SECONDS = 24 * 3600
# Paid bills stay in the /anchor-bills response for a reconciliation week.
_ANCHOR_BILL_RECENTLY_PAID_SECONDS = 7 * 24 * 3600

_ANCHOR_RECEIPT_FIELDS = (
    ("txid", str),
    ("fee_sats", int),
    ("commitments", int),
    ("confirmed_height", int),
    ("confirmed_at", int),
    ("records", int),
)
# A receipt's confirmed_at is the fork's wall clock on the same box; a line
# further ahead of our clock than this is not a clock, it is a bad line.
_ANCHOR_RECEIPT_FUTURE_SLACK = 24 * 3600
# SQLite INTEGER is signed 64-bit: a line whose records × rate would not fit
# is malformed, rejected before it can raise inside the ingest pass.
_LEDGER_INT_MAX = 2**63 - 1


def _ingest_anchor_receipts() -> int:
    """Ingest the fork's anchor receipts into anchor_bills. Returns the
    number of lines rejected as malformed this pass — recounted from the
    file every time, so the counter clears when the file is fixed.

    INSERT OR IGNORE keyed on txid dedupes the fork's crash-preferred
    duplicate lines, and amount_sats is computed exactly once: the bill's
    mint-time amount, immutable across later rate changes, like the quote.
    records=0 (the fork could not prove a count) is never billed — no row,
    a warning naming the txid — and is not counted as rejected: the line is
    well-formed and permanent in the append-only file. A line missing
    records (an un-upgraded fork) is malformed and skipped with a warning,
    so an old five-field receipts file never bills. The rest of what the
    fork cannot write is malformed too: a txid that is not 64 hex chars
    (stored lowercase, so a re-cased copy of a line can never bill an
    anchor twice), a negative fee, an empty tree, a confirmed_at ahead of
    our clock, or a records × rate that would not fit the ledger's integer.
    An absent file is healthy (billing on, no anchors yet). Raises only for
    an unreadable-but-present file or a failing DB — the caller classifies
    that as billing "error"."""
    try:
        with open(ANCHOR_RECEIPTS_PATH, "r") as fd:
            lines = fd.readlines()
    except FileNotFoundError:
        return 0

    rejected = 0
    conn = _obligation_connect()
    try:
        for lineno, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                receipt = json.loads(line)
                if not isinstance(receipt, dict):
                    raise ValueError("not a JSON object")
                for field, ftype in _ANCHOR_RECEIPT_FIELDS:
                    if not isinstance(receipt[field], ftype) or isinstance(
                        receipt[field], bool
                    ):
                        raise ValueError(f"bad {field}")
                # The fork cannot produce a negative count: malformed.
                if receipt["records"] < 0:
                    raise ValueError("bad records")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", receipt["txid"]):
                    raise ValueError("bad txid")
                receipt["txid"] = receipt["txid"].lower()
                if receipt["fee_sats"] < 0:
                    raise ValueError("bad fee_sats")
                if receipt["commitments"] < 1:
                    raise ValueError("bad commitments")
                if receipt["confirmed_height"] < 0:
                    raise ValueError("bad confirmed_height")
                if not 0 <= receipt["confirmed_at"] <= int(time.time()) + _ANCHOR_RECEIPT_FUTURE_SLACK:
                    raise ValueError("bad confirmed_at")
                if receipt["records"] * PER_RECORD_SATS > _LEDGER_INT_MAX:
                    raise ValueError("records x rate exceeds the ledger's integer range")
            except (ValueError, KeyError) as exp:
                rejected += 1
                logging.warning(
                    "Anchor receipt line %d skipped (malformed): %r", lineno, exp
                )
                continue
            if receipt["records"] == 0:
                logging.warning(
                    "Anchor receipt %s not billed: the fork could not prove "
                    "a record count (records=0)",
                    receipt["txid"],
                )
                continue
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO anchor_bills "
                    "(txid, fee_sats, commitments, confirmed_height, confirmed_at, "
                    "records, amount_sats, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'unpaid')",
                    (
                        receipt["txid"],
                        receipt["fee_sats"],
                        receipt["commitments"],
                        receipt["confirmed_height"],
                        receipt["confirmed_at"],
                        receipt["records"],
                        receipt["records"] * PER_RECORD_SATS,
                    ),
                )
            except OverflowError as exp:
                # Belt and braces behind the range check above: one line can
                # never take the rest of the file down with it.
                rejected += 1
                logging.warning(
                    "Anchor receipt line %d skipped (malformed): %r", lineno, exp
                )
        conn.commit()
    finally:
        conn.close()
    return rejected


def _store_anchor_bill_invoice(
    txid: str, payment_hash: str, bolt11: str, created_at: int,
    expected_hash: str | None,
) -> bool:
    """Attach a freshly minted invoice to a bill — only if the row still
    holds what this poll read (expected_hash; None = no invoice yet, the
    IS NULL arm). Two concurrent polls can both decide to mint for one
    bill; the conditional WHERE makes exactly one store win. Returns False
    to the loser, which must re-read and serve the winner's invoice: a
    blind overwrite would orphan an invoice the payer may already hold —
    paid, it would settle invisibly (no poll ever checks a replaced hash),
    the bill would re-serve as unpaid, and one anchor would be paid twice.
    The losing mint is never stored and never served; it dies unpaid at
    its own Lightning expiry."""
    conn = _obligation_connect()
    try:
        cur = conn.execute(
            "UPDATE anchor_bills SET payment_hash=?, bolt11=?, "
            "invoice_created_at=? "
            "WHERE txid=? AND (payment_hash IS NULL OR payment_hash=?)",
            (payment_hash, bolt11, created_at, txid, expected_hash),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _read_anchor_bill_invoice(txid: str) -> tuple[str | None, str | None, int | None]:
    """The bill's stored (payment_hash, bolt11, invoice_created_at) — what a
    poll that lost the mint race serves instead of its own orphaned mint."""
    conn = _obligation_connect()
    try:
        row = conn.execute(
            "SELECT payment_hash, bolt11, invoice_created_at "
            "FROM anchor_bills WHERE txid=?",
            (txid,),
        ).fetchone()
        return (row[0], row[1], row[2]) if row else (None, None, None)
    finally:
        conn.close()


def _mark_anchor_bill_paid(txid: str, paid_at: int) -> None:
    conn = _obligation_connect()
    try:
        conn.execute(
            "UPDATE anchor_bills SET status='paid', paid_at=? WHERE txid=?",
            (paid_at, txid),
        )
        conn.commit()
    finally:
        conn.close()


def _billing_status(receipts_state: bool | None = None) -> tuple[str, int, int]:
    """Classify anchor billing for /health. Returns (status, bills, rejected)
    where bills is the total rows in anchor_bills, rejected the receipt
    lines rejected as malformed this pass, and status one of:
      off          — ANCHOR_BILLING_ENABLED=false; reported, never degrades
      ok           — receipts ingestable, nothing rejected, no unpaid bill
                     older than 24h. With bills 0 this is "no receipts
                     yet", distinct from rejection.
      rejected     — receipt lines are being rejected as malformed
                     (rejected > 0); degrades. The three below outrank it.
      receipts_off — the calendar says it is anchoring without writing
                     receipts (its homepage line "Anchor receipts: off"):
                     every anchor from here is unbilled. receipts_state
                     carries that reading — True on, False off, None when
                     the calendar did not say (unreachable, or a fork
                     without the line), which never degrades on its own.
      overdue      — an unpaid bill has sat >24h past its anchor's
                     confirmed_at (bookkeeping alarm; sales are never gated
                     by billing state)
      error        — the receipts file is present but unreadable, or the
                     bills table cannot be read, with billing on
    Ingests receipts itself so overdue is seen even if the payer never
    polls, but never mints invoices — minting is poll-only.
    Never raises — /health must never crash."""
    if not ANCHOR_BILLING_ENABLED:
        return "off", 0, 0
    try:
        rejected = _ingest_anchor_receipts()
        conn = _obligation_connect()
        try:
            bills = conn.execute(
                "SELECT COUNT(*) FROM anchor_bills"
            ).fetchone()[0]
            overdue = conn.execute(
                "SELECT COUNT(*) FROM anchor_bills "
                "WHERE status='unpaid' AND confirmed_at < ?",
                (int(time.time()) - _ANCHOR_BILL_OVERDUE_SECONDS,),
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        logging.warning("Health check: anchor billing unreadable", exc_info=True)
        return "error", 0, 0
    if overdue:
        return "overdue", bills, rejected
    if receipts_state is False:
        return "receipts_off", bills, rejected
    if rejected:
        return "rejected", bills, rejected
    return "ok", bills, rejected


@app.get("/")
def root():
    return {"status": "running"}


def _probe_otsd() -> tuple[str, bool | None]:
    """One homepage render of the calendar: (otsd_status, receipts_state).
    otsd_status is "ok", "error", or "n/a" (no calendar URL); receipts_state
    True/False when the page carries the "Anchor receipts" line and billing
    is on, else None."""
    if not OTS_CALENDAR_URL:
        return "n/a", None
    receipts_state = None
    otsd_status = "ok"
    try:
        # (5, 45): connect is local (compose network / localhost) — 5s is
        # generous. The read leg races otsd's FULL homepage render: otsd
        # commits headers instantly and writes the body in ONE shot after
        # ~4 Bitcoin RPCs over Tor, each allowed up to a 30s stall by the
        # fork's make_proxy(timeout=30) — so a single-stall render can
        # take ~34s. A plain 5 here timed out ~11% of such renders
        # (2026-07-17). Paired with the fork homepage timeout: change the
        # two together.
        resp = requests.get(OTS_CALENDAR_URL, timeout=(5, 45))
        resp.raise_for_status()
        # A 200 from otsd proves nothing: its homepage (fork rpc.py,
        # do_GET) commits the status line BEFORE any Bitcoin call, and both
        # failure shapes — make_proxy() raising, or the first RPC call dying
        # after the headers went out — yield an empty 200 body. "Best-block"
        # renders only after getbestblockhash and getblockcount both
        # succeed, so its presence is the only external proof that otsd's
        # Bitcoin RPC path is alive. Residual: this proves RPC reachability,
        # not that the stamper thread is unwedged — that class surfaces in
        # the proofs-status field, with hours of latency.
        if b"Best-block" not in resp.content:
            logging.warning(
                "Health check: otsd HTTP up but Bitcoin-blind at %s "
                "(homepage lacks the Best-block marker)",
                OTS_CALENDAR_URL,
            )
            otsd_status = "error"
        elif ANCHOR_BILLING_ENABLED:
            if b"Anchor receipts: on" in resp.content:
                receipts_state = True
            elif b"Anchor receipts: off" in resp.content:
                receipts_state = False
                logging.warning(
                    "Health check: billing is on but otsd at %s reports "
                    "anchor receipts off; anchors are going unbilled",
                    OTS_CALENDAR_URL,
                )
    except Exception:
        logging.warning("Health check: otsd unreachable at %s", OTS_CALENDAR_URL)
        otsd_status = "error"
    return otsd_status, receipts_state


# The probe's cache: one homepage render per HEALTH_PROBE_CACHE_SECONDS,
# however many callers ask. The lock makes concurrent callers wait for the
# one render in flight instead of starting their own (single flight), so a
# flood of /health can no longer hold every worker on the calendar or make
# bitcoind answer four RPCs per hit (2026-09-08 review, D1).
_health_probe_lock = threading.Lock()
_health_probe_cache: dict = {"at": None, "result": None}


def _otsd_probe_cached() -> tuple[str, bool | None]:
    """The calendar probe, at most one render in flight and at most one per
    HEALTH_PROBE_CACHE_SECONDS. A result inside the TTL is served as is. An
    expired result is served stale while one caller refreshes it, so a
    flood of /health during a slow render costs one render and holds one
    worker thread, not one per caller. Only the cold start — no result yet
    — makes concurrent callers wait for the first render (bounded by the
    per-peer bucket /health draws from)."""
    if HEALTH_PROBE_CACHE_SECONDS <= 0:
        return _probe_otsd()
    now = time.monotonic()
    at = _health_probe_cache["at"]
    if at is not None and now - at < HEALTH_PROBE_CACHE_SECONDS:
        return _health_probe_cache["result"]
    if at is not None and not _health_probe_lock.acquire(blocking=False):
        # A refresh is in flight: the stale answer, now.
        return _health_probe_cache["result"]
    if at is None:
        _health_probe_lock.acquire()
    try:
        now = time.monotonic()
        at = _health_probe_cache["at"]
        if at is not None and now - at < HEALTH_PROBE_CACHE_SECONDS:
            return _health_probe_cache["result"]
        result = _probe_otsd()
        _health_probe_cache["at"] = time.monotonic()
        _health_probe_cache["result"] = result
        return result
    finally:
        _health_probe_lock.release()


@app.get("/health")
def health(request: Request):
    # The same per-peer budget as /verify: /health used to be the one
    # unlimited, unauthenticated route, and each hit cost a calendar render.
    retry_after = _verify_rate_limit_retry_after(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={"status": "rate_limited", "retry_after_seconds": retry_after},
        )
    paused = is_paused()
    # Float backstop: ok / alarm / stop / inactive. "stop" is the gateway's
    # own full stop (overall "auto_paused"); the operator's PAUSED file wins
    # the label when both hold. "inactive" (no balance reading — e.g. the ops
    # timers are not installed) reports and never degrades.
    float_state = _float_state()
    # Payment: the outcome of the last real mint (see _last_mint), never a
    # reachability probe. "unknown" (no mint since startup) reports and never
    # degrades, same contract as wallet "absent" / float "inactive";
    # "degraded" (last mint failed) degrades like a backend failure.
    last_mint = _last_mint
    if last_mint["result"] == "ok":
        payment_status = "ok"
    elif last_mint["result"] == "failed":
        payment_status = "degraded"
    else:
        payment_status = "unknown"

    # With billing on, the calendar's homepage also says whether it is
    # writing receipts ("Anchor receipts: on"/"off"); a calendar restarted
    # with receipts off is anchoring for free, and billing must say so.
    otsd_status, receipts_state = _otsd_probe_cached()

    # File-mediated fields (wallet, proofs, backup) and billing: each
    # classifier's docstring lists its vocabulary. "absent", "local_only",
    # and billing "off" report without degrading; every other non-ok value
    # degrades like a backend failure — the expression below is the rule.
    wallet_status = _wallet_status()
    proofs_status = _proofs_status()
    backup_status = _backup_status()
    billing_status, billing_bills, billing_rejected = _billing_status(receipts_state)

    # L402 door: "on"/"off" — a mode, reported, never degrading; the same
    # contract as billing "off". "off" means /timestamp stamps free of charge
    # and the per-record cost lands on the anchor bill.
    l402_status = "on" if L402_ENABLED else "off"

    if paused:
        overall = "paused"
    elif float_state == "stop":
        overall = "auto_paused"
    else:
        overall = (
            "ok"
            if payment_status in ("ok", "unknown")
            and otsd_status in ("ok", "n/a")
            and wallet_status in ("ok", "absent")
            and proofs_status in ("ok", "absent")
            and backup_status in ("ok", "local_only", "absent")
            and float_state in ("ok", "inactive")
            and billing_status in ("off", "ok")
            else "degraded"
        )

    content = {
        "status": overall,
        "paused": paused,
        "payment": payment_status,
        "payment_backend": PAYMENT_BACKEND_TYPE,
        "last_mint_at": last_mint["at"],
        "otsd": otsd_status,
        "wallet": wallet_status,
        "float": float_state,
        "proofs": proofs_status,
        "backup": backup_status,
        "billing": billing_status,
        "l402": l402_status,
    }
    # Billing "off" means the feature is entirely absent (same contract as
    # the 404 on /anchor-bills), so the counters exist only with billing on.
    if ANCHOR_BILLING_ENABLED:
        content["billing_bills"] = billing_bills
        content["billing_rejected"] = billing_rejected

    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content=content,
    )


@app.get("/anchor-bills")
def anchor_bills(request: Request):
    """The standing payer's bills ledger. Poll-driven: minting and settlement
    checks happen here, never at ingestion — a bill lacking a live invoice
    (none yet, or expired) gets a fresh bolt11 on each poll that needs one,
    and a paid bill never re-mints. Plain bolt11s: anchor bills are NOT L402;
    the macaroon machinery and verify_payment are not involved."""
    # Absent unless billing is enabled: same 404 as an unregistered route.
    if not ANCHOR_BILLING_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    # Same budget as the other non-sales endpoints (/verify, /upgrade): polls
    # cost gateway CPU and phoenixd round-trips. Limiting before the token
    # compare also throttles guessing.
    retry_after = _verify_rate_limit_retry_after(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={"status": "rate_limited", "retry_after_seconds": retry_after},
        )

    # Operational history is not public: bearer token, constant-time compare.
    auth = request.headers.get("Authorization", "")
    provided = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    if not provided or not secrets.compare_digest(
        provided.encode(), ANCHOR_BILLS_TOKEN.encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")

    _ingest_anchor_receipts()

    now = int(time.time())
    # Read everything first and close — never hold a connection across the
    # phoenixd round-trips below (the sweeper's short-transaction pattern).
    conn = _obligation_connect()
    try:
        rows = conn.execute(
            "SELECT txid, fee_sats, commitments, confirmed_height, confirmed_at, "
            "records, amount_sats, payment_hash, bolt11, invoice_created_at, "
            "status, paid_at "
            "FROM anchor_bills WHERE status='unpaid' OR paid_at >= ? "
            "ORDER BY confirmed_at",
            (now - _ANCHOR_BILL_RECENTLY_PAID_SECONDS,),
        ).fetchall()
    finally:
        conn.close()

    bills = []
    unpaid_count = 0
    unpaid_sats = 0
    for (
        txid, fee_sats, commitments, confirmed_height, confirmed_at, records,
        amount_sats, payment_hash, bolt11, invoice_created_at, status, paid_at,
    ) in rows:
        bill = {
            "txid": txid,
            "fee_sats": fee_sats,
            "commitments": commitments,
            "confirmed_height": confirmed_height,
            "confirmed_at": confirmed_at,
            # The payer's audit handle: amount_sats = records × the
            # contracted rate. NULL on markup-era bills — their amounts
            # stand as stored, whatever formula made them.
            "records": records,
            "amount_sats": amount_sats,
            "status": status,
        }
        if status == "paid":
            # Recently paid: reported for reconciliation, bolt11 dropped.
            bill["payment_hash"] = payment_hash
            bill["paid_at"] = paid_at
            bills.append(bill)
            continue

        needs_mint = payment_hash is None
        if payment_hash is not None:
            invoice_status = PAYMENT_BACKEND.lookup_invoice(payment_hash)
            if invoice_status.settled:
                _mark_anchor_bill_paid(txid, now)
                bill["status"] = "paid"
                bill["payment_hash"] = payment_hash
                bill["paid_at"] = now
                bills.append(bill)
                continue
            # expired is None when the backend has no expiry signal: treat
            # the invoice as live rather than re-mint on every poll.
            needs_mint = invoice_status.expired is True
        if needs_mint:
            # A backend failure propagates as the existing 502: the poll is
            # retryable and nothing is lost.
            new_bolt11, new_hash = create_invoice(f"anchor-bill {txid}", amount_sats)
            if _store_anchor_bill_invoice(txid, new_hash, new_bolt11, now,
                                          expected_hash=payment_hash):
                bolt11, payment_hash, invoice_created_at = new_bolt11, new_hash, now
            else:
                # Lost the mint race: a concurrent poll stored its invoice
                # after this one read the row. Serve the winner's — every
                # poller must converge on the ONE invoice later polls will
                # settle-check.
                payment_hash, bolt11, invoice_created_at = (
                    _read_anchor_bill_invoice(txid)
                )

        bill["payment_hash"] = payment_hash
        bill["bolt11"] = bolt11
        bill["invoice_created_at"] = invoice_created_at
        bills.append(bill)
        unpaid_count += 1
        unpaid_sats += amount_sats

    return JSONResponse(
        content={
            "bills": bills,
            "summary": {"unpaid_count": unpaid_count, "unpaid_sats": unpaid_sats},
        }
    )


@app.post("/verify")
def verify(body: VerifyRequest, request: Request):
    # Rate-limit BEFORE the base64 decode: parsing up to 256KB of
    # attacker-controlled bytes is part of what the limiter defends; a
    # denial must not pay the cost it exists to refuse.
    retry_after = _verify_rate_limit_retry_after(_client_ip(request))
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={"status": "rate_limited", "retry_after_seconds": retry_after},
        )
    try:
        ots_bytes = base64.b64decode(body.ots, validate=True)
    except Exception:
        return JSONResponse(status_code=200, content=_undecodable_result(body.digest))

    if len(ots_bytes) > MAX_VERIFY_OTS_BYTES:
        raise HTTPException(status_code=413, detail="OTS proof too large")

    return JSONResponse(content=_verify_ots_bytes(body.digest, ots_bytes))


def _has_upgrade_client_token(request: Request) -> bool:
    """True when the request carries UPGRADE_CLIENT_TOKEN as a bearer
    (constant-time compare). False when the token is unset, absent, or wrong
    — the request is then an anonymous one, drawn from the verify bucket."""
    if not UPGRADE_CLIENT_TOKEN:
        return False
    auth = request.headers.get("Authorization", "")
    provided = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    return bool(provided) and secrets.compare_digest(
        provided.encode(), UPGRADE_CLIENT_TOKEN.encode()
    )


@app.post("/upgrade")
def upgrade(body: VerifyRequest, request: Request):
    # Same pre-decode gate as /verify — one shared bucket for both — unless
    # the caller is the client the operator issued UPGRADE_CLIENT_TOKEN to.
    retry_after = (
        None if _has_upgrade_client_token(request)
        else _verify_rate_limit_retry_after(_client_ip(request))
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            detail={"status": "rate_limited", "retry_after_seconds": retry_after},
        )
    try:
        ots_bytes = base64.b64decode(body.ots, validate=True)
    except Exception:
        return JSONResponse(status_code=200, content=_undecodable_result(body.digest, upgrade=True))
    if len(ots_bytes) > MAX_VERIFY_OTS_BYTES:
        raise HTTPException(status_code=413, detail="OTS proof too large")
    return JSONResponse(content=_upgrade_ots_bytes(body.digest, ots_bytes))


@app.post("/timestamp")
def timestamp(body: TimestampRequest, request: Request):
    # PAUSED is enforced app-wide by _pause_gate.

    if not L402_ENABLED:
        # Free door: stamp immediately and hand over the proof. No invoice, no
        # macaroon, no 402; the payment backend is never contacted and the
        # Authorization header is never read (a token minted before the flip
        # gets its proof regardless). No obligation row: that log exists so a
        # paid customer is never dropped, and nothing is paid here — a failed
        # stamp is the client's 502 to retry. The mint rate limiter does not
        # apply: nothing mints. The per-record cost lands on the anchor bill
        # when billing is on.
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

    auth = request.headers.get("Authorization", "")

    if auth:
        parsed = parse_l402_auth(auth)
        if not parsed:
            raise HTTPException(
                status_code=401,
                detail="Invalid Authorization header; expected 'L402 <macaroon>:<preimage>'",
            )
        macaroon_b64, preimage_hex = parsed

        # Order matters: token, preimage, settlement, then the cache — an
        # unpaid replay never reaches a cached proof.
        payment_hash, mint_price_sats = verify_l402_token(macaroon_b64, body.digest)

        derived = hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest()
        if derived != payment_hash:
            raise HTTPException(status_code=401, detail="Preimage does not match token payment hash")

        if not verify_payment(payment_hash, body.digest, mint_price_sats):
            raise HTTPException(status_code=402, detail="Payment required or not settled")

        if payment_hash in _proof_cache:
            logging.info("Returning cached proof for payment_hash %s", _hash8(payment_hash))
            ots_bytes = _proof_cache[payment_hash]
            return Response(
                content=ots_bytes,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename={body.digest}.ots"},
            )

        # Record the paid obligation BEFORE stamping: if stamping fails below,
        # the row stays 'needs_stamp' and the sweeper recovers it — the settled
        # payment is never lost. Idempotent on payment_hash.
        record_obligation(payment_hash, body.digest)

        try:
            ots_bytes = stamp_digest(body.digest)
        except Exception:
            logging.exception("OTS stamping failed")
            raise HTTPException(status_code=502, detail="OTS error: stamping failed")

        _proof_cache_put(payment_hash, ots_bytes)
        mark_obligation_stamped(payment_hash)
        return Response(
            content=ots_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename={body.digest}.ots"},
        )

    # No authorization — mint an invoice and an L402 token bound to it. The
    # quote is the flat per-proof rate; invoice, macaroon, and body all carry
    # the same mint-time amount, which is what redemption later enforces.
    # Rate-limit before touching any backend: a 429 costs phoenixd nothing.
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
