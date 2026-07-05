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
from typing import Any
from urllib.parse import urlencode

import httpx

from src.exchange.base import BalanceInfo, Exchange, OrderInfo

log = logging.getLogger("exchange.binance")

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
        self.recv_window = recv_window

        urls = FUTURES_URLS if futures else SPOT_URLS
        base_url = urls["testnet"] if testnet else urls["mainnet"]
        self._base = base_url
        self._client = httpx.AsyncClient(base_url=base_url, timeout=10)

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
            log.error("Binance API error %s: %s", resp.status_code, resp.text)
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
            if wallet > 0 or margin > 0:
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
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": mapped_type,
            "quantity": quantity,
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
        except Exception as e:
            log.error("Order failed: %s", e)
            return OrderInfo(
                order_id="", symbol=symbol, side=side, type=order_type,
                quantity=quantity, status="FAILED", error=str(e),
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
