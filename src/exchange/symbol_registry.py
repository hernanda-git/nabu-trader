"""Dynamic Symbol Registry — resolves trading pairs from live exchange data.

Eliminates hardcoded coin symbol lists. On startup, fetches Binance USDⓈ-M
Futures exchangeInfo (public endpoint, no auth), builds an inverted index,
and resolves pairs from signal text via multi-strategy matching.

Architecture:
    Raw signal text → extract candidates (generic regex)
                    → SymbolRegistry.resolve() → (symbol_name, SymbolInfo)
                        → exact match | lowercase | base asset | 1000x prefix
                    → enriched TradeSignal with symbol metadata

Auto-handles:
    - 1000x contracts (BONK → 1000BONKUSDT, PEPE → 1000PEPEUSDT, etc.)
    - Typos / fuzzy matching (Levenshtein distance ≤ 2 as last resort)
    - New listings (auto-refresh every 15 min)
    - API downtime (falls back to seed data / last known cache)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.domain.models import SymbolInfo

log = logging.getLogger("exchange.symbol_registry")

# ── Module-level singleton ──────────────────────────────────────────────

_registry: "SymbolRegistry | None" = None


def get_registry() -> "SymbolRegistry | None":
    """Get the global SymbolRegistry instance (set once at startup)."""
    return _registry


def set_registry(registry: "SymbolRegistry | None") -> None:
    """Set the global SymbolRegistry instance."""
    global _registry
    _registry = registry


# ── Default seed data (built-in fallback) ──────────────────────────────
# Covers the most actively traded USDⓈ-M Futures pairs.
# Updated automatically when exchangeInfo is fetched successfully.

_DEFAULT_SEED_SYMBOLS: list[dict[str, Any]] = [
    # ── Top 50+ USDⓈ-M Futures pairs ─────────────────────────────────────
    # Major / Blue Chip
    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 5,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 5,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "BNBUSDT", "baseAsset": "BNB", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 5,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "SOLUSDT", "baseAsset": "SOL", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "100000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "XRPUSDT", "baseAsset": "XRP", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 1,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ADAUSDT", "baseAsset": "ADA", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 1,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "DOGEUSDT", "baseAsset": "DOGE", "quoteAsset": "USDT",
     "pricePrecision": 5, "quantityPrecision": 1,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.00001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "100000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "AVAXUSDT", "baseAsset": "AVAX", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "DOTUSDT", "baseAsset": "DOT", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "LINKUSDT", "baseAsset": "LINK", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ATOMUSDT", "baseAsset": "ATOM", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "UNIUSDT", "baseAsset": "UNI", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "NEARUSDT", "baseAsset": "NEAR", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "APTUSDT", "baseAsset": "APT", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ARBUSDT", "baseAsset": "ARB", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "OPUSDT", "baseAsset": "OP", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "SUIUSDT", "baseAsset": "SUI", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "INJUSDT", "baseAsset": "INJ", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "FETUSDT", "baseAsset": "FET", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "WIFUSDT", "baseAsset": "WIF", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "FILUSDT", "baseAsset": "FIL", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "AAVEUSDT", "baseAsset": "AAVE", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 4,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "MKRUSDT", "baseAsset": "MKR", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 4,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "CRVUSDT", "baseAsset": "CRV", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "SNXUSDT", "baseAsset": "SNX", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ENSUSDT", "baseAsset": "ENS", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "LTCUSDT", "baseAsset": "LTC", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 4,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "BCHUSDT", "baseAsset": "BCH", "quoteAsset": "USDT",
     "pricePrecision": 2, "quantityPrecision": 4,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ETCUSDT", "baseAsset": "ETC", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "SEIUSDT", "baseAsset": "SEI", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "TIAUSDT", "baseAsset": "TIA", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "RNDRUSDT", "baseAsset": "RNDR", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "JUPUSDT", "baseAsset": "JUP", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "PYTHUSDT", "baseAsset": "PYTH", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "ONDOUSDT", "baseAsset": "ONDO", "quoteAsset": "USDT",
     "pricePrecision": 4, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "10000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "OMNIUSDT", "baseAsset": "OMNI", "quoteAsset": "USDT",
     "pricePrecision": 3, "quantityPrecision": 3,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.001"},
                 {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01", "maxQty": "1000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    # ── 1000x contracts ────────────────────────────────────────────────
    {"symbol": "1000BONKUSDT", "baseAsset": "1000BONK", "quoteAsset": "USDT",
     "pricePrecision": 7, "quantityPrecision": 0,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE", "quoteAsset": "USDT",
     "pricePrecision": 7, "quantityPrecision": 0,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "1000SHIBUSDT", "baseAsset": "1000SHIB", "quoteAsset": "USDT",
     "pricePrecision": 7, "quantityPrecision": 0,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
    {"symbol": "1000FLOKIUSDT", "baseAsset": "1000FLOKI", "quoteAsset": "USDT",
     "pricePrecision": 7, "quantityPrecision": 0,
     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.0000001"},
                 {"filterType": "LOT_SIZE", "stepSize": "1", "minQty": "1", "maxQty": "10000000000"},
                 {"filterType": "MIN_NOTIONAL", "notional": "5"}],
     "contractType": "PERPETUAL", "onboardDate": "", "status": "TRADING"},
]


# ═════════════════════════════════════════════════════════════════════════
# SymbolRegistry
# ═════════════════════════════════════════════════════════════════════════


class SymbolRegistry:
    """Dynamic trading pair resolver with live exchangeInfo caching.

    Usage:
        registry = SymbolRegistry()
        await registry.initialize()

        symbol, info = registry.resolve("#BTC/USDT LONG")
        # → ("BTCUSDT", SymbolInfo(symbol="BTCUSDT", base_asset="BTC", ...))

        symbol, info = registry.resolve("BONK entry 0.0044")
        # → ("1000BONKUSDT", SymbolInfo(... is_1000x=True))
    """

    BINANCE_FUTURES_EXCHANGE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    _CANDIDATE_PATTERNS = [
        (re.compile(r'[#\$](\w{2,12})\b'), 1),                    # #BTC, $ETH, #1000BONK
        (re.compile(r'\b(\w{2,12})/USDT\b', re.I), 1),            # BTC/USDT, 1000BONK/USDT
        (re.compile(r'\b(\w{3,18}USDT)\b', re.I), 1),             # BTCUSDT, 1000BONKUSDT
        (re.compile(r'\b(\w{2,12})-USD\b', re.I), 1),             # BTC-USD
    ]

    def __init__(self, seed_path: str | None = None):
        self._symbols: dict[str, SymbolInfo] = {}           # symbol -> info (exact + lowercase)
        self._by_base: dict[str, list[str]] = {}            # base_asset -> [symbols]
        self._last_refresh: datetime | None = None
        self._refresh_interval = 15  # minutes
        self._refresh_task: asyncio.Task | None = None
        self._initialized = False
        self._seed_path = seed_path
        self._last_exchange_data: dict | None = None        # raw exchangeInfo for seed saving

    # ── Public API ──────────────────────────────────────────────────────

    async def initialize(self, force_refresh: bool = False) -> None:
        """Initialize the registry from exchangeInfo or fallback seed.

        Called once at startup. Non-blocking if exchangeInfo fails
        (falls back to built-in seed data).
        """
        if self._initialized and not force_refresh:
            return

        # Try live exchangeInfo first
        try:
            await self._fetch_and_build()
            self._initialized = True
            if self._symbols:
                log.info("SymbolRegistry: initialized with %d symbols from exchangeInfo",
                         self.symbol_count)
                return
        except Exception as e:
            log.warning("exchangeInfo fetch failed: %s", e)

        # Fallback: load seed from file or built-in defaults
        if self._seed_path and os.path.exists(self._seed_path):
            self._load_seed_file(self._seed_path)
            log.info("SymbolRegistry: loaded %d symbols from seed file %s",
                     self.symbol_count, self._seed_path)
        else:
            self._load_default_seed()
            log.info("SymbolRegistry: loaded %d symbols from built-in seed",
                     self.symbol_count)

        self._initialized = True

    def resolve(self, text: str) -> tuple[str | None, SymbolInfo | None]:
        """Extract and resolve a trading pair from signal text.

        Returns (trading_symbol, SymbolInfo) or (None, None) if no pair found.

        The matching is multi-strategy and ordered by precision:
          1. Exact match against cached symbols
          2. Lowercase match
          3. Base asset lookup (handles 'BTC' -> 'BTCUSDT')
          4. USDT suffix/prefix normalization
          5. 1000x prefix auto-detection (e.g. 'BONK' -> '1000BONKUSDT')
          6. Fuzzy match (Levenshtein distance ≤ 2 — last resort)
        """
        candidates = self._extract_candidates(text)
        if not candidates:
            return None, None

        for candidate in candidates:
            result = self._lookup(candidate)
            if result:
                return result

        # Last resort: try fuzzy matching on the first candidate
        if candidates:
            result = self._fuzzy_lookup(candidates[0])
            if result:
                return result

        return None, None

    def get_symbol_info(self, symbol: str) -> SymbolInfo | None:
        """Get cached SymbolInfo by exact trading pair name."""
        info = self._symbols.get(symbol)
        if info:
            return info
        info = self._symbols.get(symbol.lower())
        return info

    def get_all_symbols(self) -> list[SymbolInfo]:
        """Get all unique cached symbols."""
        seen: set[str] = set()
        result: list[SymbolInfo] = []
        for info in self._symbols.values():
            if info.symbol not in seen:
                seen.add(info.symbol)
                result.append(info)
        return sorted(result, key=lambda x: x.symbol)

    def start_background_refresh(self, interval_minutes: int = 15) -> None:
        """Start a background task to refresh symbols periodically."""
        self._refresh_interval = interval_minutes
        if self._refresh_task and not self._refresh_task.done():
            return  # already running
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        log.info("SymbolRegistry: background refresh every %d min started",
                 interval_minutes)

    async def stop(self) -> None:
        """Cancel the background refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
            log.info("SymbolRegistry: background refresh stopped")

    async def force_refresh(self) -> None:
        """Force an immediate refresh of all symbols from exchangeInfo."""
        try:
            await self._fetch_and_build()
            self._initialized = True
            log.info("SymbolRegistry: force-refreshed %d symbols", self.symbol_count)
        except Exception as e:
            log.warning("SymbolRegistry force-refresh failed: %s", e)
            raise

    @property
    def is_ready(self) -> bool:
        return self._initialized and len(self._symbols) > 0

    @property
    def symbol_count(self) -> int:
        return len(self.get_all_symbols())

    @property
    def last_refresh(self) -> datetime | None:
        return self._last_refresh

    # ── ExchangeInfo Fetch & Build ───────────────────────────────────────

    async def _fetch_and_build(self) -> None:
        """Fetch USDⓈ-M Futures exchangeInfo and rebuild the index."""
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self.BINANCE_FUTURES_EXCHANGE_INFO)
            resp.raise_for_status()
            data: dict = resp.json()

        self._rebuild_index(data)
        self._last_exchange_data = data
        self._last_refresh = datetime.now(timezone.utc)

        # Save as seed file for next startup
        if self._seed_path:
            try:
                self._save_seed_file(data)
            except Exception as e:
                log.debug("Failed to save seed file: %s", e)

    def _rebuild_index(self, exchange_info: dict) -> None:
        """Rebuild the internal index from exchangeInfo data."""
        self._symbols.clear()
        self._by_base.clear()

        for s in exchange_info.get("symbols", []):
            if s.get("status") != "TRADING":
                continue
            if s.get("quoteAsset") != "USDT":
                continue
            self._add_symbol(s)

    def _add_symbol(self, s: dict) -> None:
        """Parse a single exchangeInfo symbol entry and add to index."""
        name: str = s["symbol"]
        base: str = s["baseAsset"]
        filters_raw: list[dict] = s.get("filters", [])
        filters: dict[str, dict] = {f["filterType"]: f for f in filters_raw}

        lot = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional_filter = filters.get("MIN_NOTIONAL", {})
        # MIN_NOTIONAL can have 'notional' as a string or number
        raw_notional = min_notional_filter.get("notional", "5")
        try:
            min_notional = float(raw_notional)
        except (ValueError, TypeError):
            min_notional = 5.0

        # Detect 1000x contracts: base starts with "1000" followed by letters
        is_1000x = bool(re.match(r'^1000[A-Z]', base))

        try:
            tick_size = float(price_filter.get("tickSize", "0.001"))
        except (ValueError, TypeError):
            tick_size = 0.001
        try:
            step_size = float(lot.get("stepSize", "0.001"))
        except (ValueError, TypeError):
            step_size = 0.001
        try:
            min_qty = float(lot.get("minQty", "0.001"))
        except (ValueError, TypeError):
            min_qty = 0.001
        try:
            max_qty = float(lot.get("maxQty", "1000000"))
        except (ValueError, TypeError):
            max_qty = 1_000_000

        info = SymbolInfo(
            symbol=name,
            base_asset=base,
            quote_asset=s.get("quoteAsset", "USDT"),
            price_precision=s.get("pricePrecision", 8),
            quantity_precision=s.get("quantityPrecision", 8),
            min_notional=min_notional,
            tick_size=tick_size,
            step_size=step_size,
            min_qty=min_qty,
            max_qty=max_qty,
            contract_type=s.get("contractType", "PERPETUAL"),
            onboard_date=str(s.get("onboardDate", "")),
            is_1000x=is_1000x,
        )

        # Index by exact symbol (upper and lower)
        self._symbols[name] = info
        self._symbols[name.lower()] = info

        # Index by base asset (handles 'BTC' -> ['BTCUSDT'])
        # For 1000x, index both '1000BONK' and 'BONK' as base asset lookups
        self._by_base.setdefault(base, []).append(name)
        if is_1000x and base.startswith("1000"):
            clean_base = re.sub(r'^\d+', '', base)
            if clean_base and clean_base != base:
                self._by_base.setdefault(clean_base, []).append(name)

    # ── Candidate Extraction ─────────────────────────────────────────────

    def _extract_candidates(self, text: str) -> list[str]:
        """Extract potential pair names from signal text using generic patterns.

        Returns an ordered list of candidate strings (uppercase, deduplicated).
        """
        seen: set[str] = set()
        candidates: list[str] = []

        # Pattern 1-4: Structured pair formats (#BTC, BTC/USDT, BTCUSDT, BTC-USD)
        for pattern, group_idx in self._CANDIDATE_PATTERNS:
            for m in pattern.finditer(text):
                val = m.group(group_idx).upper().replace("#", "").replace("$", "")
                if val not in seen:
                    seen.add(val)
                    candidates.append(val)

        # Pattern 5: Bare 2-10 char uppercase words near trading keywords
        # e.g. "SOL LONG", "ADA entry 0.50"
        # Find direction words first, then look at nearby words
        direction_words = {'LONG', 'SHORT', 'BUY', 'SELL', 'ENTRY', 'ENTER',
                           'STOP', 'TARGET', 'TP', 'ENTRY'}
        dir_positions = []
        for m in re.finditer(r'\b(LONG|SHORT|BUY|SELL|ENTRY|ENTER|SL|STOP|TP|TARGET)\b', text, re.I):
            dir_positions.append(m.start())

        if dir_positions:
            # Find all uppercase words in the text
            for m in re.finditer(r'\b([A-Z]{2,10})\b', text):
                val = m.group(1).upper()
                if val in direction_words or val in ('USDT', 'USD', 'THE', 'FOR', 'AND',
                       'WITH', 'FROM', 'THIS', 'THAT', 'HERE', 'WILL', 'CAN', 'ARE', 'NOT'):
                    continue
                # Check if this word is near any direction word (within 30 chars)
                word_pos = m.start()
                for dp in dir_positions:
                    if abs(word_pos - dp) <= 30:
                        if val not in seen:
                            seen.add(val)
                            candidates.append(val)
                        break

        # Pattern 6: Any uppercase 3-6 char word (last resort, low confidence)
        # Only if no candidates found yet
        if not candidates:
            for m in re.finditer(r'\b([A-Z]{3,6})\b', text):
                val = m.group(1).upper()
                if val not in seen and val not in (
                    'LONG', 'SHORT', 'BUY', 'SELL', 'ENTRY', 'THE', 'FOR',
                    'AND', 'WITH', 'FROM', 'THIS', 'THAT', 'USDT', 'USD',
                    'STOP', 'TARGET', 'PRICE', 'AREA', 'ZONE', 'RANGE',
                    'MARKET', 'ORDER', 'LIMIT', 'LEVEL', 'ABOVE', 'BELOW',
                ):
                    seen.add(val)
                    candidates.append(val)

        return candidates

    # ── Multi-Strategy Lookup ─────────────────────────────────────────────

    def _lookup(self, candidate: str) -> tuple[str, SymbolInfo] | None:
        """Multi-strategy lookup for a candidate string.

        Returns (cached_symbol_name, SymbolInfo) or None.
        """
        # Strategy 1: Direct exact match
        info = self._symbols.get(candidate)
        if info:
            return info.symbol, info

        # Strategy 2: Lowercase match
        info = self._symbols.get(candidate.lower())
        if info:
            return info.symbol, info

        # Strategy 3: Base asset lookup (bare coin like 'BTC')
        bases = self._by_base.get(candidate)
        if bases:
            info = self._symbols.get(bases[0])
            if info:
                return info.symbol, info

        # Strategy 4: Strip USDT suffix and look up base
        if candidate.endswith("USDT"):
            base = candidate[:-4]
            bases = self._by_base.get(base)
            if bases:
                info = self._symbols.get(bases[0])
                if info:
                    return info.symbol, info

        # Strategy 5: Add USDT suffix and look up
        if not candidate.endswith("USDT") and not candidate.endswith("USD"):
            test = candidate + "USDT"
            info = self._symbols.get(test)
            if info:
                return info.symbol, info
            info = self._symbols.get(test.lower())
            if info:
                return info.symbol, info

        # Strategy 6: 1000x prefix check
        # If candidate is 'BONK', check if any 1000-prefixed base assets match
        for base_key, symbols in self._by_base.items():
            if base_key == candidate and any(
                self._symbols[s].is_1000x for s in symbols
            ):
                info = self._symbols.get(symbols[0])
                if info:
                    return info.symbol, info

        return None

    def _fuzzy_lookup(self, candidate: str, max_distance: int = 2) -> tuple[str, SymbolInfo] | None:
        """Fuzzy match a candidate against all known symbols.

        Only used as a last resort — expensive.
        """
        if len(candidate) < 2:
            return None

        cand_upper = candidate.upper()
        best_symbol: str | None = None
        best_info: SymbolInfo | None = None
        best_distance = max_distance + 1

        for info in self.get_all_symbols():
            # Compare against both full symbol and clean base name
            for target in [info.symbol, info.clean_base()]:
                dist = self._levenshtein(cand_upper, target.upper())
                if dist < best_distance:
                    best_distance = dist
                    best_symbol = info.symbol
                    best_info = info
                    if dist == 0:
                        return best_symbol, best_info

        if best_symbol and best_info:
            log.debug("Fuzzy match: '%s' -> '%s' (dist=%d)",
                      candidate, best_symbol, best_distance)
            return best_symbol, best_info
        return None

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        """Compute Levenshtein distance between two strings."""
        if len(a) < len(b):
            a, b = b, a
        if not b:
            return len(a)

        prev_row = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr_row = [i + 1]
            for j, cb in enumerate(b):
                cost = 0 if ca == cb else 1
                curr_row.append(min(
                    curr_row[j] + 1,           # deletion
                    prev_row[j + 1] + 1,        # insertion
                    prev_row[j] + cost,          # substitution
                ))
            prev_row = curr_row
        return prev_row[-1]

    # ── Background Refresh ───────────────────────────────────────────────

    async def _refresh_loop(self) -> None:
        """Background loop that periodically refreshes symbols."""
        while True:
            try:
                await asyncio.sleep(self._refresh_interval * 60)
                await self._fetch_and_build()
                log.debug("SymbolRegistry: background refresh completed (%d symbols)",
                          self.symbol_count)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("SymbolRegistry background refresh failed: %s", e)

    # ── Seed Persistence ─────────────────────────────────────────────────

    def _load_seed_file(self, path: str) -> None:
        """Load symbols from a JSON seed file."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
            symbols = data if isinstance(data, list) else data.get("symbols", data)
            for s in symbols:
                if s.get("status", "TRADING") == "TRADING" and \
                   s.get("quoteAsset", "USDT") == "USDT":
                    self._add_symbol(s)
        except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
            log.warning("Failed to load seed file %s: %s — using built-in defaults",
                        path, e)
            self._load_default_seed()

    def _load_default_seed(self) -> None:
        """Load built-in default seed symbols."""
        self._symbols.clear()
        self._by_base.clear()
        for s in _DEFAULT_SEED_SYMBOLS:
            self._add_symbol(s)

    def _save_seed_file(self, data: dict) -> None:
        """Save the raw exchangeInfo response as a seed file."""
        if not self._seed_path:
            return
        try:
            os.makedirs(os.path.dirname(self._seed_path) or ".", exist_ok=True)
            with open(self._seed_path, "w") as f:
                json.dump(data, f, indent=2)
            log.debug("Seed file saved to %s", self._seed_path)
        except Exception as e:
            log.debug("Could not save seed file: %s", e)

    def to_dict(self) -> list[dict]:
        """Serialize cached symbols for API output."""
        return [
            {
                "symbol": info.symbol,
                "base_asset": info.base_asset,
                "quote_asset": info.quote_asset,
                "display": info.display_symbol(),
                "price_precision": info.price_precision,
                "quantity_precision": info.quantity_precision,
                "min_notional": info.min_notional,
                "is_1000x": info.is_1000x,
                "contract_type": info.contract_type,
            }
            for info in self.get_all_symbols()
        ]

    # ── Class Methods ────────────────────────────────────────────────────

    @classmethod
    def create_and_init(cls, seed_path: str | None = None) -> "SymbolRegistry":
        """Synchronous factory for startup (uses blocking HTTP)."""
        import httpx
        registry = cls(seed_path=seed_path)

        try:
            resp = httpx.get(cls.BINANCE_FUTURES_EXCHANGE_INFO, timeout=10.0)
            resp.raise_for_status()
            registry._rebuild_index(resp.json())
            registry._last_exchange_data = resp.json()
            registry._last_refresh = datetime.now(timezone.utc)

            if seed_path:
                try:
                    registry._save_seed_file(resp.json())
                except Exception:
                    pass

            log.info("SymbolRegistry: initialized with %d symbols (synchronous)",
                     registry.symbol_count)
        except Exception as e:
            log.warning("exchangeInfo fetch failed (sync): %s — using seed", e)
            if seed_path and os.path.exists(seed_path):
                registry._load_seed_file(seed_path)
            else:
                registry._load_default_seed()

        registry._initialized = True
        return registry
