"""Task 5 integration — OrderService blocks orders that fail the validation
gate before any exchange call (entry, SL, TP).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.execution.order_service import OrderService
from src.domain.models import TradeDecision


def _make_service(exchange):
    cfg = {"risk": {"margin_type": "ISOLATED", "margin_usage_pct": 80,
                    "min_notional_usdt": 1.0}}
    repos = MagicMock()
    repos.get_active_for_decision.return_value = None
    # filters: APEUSDT with a low maxPrice so 1500 is rejected
    exchange._load_futures_filters = AsyncMock(return_value={
        "tickSize": 0.0001, "minPrice": 0.001, "maxPrice": 1000,
        "stepSize": 1, "minQty": 1, "minNotional": 5.0,
    })
    svc = OrderService(exchange, cfg, repos, repos, repos, repos)
    return svc, repos


def test_entry_blocked_before_exchange_call():
    exchange = MagicMock()
    exchange.name = "binance_futures"
    exchange.set_symbol_leverage = AsyncMock()
    exchange.set_margin_type = AsyncMock()
    exchange.limit_buy = AsyncMock()
    svc, repos = _make_service(exchange)

    dec = TradeDecision(action="ENTER", pair="APEUSDT", direction="LONG",
                        quantity=31, entry_price=1500.0,  # > maxPrice 1000
                        sl_price=0.15, tp_prices=[0.18], leverage=5,
                        confidence=0.9, reason="t")
    res = asyncio.run(svc.execute(signal_id=1, decision=dec, decision_id=7))

    assert res.status == "VALIDATION_SKIP", res
    assert res.success is False
    exchange.limit_buy.assert_not_called()  # never hit the API
