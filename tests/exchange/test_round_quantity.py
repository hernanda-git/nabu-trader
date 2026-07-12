"""Task 3 — _round_quantity must respect stepSize (integer lots) and the
exchange MIN_NOTIONAL using the real filter value (not a hardcoded 5.0).

APEUSDT: stepSize=1 (integer qty), minQty=1, minNotional=$5.
"""

import asyncio

from src.exchange.binance import BinanceExchange


def test_round_quantity_ape_integer_and_min_notional():
    exch = BinanceExchange("k", "s", futures=True)
    exch._filters["APEUSDT"] = {"stepSize": 1, "minQty": 1, "minNotional": 5.0}
    # 30.864 -> rounds up to integer lot 31; 31 * 0.162 = 5.022 >= 5.0
    q, _ = asyncio.run(exch._round_quantity("APEUSDT", 30.864, price_ref=0.162))
    assert q == 31, q
    assert q * 0.162 >= 5.0


def test_round_quantity_meets_min_notional_by_loop():
    exch = BinanceExchange("k", "s", futures=True)
    exch._filters["APEUSDT"] = {"stepSize": 1, "minQty": 1, "minNotional": 5.0}
    # qty 30 -> 30*0.162 = 4.86 < 5 -> loop must bump to 31 (4.86 -> 5.022)
    q, _ = asyncio.run(exch._round_quantity("APEUSDT", 30, price_ref=0.162))
    assert q == 31, q
    assert q * 0.162 >= 5.0
