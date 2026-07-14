"""Regression — reduceOnly is an explicit per-call intent, not a per-type default.

Reproduces the -2022 "ReduceOnly Order is rejected" failure: `_place_order`
previously forced `reduceOnly=true` onto EVERY LIMIT/STOP/TAKE_PROFIT order,
including plain LIMIT *entry* orders (limit_buy/limit_sell). Binance rejects
opening orders that carry reduceOnly, so entries failed outright — and the
deterministic repair path (which also used limit_buy/limit_sell) failed the
same way, guaranteeing a permanent -2022 with no recovery.

The fix makes `reduceOnly` an explicit `reduce=` flag instead of inferring it
from the order type. Plain LIMIT entry orders pass reduce=False; every genuine
EXIT (stop_loss, take_profit, maker-close, TP-fallback LIMIT) passes
reduce=True. This guarantees:
  - entries are NEVER reduce-only (no -2022), and
  - exits ARE always reduce-only (no flip risk).
"""

import asyncio
from unittest.mock import AsyncMock

from src.exchange.binance import BinanceExchange


def _exch():
    return BinanceExchange("k", "s", futures=True, testnet=False)


def _capture(exch):
    captured = {}

    async def _fake_signed(method, path, params):
        captured.clear()
        captured.update(params)
        return {"orderId": "1", "symbol": params["symbol"], "side": params["side"],
                "type": params["type"], "status": "NEW", "origQty": params["quantity"],
                "executedQty": "0", "cummulativeQuoteQty": "0"}

    exch._preflight_symbol = AsyncMock(return_value=True)
    exch._signed_request = AsyncMock(side_effect=_fake_signed)
    return captured


def test_entry_limit_is_not_reduce_only():
    """limit_buy / limit_sell (entry) must NOT carry reduceOnly."""
    exch = _exch()
    cap = _capture(exch)
    asyncio.run(exch.limit_buy("PUNDIXUSDT", 54.0, 0.0929))
    assert cap.get("reduceOnly") is None, f"entry LIMIT must not be reduce-only: {cap}"
    assert cap["type"] == "LIMIT"


def test_exit_limit_close_is_reduce_only():
    """limit_sell(..., reduce=True) maker-close MUST carry reduceOnly=true."""
    exch = _exch()
    cap = _capture(exch)
    asyncio.run(exch.limit_sell("PUNDIXUSDT", 54.0, 0.0929, reduce=True))
    assert cap.get("reduceOnly") == "true", f"exit LIMIT must be reduce-only: {cap}"


def test_tp_fallback_limit_is_reduce_only():
    """limit_buy(..., reduce=True) TP-fallback MUST carry reduceOnly=true."""
    exch = _exch()
    cap = _capture(exch)
    asyncio.run(exch.limit_buy("PUNDIXUSDT", 54.0, 0.11, reduce=True))
    assert cap.get("reduceOnly") == "true", f"TP-fallback LIMIT must be reduce-only: {cap}"


def test_stop_is_reduce_only():
    """stop_loss (STOP) MUST carry reduceOnly=true."""
    exch = _exch()
    cap = _capture(exch)
    asyncio.run(exch.stop_loss("PUNDIXUSDT", 54.0, 0.09, "SELL"))
    assert cap.get("reduceOnly") == "true", f"STOP must be reduce-only: {cap}"
    assert cap["type"] == "STOP"


def test_take_profit_is_reduce_only():
    """take_profit (TAKE_PROFIT_LIMIT) MUST carry reduceOnly=true."""
    exch = _exch()
    cap = _capture(exch)
    asyncio.run(exch.take_profit("PUNDIXUSDT", 54.0, 0.11, "SELL"))
    assert cap.get("reduceOnly") == "true", f"TAKE_PROFIT must be reduce-only: {cap}"
    assert cap["type"] == "TAKE_PROFIT"
