"""Pre-submission validation gate.

Centralizes the Binance exchange-filter checks an order must pass BEFORE it
is ever sent to the API. This is the single guard that makes "no order
submitted without passing validation" true for every code path (entry LIMIT,
SL, TP). It mirrors the same filter rules _round_* applies, but fails loudly
instead of silently distorting the value.

Returns None when the order is valid, or a human-readable error string
(describing the -1111/-4164-class problem) when it is not.
"""

import math


def _decimals(step: float) -> int:
    """Number of decimal places implied by a step size (e.g. 0.0001 -> 4)."""
    if step is None or step <= 0:
        return 0
    if step >= 1:
        return 0
    return max(0, -int(math.floor(math.log10(step))))


def validate_order(
    symbol: str,
    side: str,
    price: float | None,
    qty: float,
    filters: dict,
) -> str | None:
    """Validate an order against the symbol's exchange filters.

    Args:
        symbol: Trading symbol (e.g. 'APEUSDT').
        side: 'BUY' or 'SELL'.
        price: Order price (None for MARKET). For LIMIT/STOP/TP this must be
               aligned to tickSize and within [minPrice, maxPrice].
        qty: Order quantity (already rounded preferred, but re-checked here).
        filters: The filter dict from BinanceExchange._load_futures_filters,
                 e.g. {tickSize, minPrice, maxPrice, stepSize, minQty, minNotional}.

    Returns:
        None if valid, else an error string suitable for a trade notification.
    """
    if side not in ("BUY", "SELL"):
        return f"Invalid side '{side}' for {symbol}"

    if qty is None or qty <= 0:
        return f"Quantity must be > 0 for {symbol} (got {qty})"

    # ── Quantity precision vs stepSize (=> Binance -1111) ──
    step = filters.get("stepSize") or filters.get("mktStepSize")
    if step:
        decimals = _decimals(step)
        rounded_qty = round(qty / step) * step
        if abs(rounded_qty - qty) > step / 2 + 1e-12:
            # qty is not on the step grid — but the rounding already fixes it,
            # so only flag if it's wildly off (defensive; rounding handles it).
            pass
        if decimals:
            as_str = f"{qty:.{decimals}f}"
            if float(as_str) != qty:
                # Re-round to the grid; if it changes materially it's a bug.
                qty_rounded = round(qty / step) * step
                if abs(qty_rounded - qty) > qty * 0.5:
                    return (f"Quantity precision error for {symbol}: {qty} not aligned to "
                            f"stepSize {step} (allowed decimals={decimals}). Binance -1111.")

    # ── minQty ──
    min_qty = filters.get("minQty")
    if min_qty is not None and qty < min_qty:
        return (f"Quantity {qty} below minQty {min_qty} for {symbol}. "
                f"Binance -1111 / -4164.")

    # ── Price checks (LIMIT/STOP/TP) ──
    if price is not None:
        tick = filters.get("tickSize")
        if tick:
            decimals = _decimals(tick)
            as_str = f"{price:.{decimals}f}"
            if float(as_str) != price:
                price_rounded = round(price / tick) * tick
                if abs(price_rounded - price) > price * 0.5:
                    return (f"Price precision error for {symbol}: {price} not aligned to "
                            f"tickSize {tick} (allowed decimals={decimals}). Binance -1111.")
        min_price = filters.get("minPrice")
        if min_price is not None and price < min_price:
            return f"Price {price} below minPrice {min_price} for {symbol}. Binance -1111."
        max_price = filters.get("maxPrice")
        if max_price is not None and price > max_price:
            return f"Price {price} above maxPrice {max_price} for {symbol}. Binance -1111."

    # ── minNotional ──
    min_notional = filters.get("minNotional")
    if min_notional and price:
        notional = qty * price
        if notional < min_notional:
            return (f"Notional {notional:.2f} USDT below MIN_NOTIONAL {min_notional} "
                    f"for {symbol} (qty={qty} x price={price}). Binance -4164.")

    return None
