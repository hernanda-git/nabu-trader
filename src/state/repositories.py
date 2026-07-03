"""Repository layer — business logic never touches SQL directly."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from src.domain.models import Position, TradeDecision, TradeSignal
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

    def get_daily_pnl(self) -> float:
        """Sum P&L of positions closed today."""
        cursor = self.conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM positions WHERE date(exit_time) = date('now')"
        )
        return cursor.fetchone()[0]

    def get_open_count(self) -> int:
        cursor = self.conn.execute("SELECT COUNT(*) FROM positions WHERE status = 'OPEN'")
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
