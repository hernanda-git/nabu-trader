"""Regex pre-parse — fast signal extraction from raw Telegram messages.

Runs in ~1ms. Extracts pair, direction, entry, SL, TP before the LLM call,
reducing hallucination rate and token usage.
"""

from __future__ import annotations

import re
from typing import Any

from typing import Literal

from src.domain.models import TradeSignal

PAIR_PATTERNS = [
    re.compile(r"\b(BTC|ETH|BNB|SOL|XRP|ADA|DOGE|AVAX|DOT|LINK|ATOM|UNI|SHIB|PEPE|FIL|LTC|BCH|ETC|NEAR|APT|ARB|OP|SUI|SEI|TIA|INJ|FET|RENDER|WIF|BONK|JUP|PYTH|ONDO|OMNI|ENS|AAVE|MKR|CRV|SNX)\s*/?\s*(USDT|USD|BUSD|USDC|BTC|ETH)\b", re.I),
    re.compile(r"\b([A-Z]{3,6})\s*(?:USDT|USD|BUSD)\b", re.I),
    re.compile(r"[#$](\w{2,20})\b", re.I),
    re.compile(r"\b([A-Z]{2,10})\b"),
]

DIRECTION_PATTERNS = [
    re.compile(r"\b(buy|long|bullish|upside|Call)\b", re.I),
    re.compile(r"\b(sell|short|bearish|downside|Put)\b", re.I),
]

ENTRY_PATTERNS = [
    re.compile(r"(?:entry|enter|open|buy\s*at|sell\s*at|limit|@\s*)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:zone|range|area)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
]

SL_PATTERNS = [
    re.compile(r"(?:sl|stop\s*(?:loss)?|stoploss)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:invalidat|invalid)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
]

TP_PATTERNS = [
    re.compile(r"(?:tp\s*1?|take\s*profit\s*1?|target\s*1?|tgt\s*1?)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:tp\s*2|take\s*profit\s*2|target\s*2|tgt\s*2)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:tp\s*3|take\s*profit\s*3|target\s*3|tgt\s*3)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
]


def parse_signal(message_id: int, channel: str, raw_text: str,
                 has_media: bool = False) -> TradeSignal:
    """Fast regex pre-parse of a Telegram message into a TradeSignal.

    Returns a partially-populated TradeSignal. Missing fields (None) are
    expected — the LLM agent will enrich them.
    """
    text = raw_text.strip()
    if not text:
        return TradeSignal(message_id=message_id, channel=channel, raw_text=raw_text)

    pair = _extract_pair(text)
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


def _extract_pair(text: str) -> str | None:
    for pat in PAIR_PATTERNS:
        m = pat.search(text)
        if m:
            raw = m.group(0).strip().upper()
            # Clean up # / $ prefix for display
            if raw.startswith("#") or raw.startswith("$"):
                return raw  # keep as-is, LLM can normalize
            return raw
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
