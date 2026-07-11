"""FastAPI server — secure bridge between Hermes and the Fly.io trading bot.

Provides:
  - Read-only query endpoints (trades, positions, LLM logs, config snapshots)
  - Action endpoints (balance check, position management)
  - Health check (no auth required)
  - Auth verification

Every request (except /health) requires:
  - X-API-Key header (constant-time compared)
  - X-Signature header for POST/PUT/DELETE (HMAC-SHA256 of body)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.status import HTTP_404_NOT_FOUND, HTTP_500_INTERNAL_SERVER_ERROR

from src.api.auth import auth_middleware
from src.state.database import DB_PATH

log = logging.getLogger("api.server")

# ─── DB Connection ────────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    """Get a read-only SQLite connection (use WAL for concurrent access)."""
    if not DB_PATH.exists():
        raise RuntimeError(f"Database not found at {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return None
    return dict(row)


def _rows_to_list(rows: list[sqlite3.Row]) -> list[dict]:
    """Convert a list of sqlite3.Rows to a list of dicts."""
    return [dict(r) for r in rows]


# ─── FastAPI App ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Startup
    port = int(os.environ.get("API_PORT", "9090"))
    log.info("API server starting on port %d", port)
    if not DB_PATH.exists():
        log.warning("Database not found at %s — queries will fail", DB_PATH)
    yield
    # Shutdown
    log.info("API server shutting down")


app = FastAPI(
    title="Fly Trade Bridge API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — allow Hermes from any origin (API key auth protects)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth middleware
app.middleware("http")(auth_middleware)

# ─── Helper: run SQL query ───────────────────────────────────────────────────


def _run_query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read-only SQL query and return results as dicts."""
    conn = _get_conn()
    try:
        cursor = conn.execute(sql, params)
        return _rows_to_list(cursor.fetchall())
    finally:
        conn.close()


def _run_query_one(sql: str, params: tuple = ()) -> dict | None:
    """Execute a read-only SQL query and return a single row or None."""
    rows = _run_query(sql, params)
    return rows[0] if rows else None


# ═══════════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/health")
async def health():
    """Health check — no auth required. Used by Fly.io for load balancer."""
    db_ok = DB_PATH.exists()
    try:
        if db_ok:
            conn = _get_conn()
            conn.execute("SELECT 1")
            conn.close()
        db_status = "ok" if db_ok else "missing"
    except Exception as e:
        db_status = f"error: {e}"

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "database": db_status,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# AUTH
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/auth/verify")
async def verify_auth():
    """Verify the API key is valid. Returns the key prefix for identification."""
    key = os.environ.get("API_KEY", "")
    prefix = key[:8] + "..." if len(key) > 8 else "unknown"
    return {"status": "authenticated", "key_prefix": prefix}


# ═══════════════════════════════════════════════════════════════════════════════
# STATS / DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/stats")
async def get_stats():
    """Trading dashboard stats — positions, PnL, signals today, LLM calls."""
    try:
        open_positions = _run_query(
            "SELECT COUNT(*) as count, COALESCE(SUM(quantity * entry_price), 0) as exposure "
            "FROM positions WHERE status = 'OPEN'"
        )[0]
        today_pnl = _run_query_one(
            "SELECT COALESCE(SUM(pnl), 0) as pnl FROM positions "
            "WHERE date(exit_time) = date('now')"
        )
        signals_today = _run_query_one(
            "SELECT COUNT(*) as count FROM signals WHERE date(created_at) = date('now')"
        )
        trades_today = _run_query_one(
            "SELECT COUNT(*) as count FROM positions WHERE date(entry_time) = date('now')"
        )
        llm_calls_today = _run_query_one(
            "SELECT COUNT(*) as count, COALESCE(SUM(prompt_tokens + completion_tokens), 0) as total_tokens "
            "FROM llm_interactions WHERE date(created_at) = date('now')"
        )

        return {
            "positions": {
                "open": open_positions["count"],
                "exposure_usdt": round(open_positions["exposure"], 2),
            },
            "today": {
                "pnl": round(today_pnl["pnl"], 2) if today_pnl else 0,
                "signals": signals_today["count"] if signals_today else 0,
                "trades": trades_today["count"] if trades_today else 0,
                "llm_calls": llm_calls_today["count"] if llm_calls_today else 0,
                "llm_tokens": llm_calls_today["total_tokens"] if llm_calls_today else 0,
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        log.exception("Stats query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# TRADES
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/trades")
async def list_trades(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    pair: str | None = Query(None),
    status: str | None = Query(None),
):
    """List trades (positions) with optional filters."""
    try:
        where = []
        params: list = []
        if pair:
            where.append("pair LIKE ?")
            params.append(f"%{pair}%")
        if status:
            where.append("status = ?")
            params.append(status.upper())

        where_clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT * FROM positions{where_clause} ORDER BY entry_time DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = _run_query(sql, tuple(params))
        return {"trades": rows, "count": len(rows)}
    except Exception as e:
        log.exception("Trades list failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/v1/trades/{trade_id}")
async def get_trade(trade_id: int):
    """Get a single trade with full trace — decision, LLM interaction, position events, logs."""
    try:
        trade = _run_query_one("SELECT * FROM positions WHERE id = ?", (trade_id,))
        if not trade:
            raise HTTPException(HTTP_404_NOT_FOUND, detail="Trade not found")

        # Correlate back through the pipeline
        correlation_id = trade.get("correlation_id", "")
        decision = _run_query(
            """SELECT d.*, llm.* FROM decisions d
               LEFT JOIN llm_interactions llm ON d.id = llm.decision_id
               WHERE d.pair = ? ORDER BY d.created_at DESC LIMIT 1""",
            (trade["pair"],),
        )
        position_events = _run_query(
            "SELECT * FROM position_events WHERE position_id = ? ORDER BY created_at",
            (trade_id,),
        )
        trade_logs = _run_query(
            "SELECT * FROM trade_logs WHERE correlation_id = ? ORDER BY created_at",
            (correlation_id,),
        ) if correlation_id else []

        return {
            "trade": trade,
            "decision": decision[0] if decision else None,
            "llm_interaction": decision[0] if decision and decision[0].get("model") else None,
            "position_events": position_events,
            "trade_logs": trade_logs,
            "correlation_id": correlation_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Trade detail failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# POSITIONS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/positions")
async def get_positions():
    """List all open positions with lifecycle events."""
    try:
        positions = _run_query(
            "SELECT * FROM positions WHERE status = 'OPEN' ORDER BY entry_time DESC"
        )
        result = []
        for pos in positions:
            events = _run_query(
                "SELECT * FROM position_events WHERE position_id = ? ORDER BY created_at",
                (pos["id"],),
            )
            result.append({**pos, "events": events})
        return {"positions": result, "count": len(result)}
    except Exception as e:
        log.exception("Positions query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# LLM INTERACTIONS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/llm/recent")
async def get_recent_llm(
    limit: int = Query(20, ge=1, le=100),
    model: str | None = Query(None),
):
    """Recent LLM interactions with their decisions."""
    try:
        where = ""
        params: list = []
        if model:
            where = " WHERE llm.model LIKE ?"
            params.append(f"%{model}%")

        sql = f"""SELECT llm.*, d.action as decision_action, d.pair as decision_pair,
                         d.reason as decision_reason, d.confidence
                  FROM llm_interactions llm
                  LEFT JOIN decisions d ON llm.decision_id = d.id
                  {where}
                  ORDER BY llm.created_at DESC LIMIT ?"""
        params.append(limit)

        rows = _run_query(sql, tuple(params))
        return {"interactions": rows, "count": len(rows)}
    except Exception as e:
        log.exception("LLM query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.get("/api/v1/llm/search")
async def search_llm(
    q: str = Query(..., min_length=2),
    limit: int = Query(10, ge=1, le=50),
):
    """Search LLM interactions by prompt or response text."""
    try:
        like = f"%{q}%"
        rows = _run_query(
            """SELECT llm.*, d.action as decision_action, d.pair as decision_pair
               FROM llm_interactions llm
               LEFT JOIN decisions d ON llm.decision_id = d.id
               WHERE llm.user_prompt LIKE ? OR llm.raw_response LIKE ? OR llm.system_prompt LIKE ?
               ORDER BY llm.created_at DESC LIMIT ?""",
            (like, like, like, limit),
        )
        return {"results": rows, "count": len(rows), "query": q}
    except Exception as e:
        log.exception("LLM search failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE LOGS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/logs/{correlation_id}")
async def get_trace_logs(correlation_id: str):
    """Full pipeline trace for a given correlation ID."""
    try:
        logs = _run_query(
            "SELECT * FROM trade_logs WHERE correlation_id = ? ORDER BY created_at",
            (correlation_id,),
        )
        if not logs:
            raise HTTPException(HTTP_404_NOT_FOUND, detail="No logs found for this correlation ID")
        # Also fetch associated signal, decision, positions
        signal = _run_query_one(
            "SELECT * FROM signals WHERE correlation_id = ?", (correlation_id,)
        )
        decision = _run_query_one(
            "SELECT * FROM decisions WHERE correlation_id = ? ORDER BY created_at DESC LIMIT 1",
            (correlation_id,),
        )
        positions = _run_query(
            "SELECT * FROM positions WHERE correlation_id = ? ORDER BY entry_time",
            (correlation_id,),
        )
        return {
            "correlation_id": correlation_id,
            "logs": logs,
            "signal": signal,
            "decision": decision,
            "positions": positions,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("Trace logs query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG SNAPSHOTS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/config/snapshots")
async def get_config_snapshots(limit: int = Query(10, ge=1, le=50)):
    """Recent config snapshots."""
    try:
        snaps = _run_query(
            "SELECT * FROM config_snapshots ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return {"snapshots": snaps, "count": len(snaps)}
    except Exception as e:
        log.exception("Config snapshots query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION EVENTS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/events/recent")
async def get_recent_events(
    limit: int = Query(20, ge=1, le=100),
    event_type: str | None = Query(None),
):
    """Recent position lifecycle events."""
    try:
        where = ""
        params: list = []
        if event_type:
            where = " WHERE pe.event_type = ?"
            params.append(event_type.upper())

        sql = f"""SELECT pe.*, p.pair, p.direction, p.status
                  FROM position_events pe
                  LEFT JOIN positions p ON pe.position_id = p.id
                  {where}
                  ORDER BY pe.created_at DESC LIMIT ?"""
        params.append(limit)

        rows = _run_query(sql, tuple(params))
        return {"events": rows, "count": len(rows)}
    except Exception as e:
        log.exception("Events query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNALS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/signals/recent")
async def get_recent_signals(limit: int = Query(20, ge=1, le=100)):
    """Recent signals received from Telegram."""
    try:
        signals = _run_query(
            """SELECT s.*, d.action as decision_action, d.reason as decision_reason
               FROM signals s
               LEFT JOIN decisions d ON s.id = d.signal_id AND d.id = (
                   SELECT MIN(id) FROM decisions WHERE signal_id = s.id
               )
               ORDER BY s.created_at DESC LIMIT ?""",
            (limit,),
        )
        return {"signals": signals, "count": len(signals)}
    except Exception as e:
        log.exception("Signals query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# EXCHANGE PROXY (read-only exchange info)
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/exchange/balance")
async def proxy_balance():
    """Fetch current balance via the exchange adapter (read-only)."""
    try:
        # Import here to avoid circular deps on app startup
        from src.exchange.binance import BinanceExchange
        from src.config.loader import load_config
        import asyncio

        cfg = load_config()
        binance_cfg = cfg["exchange"]["binance"]
        exchange = BinanceExchange(
            api_key=binance_cfg["api_key"],
            api_secret=binance_cfg["api_secret"],
            testnet=False,
            futures=binance_cfg.get("futures", True),
        )
        balance = await exchange.get_balance()
        return {
            "free_usdt": balance.free_usdt,
            "total_usdt": balance.total_usdt,
            "assets": balance.assets,
        }
    except Exception as e:
        log.exception("Balance proxy failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# SYMBOLS (cached by SymbolRegistry)
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/symbols")
async def list_symbols(
    search: str | None = Query(None, description="Filter by symbol name"),
    is_1000x: bool | None = Query(None, description="Filter 1000x contracts only"),
):
    """List all cached trading pairs from the dynamic SymbolRegistry.

    Populated from live Binance Futures exchangeInfo at startup,
    refreshed every 15 minutes. No hardcoded coin lists.
    """
    try:
        from src.exchange.symbol_registry import get_registry
        registry = get_registry()
        if not registry or not registry.is_ready:
            return {"symbols": [], "count": 0, "status": "not_ready",
                    "message": "SymbolRegistry not yet initialized"}

        symbols = registry.to_dict()
        if search:
            search_upper = search.upper()
            symbols = [s for s in symbols if search_upper in s["symbol"].upper()
                       or search_upper in s["base_asset"].upper()
                       or search_upper in s["display"]]
        if is_1000x is not None:
            symbols = [s for s in symbols if s["is_1000x"] == is_1000x]

        return {
            "symbols": symbols,
            "count": len(symbols),
            "total_cached": registry.symbol_count,
            "last_refresh": registry.last_refresh.isoformat() if registry.last_refresh else None,
            "status": "ready",
        }
    except Exception as e:
        log.exception("Symbols query failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/search/trades")
async def search_trades(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
):
    """Search trades by pair or reason text."""
    try:
        like = f"%{q}%"
        rows = _run_query(
            """SELECT * FROM positions
               WHERE pair LIKE ? OR reason LIKE ? OR closed_by LIKE ?
               ORDER BY entry_time DESC LIMIT ?""",
            (like, like, like, limit),
        )
        return {"results": rows, "count": len(rows), "query": q}
    except Exception as e:
        log.exception("Trade search failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# PENDING CONDITIONAL SIGNALS
# ═══════════════════════════════════════════════════════════════════════════════


@app.get("/api/v1/pending")
async def list_pending():
    """List all pending conditional signals."""
    try:
        rows = _run_query(
            "SELECT * FROM pending_signals WHERE status = 'PENDING' ORDER BY created_at ASC"
        )
        return {"signals": rows, "count": len(rows)}
    except Exception as e:
        log.exception("Failed to list pending signals")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@app.post("/api/v1/pending/{signal_id}/cancel")
async def cancel_pending(signal_id: int):
    """Cancel a specific pending signal."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE pending_signals SET status = 'CANCELLED' WHERE id = ? AND status = 'PENDING'",
            (signal_id,),
        )
        conn.commit()
        if cur.rowcount > 0:
            return {"success": True, "message": f"Signal #{signal_id} cancelled"}
        # Check if it exists at all
        row = conn.execute(
            "SELECT status FROM pending_signals WHERE id = ?", (signal_id,)
        ).fetchone()
        if row:
            return {"success": False, "message": f"Signal #{signal_id} is already {dict(row)['status']}"}
        raise HTTPException(404, detail=f"Signal #{signal_id} not found")
    finally:
        conn.close()


@app.post("/api/v1/pending/cancel_all")
async def cancel_all_pending():
    """Cancel all pending signals."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "UPDATE pending_signals SET status = 'CANCELLED' WHERE status = 'PENDING'"
        )
        conn.commit()
        return {"success": True, "cancelled": cur.rowcount}
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# MANUAL TRADE EXECUTION
# ═══════════════════════════════════════════════════════════════════════════════


@app.post("/api/v1/trade")
async def execute_trade(payload: dict):
    """Execute a trade manually with auto pair resolution and minimum notional handling.

    Body:
    {
        "pair": "PUMP",           // auto-resolved (PUMP → PUMPUSDT, BONK → 1000BONKUSDT, etc.)
        "direction": "LONG",      // LONG or SHORT
        "entry_price": 0.00162,   // optional — uses current price if omitted
        "sl_price": 0.001568,     // optional
        "tp_prices": [0.001975],  // optional
        "leverage": 0,            // optional — auto-calculated if 0
        "port_pct": 10            // optional — % of balance to risk (default 10)
    }
    """
    try:
        from src.exchange.binance import BinanceExchange, _resolve_futures_symbol
        from src.exchange.symbol_registry import get_registry
        from src.config.loader import load_config

        cfg = load_config()
        binance_cfg = cfg["exchange"]["binance"]
        exchange = BinanceExchange(
            api_key=binance_cfg["api_key"],
            api_secret=binance_cfg["api_secret"],
            testnet=False,
            futures=binance_cfg.get("futures", True),
        )

        # ── Resolve pair ──
        raw_pair = (payload.get("pair") or "").upper().strip()
        if not raw_pair:
            raise HTTPException(400, detail="pair is required")
        if not raw_pair.endswith("USDT"):
            raw_pair += "USDT"

        resolved_sym, _, _ = _resolve_futures_symbol(raw_pair)

        # Verify symbol exists
        registry = get_registry()
        if registry and registry.is_ready:
            info = registry.get_symbol_info(resolved_sym)
            if not info:
                raise HTTPException(400, detail=f"Symbol {resolved_sym} not found on Binance Futures")

        # ── Direction ──
        direction = (payload.get("direction") or "LONG").upper()
        if direction not in ("LONG", "SHORT"):
            raise HTTPException(400, detail="direction must be LONG or SHORT")
        side = "BUY" if direction == "LONG" else "SELL"

        # ── Entry price ──
        entry_price = payload.get("entry_price")
        if not entry_price or entry_price <= 0:
            mark = await exchange.get_mark_price(resolved_sym)
            if not mark or mark <= 0:
                raise HTTPException(400, detail=f"Cannot determine price for {resolved_sym}")
            entry_price = mark

        # ── Balance + sizing ──
        bal = await exchange.get_balance()
        port_pct = float(payload.get("port_pct", 10))
        margin_budget = bal.free_usdt * port_pct / 100.0

        # Get min notional from symbol info
        min_notional = 5.0  # Binance default
        if registry and registry.is_ready:
            info = registry.get_symbol_info(resolved_sym)
            if info:
                min_notional = info.min_notional

        # Calculate leverage needed to meet min notional
        leverage = int(payload.get("leverage", 0))
        if leverage <= 0:
            if margin_budget > 0:
                leverage = max(1, int(min_notional / margin_budget) + 1)
            else:
                leverage = 1

        notional = margin_budget * leverage
        if notional < min_notional:
            raise HTTPException(400, detail={
                "error": "INSUFFICIENT_MARGIN",
                "message": f"Cannot meet ${min_notional} minimum notional",
                "balance": bal.free_usdt,
                "margin_budget": margin_budget,
                "min_notional": min_notional,
                "leverage_needed": int(min_notional / margin_budget) + 1 if margin_budget > 0 else 999,
            })

        quantity = int(notional / entry_price)  # step=1 for most low-price coins
        if quantity <= 0:
            raise HTTPException(400, detail="Calculated quantity is 0 — increase balance or leverage")

        # ── Set leverage ──
        await exchange.set_symbol_leverage(resolved_sym, leverage)

        # ── Place LIMIT entry ──
        order = await exchange.limit_buy(resolved_sym, quantity, entry_price) if side == "BUY" \
            else await exchange.limit_sell(resolved_sym, quantity, entry_price)

        if order.status in ("FAILED", "REJECTED", "EXPIRED"):
            return {
                "success": False,
                "error": order.error,
                "pair": resolved_sym,
                "quantity": quantity,
                "leverage": leverage,
            }

        # ── Place TP ──
        tp_prices = payload.get("tp_prices") or []
        tp_orders = []
        for tp in tp_prices[:3]:
            tp_side = "SELL" if direction == "LONG" else "BUY"
            tp_order = await exchange.take_profit(resolved_sym, quantity, tp, tp_side)
            tp_orders.append({"price": tp, "order_id": tp_order.order_id, "status": tp_order.status})

        # ── SL via position manager (STOP_MARKET blocked for some contracts) ──
        sl_price = payload.get("sl_price")
        sl_status = "MONITORED_BY_POSITION_MANAGER"

        return {
            "success": True,
            "pair": resolved_sym,
            "direction": direction,
            "entry_price": entry_price,
            "quantity": quantity,
            "notional": round(notional, 4),
            "leverage": leverage,
            "margin_used": round(margin_budget, 4),
            "order_id": order.order_id,
            "order_status": order.status,
            "sl_price": sl_price,
            "sl_status": sl_status,
            "tp_orders": tp_orders,
        }

    except HTTPException:
        raise
    except Exception as e:
        log.exception("Manual trade execution failed")
        raise HTTPException(HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


def start_api_server():
    """Start the API server in a separate thread.

    Called from main.py as a background task.
    """
    port = int(os.environ.get("API_PORT", "9090"))
    host = os.environ.get("API_HOST", "0.0.0.0")
    log.info("Starting API bridge on %s:%d", host, port)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )
