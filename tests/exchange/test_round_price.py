"""Task 2 — _round_price must never inflate a valid price by clamping to a
bogus (orders-of-magnitude-too-high) minPrice.

This guards against the APE -1111 class of bug: when the wrong symbol's
filters leak in (or a bad minPrice is cached), the old clamp did
round(0.162) -> 0.162 < minPrice(556.8) -> 556.8, distorting the price by
>1000x. A minPrice that is >10x the requested price signals a bad filter;
clamp only when the price is genuinely below the floor.
"""

import asyncio

from src.exchange.binance import BinanceExchange


def test_round_price_no_magnitude_flip():
    exch = BinanceExchange("k", "s", futures=True)
    # Correct tick size, but an absurd minPrice (simulates leaked rules[0]).
    exch._filters["APEUSDT"] = {"tickSize": 0.0001, "minPrice": 100.0, "maxPrice": 1000}
    price, _ = asyncio.run(exch._round_price("APEUSDT", 0.162))
    assert abs(price - 0.162) < 1e-6, f"price was inflated to {price}"


def test_round_price_clamps_only_when_truly_out_of_bounds():
    exch = BinanceExchange("k", "s", futures=True)
    exch._filters["APEUSDT"] = {"tickSize": 0.0001, "minPrice": 0.001, "maxPrice": 1000}
    # A genuinely too-low price IS clamped to the floor (legit).
    low, _ = asyncio.run(exch._round_price("APEUSDT", 0.0003))
    assert low == 0.001, low
    # A valid price is never inflated.
    ok, _ = asyncio.run(exch._round_price("APEUSDT", 0.162))
    assert abs(ok - 0.162) < 1e-6, ok
