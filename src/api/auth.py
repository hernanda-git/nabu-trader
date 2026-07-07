"""Authentication layer for the Fly trade bridge API.

Two-layer security:
  1. API key (required for every request, via X-API-Key header)
  2. HMAC request signing (required for POST/PUT/DELETE actions, via X-Signature header)

The API key is stored as a Fly.io secret (API_KEY) and injected into the
running container as an environment variable. Hermes stores it locally in
~/.hermes/config.yaml or a dedicated secrets file.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Callable

from fastapi import HTTPException, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED

log = logging.getLogger("api.auth")

# ─── Config ──────────────────────────────────────────────────────────────────
# The API key is loaded from environment. On Fly.io, set via:
#   flyctl secrets set API_KEY=your-secure-key-here
# Local dev: set API_KEY in .env or export it.
ENV_API_KEY = "API_KEY"
ENV_HMAC_SECRET = "API_HMAC_SECRET"

# Rate limit: max requests per window per IP
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 30     # requests per window

_rate_limit_store: dict[str, list[float]] = {}


def _get_api_key() -> str:
    """Get the configured API key from environment."""
    key = os.environ.get(ENV_API_KEY, "")
    if not key:
        log.warning("API_KEY not set — API server will reject all requests")
    return key


def _get_hmac_secret() -> str:
    """Get the HMAC secret from environment."""
    return os.environ.get(ENV_HMAC_SECRET, "")


# ─── Middleware ───────────────────────────────────────────────────────────────


async def auth_middleware(request: Request, call_next: Callable):
    """FastAPI middleware that authenticates every request except /health.

    1. Rate limit check (per IP)
    2. API key check (X-API-Key header)
    3. HMAC verification for mutating methods (POST/PUT/DELETE)
    """
    # Skip auth for health check
    if request.url.path == "/health":
        return await call_next(request)

    # Rate limiting
    client_ip = request.client.host if request.client else "unknown"
    if _is_rate_limited(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "message": "Too many requests"},
        )

    # API key authentication
    api_key = request.headers.get("X-API-Key", "")
    expected_key = _get_api_key()

    if not expected_key:
        log.error("API_KEY not configured on server — rejecting all requests")
        return JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={"error": "not_configured", "message": "Server not configured"},
        )

    # Constant-time comparison to prevent timing attacks
    if not _constant_time_compare(api_key, expected_key):
        log.warning("Invalid API key attempt from %s", client_ip)
        return JSONResponse(
            status_code=HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_key", "message": "Invalid API key"},
        )

    # HMAC verification for mutating methods
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        sig = request.headers.get("X-Signature", "")
        body = await request.body()
        if not _verify_hmac(body, sig):
            log.warning("Invalid HMAC signature from %s", client_ip)
            return JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"error": "invalid_signature", "message": "Invalid HMAC signature"},
            )

    return await call_next(request)


# ─── Helpers ─────────────────────────────────────────────────────────────────-


def _constant_time_compare(a: str, b: str) -> bool:
    """Compare two strings in constant time to prevent timing attacks."""
    return hmac.compare_digest(a.encode(), b.encode())


def _sign_body(body: bytes, secret: str) -> str:
    """Create an HMAC-SHA256 signature of the request body."""
    if not secret:
        return ""
    h = hmac.new(secret.encode(), body, hashlib.sha256)
    return h.hexdigest()


def _verify_hmac(body: bytes, signature: str) -> bool:
    """Verify that the HMAC signature matches the request body."""
    secret = _get_hmac_secret()
    if not secret:
        # If no HMAC secret is configured, skip HMAC verification
        # (API key alone is sufficient)
        return True
    expected = _sign_body(body, secret)
    return _constant_time_compare(signature, expected)


def _is_rate_limited(ip: str) -> bool:
    """Simple in-memory sliding window rate limiter."""
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW

    # Clean up old entries
    if ip in _rate_limit_store:
        _rate_limit_store[ip] = [
            t for t in _rate_limit_store[ip] if t > window_start
        ]
    else:
        _rate_limit_store[ip] = []

    # Check limit
    if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX:
        return True

    _rate_limit_store[ip].append(now)
    return False


def verify_api_key(key: str) -> bool:
    """Utility for Hermes to verify its API key is valid (calls /api/v1/auth/verify)."""
    return _constant_time_compare(key, _get_api_key())
