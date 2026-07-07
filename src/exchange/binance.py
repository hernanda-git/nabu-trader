"""Binance exchange adapter — REST API for both Spot and USDⓈ-M Futures.

Supports:
  - Spot trading (api.binance.com)
  - USDⓈ-M Futures (fapi.binance.com)
  - Testnet for both (testnet.binance.vision / testnet futures)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from math import log10
from typing import Any
from urllib.parse import urlencode

import httpx

from src.exchange.base import BalanceInfo, Exchange, OrderInfo, PositionInfo

log = logging.getLogger("exchange.binance")


class BinanceErrorCategory(Enum):
    AUTH = "auth"        # 401/403 — bad key, no permission
    RATE_LIMIT = "rate"  # 429 — backoff needed
    SERVER = "server"    # 5xx — Binance internal error
    VALIDATION = "validation"  # 400 — bad request params
    NETWORK = "network"  # timeout / connection
    UNKNOWN = "unknown"


# ─── Binance error code → user-friendly message ─────────────────────────────
_ERROR_MESSAGES: dict[int, str] = {
    -1100: "Illegal characters in request — possible symbol or quantity format issue",
    -1013: "Order value too small — increase quantity or use higher leverage",
    -1102: "Missing required parameter — contact developer",
    -1111: "Price/quantity precision too high for this pair — rounding issue",
    -1121: "Invalid symbol — this pair may not exist on Binance Futures",
    -2019: "Insufficient margin — not enough free USDT for this position size",
    -2020: "Order would immediately fill — try a different price or order type",
    -4003: "Quantity must be greater than zero",
    -4004: "Quantity too large for this pair",
    -4164: "Order notional too small — minimum $5 on Binance Futures (increase quantity or leverage)",
    -4120: "Order type not supported for this contract — SL/TP handled by position manager",
    -4136: "Invalid order type/parameters for this contract",
    -4188: "Reduce-only order would increase position — check order side",
}


def _parse_binance_error(response_text: str) -> tuple[int, str]:
    """Extract Binance error code and message from JSON response body.

    Returns (code, message). Falls back to raw text if not JSON.
    """
    try:
        data = json.loads(response_text)
        code = int(data.get("code", 0))
        msg = str(data.get("msg", ""))
        return code, msg
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0, response_text[:200]


def _format_order_error(symbol: str, side: str, order_type: str,
                        quantity: float, price: float | None,
                        leverage: int, status_code: int,
                        response_text: str) -> str:
    """Build a user-friendly, traceable error message for a failed order.

    Includes: what was attempted, why it failed, and what to do about it.
    """
    code, binance_msg = _parse_binance_error(response_text)
    friendly = _ERROR_MESSAGES.get(code, binance_msg or f"HTTP {status_code}")

    # Compute notional for context
    ref_price = price or 0
    notional = quantity * ref_price if ref_price > 0 else 0

    parts = [
        f"❌ **Order failed — {symbol}**",
        f"   ├ Side: `{side}` | Type: `{order_type}`",
        f"   ├ Qty: `{quantity}`",
    ]
    if ref_price > 0:
        parts.append(f"   ├ Price: `{ref_price}` | Notional: `${notional:.2f}`")
    if leverage > 1:
        parts.append(f"   ├ Leverage: `{leverage}x`")
    parts.append(f"   ├ Error code: `{code}`" if code else f"   ├ HTTP: `{status_code}`")
    parts.append(f"   └ {friendly}")

    return "\n".join(parts)


def _categorize_error(status_code: int | None, error_text: str,
                      exception: Exception | None) -> BinanceErrorCategory:
    if status_code == 401 or status_code == 403:
        return BinanceErrorCategory.AUTH
    if status_code == 429:
        return BinanceErrorCategory.RATE_LIMIT
    if status_code and 500 <= status_code < 600:
        return BinanceErrorCategory.SERVER
    if status_code == 400:
        return BinanceErrorCategory.VALIDATION
    if isinstance(exception, (httpx.TimeoutException, httpx.ConnectError,
                               httpx.RemoteProtocolError, httpx.TransportError)):
        return BinanceErrorCategory.NETWORK
    return BinanceErrorCategory.UNKNOWN

# ─── Base URLs ────────────────────────────────────────────────────────────────
SPOT_URLS = {
    "mainnet": "https://api.binance.com",
    "testnet": "https://testnet.binance.vision",
}
FUTURES_URLS = {
    "mainnet": "https://fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
}

# ─── Order type mapping (spot -> futures equivalent) ─────────────────────────
FUTURES_ORDER_TYPES = {
    "MARKET": "MARKET",
    "LIMIT": "LIMIT",
    "STOP_LOSS": "STOP_MARKET",       # spot STOP_LOSS → futures STOP_MARKET
    "STOP_LOSS_LIMIT": "STOP",        # less common
    "TAKE_PROFIT": "TAKE_PROFIT_MARKET",
    "TAKE_PROFIT_LIMIT": "TAKE_PROFIT",
}

# ─── Symbol mapping: user-facing pair → actual Binance Futures symbol + price multiplier ──
# Some low-price tokens are listed as 1000x contracts on Binance Futures
# (e.g., 1 contract = 1000 BONK tokens)
# CRITICAL: The price on 1000x contracts is QUOTED in the base asset price (e.g. BONK = 0.0044),
# NOT multiplied by 1000. The lot multiplier handles the conversion internally.
# QUANTITY is also in base asset tokens (e.g. BONK), not divided by 1000.
# The "1000" in the symbol name only affects the CONTRACT MULTIPLIER for PnL calc.
# (symbol, price_multiplier, qty_divisor)
SYMBOL_MAP: dict[str, tuple[str, float, float]] = {
    # (actual_symbol, price_multiplier, qty_divisor)
    # Both price and quantity remain in base asset (BONK) terms
    "BONKUSDT":   ("1000BONKUSDT", 1.0, 1.0),
    "PEPEUSDT":   ("1000PEPEUSDT", 1.0, 1.0),
    "SHIBUSDT":   ("1000SHIBUSDT", 1.0, 1.0),
    "FLOKIUSDT":  ("1000FLOKIUSDT", 1.0, 1.0),
    "1000BONK":   ("1000BONKUSDT", 1.0, 1.0),
    "1000PEPE":   ("1000PEPEUSDT", 1.0, 1.0),
    "1000SHIB":   ("1000SHIBUSDT", 1.0, 1.0),
    "1000FLOKI":  ("1000FLOKIUSDT", 1.0, 1.0),
}

def _resolve_futures_symbol(raw_symbol: str) -> tuple[str, float, float]:
    """Resolve a user-facing symbol to the actual Binance Futures symbol + multipliers.

    Uses the dynamic SymbolRegistry first (populated from live exchangeInfo),
    falling back to the static SYMBOL_MAP for known 1000x contracts.

    1000x contracts (e.g. 1000BONKUSDT, 1000PEPEUSDT):
    - The "1000" prefix is a CONTRACT MULTIPLIER for PnL calculation only.
    - PRICE is quoted in base asset terms (e.g. BONK = 0.0044), NOT multiplied by 1000.
    - QUANTITY is also in base asset tokens (e.g. BONK), NOT divided by 1000.
    - STOP/STOP_MARKET orders are BLOCKED by Binance for 1000x contracts (error -4120).
    - LIMIT orders (TP) work fine on 1000x contracts.

    Args:
        raw_symbol: User-facing symbol (e.g. 'BONKUSDT', 'PEPEUSDT', '1000BONKUSDT')

    Returns:
        (resolved_symbol, price_multiplier, qty_divisor)
        - price_multiplier: always 1.0 (price stays in base asset terms)
        - qty_divisor: always 1.0 (quantity stays in base asset terms)
    """
    s = raw_symbol.upper().strip()

    # Try dynamic SymbolRegistry first (live exchangeInfo data)
    try:
        from src.exchange.symbol_registry import get_registry
        registry = get_registry()
        if registry and registry.is_ready:
            info = registry.get_symbol_info(s)
            if info:
                return info.symbol, 1.0, 1.0
            # Try resolving via the registry's resolve text-based method
            resolved_symbol, resolved_info = registry.resolve(s)
            if resolved_symbol and resolved_info:
                return resolved_symbol, 1.0, 1.0
    except Exception:
        pass

    # Fall back to static SYMBOL_MAP for known 1000x contracts
    if s in SYMBOL_MAP:
        mapped, pm, qd = SYMBOL_MAP[s]
        return mapped, pm, qd
    # Try without 'USDT' suffix for coin-only names
    base = s.replace("USDT", "").replace("USD", "")
    if base in SYMBOL_MAP:
        mapped, pm, qd = SYMBOL_MAP[base]
        return mapped, pm, qd
    # Check if already 1000x prefixed
    if s.startswith("1000"):
        return s, 1.0, 1.0
    # Default: use the symbol as-is
    return s, 1.0, 1.0


class BinanceExchange(Exchange):
    """Binance API adapter for Spot or USDⓈ-M Futures.

    Args:
        api_key: Binance API key.
        api_secret: Binance API secret.
        testnet: If True, use testnet endpoints.
        futures: If True, use Futures API (fapi) instead of Spot API.
        leverage: Futures leverage (default 1 = no leverage). Ignored for Spot.
        recv_window: Binance recvWindow param.
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 futures: bool = False, leverage: int = 1,
                 recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.futures = futures
        self.leverage = max(1, leverage)  # fallback default
        self.recv_window = min(int(recv_window), 60000)

        urls = FUTURES_URLS if futures else SPOT_URLS
        base_url = urls["testnet"] if testnet else urls["mainnet"]
        self._base = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=10)
        # Cache symbol rules: stepSize/minQty/maxQty/valid
        self._filters: dict[str, dict] = {}
        self._invalid_symbols: dict[str, datetime] = {}

        # Leverage is set per-symbol dynamically before each trade.
        # No startup leverage call needed.

    @property
    def name(self) -> str:
        prefix = "binance"
        if self.testnet:
            prefix += "_testnet"
        if self.futures:
            prefix += "_futures"
        return prefix

    # ─── Auth ──────────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        """Create HMAC-SHA256 signature for the params."""
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self.api_key}

    async def _signed_request(self, method: str, path: str,
                        params: dict | None = None) -> dict:
        """Send a signed request to Binance API."""
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        params["signature"] = self._sign(params)

        resp = await self._client.request(method, path, params=params,
                                    headers=self._headers())
        if resp.status_code != 200:
            category = _categorize_error(resp.status_code, resp.text, None)
            log.error("Binance API error %s [%s]: %s", resp.status_code, category.value, resp.text)
            if category == BinanceErrorCategory.AUTH:
                raise httpx.HTTPStatusError(
                    f"Binance auth failed ({resp.status_code}): {resp.text}",
                    request=resp.request, response=resp,
                )
            body = ""
            try:
                body = resp.text
            except Exception:
                pass
            resp.raise_for_status()
        return resp.json()

    async def _public_request(self, method: str, path: str,
                        params: dict | None = None) -> dict:
        """Send a public request (no auth required)."""
        resp = await self._client.request(method, path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _order_path(self) -> str:
        return "/fapi/v1/order" if self.futures else "/api/v3/order"

    def _map_type(self, order_type: str) -> str:
        """Map spot order type to futures equivalent if needed."""
        if self.futures:
            return FUTURES_ORDER_TYPES.get(order_type.upper(), order_type.upper())
        return order_type.upper()

    async def set_symbol_leverage(self, symbol: str, leverage: int):
        """Set leverage for a specific symbol before trading."""
        if not self.futures:
            return
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        leverage = max(1, min(leverage, 125))
        try:
            params = {
                "symbol": resolve_sym,
                "leverage": leverage,
            }
            result = await self._signed_request("POST", "/fapi/v1/leverage", params)
            self._last_leverage = leverage
            log.info("Leverage set: %s -> %s %dx", symbol, resolve_sym, leverage)
        except Exception as e:
            log.warning("Failed to set leverage for %s: %s", symbol, e)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        """Set margin type (ISOLATED or CROSSED) for futures position."""
        if not self.futures:
            return
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            params = {
                "symbol": resolve_sym,
                "marginType": margin_type.upper(),
            }
            await self._signed_request("POST", "/fapi/v1/marginType", params)
            log.info("Margin type: %s -> %s %s", symbol, resolve_sym, margin_type.upper())
        except Exception as e:
            # Error -4008 means already set, ignore
            log.info("Margin type %s for %s (may already be set)", margin_type, symbol)

    # ─── Balance ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> BalanceInfo:
        """Get account balance.

        For futures: returns available wallet balance in USDT.
        For spot: returns total USDT + asset values.
        """
        if self.futures:
            return await self._get_futures_balance()
        return await self._get_spot_balance()

    async def _get_spot_balance(self) -> BalanceInfo:
        """Get spot account balance."""
        data = await self._signed_request("GET", "/api/v3/account")
        assets = {}
        free_usdt = 0.0
        total_usdt = 0.0

        for bal in data.get("balances", []):
            asset = bal["asset"]
            free = float(bal["free"])
            locked = float(bal["locked"])
            if free > 0 or locked > 0:
                assets[asset] = {"free": free, "locked": locked}
                if asset == "USDT":
                    free_usdt = free
                    total_usdt = free + locked
                else:
                    try:
                        ticker_path = "/api/v3/ticker/price"
                        ticker = await self._public_request(
                            "GET", ticker_path,
                            {"symbol": f"{asset}USDT"},
                        )
                        price = float(ticker.get("price", 0))
                        total_usdt += (free + locked) * price
                    except Exception:
                        pass

        return BalanceInfo(
            total_usdt=round(total_usdt, 2),
            free_usdt=round(free_usdt, 2),
            assets=assets,
        )
    async def _get_futures_balance(self) -> BalanceInfo:
        """Get futures account balance.

        Returns available balance in USDT (wallet balance).
        """
        data = await self._signed_request("GET", "/fapi/v2/account")

        assets = {}
        free_usdt = 0.0
        total_usdt = 0.0

        for bal in data.get("assets", []):
            asset = bal["asset"]
            wallet = float(bal.get("walletBalance", 0))
            cross = float(bal.get("crossWalletBalance", 0))
            margin = float(bal.get("marginBalance", 0))
            # Unrealized PnL
            upnl = float(bal.get("unrealizedProfit", 0))
            if wallet > 0 or margin > 0 or asset == "USDT":
                assets[asset] = {
                    "wallet": wallet,
                    "cross": cross,
                    "margin": margin,
                    "unrealized_pnl": upnl,
                }
                if asset == "USDT":
                    free_usdt = cross   # available for trading
                    total_usdt = margin  # total equity including unrealized PnL

        return BalanceInfo(
            total_usdt=round(total_usdt, 2),
            free_usdt=round(free_usdt, 2),
            assets=assets,
        )

    # ─── Symbol filters/cache ──────────────────────────────────────────────────

    async def _load_futures_filters(self, symbol: str) -> dict | None:
        """Get symbol trading rules from exchange info, with caching."""
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        now = datetime.now(timezone.utc)
        # Reuse if recent
        cached = self._filters.get(resolve_sym)
        if cached:
            if (now - cached["_updated"]).total_seconds() < 600:
                return cached
            if not cached.get("valid", True):
                return None
        # Reject recently known invalid symbols
        rejected = self._invalid_symbols.get(symbol)
        if rejected:
            if (now - rejected).total_seconds() < 3600:
                return None
        # Fetch
        try:
            data = await self._public_request("GET", "/fapi/v1/exchangeInfo", {"symbol": resolve_sym})
            rules = data.get("symbols", [])
            if not rules:
                self._invalid_symbols[resolve_sym] = now
                log.info("Binance futures symbol not found: %s (from %s)", resolve_sym, symbol)
                return None
            s = rules[0]
            out = {"_updated": now, "valid": True}
            for f in s.get("filters", []):
                ft = f.get("filterType")
                if ft == "LOT_SIZE":
                    out["minQty"] = float(f.get("minQty", 0))
                    out["maxQty"] = float(f.get("maxQty", 0))
                    out["stepSize"] = float(f.get("stepSize", 1))
                elif ft == "MARKET_LOT_SIZE":
                    out["mktMinQty"] = float(f.get("minQty", 0))
                    out["mktMaxQty"] = float(f.get("maxQty", 0))
                    out["mktStepSize"] = float(f.get("stepSize", 1))
            status = s.get("status")
            out["status"] = status
            if status != "TRADING":
                out["valid"] = False
                self._invalid_symbols[resolve_sym] = now
                log.info("Binance symbol not trading: %s (%s)", resolve_sym, status)
                return None
            self._filters[resolve_sym] = out
            return out
        except Exception as e:
            log.debug("exchangeInfo lookup failed for %s: %s", symbol, e)
            return None

    async def _round_quantity(self, symbol: str, quantity: float) -> tuple[float, dict | None]:
        """Round quantity to exchange precision for symbol.

        Lazily loads filters on cache miss (async) for reliability.
        Falls back to integer rounding for low-price coins if filters unavailable.

        Returns (rounded_quantity, rules_or_None).
        """
        if quantity <= 0:
            return quantity, None

        # Lazy-load filters if not cached
        rules = self._filters.get(symbol)
        if not rules:
            filters = await self._load_futures_filters(symbol)
            if filters:
                rules = filters
            else:
                # Fallback: integer rounding for low-price coins (< 1 USDT per token)
                if quantity > 0 and quantity < 10000000:
                    qty = round(quantity)
                    prec = max(0, -int(round(log10(1))) if 1 < 1 else 0)
                    qty = float(f"{qty:.{prec}f}")
                    return qty, None
                return round(quantity), None

        step = rules.get("stepSize") or rules.get("mktStepSize") or 1.0
        rounded = round(quantity / step) * step
        if rounded <= 0 and step > 0:
            rounded = step
        if "minQty" in rules:
            rounded = max(rounded, rules["minQty"])
        if "mktMinQty" in rules:
            rounded = max(rounded, rules["mktMinQty"])
        prec = max(0, -int(round(log10(step))) if step < 1 else 0)
        rounded = float(f"{rounded:.{prec}f}")
        return rounded, rules

    async def _preflight_symbol(self, symbol: str) -> bool:
        """Ensure a futures symbol can be traded; loads filters if needed."""
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        if resolve_sym in self._filters or resolve_sym in self._invalid_symbols:
            return bool(self._filters.get(resolve_sym, {}).get("valid") is True)
        rules = await self._load_futures_filters(resolve_sym)
        return bool(rules)

    async def get_positions(self) -> list[PositionInfo]:
        """Get all open futures positions from the exchange."""
        if not self.futures:
            return []
        try:
            data = await self._signed_request("GET", "/fapi/v2/account")
            positions: list[PositionInfo] = []
            for pos in data.get("positions", []):
                size = float(pos.get("positionAmt", 0))
                if size == 0:
                    continue
                direction = "LONG" if size > 0 else "SHORT"
                positions.append(PositionInfo(
                    symbol=pos.get("symbol", ""),
                    direction=direction,
                    size=abs(size),
                    entry_price=float(pos.get("entryPrice", 0)),
                    mark_price=float(pos.get("markPrice", 0)),
                    liquidation_price=float(pos.get("liquidationPrice", 0)),
                    unrealized_pnl=float(pos.get("unrealizedProfit", 0)),
                    margin=float(pos.get("isolatedWallet", 0)),
                    leverage=int(float(pos.get("leverage", 1))),
                    notional=abs(float(pos.get("notional", 0))),
                ))
            return positions
        except Exception as e:
            log.error("Failed to fetch positions: %s", e)
            return []

    # ─── Price / Candles ──────────────────────────────────────────────────

    async def get_klines(self, symbol: str, interval: str = "4h",
                         limit: int = 100) -> list[list]:
        """Get full OHLCV klines from Binance Futures.

        Each candle: [open_time, open, high, low, close, volume, close_time,
                      quote_vol, trades, taker_buy_vol, taker_buy_quote_vol, ignore].

        Returns the raw Binance array (excluding the still-forming last candle).
        """
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            params = {"symbol": resolve_sym, "interval": interval, "limit": limit + 1}
            resp = await self._public_request(
                "GET",
                "/fapi/v1/klines" if self.futures else "/api/v3/klines",
                params,
            )
            # Drop the last candle (still forming) — only closed candles
            data = resp if isinstance(resp, list) else []
            return data[:-1] if len(data) > 1 else data
        except Exception as e:
            log.warning("Failed to fetch klines for %s %s: %s", symbol, interval, e)
            return []

    async def get_klines_close(self, symbol: str, interval: str = "4h") -> float | None:
        """Get the latest closed candle close price from Binance Futures klines.

        Args:
            symbol: Trading pair (e.g. 'BONKUSDT')
            interval: Candle interval (1m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, etc.)

        Returns:
            Close price of the most recently closed candle, or None on error.
        """
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            params = {"symbol": resolve_sym, "interval": interval, "limit": 2}
            resp = await self._public_request("GET", "/fapi/v1/klines" if self.futures else "/api/v3/klines", params)
            # Last item is the forming candle; second-to-last is the last closed one
            if len(resp) >= 2:
                close_price = float(resp[-2][4])  # index 4 = close
                return close_price
            if len(resp) == 1:
                return float(resp[0][4])
            return None
        except httpx.HTTPStatusError as e:
            # 400 Bad Request — symbol/interval unavailable on this exchange tier
            if e.response.status_code == 400:
                log.debug("Klines unavailable for %s %s (HTTP 400)", symbol, interval)
            else:
                log.warning("Failed to fetch klines for %s %s: %s", symbol, interval, e)
            return None
        except Exception as e:
            log.warning("Failed to fetch klines for %s %s: %s", symbol, interval, e)
            return None

    async def get_mark_price(self, symbol: str) -> float | None:
        """Get the current mark price from Binance Futures 24hr ticker.

        Faster than klines for real-time price checks (e.g. SL monitoring).
        Uses /fapi/v1/ticker/24hr (or /api/v3/ticker/24hr for spot).

        Args:
            symbol: Trading pair (e.g. 'BONKUSDT')

        Returns:
            Current mark/close price, or None on error.
        """
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            if self.futures:
                resp = await self._public_request("GET", "/fapi/v1/ticker/24hr", {"symbol": resolve_sym})
            else:
                resp = await self._public_request("GET", "/api/v3/ticker/24hr", {"symbol": resolve_sym})
            return float(resp.get("lastPrice", 0))
        except Exception as e:
            log.warning("Failed to fetch mark price for %s: %s", symbol, e)
            return None

    # ─── Orders ────────────────────────────────────────────────────────────────

    async def _place_order(self, symbol: str, side: str, order_type: str,
                     quantity: float, price: float | None = None,
                     stop_price: float | None = None) -> OrderInfo:
        """Place an order on Binance (Spot or Futures).

        For futures:
          - STOP_LOSS → STOP_MARKET (triggers market order)
          - Limit orders work the same
          - 1000x symbols (e.g. BONKUSDT → 1000BONKUSDT) are auto-resolved

        1000x contract notes:
        - PRICE is in base asset terms (e.g. BONK = 0.0044), NOT multiplied by 1000
        - QUANTITY is in base asset tokens (e.g. BONK), NOT divided by 1000
        - The "1000" prefix is the CONTRACT MULTIPLIER for PnL calculation only
        - No price/qty scaling is needed — multipliers are both 1.0

        NOTE: Call set_symbol_leverage() BEFORE this method for futures.
        """
        resolve_sym, price_mult, qty_div = _resolve_futures_symbol(symbol)
        # 1000x symbols (1000BONKUSDT etc.): multipliers are always 1.0 because
        # PRICE is in base asset terms (e.g. BONK = 0.0044) and QUANTITY is in
        # base asset tokens (e.g. BONK). The "1000" is the contract multiplier
        # for PnL calculation only — no price/qty scaling is needed.
        # Guard clauses below keep this safe if SYMBOL_MAP ever changes.
        if qty_div > 1.0:
            quantity = quantity / qty_div
            quantity = round(quantity)
        if price is not None:
            price = price * price_mult
        if stop_price is not None:
            stop_price = stop_price * price_mult

        mapped_type = self._map_type(order_type)
        symbol_key = resolve_sym

        if self.futures and mapped_type not in {"MARKET", "LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET", "TRAILING_STOP_MARKET"}:
            return OrderInfo(
                order_id="", symbol=symbol_key, side=side.upper(), type=mapped_type,
                quantity=quantity, status="FAILED",
                error=f"Unsupported futures order type: {mapped_type}",
            )

        if self.futures and mapped_type == "STOP_MARKET":
            # Binance algo-close orders must use the CLOSE side, not the open side.
            side_upper = side.upper()
            if side_upper not in {"SELL", "BUY"}:
                return OrderInfo(
                    order_id="", symbol=symbol_key, side=side_upper, type=mapped_type,
                    quantity=quantity, status="FAILED",
                    error=f"Invalid side for stop order: {side_upper}",
                )
        else:
            side_upper = side.upper()

        if self.futures and not await self._preflight_symbol(symbol_key):
            return OrderInfo(
                order_id="", symbol=symbol_key, side=side_upper, type=mapped_type,
                quantity=quantity, status="FAILED",
                error=f"Symbol not available for futures trading: {symbol_key}",
            )

        qty = quantity
        if self.futures:
            qty, rules = await self._round_quantity(symbol_key, qty)
            # For 1000x contracts where LOT_SIZE.stepSize = 1,
            # _round_quantity already handles integer rounding.
            # For very low-price coins with cache miss, the fallback
            # in _round_quantity applies integer rounding.

        params = {
            "symbol": symbol_key,
            "side": side_upper,
            "type": mapped_type,
            "quantity": qty,
        }
        if price and mapped_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["price"] = price
        if stop_price:
            params["stopPrice"] = stop_price

        if mapped_type in ("LIMIT", "STOP", "TAKE_PROFIT"):
            params["timeInForce"] = "GTC"

        try:
            data = await self._signed_request("POST", self._order_path(), params)
            return OrderInfo(
                order_id=str(data.get("orderId", "")),
                symbol=data.get("symbol", symbol),
                side=data.get("side", side),
                type=data.get("type", mapped_type),
                quantity=float(data.get("origQty", quantity)),
                price=float(data.get("price", 0)) if data.get("price") else 0.0,
                status=data.get("status", "NEW"),
                filled_quantity=float(data.get("executedQty", 0)),
                avg_price=float(data.get("avgPrice", 0)) or (
                    float(data.get("cummulativeQuoteQty", 0)) / max(float(data.get("executedQty", 1)), 0.001)
                    if float(data.get("executedQty", 0)) > 0 else 0.0
                ),
            )
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            category = _categorize_error(status, e.response.text, e)
            error_msg = _format_order_error(
                symbol, side_upper, mapped_type, quantity, price,
                getattr(self, "_last_leverage", 1), status, e.response.text,
            )
            log.error("Order failed [%s]: %s", category.value, error_msg)
            return OrderInfo(
                order_id="", symbol=symbol, side=side, type=order_type,
                quantity=quantity, status="FAILED" if category != BinanceErrorCategory.NETWORK else "PENDING",
                error=error_msg,
            )
        except Exception as e:
            category = _categorize_error(None, "", e)
            log.error("Order failed [%s]: %s", category.value, e)
            return OrderInfo(
                order_id="", symbol=symbol, side=side, type=order_type,
                quantity=quantity, status="FAILED" if category != BinanceErrorCategory.NETWORK else "PENDING",
                error=f"[{category.value}] {e}",
            )

    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        return await self._place_order(symbol, "BUY", "MARKET", quantity)

    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo:
        return await self._place_order(symbol, "SELL", "MARKET", quantity)

    async def limit_buy(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        return await self._place_order(symbol, "BUY", "LIMIT", quantity, price=price)

    async def limit_sell(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        return await self._place_order(symbol, "SELL", "LIMIT", quantity, price=price)

    async def stop_loss(self, symbol: str, quantity: float, stop_price: float,
                        side: str = "SELL") -> OrderInfo:
        """Place a stop-loss as a conditional STOP-LIMIT order.

        Uses STOP (not STOP_MARKET) so it appears in the Conditional tab
        and fills as a LIMIT order at the specified price — no slippage.

        For 1000x contracts where STOP is blocked (-4120), falls back to
        position manager monitoring.
        """
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        is_1000x = resolve_sym.startswith("1000")

        if self.futures and is_1000x:
            log.info("STOP orders blocked for %s (1000x). SL monitored by position manager.", resolve_sym)
            return OrderInfo(
                order_id="", symbol=symbol, side=side.upper(),
                type="STOP", quantity=quantity,
                status="UNPROTECTED",
                error="STOP not supported for 1000x; SL handled by position manager",
            )

        # STOP-LIMIT: triggers at stop_price, fills as LIMIT at stop_price
        return await self._place_order(symbol, side, "STOP", quantity,
                                       price=stop_price, stop_price=stop_price)

    async def take_profit(self, symbol: str, quantity: float, tp_price: float,
                          side: str = "SELL") -> OrderInfo:
        """Place a take-profit as a conditional TAKE_PROFIT-LIMIT order.

        Uses TAKE_PROFIT (not TAKE_PROFIT_MARKET) so it appears in the
        Conditional tab and fills as a LIMIT order — no slippage.
        """
        # TAKE_PROFIT-LIMIT: triggers at tp_price, fills as LIMIT at tp_price
        return await self._place_order(symbol, side, "TAKE_PROFIT", quantity,
                                       price=tp_price, stop_price=tp_price)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            await self._signed_request("DELETE", self._order_path(), {
                "symbol": resolve_sym,
                "orderId": order_id,
            })
            return True
        except Exception:
            return False

    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        resolve_sym, _, _ = _resolve_futures_symbol(symbol)
        try:
            data = await self._signed_request("GET", self._order_path(), {
                "symbol": resolve_sym,
                "orderId": order_id,
            })
            return OrderInfo(
                order_id=str(data.get("orderId", "")),
                symbol=data.get("symbol", ""),
                side=data.get("side", ""),
                type=data.get("type", ""),
                quantity=float(data.get("origQty", 0)),
                price=float(data.get("price", 0)) if data.get("price") else 0.0,
                status=data.get("status", ""),
                filled_quantity=float(data.get("executedQty", 0)),
                avg_price=float(data.get("avgPrice", 0)) or (
                    float(data.get("cummulativeQuoteQty", 0)) / max(float(data.get("executedQty", 1)), 0.001)
                    if float(data.get("executedQty", 0)) > 0 else 0.0
                ),
            )
        except Exception:
            return OrderInfo()

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        open_path = "/fapi/v1/openOrders" if self.futures else "/api/v3/openOrders"
        params = {}
        if symbol:
            resolve_sym, _, _ = _resolve_futures_symbol(symbol)
            params["symbol"] = resolve_sym
        try:
            data = await self._signed_request("GET", open_path, params)
            orders = []
            for o in data:
                orders.append(OrderInfo(
                    order_id=str(o.get("orderId", "")),
                    symbol=o.get("symbol", ""),
                    side=o.get("side", ""),
                    type=o.get("type", ""),
                    quantity=float(o.get("origQty", 0)),
                    price=float(o.get("price", 0)) if o.get("price") else 0.0,
                    status=o.get("status", ""),
                    filled_quantity=float(o.get("executedQty", 0)),
                    avg_price=float(o.get("avgPrice", 0)) or 0.0,
                ))
            return orders
        except Exception:
            return []
