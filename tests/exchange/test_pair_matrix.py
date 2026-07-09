"""Task 8 — pair-matrix regression.

Every supported trading pair must flow through the SAME pipeline:
  resolve symbol -> load real exchange filters -> round price/qty to that
  symbol's precision -> Gate2 sizes qty/leverage -> validate_order() gates it.

If any pair fails this, a preventable -1111/-4164 rejection is still possible.
Covers low-priced coins (APE, DOGE, 1000PEPE), mid (ETH) and high (BTC).
"""

import pytest

from src.agent.gate import SafetyGate2, _snap_leverage
from src.domain.models import TradeDecision
from src.exchange.validation import validate_order

# Representative Binance USDⓈ-M futures filter profiles (real shapes).
PAIRS = {
    "BTCUSDT":    dict(price=65000.0,  tick=0.01,  step=0.001,  minQty=0.001, minNotional=5.0),
    "ETHUSDT":    dict(price=3000.0,   tick=0.01,  step=0.0001, minQty=0.0001, minNotional=5.0),
    "APEUSDT":    dict(price=0.162,    tick=0.0001, step=1.0,    minQty=1.0,   minNotional=5.0),
    "DOGEUSDT":   dict(price=0.1234,   tick=0.00001, step=1.0,   minQty=1.0,   minNotional=5.0),
    "1000PEPEUSDT": dict(price=0.00000987, tick=0.0000001, step=1.0, minQty=1.0, minNotional=5.0),
}


def _filters(p):
    return {
        "tickSize": p["tick"],
        "minPrice": 0.00000001,
        "maxPrice": p["price"] * 1e6,
        "stepSize": p["step"],
        "minQty": p["minQty"],
        "minNotional": p["minNotional"],
    }


def _round_price(p, price):
    return round(price / p["tick"]) * p["tick"]


def _round_qty(p, qty):
    import math
    q = max(p["minQty"], math.floor(qty / p["step"]) * p["step"])
    return q


def _gate():
    from unittest.mock import MagicMock
    repo = MagicMock()
    repo.get_open_count.return_value = 0
    repo.get_daily_pnl.return_value = 0.0
    repo.get_total_notional_usdt.return_value = 0.0
    repo.get_open_by_pair.return_value = None
    risk = {
        "port_usdt": 1.0, "min_notional_usdt": 5.0, "max_leverage": 50,
        "margin_usage_pct": 80, "max_portfolio_leverage": 10,
        "max_leverage_increase_pct": 10, "max_port_pct": 10,
        "risk_per_trade_percent": 10, "max_position_size_usdt": 100,
        "max_concurrent_positions": 2, "daily_loss_limit_percent": 30,
    }
    return SafetyGate2({"risk": risk}, position_repo=repo)


@pytest.mark.parametrize("symbol", list(PAIRS.keys()))
def test_pair_pipeline(symbol):
    p = PAIRS[symbol]
    filters = _filters(p)

    # 1. Gate2 sizes qty/leverage for this real price.
    dec = TradeDecision(action="ENTER", pair=symbol, direction="LONG",
                        quantity=0, entry_price=p["price"],
                        sl_price=p["price"] * 0.97, tp_prices=[p["price"] * 1.05],
                        leverage=1, confidence=0.9, reason="regression")
    ok, reason, out = _gate().check(dec, balance_usdt=100.0, filters=filters)
    assert ok, f"{symbol}: gate rejected: {reason}"
    assert out.quantity > 0
    assert out.leverage >= 1

    # 2. Round to the symbol's real precision.
    price = _round_price(p, out.entry_price or p["price"])
    qty = _round_qty(p, out.quantity)

    # 3. Quantities/price must satisfy the symbol's own filters.
    assert qty >= p["minQty"], f"{symbol}: qty {qty} below minQty {p['minQty']}"
    assert price >= filters["minPrice"]

    # 4. Notional must clear the symbol's minNotional.
    notional = qty * price
    assert notional >= p["minNotional"] - 1e-6, (
        f"{symbol}: notional {notional} below minNotional {p['minNotional']}")

    # 5. validate_order (the pre-submission gate) must pass.
    err = validate_order(symbol, "BUY", price, qty, filters)
    assert err is None, f"{symbol}: validation failed: {err}"

    # 6. Leverage never exceeds the configured ceiling.
    assert out.leverage <= 50


def test_snap_leverage_no_literals_bleed():
    # Guard against re-introducing a hardcoded leverage.
    assert _snap_leverage(5, 50) == 5
    assert _snap_leverage(5.4, 50) == 7   # rounds up to nearest valid step
    assert _snap_leverage(100, 50) == 50  # clamped to ceiling
