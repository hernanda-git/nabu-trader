"""Tests for the listener's proxy-aware BinanceExchange.

Verifies:
  - proxy OFF  → calls go to Binance base URL (self._client)
  - proxy ON   → calls go to the gateway URL with X-Gateway-Sig headers,
                 and NO Binance key is required locally.

No live network: we stub httpx.AsyncClient.
"""

import asyncio
from unittest.mock import patch, AsyncMock

import pytest

from src.exchange.binance import BinanceExchange


def _make_exchange(proxy=None, key="k", secret="s"):
    ex = BinanceExchange(api_key=key, api_secret=secret, futures=True,
                         testnet=False, proxy=proxy)
    return ex


# Capture what URL/headers a request used.
class _CapturingClient:
    def __init__(self, *a, **k):
        self.last = {}
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def request(self, method, url, **kw):
        self.last = {"method": method, "url": str(url),
                     "headers": kw.get("headers", {}),
                     "params": kw.get("params")}
        class _R:
            status_code = 200
            text = "{}"
            def json(self): return {"ok": True}
            def raise_for_status(self): pass
        return _R()


@pytest.mark.asyncio
async def test_direct_mode_hits_binance_base():
    ex = _make_exchange(proxy=None)
    captured = _CapturingClient()
    with patch.object(ex, "_client", captured):
        out = await ex._public_request("GET", "/fapi/v1/exchangeInfo", {"symbol": "BTCUSDT"})
    assert "binance.com" in ex._base
    assert "ok" in out


@pytest.mark.asyncio
async def test_proxy_mode_routes_to_gateway_with_sig_and_no_local_key():
    """Proxy ON: request goes to gateway URL, carries gateway signature,
    and works even with empty local Binance key."""
    proxy = {"enabled": True, "url": "https://binance-gateway.fly.dev",
             "hmac_secret": "shared-secret"}
    ex = _make_exchange(proxy=proxy, key="", secret="")
    assert ex.proxy_enabled is True

    captured = _CapturingClient()
    with patch("httpx.AsyncClient", return_value=captured):
        out = await ex._public_request("GET", "/fapi/v1/exchangeInfo", {"symbol": "BTCUSDT"})

    assert captured.last["url"].startswith("https://binance-gateway.fly.dev")
    assert "X-Gateway-Sig" in captured.last["headers"]
    assert "X-Gateway-Ts" in captured.last["headers"]
    assert "ok" in out


@pytest.mark.asyncio
async def test_proxy_mode_signed_request_forwards_with_sig():
    proxy = {"enabled": True, "url": "https://gw.test", "hmac_secret": "sec"}
    ex = _make_exchange(proxy=proxy, key="localkey", secret="localsecret")
    captured = _CapturingClient()
    with patch("httpx.AsyncClient", return_value=captured):
        out = await ex._signed_request("POST", "/fapi/v1/order",
                                        {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT"})
    assert captured.last["url"].startswith("https://gw.test/fapi/v1/order")
    assert "X-Gateway-Sig" in captured.last["headers"]
    # signature param still present (gateway swaps in its own key server-side)
    assert "signature" in (captured.last["params"] or {})
    assert "ok" in out
