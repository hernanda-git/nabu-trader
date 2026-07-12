"""Webhook emitter — pushes trade events from Fly.io to a configurable HTTP endpoint.

When a trade executes (ENTER, CLOSE, SL_HIT, MODIFY), the orchestrator calls
this module to POST a signed JSON payload to the configured webhook URL.

The webhook URL is configured via:
  - config.yaml: webhook.url
  - Environment: WEBHOOK_URL (overrides config)
  - Environment: WEBHOOK_HMAC_SECRET (for signing)

The payload includes:
  - event_type: TRADE_ENTERED, TRADE_CLOSED, TRADE_REJECTED, SL_HIT, etc.
  - timestamp: ISO 8601
  - data: Full trade details (pair, direction, qty, price, PnL, SL, TP)
  - correlation_id: Links back to the full trace in the DB

The webhook is fire-and-forget (async, no retry). Failure is logged but
never blocks the trading pipeline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger("api.webhook")

# Default timeout for webhook POST
WEBHOOK_TIMEOUT = 10  # seconds


def _get_webhook_url(config: dict | None = None) -> str | None:
    """Get the configured webhook URL.

    Priority: env var > config.yaml
    """
    url = os.environ.get("WEBHOOK_URL", "")
    if url:
        return url
    if config:
        return config.get("webhook", {}).get("url", "")
    return None


def _get_webhook_secret(config: dict | None = None) -> str | None:
    """Get the HMAC secret for signing webhook payloads."""
    secret = os.environ.get("WEBHOOK_HMAC_SECRET", "")
    if secret:
        return secret
    if config:
        return config.get("webhook", {}).get("hmac_secret", "")
    return None


def _sign_payload(payload: bytes, secret: str) -> str:
    """Create HMAC-SHA256 signature for the payload."""
    h = hmac.new(secret.encode(), payload, hashlib.sha256)
    return h.hexdigest()


async def emit_event(
    event_type: str,
    data: dict[str, Any],
    correlation_id: str = "",
    config: dict | None = None,
) -> bool:
    """Emit a trade event to the configured webhook.

    Returns True if the POST succeeded, False otherwise.
    Never raises — all errors are caught and logged.
    """
    url = _get_webhook_url(config)
    if not url:
        return False  # No webhook configured — silently skip

    secret = _get_webhook_secret(config)
    payload = {
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "correlation_id": correlation_id,
        "data": data,
    }

    body = json.dumps(payload, default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Event-Type": event_type,
    }

    # Sign payload if secret configured
    if secret:
        headers["X-Signature"] = _sign_payload(body, secret)

    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT) as client:
            resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                log.info("Webhook sent: %s -> %s (status=%d)", event_type, url, resp.status_code)
                return True
            else:
                log.warning("Webhook returned %d for %s: %s", resp.status_code, event_type, resp.text[:200])
                return False
    except httpx.TimeoutException:
        log.warning("Webhook timeout for %s -> %s", event_type, url)
        return False
    except Exception as e:
        log.warning("Webhook failed for %s: %s", event_type, e)
        return False
