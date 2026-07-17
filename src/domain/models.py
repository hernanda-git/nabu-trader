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
    # ── Management-command routing ───────────────────────────────────────
    # When a message is a position-management command (sl to entry / tpN / full),
    # mgmt_action carries the kind and tp_index the 0-based TP level. Entry
    # signals leave these as None.
    mgmt_action: str | None = None   # "SL_ENTRY" | "TP" | "FULL"
    tp_index: int | None = None      # 0-based index for "tpN" commands


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


# ─── LLM Interaction ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LLMInteraction:
    """Every LLM call — full request/response for future traceability."""
    id: int = 0
    decision_id: int = 0
    model: str = ""
    system_prompt: str = ""
    user_prompt: str = ""
    raw_response: str = ""
    parsed_decision_json: str = ""  # The JSON that was parsed from response
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    success: bool = True
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Trade Log Entry ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeLogEntry:
    """Structured log entry with correlation ID for cross-component tracing."""
    id: int = 0
    correlation_id: str = ""
    level: str = "INFO"  # DEBUG, INFO, WARNING, ERROR
    module: str = ""
    message: str = ""
    metadata_json: str = "{}"  # JSON blob for structured data
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Position Event ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionEvent:
    """A lifecycle event for a position — SL set, TP hit, SL modified, closed, etc."""
    id: int = 0
    position_id: int = 0
    event_type: str = ""  # POSITION_OPENED, SL_PLACED, TP_PLACED, SL_MODIFIED,
                          # TP_MODIFIED, SL_HIT, TP_HIT, PARTIAL_EXIT,
                          # POSITION_CLOSED, TIME_EXIT, AUTO_DETECTED_CLOSE
    details: str = ""
    metadata_json: str = "{}"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ─── Symbol Info ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolInfo:
    """Resolved symbol information from exchange exchangeInfo.

    Used by SymbolRegistry to cache trading pair metadata,
    eliminating the need for hardcoded symbol lists.
    """
    symbol: str                          # e.g. BTCUSDT, 1000BONKUSDT
    base_asset: str                      # e.g. BTC, 1000BONK
    quote_asset: str                     # e.g. USDT
    price_precision: int = 8             # from exchangeInfo.pricePrecision
    quantity_precision: int = 8          # from exchangeInfo.quantityPrecision
    min_notional: float = 5.0            # from MIN_NOTIONAL filter
    tick_size: float = 0.001             # from PRICE_FILTER.tickSize
    step_size: float = 0.001             # from LOT_SIZE.stepSize
    min_qty: float = 0.001               # from LOT_SIZE.minQty
    max_qty: float = 1_000_000           # from LOT_SIZE.maxQty
    contract_type: str = "PERPETUAL"     # PERPETUAL, CURRENT_MONTH, etc.
    onboard_date: str = ""               # timestamp when listed
    is_1000x: bool = False              # True if base_asset starts with "1000" + letters
    max_leverage: int = 20             # pair-specific max leverage (from leverageBrackets[0])

    def clean_base(self) -> str:
        """Return the display-friendly base asset (strip 1000 prefix).

        Example: '1000BONK' → 'BONK', 'BTC' → 'BTC'
        """
        if self.is_1000x and self.base_asset.startswith("1000"):
            remaining = self.base_asset[4:]
            if remaining.isalpha():
                return remaining
        return self.base_asset

    def display_symbol(self) -> str:
        """Return a human-readable symbol for display/LLM context.

        Example: '1000BONKUSDT' → 'BONKUSDT'
        """
        return self.symbol.replace(f"{self.base_asset}", f"{self.clean_base()}", 1)


# ─── Config Snapshot ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigSnapshot:
    """Point-in-time snapshot of the effective config, tied to a trade."""
    id: int = 0
    config_hash: str = ""
    config_yaml: str = ""
    app_version: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
