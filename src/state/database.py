"""SQLite database management — schema creation, connection, migrations."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

log = logging.getLogger("state.database")

from src.config.loader import get_data_dir
DATA_DIR = get_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "trades.db"

SCHEMA_SQL = """
-- Every signal received from Telegram
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    pair TEXT,
    direction TEXT,
    entry_price REAL,
    sl_price REAL,
    tp_prices TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every decision made by the agent
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    action TEXT NOT NULL,
    pair TEXT,
    direction TEXT,
    quantity REAL,
    confidence REAL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every order sent to exchange
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions(id),
    client_order_id TEXT UNIQUE,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,
    status TEXT DEFAULT 'PENDING',
    exchange_order_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every fill / execution
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id),
    filled_quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL,
    fee_asset TEXT,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Active and historical positions
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    sl_price REAL,
    tp_prices TEXT,
    entry_order_id TEXT,
    entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exit_price REAL,
    exit_order_id TEXT,
    exit_time TIMESTAMP,
    pnl REAL,
    status TEXT DEFAULT 'OPEN',
    reason TEXT,
    closed_by TEXT
);

-- Idempotency: processed signal tracking
CREATE TABLE IF NOT EXISTS processed_signals (
    message_id INTEGER PRIMARY KEY,
    signal_hash TEXT NOT NULL,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Immutable audit trail
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Daily aggregated metrics
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    total_signals INTEGER DEFAULT 0,
    trades_opened INTEGER DEFAULT 0,
    trades_closed INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Pending conditional signals awaiting price triggers
CREATE TABLE IF NOT EXISTS pending_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    condition_type TEXT NOT NULL DEFAULT 'close_above',
    trigger_price REAL NOT NULL,
    timeframe TEXT NOT NULL DEFAULT '4h',
    raw_text TEXT NOT NULL,
    message_id INTEGER,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    triggered_at TIMESTAMP
);
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with proper settings.

    Creates the data directory and initializes schema if needed.
    """
    db_path = Path(db_path) if db_path else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript(SCHEMA_SQL)
    conn.commit()

    log.info("Database ready at %s", db_path)
    return conn


def json_dumps(obj: Any) -> str:
    """Serialize to JSON string for storage."""
    return json.dumps(obj, default=str)


def json_loads(text: str | None) -> Any:
    """Deserialize JSON string from storage."""
    if not text:
        return None
    return json.loads(text)
