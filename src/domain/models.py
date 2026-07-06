"""Domain models — strongly-typed immutable dataclasses for all inter-module communication."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal


# ─── Signal ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeSignal:
    """Raw parsed signal from Telegram after regex pre-parse."""
    message_id: int
    channel: str
    raw_text: str
    pair: str | None = None
    direction: Literal["LONG", "SHORT"] | None = None
    entry_price: float | None = None
    sl_price: float | None = None
    tp_prices: list[float] = field(default_factory=list)
    has_media: bool = False
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Decision ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeDecision:
    """Structured output from the agent brain (LLM)."""
    action: Literal["ENTER", "CLOSE", "SKIP", "MODIFY", "CONDITIONAL"]
    pair: str
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    quantity: float = 0.0
    entry_price: float | None = None
    sl_price: float | None = None
    tp_prices: list[float] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0
    leverage: int = 1  # futures leverage (calculated by Gate2; LLM can also suggest)


# ─── Order ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderRequest:
    """An order ready to send to the exchange."""
    exchange: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP_LOSS_LIMIT"]
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    client_order_id: str = ""  # idempotency key


@dataclass(frozen=True)
class ExecutionResult:
    """Result returned by the exchange after placing an order."""
    success: bool
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    status: str = ""
    error: str | None = None


# ─── Position ──────────────────────────────────────────────────────────────────

@dataclass
class Position:
    """An open or historical position."""
    id: int = 0
    pair: str = ""
    direction: str = ""
    entry_price: float = 0.0
    quantity: float = 0.0
    sl_price: float | None = None
    tp_prices: list[float] = field(default_factory=list)
    entry_order_id: str = ""
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exit_price: float | None = None
    exit_time: datetime | None = None
    status: Literal["OPEN", "CLOSED", "CANCELLED"] = "OPEN"
    pnl: float | None = None
    reason: str | None = None
    closed_by: str | None = None  # 'SL' | 'TP' | 'MANUAL' | 'TRIGGER'


# ─── Events ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Event:
    """Base event for the event bus."""
    event_type: str
    payload: dict
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Pending Conditional Signal ──────────────────────────────────────────────

@dataclass
class PendingSignal:
    """A conditional signal waiting for price conditions to trigger."""
    id: int = 0
    pair: str = ""
    direction: Literal["LONG", "SHORT"] = "LONG"
    condition_type: Literal["close_above", "close_below"] = "close_above"
    trigger_price: float = 0.0
    timeframe: str = "4h"
    raw_text: str = ""
    message_id: int = 0
    status: Literal["PENDING", "TRIGGERED", "EXPIRED", "CANCELLED"] = "PENDING"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    triggered_at: datetime | None = None
