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

-- Full LLM request/response capture for every agent brain call
CREATE TABLE IF NOT EXISTS llm_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions(id),
    model TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    user_prompt TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    parsed_decision_json TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1,
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Structured log entries with correlation IDs for cross-component tracing
CREATE TABLE IF NOT EXISTS trade_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'INFO',
    module TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trade_logs_correlation ON trade_logs(correlation_id);
CREATE INDEX IF NOT EXISTS idx_trade_logs_level ON trade_logs(level);
CREATE INDEX IF NOT EXISTS idx_trade_logs_module ON trade_logs(module);

-- Lifecycle events for each position — SL placed, TP hit, modified, closed, etc.
CREATE TABLE IF NOT EXISTS position_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL REFERENCES positions(id),
    event_type TEXT NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_position_events_position ON position_events(position_id);
CREATE INDEX IF NOT EXISTS idx_position_events_type ON position_events(event_type);

-- Point-in-time config snapshots tied to trades
CREATE TABLE IF NOT EXISTS config_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash TEXT NOT NULL DEFAULT '',
    config_yaml TEXT NOT NULL DEFAULT '',
    app_version TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

MIGRATIONS_SQL = """
-- Add correlation_id and config_snapshot_id columns (safe IF NOT EXISTS via try/except)
ALTER TABLE signals ADD COLUMN correlation_id TEXT DEFAULT '';
ALTER TABLE decisions ADD COLUMN correlation_id TEXT DEFAULT '';
ALTER TABLE decisions ADD COLUMN llm_interaction_id INTEGER REFERENCES llm_interactions(id);
ALTER TABLE orders ADD COLUMN correlation_id TEXT DEFAULT '';
ALTER TABLE positions ADD COLUMN correlation_id TEXT DEFAULT '';
ALTER TABLE positions ADD COLUMN config_snapshot_id INTEGER REFERENCES config_snapshots(id);
"""


def get_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """Get a SQLite connection with proper settings.

    Creates the data directory and initializes schema if needed.
    Includes backward-compatible migrations for new columns.
    """
    db_path = Path(db_path) if db_path else DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript(SCHEMA_SQL)
    conn.commit()

    # Run backward-compatible migrations (safe on existing DBs)
    _run_migrations(conn)

    log.info("Database ready at %s", db_path)
    return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply backward-compatible column additions.

    Each ALTER TABLE ... ADD COLUMN is wrapped in a try/except to
    handle the case where the column already exists (sqlite doesn't
    support IF NOT EXISTS for ALTER TABLE).
    """
    for stmt in MIGRATIONS_SQL.strip().split("\n"):
        stmt = stmt.strip()
        if not stmt:
            continue
        try:
            conn.execute(stmt)
            conn.commit()
            log.debug("Migration applied: %s", stmt[:60])
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                log.debug("Migration skipped (already exists): %s", stmt[:60])
            else:
                log.warning("Migration failed: %s — %s", stmt[:60], e)


def json_dumps(obj: Any) -> str:
    """Serialize to JSON string for storage."""
    return json.dumps(obj, default=str)


def json_loads(text: str | None) -> Any:
    """Deserialize JSON string from storage."""
    if not text:
        return None
    return json.loads(text)
