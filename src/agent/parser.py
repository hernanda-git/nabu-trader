"""Regex pre-parse — fast signal extraction from raw Telegram messages.

Runs in ~1ms but delegates pair resolution to the dynamic SymbolRegistry,
eliminating hardcoded coin symbol lists.

Architecture:
    Raw text → SymbolRegistry.resolve()  → (symbol_name, SymbolInfo)
             → generic regex for direction, entry, SL, TP, volume
             → TradeSignal

The SymbolRegistry is populated from live Binance Futures exchangeInfo
at startup, with background refresh every 15 minutes. Falls back to
built-in seed data if the API is unreachable.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from src.domain.models import TradeSignal

# Try to import SymbolRegistry; if not available, pair will be None
try:
    from src.exchange.symbol_registry import get_registry
except ImportError:
    get_registry = None  # type: ignore[assignment]

DIRECTION_PATTERNS = [
    re.compile(r"\b(buy|long|bullish|upside|Call)\b", re.I),
    re.compile(r"\b(sell|short|bearish|downside|Put)\b", re.I),
]

ENTRY_PATTERNS = [
    re.compile(r'(?:entry|enter|open|buy\s*at|sell\s*at|limit|@\s*)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
    re.compile(r'(?:zone|range|area)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
]

SL_PATTERNS = [
    re.compile(r'(?:sl|stop\s*(?:loss)?|stoploss)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
    re.compile(r'(?:invalidat|invalid)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
]

TP_PATTERNS = [
    re.compile(r'(?:tp\s*1?|take\s*profit\s*1?|target\s*1?|tgt\s*1?)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
    re.compile(r'(?:tp\s*2|take\s*profit\s*2|target\s*2|tgt\s*2)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
    re.compile(r'(?:tp\s*3|take\s*profit\s*3|target\s*3|tgt\s*3)\s*[:-]?\s*([\d,]+\'?\.?\d*)', re.I),
]

# Additional patterns for extended signal metadata
VOLUME_PATTERNS = [
    re.compile(r'(?:vol|volume)\s*[:-]?\s*([\d,.]+[kKmMbB]?)', re.I),
]

LEVERAGE_PATTERNS = [
    re.compile(r'(?:lev|leverage|x)\s*[:-]?\s*(\d+)(?:\s*x)?', re.I),
]


def parse_signal(message_id: int, channel: str, raw_text: str,
                 has_media: bool = False) -> TradeSignal:
    """Fast regex pre-parse of a Telegram message into a TradeSignal.

    Pair resolution is delegated to the dynamic SymbolRegistry, which
    caches all USDⓈ-M Futures pairs from Binance exchangeInfo.

    Missing fields (None) are expected — the LLM agent will enrich them.
    """
    text = raw_text.strip()
    if not text:
        return TradeSignal(message_id=message_id, channel=channel, raw_text=raw_text)

    # Step 1: Resolve pair via SymbolRegistry (dynamic, no hardcoded symbols)
    pair = _resolve_pair(text)

    # Step 2: Extract other fields via generic regex
    direction = _extract_direction(text)
    entry_price = _extract_entry(text)
    sl_price = _extract_sl(text)
    tp_prices = _extract_tp(text)

    return TradeSignal(
        message_id=message_id,
        channel=channel,
        raw_text=raw_text,
        pair=pair,
        direction=direction,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_prices=tp_prices,
        has_media=has_media,
    )


def _resolve_pair(text: str) -> str | None:
    """Resolve a trading pair from text using the SymbolRegistry.

    If the SymbolRegistry is not available (not initialized yet or
    import failed), falls back to generic single-word extraction.
    """
    registry = get_registry() if get_registry else None

    if registry and registry.is_ready:
        symbol, info = registry.resolve(text)
        if symbol:
            return symbol
        return None

    # Fallback: very basic generic pair extraction without hardcoded symbols
    # This catches #BTC, BTC/USDT patterns without needing a registry
    fallback = re.search(r'[#\$]?([A-Za-z]{2,10})\s*/?\s*(USDT|USD|BUSD)\b', text, re.I)
    if fallback:
        base = fallback.group(1).upper()
        quote = fallback.group(2).upper()
        return f"{base}{quote}"

    fallback2 = re.search(r'\b([A-Za-z]{2,10}USDT)\b', text, re.I)
    if fallback2:
        return fallback2.group(1).upper()

    return None


def _extract_direction(text: str) -> Literal["LONG", "SHORT"] | None:
    for pat in DIRECTION_PATTERNS:
        m = pat.search(text)
        if m:
            word = m.group(1).lower()
            return "LONG" if word in ("buy", "long", "bullish", "upside", "call") else "SHORT"
    return None


def _extract_entry(text: str) -> float | None:
    for pat in ENTRY_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _extract_sl(text: str) -> float | None:
    for pat in SL_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return None


def _extract_tp(text: str) -> list[float]:
    tps: list[float] = []
    for pat in TP_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                tps.append(float(m.group(1).replace(",", "")))
            except ValueError:
                continue
    return tps


def extract_volume(text: str) -> float | None:
    """Extract trade volume from signal text (optional)."""
    for pat in VOLUME_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(1).replace(",", "").strip()
            try:
                if raw.upper().endswith("K"):
                    return float(raw[:-1]) * 1000
                elif raw.upper().endswith("M"):
                    return float(raw[:-1]) * 1_000_000
                elif raw.upper().endswith("B"):
                    return float(raw[:-1]) * 1_000_000_000
                return float(raw)
            except ValueError:
                continue
    return None


def extract_leverage(text: str) -> int | None:
    """Extract suggested leverage from signal text (optional)."""
    for pat in LEVERAGE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


# ─── Management command detection ──────────────────────────────────────────
# Channel management messages like "sl to entry" / "tp1" / "full" are replies
# to a previous signal. They are NOT fresh entries — they are instructions to
# modify the open position.
_MANAGEMENT_PATTERNS = [
    re.compile(r"\bsl\s+(?:to|at|@)\s+entry\b", re.I),
    re.compile(r"\bsl\s*=\s*entry\b", re.I),
    re.compile(r"\bstop\s*(?:loss)?\s+(?:to|at)\s+entry\b", re.I),
    re.compile(r"\bbreakeven\b", re.I),
    re.compile(r"\bbe\b", re.I),  # "be" shorthand for "breakeven"
]

# Close-trigger patterns — channel posts "closed at entry", "cutting it here",
# or similar to signal the position should be closed.
_MGMT_CLOSE_RE = re.compile(r"(?:closed|close|cutting|cancel)\s+(?:at|it|the|my)?\s*(?:entry|here|\-?\d)", re.I)

# "tp1" / "tp2" / "tp3" — optional explicit price after a separator.
_MGMT_TP_RE = re.compile(r"\btp\s*(\d)\b\s*(?:[:@\-]\s*([\d,]+(?:\.\d+)?))?", re.I)
# "full" (standalone) — full market close of the position.
_MGMT_FULL_RE = re.compile(r"\bfull\b", re.I)

# TP1 / partial-exit patterns — channel posts "tp1 booked", "tp1 done",
# "partial taken" etc. to signal a partial exit at the first target.
_TP1_PARTIAL_PATTERNS = [
    re.compile(r"\btp1?\s*(?:booked|done|hit|filled|reached|taken)\b", re.I),
    re.compile(r"\bpartial\s*(?:close|profit|taken|done|booked)?\b", re.I),
]

# Management action sentinels.
_MGMT_SL_ENTRY = "SL_ENTRY"
_MGMT_TP = "TP"
_MGMT_FULL = "FULL"
_MGMT_TP1_PARTIAL = "TP1_PARTIAL"


def is_management_command(raw_text: str) -> bool:
    """Return True if the message looks like a position-management command.

    Covers sl-to-entry, close-trigger, tpN, full-close, and TP1-partial.
    These skip the LLM and are handled directly by the orchestrator's
    management path.
    """
    if not raw_text:
        return False
    if any(p.search(raw_text) for p in _MANAGEMENT_PATTERNS):
        return True
    if _MGMT_CLOSE_RE.search(raw_text):
        return True
    if _MGMT_FULL_RE.search(raw_text):
        return True
    if _MGMT_TP_RE.search(raw_text):
        return True
    if any(p.search(raw_text) for p in _TP1_PARTIAL_PATTERNS):
        return True
    return False


def _mgmt_signal(message_id, channel, raw_text, pair, *, action, tp_index=None,
                 tp_price=None, sl_price=None):
    """Helper to build a management TradeSignal with the right routing fields."""
    tp_prices = [tp_price] if tp_price is not None else []
    return TradeSignal(
        message_id=message_id, channel=channel, raw_text=raw_text,
        pair=pair, direction=None,
        sl_price=sl_price if sl_price is not None else _SL_TO_ENTRY if action == _MGMT_SL_ENTRY else None,
        tp_prices=tp_prices,
        mgmt_action=action,
        tp_index=tp_index,
    )


def parse_management_command(message_id: int, channel: str, raw_text: str,
                             reply_pair: str | None = None) -> TradeSignal | None:
    """Detect and classify a position-management command.

    Returns a TradeSignal carrying ``mgmt_action`` (SL_ENTRY / TP / FULL /
    TP1_PARTIAL) and any level/price info, or None if this is not a management
    command. The pair is resolved from the reply context first, then from the
    message's own text.

    The orchestrator's management path handles these directly (NOT the LLM):
      * SL_ENTRY     — move SL to the position's entry price (breakeven).
      * TP           — move/refresh take-profit level tpN (rest of position stays open).
      * FULL         — full market close of the position.
      * TP1_PARTIAL  — TP1 was hit: close 1/3 of position, move SL to entry,
                       adjust remaining TP for the rest.
    """
    text = raw_text or ""
    if not is_management_command(text):
        return None

    # Pair: prefer the replied-to message's pair, then try own text.
    pair = reply_pair or _resolve_pair(text)

    # 1) TP1 / partial-exit command — check BEFORE generic tpN so "tp1 booked"
    #    is routed to the partial-close handler, not the generic tp-modify path.
    for p in _TP1_PARTIAL_PATTERNS:
        if p.search(text):
            return _mgmt_signal(
                message_id, channel, raw_text, pair,
                action=_MGMT_TP1_PARTIAL,
                sl_price=-2.0,
            )

    # 2) Close-trigger commands (e.g. "closed at entry", "cutting it here")
    if _MGMT_CLOSE_RE.search(text):
        if not pair:
            return _mgmt_signal(message_id, channel, raw_text, None, action=_MGMT_FULL)
        return _mgmt_signal(message_id, channel, raw_text, pair, action=_MGMT_FULL)

    # 3) sl to entry (breakeven)
    if any(p.search(text) for p in _MANAGEMENT_PATTERNS):
        if not pair:
            return _mgmt_signal(message_id, channel, raw_text, None, action=_MGMT_SL_ENTRY)
        return _mgmt_signal(message_id, channel, raw_text, pair, action=_MGMT_SL_ENTRY)

    # 3) full close — decisive; takes priority over tpN if both appear.
    if _MGMT_FULL_RE.search(text):
        if not pair:
            return _mgmt_signal(message_id, channel, raw_text, None, action=_MGMT_FULL)
        return _mgmt_signal(message_id, channel, raw_text, pair, action=_MGMT_FULL)

    # 4) tpN — move/refresh that TP level.
    m = _MGMT_TP_RE.search(text)
    if m:
        tp_index = int(m.group(1)) - 1
        tp_price = None
        if m.group(2):
            try:
                tp_price = float(m.group(2).replace(",", ""))
            except ValueError:
                tp_price = None
        if not pair:
            return _mgmt_signal(message_id, channel, raw_text, None, action=_MGMT_TP,
                                tp_index=tp_index, tp_price=tp_price)
        return _mgmt_signal(message_id, channel, raw_text, pair, action=_MGMT_TP,
                            tp_index=tp_index, tp_price=tp_price)

    return None


# Management sentinel for sl-to-entry fallback.
_SL_TO_ENTRY = -1.0
