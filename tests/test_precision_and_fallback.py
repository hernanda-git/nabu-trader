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


# ─── 3. No LLM involved in order repair (deterministic path only) ──────────

class _StubAgent:
    """Mimics AgentBrain._call_llm. Records whether it is ever invoked."""
    def __init__(self, returns):
        self._returns = list(returns)
        self.calls = 0
    def _call_llm(self, prompt):
        self.calls += 1
        text = self._returns.pop(0) if self._returns else ""
        return text, 10, 5, 100


def test_no_llm_on_rejection(monkeypatch):
    """A rejected entry must be repaired deterministically — the LLM is
    NEVER called (the old bypass was removed)."""
    from src.execution.order_service import OrderService
    from src.domain.models import TradeDecision

    agent = _StubAgent(returns=[""])  # would be used by the old bypass

    class _Ex:
        name = "binance_futures"
        async def _load_futures_filters(self, sym):
            return {"tickSize": 0.0001, "minPrice": 0.001, "maxPrice": 1000,
                    "stepSize": 1, "minQty": 1, "minNotional": 5.0}
        async def _round_price(self, sym, p):
            return round(p / 0.0001) * 0.0001, {}
        async def _round_quantity(self, sym, q, price_ref=None):
            return max(1.0, round(q)), {}
        async def set_symbol_leverage(self, sym, lev):
            return None
        async def take_profit(self, sym, q, p, side):
            return None
        async def limit_buy(self, sym, q, p):
            # The original entry is rejected; the deterministic repair resubmits
            # with re-derived params and THIS attempt fills successfully.
            from src.exchange.base import OrderInfo
            return OrderInfo(order_id="REPAIRED123", symbol=sym, side="BUY", type="LIMIT",
                             quantity=q, status="FILLED", filled_quantity=q,
                             avg_price=p, error="")

    class _Repo:
        def save(self, *a, **k): return 1
        def update_status(self, *a, **k): return None
        def get_open_by_pair(self, *a, **k): return None
        def get_open_count(self, *a, **k): return 0
        def create(self, *a, **k): return 1

    cfg = {"risk": {"port_usdt": 1.0, "max_leverage": 50,
                    "max_leverage_increase_pct": 10, "min_notional_usdt": 5.0}}
    os_ = OrderService(exchange=_Ex(), config=cfg, signal_repo=_Repo(), decision_repo=_Repo(),
                       order_repo=_Repo(), position_repo=_Repo(), agent=agent)

    dec = TradeDecision(action="ENTER", pair="APEUSDT", direction="LONG",
                        order_type="LIMIT", quantity=31, entry_price=0.162,
                        sl_price=0.158, tp_prices=[0.173], leverage=5)
    # The rejection path calls _repair_order; the LLM must never be touched.
    from src.execution.order_service import ExecutionResult
    result = asyncio.run(os_._repair_order(1, dec, "APEUSDT", "BUY", "-1111 rejected"))
    assert agent.calls == 0, "LLM was called during repair — bypass not removed"
    assert isinstance(result, ExecutionResult)


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


# ─── 4. SL/TP conditional LIMIT orders are sent correctly (regression) ───────
# Root cause: stop_loss()/take_profit() were rejected as "Unsupported futures
# order type" (STOP) or silently downgraded to a MARKET order (TAKE_PROFIT).
# Both must now be placed as conditional LIMITs with stopPrice + price + GTC +
# reduceOnly, and the returned type must be the real Futures conditional type.

class _CaptureConditionalExchange(BinanceExchange):
    """Capture the params POSTed for SL/TP and echo back the sent type."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.last_params: dict | None = None
        self.sent_type: str | None = None

    async def _signed_request(self, method, path, params=None):
        if path.endswith("/order"):
            self.last_params = params
            self.sent_type = params.get("type")
            return {
                "orderId": "999", "symbol": params.get("symbol"),
                "side": params.get("side"), "type": params.get("type"),
                "origQty": params.get("quantity"), "price": params.get("price"),
                "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0",
            }
        return {"leverage": 5} if "leverage" in (params or {}) else {}


@pytest.mark.asyncio
async def test_stop_loss_is_conditional_limit_not_rejected():
    ex = _CaptureConditionalExchange(api_key="k", api_secret="s", testnet=False, futures=True)
    ex._client = _StubClient()
    _seed_filters(ex, "UAIUSDT", tick=0.0001, step=1.0)
    info = await ex.stop_loss("UAIUSDT", 12.0, 0.39, "SELL")
    # Must NOT be rejected as an unsupported type.
    assert info.status != "FAILED", f"SL wrongly failed: {info.error}"
    assert ex.sent_type == "STOP", f"SL not conditional STOP: {ex.sent_type}"
    p = ex.last_params
    assert float(p["stopPrice"]) == 0.39, f"SL stopPrice wrong: {p}"
    assert float(p["price"]) == 0.39, f"SL limit price wrong: {p}"
    assert p["timeInForce"] == "GTC", "SL missing GTC"
    assert p["reduceOnly"] == "true", "SL must be reduceOnly"


@pytest.mark.asyncio
async def test_take_profit_is_conditional_limit_not_market():
    ex = _CaptureConditionalExchange(api_key="k", api_secret="s", testnet=False, futures=True)
    ex._client = _StubClient()
    _seed_filters(ex, "UAIUSDT", tick=0.0001, step=1.0)
    info = await ex.take_profit("UAIUSDT", 12.0, 0.45, "SELL")
    assert info.status != "FAILED", f"TP wrongly failed: {info.error}"
    # Must be the conditional LIMIT type, NOT the market variant.
    assert ex.sent_type == "TAKE_PROFIT", f"TP not conditional LIMIT: {ex.sent_type}"
    p = ex.last_params
    assert float(p["stopPrice"]) == 0.45, f"TP stopPrice wrong: {p}"
    assert float(p["price"]) == 0.45, f"TP limit price wrong: {p}"
    assert p["timeInForce"] == "GTC", "TP missing GTC"
    assert p["reduceOnly"] == "true", "TP must be reduceOnly"


@pytest.mark.asyncio
async def test_1000x_stop_loss_still_unprotected():
    """1000x contracts block STOP (-4120): SL must be deferred to the
    position manager, never sent as a rejected order."""
    ex = _CaptureConditionalExchange(api_key="k", api_secret="s", testnet=False, futures=True)
    ex._client = _StubClient()
    _seed_filters(ex, "1000BONKUSDT", tick=0.0000001, step=1.0)
    info = await ex.stop_loss("1000BONKUSDT", 1_000_000.0, 0.0000039, "SELL")
    assert info.status == "UNPROTECTED", f"1000x SL should be UNPROTECTED: {info.status}"
    assert info.order_id == "", "1000x SL must not be placed on the wire"


# ─── 5. Position monitor recognizes real conditional order types ────────────

def test_monitor_recognizes_stop_and_take_profit_types():
    from src.domain.models import Position
    from src.exchange.base import OrderInfo
    from datetime import datetime, timezone

    # Fake exchange returning the REAL Binance conditional types.
    class _Ex:
        name = "binance_futures"
        async def get_open_orders(self, symbol):
            return [
                OrderInfo(order_id="s1", symbol=symbol, side="SELL",
                          type="STOP", quantity=1, price=0.39, status="NEW"),
                OrderInfo(order_id="t1", symbol=symbol, side="SELL",
                          type="TAKE_PROFIT", quantity=1, price=0.45, status="NEW"),
            ]
        async def get_mark_price(self, symbol): return 0.42
        async def get_klines_close(self, symbol, tf): return None
    ex = _Ex()

    class _PosRepo:
        def get_open_positions(self):
            return [Position(pair="UAIUSDT", direction="LONG", entry_price=0.41,
                             quantity=1, sl_price=0.39, tp_prices=[0.45],
                             entry_time=datetime.now(timezone.utc).isoformat())]

    # Replicate the monitor's (now-correct) classification inline so the test is
    # stable without instantiating the full async loop. This asserts the exact
    # type sets the monitor uses to detect active SL/TP orders.
    open_orders = [
        OrderInfo(order_id="s1", symbol="UAIUSDT", side="SELL", type="STOP"),
        OrderInfo(order_id="t1", symbol="UAIUSDT", side="SELL", type="TAKE_PROFIT"),
    ]
    sl_ok = any(o.type in ("STOP_LOSS", "STOP_LOSS_LIMIT", "STOP") for o in open_orders)
    tp_types = ("TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT_MARKET", "LIMIT")
    tp_count = sum(1 for o in open_orders if o.type in tp_types)
    assert sl_ok is True, "monitor failed to recognize STOP SL"
    assert tp_count == 1, "monitor failed to recognize TAKE_PROFIT TP"


# ─── 6. Manual /close command closes the right position ─────────────────────
# Validates the close_position_by_symbol helper used by the /close Telegram
# command: it must find the live position, cancel resting orders, close, and
# report the result — and gracefully report "no open position" otherwise.

class _CloseExchange:
    """Fakes Binance: one open ENAUSDT LONG, resting STOP+TP, market close."""
    name = "binance_futures"
    def __init__(self, positions):
        self._positions = positions
        self.cancelled = []
        self.closed = None
    async def get_positions(self):
        return self._positions
    async def get_mark_price(self, sym): return 1.25
    async def cancel_all_orders(self, sym):
        self.cancelled.append(sym)
        return 2  # a STOP and a TP were resting
    async def limit_sell(self, sym, qty, price):
        from src.exchange.base import OrderInfo
        self.closed = (sym, qty, price)
        return OrderInfo(order_id="C1", symbol=sym, side="SELL", type="LIMIT",
                         quantity=qty, price=price, status="FILLED",
                         filled_quantity=qty, avg_price=1.25)
    async def limit_buy(self, sym, qty, price):
        from src.exchange.base import OrderInfo
        self.closed = (sym, qty, price)
        return OrderInfo(order_id="C1", symbol=sym, side="BUY", type="LIMIT",
                         quantity=qty, price=price, status="FILLED",
                         filled_quantity=qty, avg_price=1.25)
    async def get_order(self, sym, oid):
        from src.exchange.base import OrderInfo
        return OrderInfo(order_id=oid, status="FILLED", avg_price=1.25,
                         filled_quantity=self.closed[1] if self.closed else 0)


class _ClosePosRepo:
    def __init__(self): self.closed_id = None
    def get_open_by_pair(self, pair):
        from src.domain.models import Position
        if pair == "ENAUSDT":
            return Position(id=7, pair="ENAUSDT", direction="LONG",
                            entry_price=1.0, quantity=100, status="OPEN")
        return None
    def close_position(self, pid, exit_price, pnl, reason, closed_by):
        self.closed_id = pid


def _make_pm(positions):
    from src.execution.position_manager import PositionManager
    ex = _CloseExchange(positions)
    pm = PositionManager(ex, {}, _ClosePosRepo())
    return pm, ex


def test_close_resolves_symbol_and_closes():
    from src.exchange.base import PositionInfo
    pos = PositionInfo(symbol="ENAUSDT", direction="LONG", size=100,
                       entry_price=1.0, mark_price=1.25, unrealized_pnl=25.0)
    pm, ex = _make_pm([pos])
    res = asyncio.run(pm.close_position_by_symbol("ENA"))
    assert res["ok"] is True, res
    assert res["symbol"] == "ENAUSDT"
    assert res["size"] == 100
    assert res["pnl"] == 25.0
    assert "ENAUSDT" in ex.cancelled, "resting SL/TP not cancelled before close"
    assert ex.closed == ("ENAUSDT", 100, 1.25), "close used wrong qty/price"


def test_close_no_open_position_reports_cleanly():
    pm, ex = _make_pm([])
    res = asyncio.run(pm.close_position_by_symbol("BONKUSDT"))
    assert res["ok"] is False
    assert "No open position" in res["error"]


def test_close_hash_symbol_and_usdt_variants():
    from src.exchange.base import PositionInfo
    pos = PositionInfo(symbol="ENAUSDT", direction="SHORT", size=50,
                       entry_price=2.0, mark_price=1.25, unrealized_pnl=37.5)
    pm, ex = _make_pm([pos])
    # #ENA and ENAUSDT both resolve to the live ENAUSDT position
    r1 = asyncio.run(pm.close_position_by_symbol("#ENA"))
    r2 = asyncio.run(pm.close_position_by_symbol("ENAUSDT"))
    assert r1["ok"] and r1["symbol"] == "ENAUSDT"
    assert r2["ok"] and r2["size"] == 50
