"""Task 1 — _load_futures_filters must match the requested symbol, not rules[0].

Reproduces the APE -1111 root cause: for APEUSDT the exchangeInfo response's
first element was BTCUSDT, so rules[0] returned BTCUSDT's filters (minPrice=556.8),
which made _round_price clamp APE's valid 0.162 -> 556.8 -> -1111 rejection.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from src.exchange.binance import BinanceExchange


def _fake_exchangeInfo():
    # APEUSDT is NOT first — BTCUSDT leads, proving rules[0] is wrong.
    return {"symbols": [
        {"symbol": "BTCUSDT", "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.1", "minPrice": "556.8", "maxPrice": "100000"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "1000", "stepSize": "0.001"}]},
        {"symbol": "APEUSDT", "status": "TRADING", "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.0001", "minPrice": "0.001", "maxPrice": "1000"},
            {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "10000000", "stepSize": "1"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]},
    ]}


def test_filters_match_by_symbol_not_first():
    exch = BinanceExchange("k", "s", futures=True, testnet=False)
    with patch.object(exch, "_public_request", AsyncMock(return_value=_fake_exchangeInfo())):
        rules = asyncio.run(exch._load_futures_filters("APEUSDT"))
    assert rules is not None, "expected filters for APEUSDT"
    assert rules["tickSize"] == 0.0001, rules
    assert rules["minPrice"] == 0.001, rules
    assert rules["stepSize"] == 1, rules
    # minNotional must be captured from the MIN_NOTIONAL filter
    assert rules.get("minNotional") == 5.0, rules
