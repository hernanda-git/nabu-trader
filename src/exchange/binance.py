"""Binance exchange adapter — real API + testnet support."""

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

BASE_URLS = {
    "mainnet": "https://api.binance.com",
    "testnet": "https://testnet.binance.vision",
}


class BinanceExchange(Exchange):
    """Binance REST API adapter.

    Supports both mainnet and testnet. Uses API key + secret for auth.
    All orders are synchronous HTTP calls with HMAC-SHA256 signing.
    """

    def __init__(self, api_key: str, api_secret: str, testnet: bool = True,
                 recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.recv_window = recv_window
        base_url = BASE_URLS["testnet"] if testnet else BASE_URLS["mainnet"]
        self._base = base_url
        self._client = httpx.Client(base_url=base_url, timeout=10)

    @property
    def name(self) -> str:
        return "binance_testnet" if self.testnet else "binance"

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

    def _signed_request(self, method: str, path: str,
                        params: dict | None = None) -> dict:
        """Send a signed request to Binance API."""
        params = params or {}
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = self.recv_window
        params["signature"] = self._sign(params)

        resp = self._client.request(method, path, params=params,
                                    headers=self._headers())
        if resp.status_code != 200:
            log.error("Binance API error %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    def _public_request(self, method: str, path: str,
                        params: dict | None = None) -> dict:
        """Send a public request (no auth required)."""
        resp = self._client.request(method, path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ─── Balance ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> BalanceInfo:
        """Get account balance from Binance."""
        data = self._signed_request("GET", "/api/v3/account")
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
                    # Rough USDT value via ticker price
                    try:
                        ticker = self._public_request(
                            "GET", "/api/v3/ticker/price",
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

    # ─── Orders ────────────────────────────────────────────────────────────────

    def _place_order(self, symbol: str, side: str, order_type: str,
                     quantity: float, price: float | None = None,
                     stop_price: float | None = None) -> OrderInfo:
        """Place an order on Binance."""
        params = {
            "symbol": symbol.upper(),
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": quantity,
        }
        if price:
            params["price"] = price
        if stop_price:
            params["stopPrice"] = stop_price

        if order_type.upper() in ("LIMIT", "STOP_LOSS_LIMIT"):
            params["timeInForce"] = "GTC"

        try:
            data = self._signed_request("POST", "/api/v3/order", params)
            return OrderInfo(
                order_id=data.get("orderId", ""),
                symbol=data.get("symbol", symbol),
                side=data.get("side", side),
                type=data.get("type", order_type),
                quantity=float(data.get("origQty", quantity)),
                price=float(data.get("price", 0)) if data.get("price") else 0.0,
                status=data.get("status", "NEW"),
                filled_quantity=float(data.get("executedQty", 0)),
                avg_price=float(data.get("cummulativeQuoteQty", 0)) / max(float(data.get("executedQty", 1)), 0.001)
                if float(data.get("executedQty", 0)) > 0 else 0.0,
            )
        except Exception as e:
            log.error("Order failed: %s", e)
            return OrderInfo(
                order_id="", symbol=symbol, side=side, type=order_type,
                quantity=quantity, status="FAILED", error=str(e),
            )

    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        return self._place_order(symbol, "BUY", "MARKET", quantity)

    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo:
        return self._place_order(symbol, "SELL", "MARKET", quantity)

    async def limit_buy(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        return self._place_order(symbol, "BUY", "LIMIT", quantity, price=price)

    async def limit_sell(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        return self._place_order(symbol, "SELL", "LIMIT", quantity, price=price)

    async def stop_loss(self, symbol: str, quantity: float, stop_price: float,
                        side: str = "SELL") -> OrderInfo:
        return self._place_order(symbol, side, "STOP_LOSS", quantity, stop_price=stop_price)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            self._signed_request("DELETE", "/api/v3/order", {
                "symbol": symbol.upper(),
                "orderId": order_id,
            })
            return True
        except Exception:
            return False

    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        try:
            data = self._signed_request("GET", "/api/v3/order", {
                "symbol": symbol.upper(),
                "orderId": order_id,
            })
            return OrderInfo(
                order_id=data.get("orderId", ""),
                symbol=data.get("symbol", ""),
                side=data.get("side", ""),
                type=data.get("type", ""),
                quantity=float(data.get("origQty", 0)),
                price=float(data.get("price", 0)) if data.get("price") else 0.0,
                status=data.get("status", ""),
                filled_quantity=float(data.get("executedQty", 0)),
                avg_price=float(data.get("cummulativeQuoteQty", 0)) / max(float(data.get("executedQty", 1)), 0.001)
                if float(data.get("executedQty", 0)) > 0 else 0.0,
            )
        except Exception:
            return OrderInfo()

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        params = {}
        if symbol:
            params["symbol"] = symbol.upper()
        try:
            data = self._signed_request("GET", "/api/v3/openOrders", params)
            orders = []
            for o in data:
                orders.append(OrderInfo(
                    order_id=o.get("orderId", ""),
                    symbol=o.get("symbol", ""),
                    side=o.get("side", ""),
                    type=o.get("type", ""),
                    quantity=float(o.get("origQty", 0)),
                    price=float(o.get("price", 0)) if o.get("price") else 0.0,
                    status=o.get("status", ""),
                    filled_quantity=float(o.get("executedQty", 0)),
                    avg_price=float(o.get("cummulativeQuoteQty", 0)) / max(float(o.get("executedQty", 1)), 0.001)
                    if float(o.get("executedQty", 0)) > 0 else 0.0,
                ))
            return orders
        except Exception:
            return []
