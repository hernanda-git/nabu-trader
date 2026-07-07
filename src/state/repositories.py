"""Repository layer — business logic never touches SQL directly."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.domain.models import (
    ConfigSnapshot,
    LLMInteraction,
    PendingSignal,
    Position,
    PositionEvent,
    TradeDecision,
    TradeLogEntry,
    TradeSignal,
)
from src.state.database import get_connection, json_dumps, json_loads

log = logging.getLogger("state.repositories")

# ═════════════════════════════════════════════════════════════════════════════
# Signal Repository
# ═════════════════════════════════════════════════════════════════════════════


class SignalRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, signal: TradeSignal) -> int:
        self.conn.execute(
            """INSERT OR IGNORE INTO signals
               (message_id, channel, raw_text, pair, direction, entry_price, sl_price, tp_prices)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal.message_id,
                signal.channel,
                signal.raw_text,
                signal.pair,
                signal.direction,
                signal.entry_price,
                signal.sl_price,
                json_dumps(signal.tp_prices),
            ),
        )
        self.conn.commit()
        cursor = self.conn.execute("SELECT id FROM signals WHERE message_id = ?", (signal.message_id,))
        row = cursor.fetchone()
        return row["id"] if row else 0

    def get_by_message_id(self, message_id: int) -> dict | None:
        cursor = self.conn.execute("SELECT * FROM signals WHERE message_id = ?", (message_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def is_processed(self, message_id: int, raw_text: str) -> bool:
        """Check if signal has already been processed (idempotency)."""
        h = hashlib.sha256(raw_text.encode()).hexdigest()
        cursor = self.conn.execute(
            "SELECT 1 FROM processed_signals WHERE message_id = ? AND signal_hash = ?",
            (message_id, h),
        )
        return cursor.fetchone() is not None

    def mark_processed(self, message_id: int, raw_text: str) -> None:
        h = hashlib.sha256(raw_text.encode()).hexdigest()
        self.conn.execute(
            "INSERT OR IGNORE INTO processed_signals (message_id, signal_hash) VALUES (?, ?)",
            (message_id, h),
        )
        self.conn.commit()

    def claim(self, message_id: int, raw_text: str = "") -> bool:
        """Atomically reserve a message_id at the start of processing.

        Returns ``True`` if THIS call is the first/owner of the message (so it
        should proceed), ``False`` if the message was already claimed or is
        already in-flight (so the caller must skip it to avoid duplicate work
        and duplicate Telegram notifications).

        The method is await-free, so under asyncio's single-threaded event loop
        it runs to completion without yielding — making it atomic with respect
        to concurrent event handlers (e.g. a channel ``NewMessage`` and
        ``MessageEdited`` event for the same ``message_id`` racing each other,
        or a reconnect replaying an event). The first delivery claims the id
        and proceeds; every later delivery for that id returns ``False`` and is
        skipped before any LLM call or notification fires.
        """
        h = hashlib.sha256(raw_text.encode()).hexdigest()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO processed_signals (message_id, signal_hash) VALUES (?, ?)",
            (message_id, h),
        )
        self.conn.commit()
        return cur.rowcount == 1


# ═════════════════════════════════════════════════════════════════════════════
# Decision Repository
# ═════════════════════════════════════════════════════════════════════════════


class DecisionRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, signal_id: int, decision: TradeDecision) -> int:
        self.conn.execute(
            """INSERT INTO decisions (signal_id, action, pair, direction, quantity, confidence, reason)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id,
                decision.action,
                decision.pair,
                decision.direction,
                decision.quantity,
                decision.confidence,
                decision.reason,
            ),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ═════════════════════════════════════════════════════════════════════════════
# Order Repository
# ═════════════════════════════════════════════════════════════════════════════


class OrderRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, decision_id: int, exchange: str, symbol: str, side: str,
             order_type: str, quantity: float, price: float | None = None,
             client_order_id: str = "") -> int:
        self.conn.execute(
            """INSERT INTO orders (decision_id, client_order_id, exchange, symbol, side, order_type, quantity, price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (decision_id, client_order_id, exchange, symbol, side, order_type, quantity, price),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def update_status(self, order_id: int, status: str, exchange_order_id: str = "") -> None:
        if exchange_order_id:
            self.conn.execute(
                "UPDATE orders SET status = ?, exchange_order_id = ? WHERE id = ?",
                (status, exchange_order_id, order_id),
            )
        else:
            self.conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        self.conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# Position Repository
# ═════════════════════════════════════════════════════════════════════════════


class PositionRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def create(self, position: Position) -> int:
        self.conn.execute(
            """INSERT INTO positions (pair, direction, entry_price, quantity, sl_price, tp_prices, entry_order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                position.pair,
                position.direction,
                position.entry_price,
                position.quantity,
                position.sl_price,
                json_dumps(position.tp_prices),
                position.entry_order_id,
            ),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_open_positions(self) -> list[Position]:
        cursor = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
        )
        return [self._row_to_position(row) for row in cursor.fetchall()]

    def get_open_by_pair(self, pair: str) -> Position | None:
        cursor = self.conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND pair = ? ORDER BY entry_time DESC LIMIT 1",
            (pair,),
        )
        row = cursor.fetchone()
        return self._row_to_position(row) if row else None

    def close_position(self, position_id: int, exit_price: float, pnl: float,
                       reason: str = "", closed_by: str = "MANUAL") -> None:
        self.conn.execute(
            """UPDATE positions SET status = 'CLOSED', exit_price = ?, pnl = ?,
               exit_time = CURRENT_TIMESTAMP, reason = ?, closed_by = ?
               WHERE id = ?""",
            (exit_price, pnl, reason, closed_by, position_id),
        )
        self.conn.commit()

    def update_sl_tp(self, position_id: int, sl_price: float | None = None,
                     tp_prices: list[float] | None = None) -> None:
        """Update SL and/or TP for an open position."""
        if sl_price is not None:
            self.conn.execute(
                "UPDATE positions SET sl_price = ? WHERE id = ?",
                (sl_price, position_id),
            )
        if tp_prices is not None:
            self.conn.execute(
                "UPDATE positions SET tp_prices = ? WHERE id = ?",
                (json_dumps(tp_prices), position_id),
            )
        self.conn.commit()

    def get_daily_pnl(self) -> float:
        """Sum P&L of positions closed today."""
        cursor = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE date(exit_time) = date('now')"
        )
        return cursor.fetchone()[0]

    def get_open_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
        return cursor.fetchone()[0]

    def get_total_notional_usdt(self) -> float:
        """Sum the notional value (entry_price × quantity) of all open positions.

        Used by SafetyGate2 to enforce a portfolio-level notional cap.
        """
        cursor = self.conn.execute(
            "SELECT COALESCE(SUM(entry_price * quantity), 0) FROM positions WHERE status = 'OPEN'"
        )
        return cursor.fetchone()[0]

    def _row_to_position(self, row: sqlite3.Row) -> Position:
        d = dict(row)
        tp_raw = d.pop("tp_prices", None)
        return Position(
            tp_prices=json_loads(tp_raw) or [],
            **{k: d[k] for k in d if d[k] is not None},
        )


# ═════════════════════════════════════════════════════════════════════════════
# Event Repository
# ═════════════════════════════════════════════════════════════════════════════


class EventRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save_event(self, event_type: str, payload: dict) -> None:
        self.conn.execute(
            "INSERT INTO events (event_type, payload) VALUES (?, ?)",
            (event_type, json_dumps(payload)),
        )
        self.conn.commit()


# ═════════════════════════════════════════════════════════════════════════════
# Pending Signal Repository
# ═════════════════════════════════════════════════════════════════════════════


class PendingSignalRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, signal: PendingSignal) -> int:
        self.conn.execute(
            """INSERT INTO pending_signals
               (pair, direction, condition_type, trigger_price, timeframe, raw_text, message_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal.pair, signal.direction, signal.condition_type,
             signal.trigger_price, signal.timeframe, signal.raw_text, signal.message_id),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_pending(self) -> list[PendingSignal]:
        cursor = self.conn.execute(
            "SELECT * FROM pending_signals WHERE status = 'PENDING' ORDER BY created_at ASC"
        )
        return [self._row_to_signal(row) for row in cursor.fetchall()]

    def mark_triggered(self, signal_id: int) -> None:
        self.conn.execute(
            "UPDATE pending_signals SET status = 'TRIGGERED', triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
            (signal_id,),
        )
        self.conn.commit()

    def mark_expired(self, signal_id: int) -> None:
        self.conn.execute(
            "UPDATE pending_signals SET status = 'EXPIRED' WHERE id = ?",
            (signal_id,),
        )
        self.conn.commit()

    def _row_to_signal(self, row: sqlite3.Row) -> PendingSignal:
        return PendingSignal(**dict(row))


# ═════════════════════════════════════════════════════════════════════════════
# LLM Interaction Repository
# ═════════════════════════════════════════════════════════════════════════════


class LLMInteractionRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, interaction: LLMInteraction) -> int:
        self.conn.execute(
            """INSERT INTO llm_interactions
               (decision_id, model, system_prompt, user_prompt, raw_response,
                parsed_decision_json, prompt_tokens, completion_tokens,
                latency_ms, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                interaction.decision_id,
                interaction.model,
                interaction.system_prompt,
                interaction.user_prompt,
                interaction.raw_response,
                interaction.parsed_decision_json,
                interaction.prompt_tokens,
                interaction.completion_tokens,
                interaction.latency_ms,
                1 if interaction.success else 0,
                interaction.error,
            ),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_by_decision_id(self, decision_id: int) -> list[LLMInteraction]:
        cursor = self.conn.execute(
            "SELECT * FROM llm_interactions WHERE decision_id = ? ORDER BY created_at",
            (decision_id,),
        )
        return [LLMInteraction(**dict(r)) for r in cursor.fetchall()]

    def get_all(self, limit: int = 50) -> list[LLMInteraction]:
        cursor = self.conn.execute(
            "SELECT * FROM llm_interactions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [LLMInteraction(**dict(r)) for r in cursor.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# Trade Log Repository
# ═════════════════════════════════════════════════════════════════════════════


class TradeLogRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def log(self, correlation_id: str, level: str, module: str,
            message: str, metadata: dict | None = None) -> int:
        self.conn.execute(
            """INSERT INTO trade_logs (correlation_id, level, module, message, metadata_json)
               VALUES (?, ?, ?, ?, ?)""",
            (correlation_id, level, module, message, json_dumps(metadata or {})),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_by_correlation(self, correlation_id: str) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM trade_logs WHERE correlation_id = ? ORDER BY created_at",
            (correlation_id,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def get_by_level(self, level: str, limit: int = 50) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM trade_logs WHERE level = ? ORDER BY created_at DESC LIMIT ?",
            (level, limit),
        )
        return [dict(r) for r in cursor.fetchall()]

    def get_by_module(self, module: str, limit: int = 50) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM trade_logs WHERE module = ? ORDER BY created_at DESC LIMIT ?",
            (module, limit),
        )
        return [dict(r) for r in cursor.fetchall()]

    def get_recent(self, limit: int = 50) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM trade_logs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# Position Event Repository
# ═════════════════════════════════════════════════════════════════════════════


class PositionEventRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save_event(self, position_id: int, event_type: str,
                   details: str = "", metadata: dict | None = None) -> int:
        self.conn.execute(
            """INSERT INTO position_events (position_id, event_type, details, metadata_json)
               VALUES (?, ?, ?, ?)""",
            (position_id, event_type, details, json_dumps(metadata or {})),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_by_position(self, position_id: int) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM position_events WHERE position_id = ? ORDER BY created_at",
            (position_id,),
        )
        return [dict(r) for r in cursor.fetchall()]

    def get_by_type(self, event_type: str, limit: int = 50) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM position_events WHERE event_type = ? ORDER BY created_at DESC LIMIT ?",
            (event_type, limit),
        )
        return [dict(r) for r in cursor.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# Config Snapshot Repository
# ═════════════════════════════════════════════════════════════════════════════


class ConfigSnapshotRepository:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self.conn = conn or get_connection()

    def save(self, config_hash: str, config_yaml: str,
             app_version: str = "") -> int:
        self.conn.execute(
            """INSERT INTO config_snapshots (config_hash, config_yaml, app_version)
               VALUES (?, ?, ?)""",
            (config_hash, config_yaml, app_version),
        )
        self.conn.commit()
        return self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_by_id(self, snap_id: int) -> dict | None:
        cursor = self.conn.execute(
            "SELECT * FROM config_snapshots WHERE id = ?", (snap_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all(self, limit: int = 20) -> list[dict]:
        cursor = self.conn.execute(
            "SELECT * FROM config_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]
