"""Abstract exchange interface — all exchanges must implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class BalanceInfo:
    total_usdt: float = 0.0
    free_usdt: float = 0.0
    assets: dict[str, dict] | None = None


@dataclass
class OrderInfo:
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    type: str = ""
    quantity: float = 0.0
    price: float = 0.0
    status: str = ""
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    error: str | None = None


@dataclass
class Ticker:
    """24h ticker snapshot for a symbol (price + stats)."""
    symbol: str = ""
    last_price: float = 0.0
    mark_price: float = 0.0
    change_pct_24h: float = 0.0
    high_24h: float = 0.0
    low_24h: float = 0.0
    volume_24h: float = 0.0
    error: str | None = None


@dataclass
class PositionInfo:
    """An open futures position from the exchange."""
    symbol: str = ""
    direction: str = ""  # LONG or SHORT
    size: float = 0.0    # position size in base asset
    entry_price: float = 0.0
    mark_price: float = 0.0
    liquidation_price: float = 0.0
    unrealized_pnl: float = 0.0
    margin: float = 0.0
    leverage: int = 1
    notional: float = 0.0  # position value in USDT


class Exchange(ABC):
    """Abstract base for all exchange adapters."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Exchange identifier (e.g. 'paper', 'binance', 'binance_testnet')."""

    @abstractmethod
    async def get_balance(self) -> BalanceInfo:
        """Get account balance."""

    @abstractmethod
    async def get_positions(self) -> list[PositionInfo]:
        """Get all open futures positions."""

    @abstractmethod
    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        """Market buy order."""

    @abstractmethod
    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo:
        """Market sell order."""

    async def market_close(self, symbol: str, quantity: float,
                           side: str = "SELL") -> OrderInfo:
        """Close (reduce) a position at market. Default: route to market side.
        Exchange subclasses SHOULD override to add reduceOnly so the order can
        only reduce, never flip, the position."""
        if side.upper() == "BUY":
            return await self.market_buy(symbol, quantity)
        return await self.market_sell(symbol, quantity)

    @abstractmethod
    async def limit_buy(self, symbol: str, quantity: float, price: float,
                        reduce: bool = False) -> OrderInfo:
        """Limit buy order. ``reduce=True`` marks it reduce-only (closing)."""

    @abstractmethod
    async def limit_sell(self, symbol: str, quantity: float, price: float,
                         reduce: bool = False) -> OrderInfo:
        """Limit sell order. ``reduce=True`` marks it reduce-only (closing)."""

    @abstractmethod
    async def stop_loss(self, symbol: str, quantity: float, stop_price: float,
                        side: str = "SELL") -> OrderInfo:
        """Place stop-loss order."""

    @abstractmethod
    async def take_profit(self, symbol: str, quantity: float, tp_price: float,
                          side: str = "SELL") -> OrderInfo:
        """Place take-profit order."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        """Get order status."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        """Get all open orders."""

    async def get_klines(self, symbol: str, interval: str = "4h",
                         limit: int = 100) -> list[list]:
        """Get full OHLCV klines. Each candle: [open_time, open, high, low, close, volume, ...].
        Returns [] if not supported."""
        return []

    async def get_klines_close(self, symbol: str, interval: str = "4h") -> float | None:
        """Get the latest closed candle close price.
        Returns None if not supported by this exchange."""
        return None

    async def get_mark_price(self, symbol: str) -> float | None:
        """Get the current market price.
        Returns None if not supported by this exchange."""
        return None

    async def get_ticker(self, symbol: str) -> "Ticker | None":
        """Get 24h ticker stats (last/mark price, 24h change, high/low, volume).

        Returns a Ticker, or None if not supported by this exchange.
        Implementations should return a Ticker with ``error`` set (rather than
        raising) on lookup failure so callers can surface a clean message.
        """
        return None

    async def cancel_all_orders(self, symbol: str) -> int:
        """Cancel all open orders for a symbol. Returns number of orders cancelled.
        Override for exchange-specific bulk cancellation. Default: cancel one by one."""
        orders = await self.get_open_orders(symbol)
        count = 0
        for o in orders:
            if await self.cancel_order(symbol, o.order_id):
                count += 1
        return count

    # ─── Optional: futures leverage (no-op on spot/paper exchanges) ────────

    async def set_symbol_leverage(self, symbol: str, leverage: int):
        """Set futures leverage for a symbol. No-op on non-futures exchanges."""

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        """Set margin type for a symbol. No-op on non-futures exchanges."""
