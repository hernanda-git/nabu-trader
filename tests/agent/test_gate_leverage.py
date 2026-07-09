"""Task 6 — dynamic leverage driven by the REAL exchange MIN_NOTIONAL and
config caps (no hardcoded 5/1/50 in the decision math).
"""

import pytest

from src.agent.gate import SafetyGate2
from src.domain.models import TradeDecision


def _signal(pair, direction, entry, sl, tp):
    return TradeDecision(action="ENTER", pair=pair, direction=direction,
                         quantity=0, entry_price=entry, sl_price=sl,
                         tp_prices=tp, leverage=1, confidence=0.9, reason="t")


def _gate(risk):
    from unittest.mock import MagicMock
    repo = MagicMock()
    repo.get_open_count.return_value = 0
    repo.get_daily_pnl.return_value = 0.0
    repo.get_total_notional_usdt.return_value = 0.0
    repo.get_open_by_pair.return_value = None
    cfg = {"risk": risk}
    return SafetyGate2(cfg, position_repo=repo)


RISK = {"port_usdt": 1.0, "min_notional_usdt": 1.0, "max_leverage": 50,
        "margin_usage_pct": 80, "max_portfolio_leverage": 10,
        "max_leverage_increase_pct": 10, "max_port_pct": 10,
        "risk_per_trade_percent": 10, "max_position_size_usdt": 100,
        "max_concurrent_positions": 2, "daily_loss_limit_percent": 30}


APE_FILTERS = {"tickSize": 0.0001, "minPrice": 0.001, "stepSize": 1,
               "minQty": 1, "minNotional": 5.0}


def test_leverage_meets_exchange_min_notional():
    g = _gate(RISK)
    # APE: price 0.162, exchange minNotional $5 -> need qty to give >= $5 notional
    ok, reason, dec = g.check(_signal("APEUSDT", "LONG", 0.162, 0.1581, [0.1735]),
                              balance_usdt=100.0, filters=APE_FILTERS)
    assert ok, reason
    assert dec.leverage >= 1
    assert dec.quantity > 0
    # meets exchange min notional (real filter value, not hardcoded 5)
    assert dec.quantity * 0.162 >= 5.0 - 1e-6, (dec.quantity, dec.quantity * 0.162)
    # leverage never exceeds the configured ceiling
    assert dec.leverage <= RISK["max_leverage"]


def test_no_filters_falls_back_to_config_min_notional():
    g = _gate(RISK)
    ok, reason, dec = g.check(_signal("BTCUSDT", "LONG", 65000, 63000, [68000]),
                              balance_usdt=100.0, filters=None)
    assert ok, reason
    assert dec.leverage >= 1
