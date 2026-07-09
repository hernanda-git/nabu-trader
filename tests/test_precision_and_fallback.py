"""Deterministic tests for order precision rounding and LLM-fallback guards.

No live Binance / network needed — we stub the exchange filters cache and the
HTTP order request so we can assert the OUTGOING price/quantity precision.

Run:
    .venv/Scripts/python.exe -m pytest tests/test_precision_and_fallback.py -q
"""

import asyncio
from typing import Any

import pytest

from src.exchange.binance import BinanceExchange
from src.domain.models import SymbolInfo


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_exchange() -> BinanceExchange:
    """BinanceExchange with all network calls stubbed."""
    ex = BinanceExchange(api_key="k", api_secret="s", testnet=False, futures=True)
    # Avoid real httpx client touching the network.
    ex._client = _StubClient()
    return ex


class _StubClient:
    """No-op async client — _place_order only needs _sign/_signed_request,
    which we also stub on the exchange, so the client is never used."""
    async def request(self, *a, **k):
        raise AssertionError("network should not be hit in this test")


def _seed_filters(ex: BinanceExchange, symbol: str, tick: float, step: float,
                  min_price: float = 0.0, max_price: float = 1e9) -> None:
    from datetime import datetime, timezone
    ex._filters[symbol] = {
        "_updated": datetime.now(timezone.utc),
        "valid": True,
        "tickSize": tick,
        "stepSize": step,
        "minPrice": min_price,
        "maxPrice": max_price,
        "minQty": step,
        "maxQty": 1e12,
    }


# ─── 1. Price rounding (root cause of -1111) ─────────────────────────────────

@pytest.mark.asyncio
async def test_round_price_uses_tick_size():
    ex = _make_exchange()
    # UAIUSDT-style pair: tickSize 0.0001 → 4 decimals
    _seed_filters(ex, "UAIUSDT", tick=0.0001, step=1.0)
    rounded, rules = await ex._round_price("UAIUSDT", 0.413)
    assert rounded == 0.413
    # A price that needs snapping to the tick
    rounded2, _ = await ex._round_price("UAIUSDT", 0.4137)
    assert rounded2 == 0.4137
    # Too many decimals → must be snapped to tick precision (round half-up)
    rounded3, _ = await ex._round_price("UAIUSDT", 0.41377777)
    assert rounded3 == 0.4138
    assert "%.4f" % rounded3 == "0.4138"


@pytest.mark.asyncio
async def test_round_price_high_precision_pair():
    ex = _make_exchange()
    # 1000x contract: tickSize 0.0000001 → 7 decimals
    _seed_filters(ex, "1000BONKUSDT", tick=0.0000001, step=1.0)
    rounded, _ = await ex._round_price("1000BONKUSDT", 0.000004123456)
    # snap to 0.0000041
    assert rounded == 0.0000041


# ─── 2. _place_order emits rounded price + qty (no -1111 on wire) ────────────

class _CaptureExchange(BinanceExchange):
    """Capture the params POSTed to /fapi/v1/order."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.last_params: dict | None = None
        self.order_response = {
            "orderId": "123", "symbol": "UAIUSDT", "side": "BUY",
            "type": "LIMIT", "origQty": "12.107", "price": "0.413",
            "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0",
        }

    async def _signed_request(self, method, path, params=None):
        if path.endswith("/order"):
            self.last_params = params
            # Simulate acceptance: price is now within precision.
            return dict(self.order_response)
        # leverage / marginType calls → ok
        return {"leverage": 5} if "leverage" in (params or {}) else {}


@pytest.mark.asyncio
async def test_place_order_rounds_price_and_qty_on_wire():
    ex = _CaptureExchange(api_key="k", api_secret="s", testnet=False, futures=True)
    ex._client = _StubClient()
    # Seed filters: tick 0.0001, step 1.0 (qty must be integer)
    _seed_filters(ex, "UAIUSDT", tick=0.0001, step=1.0)

    # Exact scenario from the failing Fly log:
    #   qty=12.106537530266344  price=0.413  -> Binance -1111 before fix
    info = await ex._place_order("UAIUSDT", "BUY", "LIMIT",
                                  quantity=12.106537530266344, price=0.413)

    assert ex.last_params is not None, "order request was never sent"
    sent_qty = float(ex.last_params["quantity"])
    sent_price = float(ex.last_params["price"])
    # qty must be integer (step 1.0): 12.1065.. -> 12.0
    assert sent_qty == 12.0, f"qty not rounded to step: {sent_qty}"
    # price must align to 0.0001 tick
    assert sent_price == 0.413, f"price not on tick: {sent_price}"
    assert info.status != "FAILED", f"order wrongly failed: {info.error}"


# ─── 3. LLM fallback never throws on empty reasoning-model response ──────────

class _StubAgent:
    """Mimics AgentBrain._call_llm. Returns empty content like deepseek-v4-flash
    sometimes does (reasoning model with too-low max_tokens)."""
    def __init__(self, returns):
        self._returns = list(returns)
        self.calls = 0
    def _call_llm(self, prompt):
        self.calls += 1
        text = self._returns.pop(0) if self._returns else ""
        return text, 10, 5, 100


def test_llm_fallback_empty_response_returns_none(monkeypatch):
    """A blank LLM reply must yield None (trade left), not raise."""
    from src.execution.order_service import OrderService
    from src.domain.models import TradeDecision

    # Minimal stubs
    class _Ex:
        name = "binance_futures"
    agent = _StubAgent(returns=[""])  # empty content
    os_ = OrderService(exchange=_Ex(), config={}, signal_repo=_R(), decision_repo=_R(),
                       order_repo=_R(), position_repo=_R(), agent=agent)

    dec = TradeDecision(action="ENTER", pair="UAIUSDT", direction="LONG",
                        order_type="LIMIT", quantity=12.0, entry_price=0.413)
    # Should not raise — returns None gracefully.
    result = asyncio.run(
        os_._llm_fallback("UAIUSDT", "BUY", dec, "precision error", 0.413, 12.0)
    )
    assert result is None
    assert agent.calls >= 1


# ─── minimal no-op repos ─────────────────────────────────────────────────────
class _R:
    def save(self, *a, **k): return 1
    def get_open_by_pair(self, *a, **k): return None
    def get_open_positions(self, *a, **k): return []
    def get_open_count(self, *a, **k): return 0
    def get_daily_pnl(self, *a, **k): return 0.0
    def get_total_notional_usdt(self, *a, **k): return 0.0
    def update_status(self, *a, **k): return None
    def create(self, *a, **k): return 1
    def close_position(self, *a, **k): return None
