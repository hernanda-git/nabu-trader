"""Binance exchange adapter — REST API for both Spot and USDⓈ-M Futures.

Supports:
  - Spot trading (api.binance.com)
  - USDⓈ-M Futures (fapi.binance.com)
  - Testnet for both (testnet.binance.vision / testnet futures)
"""

from __future__ import annotations

import hashlib
import hmac
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
        # Cache symbol rules: stepSize/minQty/maxQty/valid
        self._filters: dict[str, dict] = {}
        self._invalid_symbols: dict[str, datetime] = {}

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
        leverage = max(1, min(leverage, 125))
        try:
            params = {
                "symbol": symbol.upper(),
                "leverage": leverage,
            }
            result = await self._signed_request("POST", "/fapi/v1/leverage", params)
            log.info("Leverage set: %s %dx", symbol, leverage)
        except Exception as e:
            log.warning("Failed to set leverage for %s: %s", symbol, e)

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        """Set margin type (ISOLATED or CROSSED) for futures position."""
        if not self.futures:
            return
        try:
            params = {
                "symbol": symbol.upper(),
                "marginType": margin_type.upper(),
            }
            await self._signed_request("POST", "/fapi/v1/marginType", params)
            log.info("Margin type: %s %s", symbol, margin_type.upper())
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
        now = datetime.now(timezone.utc)
        # Reuse if recent
        cached = self._filters.get(symbol)
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
            data = await self._public_request("GET", "/fapi/v1/exchangeInfo", {"symbol": symbol.upper()})
            rules = data.get("symbols", [])
            if not rules:
                self._invalid_symbols[symbol] = now
                log.info("Binance futures symbol not found: %s", symbol)
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
                self._invalid_symbols[symbol] = now
                log.info("Binance symbol not trading: %s (%s)", symbol, status)
                return None
            self._filters[symbol] = out
            return out
        except Exception as e:
            log.debug("exchangeInfo lookup failed for %s: %s", symbol, e)
            return None

    def _round_quantity(self, symbol: str, quantity: float) -> tuple[float, dict | None]:
        """Round quantity to exchange precision for symbol if known.

        Returns (rounded_quantity, rules_or_None).
        Falls back to raw quantity if rules are unknown so we
        don't reject trades due to missing cache.
        """
        if quantity <= 0:
            return quantity, None
        rules = self._filters.get(symbol)
        if not rules:
            return quantity, None
        step = rules.get("stepSize") or rules.get("mktStepSize") or 1.0
        rounded = round(quantity / step) * step
        if rounded <= 0 and step > 0:
            rounded = step
        if "minQty" in rules:
            rounded = max(rounded, rules["minQty"])
        if "mktMinQty" in rules:
            rounded = max(rounded, rules["mktMinQty"])
        mint = rules.get("minQty", rules.get("mktMinQty", 0))
        if rounded < mint:
            rounded = mint
        prec = max(0, -int(round(log10(step))) if step < 1 else 0)
        rounded = float(f"{rounded:.{prec}f}")
        return rounded, rules

    async def _preflight_symbol(self, symbol: str) -> bool:
        """Ensure a futures symbol can be traded; loads filters if needed."""
        if symbol in self._filters or symbol in self._invalid_symbols:
            return bool(self._filters.get(symbol, {}).get("valid") is True)
        rules = await self._load_futures_filters(symbol)
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

    async def get_klines_close(self, symbol: str, interval: str = "4h") -> float | None:
        """Get the latest closed candle close price from Binance Futures klines.

        Args:
            symbol: Trading pair (e.g. 'BONKUSDT')
            interval: Candle interval (1m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d, etc.)

        Returns:
            Close price of the most recently closed candle, or None on error.
        """
        try:
            params = {"symbol": symbol.upper(), "interval": interval, "limit": 2}
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

    # ─── Orders ────────────────────────────────────────────────────────────────

    async def _place_order(self, symbol: str, side: str, order_type: str,
                     quantity: float, price: float | None = None,
                     stop_price: float | None = None) -> OrderInfo:
        """Place an order on Binance (Spot or Futures).

        For futures:
          - STOP_LOSS → STOP_MARKET (triggers market order)
          - Limit orders work the same

        NOTE: Call set_symbol_leverage() BEFORE this method for futures.
        """
        mapped_type = self._map_type(order_type)
        symbol_key = symbol.upper()

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
            qty, _ = self._round_quantity(symbol_key, qty)

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
            log.error("Order failed [%s]: %s", category.value, e)
            return OrderInfo(
                order_id="", symbol=symbol, side=side, type=order_type,
                quantity=quantity, status="FAILED" if category != BinanceErrorCategory.NETWORK else "PENDING",
                error=f"[{category.value}] {e}",
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
        return await self._place_order(symbol, side, "STOP_LOSS", quantity, stop_price=stop_price)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._signed_request("DELETE", self._order_path(), {
                "symbol": symbol.upper(),
                "orderId": order_id,
            })
            return True
        except Exception:
            return False

    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        try:
            data = await self._signed_request("GET", self._order_path(), {
                "symbol": symbol.upper(),
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
            params["symbol"] = symbol.upper()
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
