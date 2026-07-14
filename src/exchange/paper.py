"""Paper trading exchange — simulates fills with no real money."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any

from src.exchange.base import BalanceInfo, Exchange, OrderInfo, PositionInfo

log = logging.getLogger("exchange.paper")


class PaperExchange(Exchange):
    """Paper trading simulation.

    Simulates order fills with configurable slippage and fees.
    All state is in-memory (no persistence on restart).
    """

    def __init__(self, config: dict | None = None):
        self._config = config or {}
        self._balance = BalanceInfo(
            total_usdt=10000.0,
            free_usdt=10000.0,
            assets={"USDT": {"free": 10000.0, "locked": 0.0}},
        )
        self._positions: dict[str, dict] = {}
        self._orders: dict[str, OrderInfo] = {}
        self._next_id = 1
        self._slippage_pct = self._config.get("slippage_pct", 0.001)  # 0.1%
        self._fee_pct = self._config.get("fee_pct", 0.001)  # 0.1%

    @property
    def name(self) -> str:
        return "paper"

    async def get_balance(self) -> BalanceInfo:
        """Return simulated balance."""
        return self._balance

    async def get_positions(self) -> list[PositionInfo]:
        """Return simulated open positions."""
        result = []
        for pair, pos in self._positions.items():
            direction = pos.get("direction", "LONG")
            size = pos.get("quantity", 0)
            entry = pos.get("entry_price", 0)
            mark = pos.get("mark_price", entry)
            result.append(PositionInfo(
                symbol=pair,
                direction=direction,
                size=size,
                entry_price=entry,
                mark_price=mark,
                notional=size * mark,
            ))
        return result

    async def _simulate_price(self, symbol: str, side: str) -> float:
        """Simulate a market price for a symbol."""
        base = symbol.replace("USDT", "").replace("USD", "")
        # Rough price estimates for common pairs
        prices = {
            "BTC": 65000.0, "ETH": 3500.0, "SOL": 140.0, "BNB": 580.0,
            "XRP": 0.50, "ADA": 0.45, "DOGE": 0.12, "AVAX": 35.0,
            "DOT": 7.0, "LINK": 14.0, "SUI": 2.0, "ARB": 0.80,
            "OP": 1.80, "APT": 9.0, "TIA": 10.0, "INJ": 25.0,
            "NEAR": 5.0, "FET": 1.50, "SEI": 0.50, "WIF": 2.50,
            "PEPE": 0.00001, "SHIB": 0.00002, "BONK": 0.00002,
            "PUMP": 0.0015,
        }
        price = prices.get(base.upper(), 1.0)
        jitter = price * random.uniform(-0.005, 0.005)
        return price + jitter

    async def _fill_order(self, symbol: str, side: str, quantity: float,
                          order_type: str, price: float | None = None) -> OrderInfo:
        """Simulate an order fill."""
        order_id = f"paper_{self._next_id}"
        self._next_id += 1

        # Simulate price
        fill_price = price or (await self._simulate_price(symbol, side))
        # Apply slippage
        slippage = fill_price * self._slippage_pct * (1 if side == "BUY" else -1)
        fill_price += slippage
        # Fee
        fee = fill_price * quantity * self._fee_pct

        order = OrderInfo(
            order_id=order_id,
            symbol=symbol,
            side=side,
            type=order_type,
            quantity=quantity,
            price=round(fill_price, 8),
            status="FILLED",
            filled_quantity=quantity,
            avg_price=round(fill_price, 8),
        )

        # Update balance
        cost = fill_price * quantity
        fee_cost = cost * self._fee_pct
        if side == "BUY":
            self._balance.free_usdt -= cost + fee_cost
            self._balance.total_usdt -= fee_cost
        else:
            self._balance.free_usdt += cost - fee_cost
            self._balance.total_usdt -= fee_cost

        self._orders[order_id] = order
        log.info("Paper fill: %s %s %s qty=%.4f @ %.8f (fee=%.6f)",
                 side, symbol, order_type, quantity, fill_price, fee)
        return order

    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        return await self._fill_order(symbol, "BUY", quantity, "MARKET")

    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo:
        return await self._fill_order(symbol, "SELL", quantity, "MARKET")

    async def limit_buy(self, symbol: str, quantity: float, price: float,
                       reduce: bool = False) -> OrderInfo:
        return await self._fill_order(symbol, "BUY", quantity, "LIMIT", price)

    async def limit_sell(self, symbol: str, quantity: float, price: float,
                        reduce: bool = False) -> OrderInfo:
        return await self._fill_order(symbol, "SELL", quantity, "LIMIT", price)

    async def stop_loss(self, symbol: str, quantity: float, stop_price: float,
                        side: str = "SELL") -> OrderInfo:
        return await self._fill_order(symbol, side, quantity, "STOP_LOSS", stop_price)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = "CANCELLED"
            return True
        return False

    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        return self._orders.get(order_id, OrderInfo())

    async def get_ticker(self, symbol: str) -> "Ticker | None":
        """Simulated 24h ticker for a symbol."""
        from src.exchange.base import Ticker
        price = await self._simulate_price(symbol, "BUY")
        return Ticker(
            symbol=symbol,
            last_price=price,
            mark_price=price,
            change_pct_24h=0.0,
            high_24h=price * 1.01,
            low_24h=price * 0.99,
            volume_24h=0.0,
        )

    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        return [o for o in self._orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]
