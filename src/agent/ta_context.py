"""Technical analysis context — fetches OHLCV and computes key levels for the LLM.

Only used when the original signal does NOT provide SL/TP, so the LLM has
real market structure to anchor its stop-loss and take-profit decisions
instead of guessing from training-data heuristics.

Computes (pure Python, no external TA libs):
  - ATR (14-period) — volatility measure for SL distance
  - Swing highs / lows — natural support/resistance levels
  - EMA (20, 50) — trend direction
  - Fibonacci retracements (0.236, 0.382, 0.5, 0.618, 0.786)
    from the most recent significant swing
  - Current price vs. key levels

Returns a formatted string injected into the LLM prompt.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.exchange.base import Exchange

log = logging.getLogger("agent.ta_context")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _float(candle: list, idx: int) -> float:
    """Safely extract a float from a kline array."""
    return float(candle[idx])


def _emas(closes: list[float], period: int) -> list[float]:
    """Compute EMA series. Returns list same length as closes (NaN-padded front)."""
    if len(closes) < period:
        return [0.0] * len(closes)
    k = 2.0 / (period + 1)
    ema = [0.0] * len(closes)
    # Seed with SMA
    ema[period - 1] = sum(closes[:period]) / period
    for i in range(period, len(closes)):
        ema[i] = closes[i] * k + ema[i - 1] * (1 - k)
    return ema


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int = 14) -> float:
    """Compute ATR (Average True Range) over the last `period` candles."""
    if len(highs) < period + 1:
        return 0.0
    trs: list[float] = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # Use last `period` true ranges
    return sum(trs[-period:]) / period


def _find_swings(highs: list[float], lows: list[float],
                 lookback: int = 5) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Find swing highs and swing lows in the last `lookback * 2 + 1` candles.

    A swing high at index i:  high[i] > all highs[i-lookback .. i+lookback]
    A swing low  at index i:  low[i]  < all lows[i-lookback .. i+lookback]

    Returns (swing_highs, swing_lows) as [(index, price), ...].
    """
    n = len(highs)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []

    # Only scan the last portion (recent structure matters most)
    start = max(lookback, n - 60)
    end = n - lookback

    for i in range(start, end):
        # Swing high
        is_high = all(highs[i] >= highs[j] for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_high:
            swing_highs.append((i, highs[i]))
        # Swing low
        is_low = all(lows[i] <= lows[j] for j in range(i - lookback, i + lookback + 1) if j != i)
        if is_low:
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows


def _fib_levels(swing_high: float, swing_low: float) -> dict[str, float]:
    """Fibonacci retracement levels from a swing high to swing low (for LONGs).
    For SHORTs, the caller should reverse the interpretation."""
    diff = swing_high - swing_low
    return {
        "0.236": swing_high - diff * 0.236,
        "0.382": swing_high - diff * 0.382,
        "0.500": swing_high - diff * 0.500,
        "0.618": swing_high - diff * 0.618,
        "0.786": swing_high - diff * 0.786,
    }


def _round_levels(price: float, step_pct: float = 2.5) -> list[float]:
    """Generate round-number levels near the current price (±10%)."""
    import math
    if price <= 0:
        return []
    magnitude = 10 ** math.floor(math.log10(price))
    step = magnitude * 0.5
    if step < 0.001:
        step = 0.001
    lo = price * 0.90
    hi = price * 1.10
    levels = []
    for mult in range(int(lo / step), int(hi / step) + 2):
        lvl = round(mult * step, 8)
        if lo <= lvl <= hi and abs(lvl - price) > step * 0.1:
            levels.append(lvl)
    return levels


# ─── Main entry point ─────────────────────────────────────────────────────────

async def fetch_ta_context(exchange: "Exchange", symbol: str,
                           timeframe: str = "4h") -> str:
    """Fetch OHLCV data and compute a technical analysis summary for the LLM.

    Returns a formatted markdown string ready to inject into the prompt.
    Returns empty string if data is unavailable.
    """
    # Fetch 100 closed candles
    klines = await exchange.get_klines(symbol, interval=timeframe, limit=100)
    if not klines or len(klines) < 30:
        log.warning("Insufficient klines for %s (%d candles) — skipping TA context",
                     symbol, len(klines))
        return ""

    # Parse OHLCV
    opens  = [_float(c, 1) for c in klines]
    highs  = [_float(c, 2) for c in klines]
    lows   = [_float(c, 3) for c in klines]
    closes = [_float(c, 4) for c in klines]
    vols   = [_float(c, 5) for c in klines]

    current_price = closes[-1]

    # ── ATR (14) ──
    atr14 = _atr(highs, lows, closes, period=14)

    # ── EMAs ──
    ema20 = _emas(closes, 20)
    ema50 = _emas(closes, 50)
    ema20_now = ema20[-1] if ema20[-1] else ema20[-2] if len(ema20) > 1 else 0
    ema50_now = ema50[-1] if ema50[-1] else ema50[-2] if len(ema50) > 1 else 0

    # Trend
    if ema20_now > 0 and ema50_now > 0:
        if ema20_now > ema50_now:
            trend = "BULLISH (EMA20 > EMA50)"
        elif ema20_now < ema50_now:
            trend = "BEARISH (EMA20 < EMA50)"
        else:
            trend = "NEUTRAL"
    else:
        trend = "INSUFFICIENT DATA"

    # ── Swing highs / lows ──
    swing_highs, swing_lows = _find_swings(highs, lows, lookback=3)

    # Take the most recent few of each (most relevant)
    recent_sh = swing_highs[-4:] if swing_highs else []
    recent_sl = swing_lows[-4:] if swing_lows else []

    # ── Fibonacci from the most recent significant swing ──
    # Use the last swing high and last swing low for the retracement
    fib: dict[str, float] = {}
    if recent_sh and recent_sl:
        sh_price = recent_sh[-1][1]
        sl_price = recent_sl[-1][1]
        if sh_price > sl_price:
            fib = _fib_levels(sh_price, sl_price)

    # ── Volume trend ──
    if len(vols) >= 20:
        vol_recent = sum(vols[-5:]) / 5
        vol_avg = sum(vols[-20:]) / 20
        if vol_avg > 0:
            vol_ratio = vol_recent / vol_avg
            if vol_ratio > 1.5:
                vol_trend = f"INCREASING ({vol_ratio:.1f}x avg)"
            elif vol_ratio < 0.6:
                vol_trend = f"DECLINING ({vol_ratio:.1f}x avg)"
            else:
                vol_trend = f"NORMAL ({vol_ratio:.1f}x avg)"
        else:
            vol_trend = "N/A"
    else:
        vol_trend = "N/A"

    # ── Build the context string ──
    lines = [
        f"## Technical Context ({symbol} {timeframe}, last {len(closes)} candles)",
        f"  Current price: {current_price:.8f}",
        f"  ATR (14): {atr14:.8f}",
        f"  Trend: {trend}",
        f"  EMA (20): {ema20_now:.8f}",
        f"  EMA (50): {ema50_now:.8f}",
        f"  Volume: {vol_trend}",
        "",
    ]

    # Swing levels
    if recent_sh or recent_sl:
        lines.append("  Swing highs (resistance):")
        for _, p in recent_sh:
            lines.append(f"    {p:.8f}")
        lines.append("  Swing lows (support):")
        for _, p in recent_sl:
            lines.append(f"    {p:.8f}")
        lines.append("")

    # Fibonacci
    if fib:
        lines.append("  Fibonacci retracement (last swing):")
        for level, price in fib.items():
            lines.append(f"    {level}: {price:.8f}")
        lines.append("")

    # Round levels near price
    rlevels = _round_levels(current_price)
    if rlevels:
        lines.append("  Key round levels:")
        for lvl in rlevels:
            lines.append(f"    {lvl:.8f}")
        lines.append("")

    return "\n".join(lines)
