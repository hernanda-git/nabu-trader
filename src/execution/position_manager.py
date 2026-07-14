"""Position manager — monitors open positions, handles SL/TP and time-based exits."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from src.domain.models import PendingSignal, Position
from src.exchange.base import Exchange
from src.exchange.validation import validate_order
from src.state.repositories import (
    DecisionRepository,
    OrderRepository,
    PendingSignalRepository,
    PositionEventRepository,
    PositionRepository,
    SignalRepository,
)

log = logging.getLogger("execution.position_manager")


class PositionManager:
    """Manages the lifecycle of open positions.

    Runs a background loop that periodically checks:
    - Are any SL/TP levels hit? (via exchange open orders)
    - Should any positions be closed due to time-out?
    """

    def __init__(self, exchange: Exchange, config: dict,
                 position_repo: PositionRepository,
                 pending_signal_repo: PendingSignalRepository | None = None,
                 position_event_repo: PositionEventRepository | None = None,
                 order_repo: OrderRepository | None = None,
                 decision_repo: DecisionRepository | None = None,
                 signal_repo: "SignalRepository | None" = None,
                 notifier=None,  # TelegramNotifier
                 orchestrator=None):  # TradeOrchestrator — for trigger execution
        self.exchange = exchange
        self.config = config
        self.position_repo = position_repo
        self.pending_signal_repo = pending_signal_repo
        self.position_event_repo = position_event_repo
        self.order_repo = order_repo
        self.decision_repo = decision_repo
        self.signal_repo = signal_repo
        self._notifier = notifier
        self._orchestrator = orchestrator
        self._running = False
        self._task: asyncio.Task | None = None
        self._interval = config.get("monitoring", {}).get("check_interval_seconds", 10)
        # Symbols whose protection (SL/TP) we've already placed this session.
        # Guards the self-heal so we (re)place missing orders exactly once
        # and never spam Binance with duplicate conditional orders.
        self._protection_placed: set[str] = set()
        # Symbols whose SL we fell back to price-monitoring (algo + standard
        # both rejected). Self-heal won't try to place SL for these again.
        self._sl_monitored: set[str] = set()

    async def start(self):
        """Start the background monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        log.info("Position manager started (interval=%ds)", self._interval)

    async def stop(self):
        """Stop the background monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Position manager stopped")

    async def _monitor_loop(self):
        """Background loop that checks open positions and pending conditions."""
        while self._running:
            try:
                await self._reconcile_pending_orders()
                await self._check_positions()
                await self._check_pending_conditions()
            except Exception:
                log.exception("Error in position monitor loop")
            await asyncio.sleep(self._interval)

    # ── Pending LIMIT reconciliation ────────────────────────────────────
    # Root-cause guard: an ENTER that places a LIMIT entry resting on the
    # book does NOT create a positions row until the order FILLS. If price
    # comes back and fills the LIMIT later (after the synchronous entry path
    # already returned PENDING), nothing records the fill — no positions row,
    # no SL/TP, invisible to the monitor. This method closes that hole by
    # polling every PENDING entry order against the exchange and, on fill,
    # building the position + placing SL/TP exactly like OrderService does.
    async def _reconcile_pending_orders(self):
        if self.order_repo is None or self.decision_repo is None or self.signal_repo is None:
            return
        try:
            rows = self._pending_entry_orders()
        except Exception:
            log.debug("reconcile: could not load pending orders")
            return
        for o in rows:
            await self._reconcile_one(o)

    def _pending_entry_orders(self) -> list[dict]:
        """Return DB order rows still PENDING that represent entry orders.

        Entry orders are BUY (LONG) or SELL (SHORT) LIMITs. We only
        reconcile entries — SL/TP are reduce-only and handled elsewhere.
        """
        cur = self.position_repo.conn.execute(
            "SELECT * FROM orders WHERE status = 'PENDING' "
            "AND side IN ('BUY','SELL') AND order_type = 'LIMIT' "
            "ORDER BY id ASC"
        )
        return [dict(r) for r in cur.fetchall()]

    async def _reconcile_one(self, order_row: dict) -> None:
        symbol = order_row["symbol"]
        decision_id = order_row.get("decision_id")
        # Idempotency 1: a positions row already exists -> nothing to do.
        if self.position_repo.get_open_by_pair(symbol) is not None:
            return
        # Idempotency 2: order no longer PENDING on our side.
        # (If SL/TP placement below fails we leave it PENDING and retry next tick.)
        try:
            live = await self.exchange.get_order(symbol, order_row["exchange_order_id"])
        except Exception as e:
            log.debug("reconcile: get_order failed for %s: %s", symbol, e)
            return
        if live.status != "FILLED":
            # Still resting, or cancelled/expired — leave for the monitor/timeout.
            if live.status in ("CANCELED", "EXPIRED", "REJECTED"):
                self.order_repo.update_status(
                    order_row["id"], live.status, live.order_id or "")
            return

        # Filled! Build the position from the original decision (source of SL/TP).
        decision = self.decision_repo.get_by_id(decision_id) if decision_id else None
        if not decision:
            log.warning("reconcile: no decision %s for filled %s — skipping",
                        decision_id, symbol)
            return
        # SL/TP for the entry are NOT stored on decisions (that table only
        # carries action/pair/direction/qty/confidence/reason). They live on
        # the originating signal row, which is the authoritative source.
        sl_price = None
        tp_prices: list[float] = []
        signal_id = decision.get("signal_id")
        signal_row = self.signal_repo.get_by_id(signal_id) if (self.signal_repo and signal_id) else None
        if signal_row:
            sl_price = signal_row.get("sl_price")
            tp_raw = signal_row.get("tp_prices")
            try:
                tp_prices = json.loads(tp_raw) if tp_raw else []
            except (json.JSONDecodeError, TypeError):
                tp_prices = []
        else:
            # Fallback: the decision's own SL/TP if a future schema version adds them.
            sl_price = decision.get("sl_price") or sl_price
            if not tp_prices:
                try:
                    tp_prices = json.loads(decision.get("tp_prices") or "[]") if decision.get("tp_prices") else []
                except (json.JSONDecodeError, TypeError):
                    tp_prices = []
        direction = decision.get("direction") or ("LONG" if order_row["side"] == "BUY" else "SHORT")
        entry_price = float(live.avg_price or decision.get("entry_price") or 0)
        quantity = float(live.filled_quantity or order_row.get("quantity") or 0)
        if quantity <= 0 or entry_price <= 0:
            log.warning("reconcile: bad fill size for %s (qty=%s px=%s) — skipping",
                        symbol, quantity, entry_price)
            return

        from src.domain.models import Position
        pos = Position(
            pair=symbol,
            direction=direction,
            entry_price=entry_price,
            quantity=quantity,
            sl_price=sl_price,
            tp_prices=tp_prices,
            entry_order_id=live.order_id or str(order_row.get("exchange_order_id", "")),
        )
        pos_id = self.position_repo.create(pos)
        self.order_repo.update_status(order_row["id"], "FILLED", live.order_id or "")

        # Place SL/TP — mirrors OrderService._enter_position.
        await self._place_protection(pos_id, pos, symbol, quantity, sl_price, tp_prices)

        if self.position_event_repo:
            self.position_event_repo.save_event(
                position_id=pos_id,
                event_type="POSITION_OPENED",
                details=f"Reconciled {direction} @ {entry_price:.8f} qty={quantity:.4f} (late LIMIT fill)",
                metadata={
                    "pair": symbol, "direction": direction,
                    "entry_price": entry_price, "quantity": quantity,
                    "sl": sl_price, "tp": tp_prices,
                    "reconciled": True,
                },
            )
        log.info("Reconciled late-fill LIMIT: %s %s qty=%.4f @ %.8f",
                 direction, symbol, quantity, entry_price)
        if self._notifier:
            try:
                await self._notifier.send_message(
                    f"🔄 **Position reconciled (late fill)** — `{symbol}`\n"
                    f"   ├ Direction: `{direction}`\n"
                    f"   ├ Qty: `{quantity:,.0f}`\n"
                    f"   ├ Entry: `{entry_price:.8f}`\n"
                    f"   ├ SL: `{sl_price or '—'}`\n"
                    f"   └ TP: `{tp_prices or '—'}`"
                )
            except Exception:
                log.debug("reconcile: notify failed")

    async def _place_protection(self, pos_id: int, pos: "Position",
                               symbol: str, quantity: float,
                               sl_price, tp_prices: list[float],
                               skip_sl: bool = False,
                               skip_tp: bool = False) -> None:
        """Place SL + TP for a (reconciled or opened) position.

        Shared by reconcile and the self-heal so protection logic lives in
        exactly one place. Idempotent per position: callers only invoke it
        once a positions row exists (and the self-heal won't re-call after a
        successful placement, tracked in ``_protection_placed``).

        ``skip_sl`` / ``skip_tp`` let the self-heal avoid re-placing an order
        type that already has a live order on the exchange (so we never stack
        a duplicate TP on top of an existing one across restarts).
        """
        try:
            filters = await self.exchange._load_futures_filters(symbol)
        except Exception:
            filters = None
        filters = filters or {}

        placed_any = False

        def _bump_for_min_notional(qty: float, price: float, filters: dict) -> float:
            """Bump qty until qty*price meets MIN_NOTIONAL.

            Protection orders are reduceOnly, so over-sizing is safe (Binance
            rejects the overflow fill, the position simply stays closed). This
            mirrors the exchange's own _round_quantity MIN_NOTIONAL loop so the
            pre-submission validate_order gate (which does NOT bump) passes.
            """
            mn = filters.get("minNotional")
            step = filters.get("stepSize") or 1.0
            if mn and price and qty * price < mn:
                q = qty
                guard = 0
                while q * price < mn and guard < 1_000_000:
                    q += step
                    guard += 1
                return q
            return qty

        if sl_price and not skip_sl:
            sl_side = "SELL" if pos.direction == "LONG" else "BUY"
            sl_qty = _bump_for_min_notional(quantity, sl_price, filters)
            sl_err = validate_order(symbol, sl_side, sl_price, sl_qty, filters)
            if sl_err:
                log.warning("reconcile: SL skipped %s @ %s: %s", symbol, sl_price, sl_err)
            else:
                sl_order = await self.exchange.stop_loss(symbol, sl_qty, sl_price, sl_side)
                if sl_order.order_id:
                    log.info("reconcile: SL placed %s @ %s (qty=%s)", symbol, sl_price, sl_qty)
                    placed_any = True
                elif sl_order.status == "FAILED":
                    # Both standard and Algo Order API rejected — last resort:
                    # price-monitor in _check_position (so the position is at
                    # least closed if SL is hit, even without a live order).
                    self._sl_monitored.add(symbol)
                    log.warning("reconcile: SL NOT placed %s @ %s (monitored by PM): %s",
                               symbol, sl_price, sl_order.error)
                else:
                    log.warning("reconcile: SL NOT placed %s @ %s (%s)",
                               symbol, sl_price, sl_order.error)
        # Place TP orders
        if not skip_tp and tp_prices:
            for i, tp_price in enumerate(tp_prices[:3]):
                tp_side = "SELL" if pos.direction == "LONG" else "BUY"
                tp_qty = _bump_for_min_notional(quantity, tp_price, filters)
                tp_err = validate_order(symbol, tp_side, tp_price, tp_qty, filters)
                if tp_err:
                    log.warning("reconcile: TP%d skipped %s @ %s: %s", i + 1, symbol, tp_price, tp_err)
                    continue
                tp_order = await self.exchange.take_profit(symbol, tp_qty, tp_price, tp_side)
                if tp_order.order_id:
                    log.info("reconcile: TP%d placed %s @ %s (qty=%s)", i + 1, symbol, tp_price, tp_qty)
                    placed_any = True
                elif tp_order.status == "FAILED":
                    # Both standard and Algo Order API rejected — final fallback:
                    # a resting LIMIT (maker close) so TP can still fill.
                    log.warning("reconcile: TP%d blocked (%s) — LIMIT fallback @ %s",
                                i + 1, tp_order.error, tp_price)
                    if tp_side == "SELL":
                        await self.exchange.limit_sell(symbol, quantity, tp_price, reduce=True)
                    else:
                        await self.exchange.limit_buy(symbol, quantity, tp_price, reduce=True)
                else:
                    log.warning("reconcile: TP%d NOT placed %s @ %s (%s)",
                                i + 1, symbol, tp_price, tp_order.error)

        # Mark protection placed so the self-heal won't re-run on the next tick.
        # (Only when at least one order actually went live — if everything
        # failed we leave it clear so the next cycle retries.)
        if placed_any:
            self._protection_placed.add(symbol)

    async def _check_positions(self):
        """Check all open positions for SL/TP hits or timeouts."""
        positions = self.position_repo.get_open_positions()
        if not positions:
            return

        for pos in positions:
            try:
                await self._check_position(pos)
            except Exception:
                log.exception("Error checking position %s", pos.pair)

    async def _check_position(self, pos: Position):
        """Check a single open position."""
        # Check if SL/TP orders were filled by looking at open orders
        open_orders = await self.exchange.get_open_orders(pos.pair)
        # Algo (conditional) orders do NOT appear in the standard openOrders
        # feed — they live on the Algo Order API. Merge both so a conditional
        # SL/TP placed via the algo endpoint is recognized as "present".
        try:
            open_orders += await self.exchange.get_algo_open_orders(pos.pair)
        except Exception:
            pass

        # Check if the position still has open SL/TP orders.
        # Binance returns conditional orders with type "STOP" (SL) and
        # "TAKE_PROFIT" (TP) — recognize those, plus the spot/legacy variants
        # and the resting Basic-tab LIMIT used as the TP fallback. Algo orders
        # report type "STOP"/"TAKE_PROFIT" too.
        sl_ok = any(o.type in ("STOP_LOSS", "STOP_LOSS_LIMIT", "STOP") for o in open_orders)
        tp_types = ("TAKE_PROFIT", "TAKE_PROFIT_LIMIT", "TAKE_PROFIT_MARKET", "LIMIT")
        tp_count = sum(1 for o in open_orders if o.type in tp_types)

        log.debug("Position %s: open_orders=%d, sl_ok=%s, tp_orders=%d",
                  pos.pair, len(open_orders), sl_ok, tp_count)

        # Self-heal: if SL and/or TP are missing on the exchange (e.g. the
        # position was reconciled before the Algo Order API fallback existed,
        # or an order got cancelled externally), (re)place them — but only
        # once per session (tracked in _protection_placed) to avoid spamming.
        needs_protection = (not sl_ok or tp_count == 0) and pos.pair not in self._protection_placed
        if needs_protection and (pos.sl_price or pos.tp_prices):
            log.info("Self-heal: (re)placing missing protection for %s (sl_ok=%s tp=%d)",
                     pos.pair, sl_ok, tp_count)
            await self._place_protection(
                pos.id, pos, pos.pair, pos.quantity, pos.sl_price, pos.tp_prices or [],
                skip_sl=sl_ok, skip_tp=(tp_count > 0),
            )

        # Price-based SL check — for positions where exchange STOP orders aren't
        # supported (e.g. 1000x contracts), monitor the price and close if SL hit
        if not sl_ok and pos.sl_price and pos.sl_price > 0:
            try:
                # Try mark price first (current price, faster detection)
                mark_price = await self.exchange.get_mark_price(pos.pair)
                if mark_price is not None:
                    current_price = mark_price
                    source = "mark"
                else:
                    # Fallback: last closed 1m candle close (slower, up to 1 min delay)
                    close_price = await self.exchange.get_klines_close(pos.pair, "1m")
                    current_price = close_price
                    source = "klines" if close_price is not None else None

                if current_price is not None:
                    if pos.direction == "LONG" and current_price <= pos.sl_price:
                        log.warning("SL HIT for %s (via %s): current=%.8f <= sl=%.8f — closing",
                                    pos.pair, source, current_price, pos.sl_price)
                        await self._close_position(pos, f"SL hit ({current_price:.8f})")
                        return
                    elif pos.direction == "SHORT" and current_price >= pos.sl_price:
                        log.warning("SL HIT for %s (via %s): current=%.8f >= sl=%.8f — closing",
                                    pos.pair, source, current_price, pos.sl_price)
                        await self._close_position(pos, f"SL hit ({current_price:.8f})")
                        return
            except Exception as e:
                log.debug("Price-based SL check failed for %s: %s", pos.pair, e)

        # If no SL/TP orders remain and position is OPEN, check if it was
        # already filled (closed by exchange). Use age as heuristic:
        # if it's been open > 30 min and has no orders, it was likely closed.
        if len(open_orders) == 0 and pos.entry_time:
            from datetime import datetime, timezone
            if isinstance(pos.entry_time, str):
                entry_dt = datetime.fromisoformat(pos.entry_time)
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=timezone.utc)
            else:
                entry_dt = pos.entry_time.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 60
            if age_min > 30:
                log.info("Position %s has no open orders (age=%.1f min) — marking as closed",
                         pos.pair, age_min)
                # Try to get latest trade info from exchange to calculate P&L
                try:
                    order = await self.exchange.get_order(pos.pair, pos.entry_order_id)
                    if order.status in ("FILLED", "CANCELED", "EXPIRED") or not order.order_id:
                        exit_px = order.avg_price or pos.sl_price or pos.entry_price
                        entry_cost = pos.entry_price * pos.quantity
                        exit_value = exit_px * pos.quantity
                        pnl = (exit_value - entry_cost) if pos.direction == "LONG" else (entry_cost - exit_value)
                        self.position_repo.close_position(
                            pos.id, exit_price=exit_px,
                            pnl=pnl, reason="Auto-detected close (no orders remaining)",
                            closed_by="SYSTEM",
                        )
                        log.info("Position %s auto-closed via exchange, PnL=%.2f", pos.pair, pnl)
                except Exception as e:
                    # If we can't query the order, close with 0 P&L to unblock
                    log.warning("Could not verify %s close reason: %s — closing anyway", pos.pair, e)
                    self.position_repo.close_position(
                        pos.id, exit_price=pos.entry_price,
                        pnl=0.0, reason="Auto-closed (no orders, verification failed)",
                        closed_by="SYSTEM",
                    )

        # Time-based exit — close if position has been open too long
        max_hold_hours = self.config.get("risk", {}).get("max_position_hold_hours", 24)
        if max_hold_hours > 0:
            from datetime import datetime, timezone
            if isinstance(pos.entry_time, str):
                entry_dt = datetime.fromisoformat(pos.entry_time).replace(tzinfo=timezone.utc)
            else:
                entry_dt = pos.entry_time.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
            if age_hours > max_hold_hours:
                log.info("Time-based exit for %s (age=%.1fh > max=%dh)",
                         pos.pair, age_hours, max_hold_hours)
                await self._close_position(pos, f"Time exit ({age_hours:.1f}h)")

    async def close_position(self, pos: Position, reason: str,
                             closed_by: str = "MANUAL") -> bool:
        """Close a position by market order and update state."""
        return await self._close_position(pos, reason, closed_by)

    async def close_position_by_symbol(self, symbol: str,
                                        reason: str = "Manual close (Telegram command)",
                                        closed_by: str = "MANUAL") -> dict:
        """Manually close an open position for ``symbol`` (e.g. via /close).

        Returns a result dict::

            {"ok": bool, "symbol": str, "side": str, "size": float,
             "fill_price": float|None, "pnl": float|None, "error": str|None}

        Flow:
          1. Find the live exchange position (source of truth for size/direction).
          2. Cancel any resting SL/TP orders for the symbol (avoid orphans).
          3. Close via market order using the live position size.
          4. If a local DB position record exists, mark it CLOSED + emit event.

        If no open position exists for ``symbol``, returns ``ok=False`` with a
        clear error so the command can report "no open position".
        """
        from src.domain.models import Position as _Position
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return {"ok": False, "symbol": symbol, "side": "", "size": 0.0,
                    "fill_price": None, "pnl": None, "error": "No symbol provided"}

        # Normalize: accept bare base ("ENA") → "ENAUSDT" via symbol registry.
        resolved = self._resolve_close_symbol(symbol)

        # 1. Find the live exchange position (authoritative open size).
        positions = []
        try:
            positions = await self.exchange.get_positions()
        except Exception as e:
            return {"ok": False, "symbol": symbol, "side": "", "size": 0.0,
                    "fill_price": None, "pnl": None,
                    "error": f"Could not fetch positions: {e}"}

        pos_info = next(
            (p for p in positions
             if p.symbol.upper() == resolved.upper() or p.symbol.upper() == symbol.upper()),
            None,
        )
        if pos_info is None:
            return {"ok": False, "symbol": resolved, "side": "", "size": 0.0,
                    "fill_price": None, "pnl": None,
                    "error": f"No open position for {resolved}"}

        # 2. Cancel resting SL/TP orders so they don't dangle after close.
        try:
            cancelled = await self.exchange.cancel_all_orders(resolved)
            if cancelled:
                log.info("Cancelled %d resting order(s) for %s before manual close",
                         cancelled, resolved)
        except Exception as e:
            log.warning("Failed to cancel resting orders for %s: %s", resolved, e)

        # 3. Build a Position from the live info and close.
        pos = _Position(
            id=0,
            pair=pos_info.symbol,
            direction=pos_info.direction,
            entry_price=pos_info.entry_price,
            quantity=pos_info.size,
            status="OPEN",
        )
        ok = await self._close_position(pos, reason, closed_by, market=True)

        # 4. Also mark the local DB record (if any) CLOSED.
        db_pos = None
        try:
            db_pos = self.position_repo.get_open_by_pair(resolved)
        except Exception:
            db_pos = None
        if db_pos is not None:
            try:
                self.position_repo.close_position(
                    db_pos.id, exit_price=pos_info.mark_price or 0,
                    pnl=pos_info.unrealized_pnl, reason=reason, closed_by=closed_by,
                )
            except Exception as e:
                log.warning("DB close record update failed for %s: %s", resolved, e)

        if ok:
            return {
                "ok": True, "symbol": pos_info.symbol,
                "side": pos_info.direction, "size": pos_info.size,
                "fill_price": pos_info.mark_price or None,
                "pnl": pos_info.unrealized_pnl or None, "error": None,
            }
        return {"ok": False, "symbol": pos_info.symbol, "side": pos_info.direction,
                "size": pos_info.size, "fill_price": None, "pnl": None,
                "error": "Close order not filled / rejected (see logs)"}

    def _resolve_close_symbol(self, symbol: str) -> str:
        """Normalize a close command symbol to a full futures pair.

        Accepts ``ENAUSDT``, ``#ENA``, or ``ENA`` and resolves via the
        SymbolRegistry when available; falls back to appending USDT.
        """
        s = symbol.upper().lstrip("#").strip()
        if s.endswith("USDT"):
            return s
        if s.endswith(("USD", "BUSD", "USDC")):
            return s
        # Try the symbol registry for a base→pair mapping.
        try:
            from src.exchange.symbol_registry import get_registry
        except ImportError:
            get_registry = None  # type: ignore[assignment]
        registry = get_registry() if get_registry else None
        if registry and registry.is_ready:
            resolved, _ = registry.resolve(s)
            if resolved:
                return resolved
        # Bare base asset — assume USDT-margined.
        return f"{s}USDT"

    async def _close_position(self, pos: Position, reason: str,
                              closed_by: str = "MANUAL", market: bool = False) -> bool:
        """Internal: close a position via LIMIT order.

        If ``market`` is True (used by the manual /close command), the position
        is closed with a MARKET order + reduceOnly for an immediate fill instead
        of a resting maker LIMIT at mark price (which can fail to fill on a thin
        book and surface as a "rejected" close).
        """
        side = "SELL" if pos.direction == "LONG" else "BUY"
        try:
            if market:
                # Immediate market close (reduceOnly). No fill-wait needed.
                order = await self.exchange.market_close(pos.pair, pos.quantity, side)
                if order.status in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                    # Market orders fill instantly; record fill price from order.
                    if order.status != "FILLED" and order.filled_quantity <= 0:
                        # Rarely NEW/partial — give a brief moment then read fill.
                        import asyncio as _aio
                        for _ in range(5):
                            await _aio.sleep(1)
                            try:
                                check = await self.exchange.get_order(pos.pair, order.order_id)
                                if check.filled_quantity > 0 or check.status == "FILLED":
                                    order = check
                                    break
                            except Exception:
                                pass
                    return self._finalize_close(pos, order, reason, closed_by)
                log.error("Market close rejected for %s: %s", pos.pair, order.error)
                return False

            # Fetch current price for LIMIT close
            close_price = await self.exchange.get_mark_price(pos.pair)
            if not close_price or close_price <= 0:
                log.error("Cannot determine close price for %s", pos.pair)
                return False

            if side == "SELL":
                order = await self.exchange.limit_sell(pos.pair, pos.quantity, close_price, reduce=True)
            else:
                order = await self.exchange.limit_buy(pos.pair, pos.quantity, close_price, reduce=True)

            # Wait for fill (up to 30s)
            if order.order_id and order.status != "FILLED":
                import asyncio as _aio
                for _ in range(15):
                    await _aio.sleep(2)
                    try:
                        check = await self.exchange.get_order(pos.pair, order.order_id)
                        if check.status == "FILLED":
                            order = check
                            break
                        if check.status in ("CANCELED", "EXPIRED", "REJECTED"):
                            log.error("Close order %s for %s", check.status, pos.pair)
                            return False
                    except Exception:
                        pass
                else:
                    try:
                        await self.exchange.cancel_order(pos.pair, order.order_id)
                    except Exception:
                        pass
                    log.error("Close LIMIT not filled within 30s for %s", pos.pair)
                    return False

            if order.status in ("FILLED", "NEW"):
                return self._finalize_close(pos, order, reason, closed_by)
            else:
                log.error("Failed to close %s: status=%s error=%s", pos.pair, order.status, order.error)
                return False
        except Exception as e:
            log.error("Failed to close %s: %s", pos.pair, e)
            return False

    def _finalize_close(self, pos: Position, order: "object",
                        reason: str, closed_by: str) -> bool:
        """Compute PnL and persist the close (DB + event)."""
        entry_cost = pos.entry_price * pos.quantity
        exit_value = (order.avg_price or 0) * pos.quantity
        pnl = (exit_value - entry_cost) if pos.direction == "LONG" else (entry_cost - exit_value)

        self.position_repo.close_position(
            pos.id, exit_price=order.avg_price or 0,
            pnl=pnl, reason=reason, closed_by=closed_by,
        )

        # Log position event
        if self.position_event_repo:
            event_type = {
                "MANUAL": "POSITION_CLOSED",
                "SL": "SL_HIT",
                "TP": "TP_HIT",
                "TRIGGER": "POSITION_CLOSED",
                "SYSTEM": "AUTO_DETECTED_CLOSE",
            }.get(closed_by, "POSITION_CLOSED")
            self.position_event_repo.save_event(
                position_id=pos.id,
                event_type=event_type,
                details=f"Closed {pos.direction} @ {order.avg_price:.8f} PnL={pnl:.2f} ({reason})",
                metadata={
                    "pair": pos.pair,
                    "direction": pos.direction,
                    "exit_price": order.avg_price,
                    "pnl": pnl,
                    "reason": reason,
                    "closed_by": closed_by,
                },
            )
        log.info("Position closed: %s %s PnL=%.2f (%s)", pos.direction, pos.pair, pnl, closed_by)
        return True

    # ── Pending conditions ──────────────────────────────────────────────────

    async def _check_pending_conditions(self):
        """Check all pending conditional signals and trigger if price condition met."""
        if not self.pending_signal_repo:
            return
        try:
            pending = self.pending_signal_repo.get_pending()
        except Exception:
            log.exception("Failed to load pending signals")
            return
        if not pending:
            return

        for ps in pending:
            try:
                await self._evaluate_condition(ps)
            except Exception:
                log.exception("Error evaluating condition for %s", ps.pair)

    async def _evaluate_condition(self, ps: PendingSignal):
        """Evaluate a single pending condition against current market price."""
        # Build the symbol
        symbol = ps.pair.replace("#", "").replace("$", "").upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        # Fetch the latest closed candle close price
        close_price = await self.exchange.get_klines_close(symbol, ps.timeframe)
        if close_price is None:
            log.warning("Klines unavailable for %s %s — expiring pending signal", symbol, ps.timeframe)
            if self.pending_signal_repo:
                self.pending_signal_repo.mark_expired(ps.id)
            return

        triggered = False
        if ps.condition_type == "close_above" and close_price > ps.trigger_price:
            triggered = True
        elif ps.condition_type == "close_below" and close_price < ps.trigger_price:
            triggered = True

        if not triggered:
            log.debug("Condition %s %s: current=%.8f trigger=%s%.8f (not met)",
                      symbol, ps.condition_type, close_price,
                      ">" if ps.condition_type == "close_above" else "<",
                      ps.trigger_price)
            return

        # ── Condition met! Execute the trade ──
        log.info("Condition TRIGGERED: %s %s current=%.8f trigger=%.8f on %s",
                 symbol, ps.direction, close_price, ps.trigger_price, ps.timeframe)

        # Mark as triggered immediately to prevent duplicate execution
        self.pending_signal_repo.mark_triggered(ps.id)

        # Send initial trigger notification
        if self._notifier:
            try:
                emoji = "🟢" if ps.direction == "LONG" else "🔴"
                await self._notifier.send_message(
                    f"🚀 **Condition triggered — {symbol}**\n"
                    f"   ├ Direction: `{ps.direction}`\n"
                    f"   ├ Condition: `{ps.condition_type}` at `{ps.trigger_price:.8f}`\n"
                    f"   ├ Current close: `{close_price:.8f}`\n"
                    f"   ├ Timeframe: `{ps.timeframe}`\n"
                    f"   └ Original: `{ps.raw_text[:150]}`\n\n"
                    f"⏳ **Auto-entering via LLM...**"
                )
            except Exception:
                log.exception("Failed to notify condition trigger")

        # Auto-enter via orchestrator (LLM decides sizing, SL, TP; Gate2 clamps to 10%)
        if self._orchestrator:
            try:
                await self._orchestrator.execute_trigger(ps)
            except Exception as e:
                log.exception("Auto-entry failed for %s", symbol)
                if self._notifier:
                    await self._notifier.send_message(
                        f"❌ **Auto-entry failed** — {symbol}\n`{e}`"
                    )
        else:
            log.warning("No orchestrator available — trigger not executed for %s", symbol)
