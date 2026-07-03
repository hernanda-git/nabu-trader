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
    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        """Market buy order."""

    @abstractmethod
    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo:
        """Market sell order."""

    @abstractmethod
    async def limit_buy(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        """Limit buy order."""

    @abstractmethod
    async def limit_sell(self, symbol: str, quantity: float, price: float) -> OrderInfo:
        """Limit sell order."""

    @abstractmethod
    async def stop_loss(self, symbol: str, quantity: float, stop_price: float,
                        side: str = "SELL") -> OrderInfo:
        """Place stop-loss order."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""

    @abstractmethod
    async def get_order(self, symbol: str, order_id: str) -> OrderInfo:
        """Get order status."""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]:
        """Get all open orders."""

    # ─── Optional: futures leverage (no-op on spot/paper exchanges) ────────

    async def set_symbol_leverage(self, symbol: str, leverage: int):
        """Set futures leverage for a symbol. No-op on non-futures exchanges."""

    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"):
        """Set margin type for a symbol. No-op on non-futures exchanges."""
