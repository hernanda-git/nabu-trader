"""Position manager — monitors open positions, handles SL/TP and time-based exits."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.domain.models import PendingSignal, Position
from src.exchange.base import Exchange
from src.state.repositories import PendingSignalRepository, PositionRepository

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
                 notifier=None):  # TelegramNotifier, lazy import to avoid circular
        self.exchange = exchange
        self.config = config
        self.position_repo = position_repo
        self.pending_signal_repo = pending_signal_repo
        self._notifier = notifier
        self._running = False
        self._task: asyncio.Task | None = None
        self._interval = config.get("monitoring", {}).get("check_interval_seconds", 10)

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
                await self._check_positions()
                await self._check_pending_conditions()
            except Exception:
                log.exception("Error in position monitor loop")
            await asyncio.sleep(self._interval)

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

        # Check if the position still has open SL/TP orders
        # If all SL/TP are filled/removed, the position may need closing
        sl_ok = any(o.type in ("STOP_LOSS", "STOP_LOSS_LIMIT") for o in open_orders)
        tp_count = sum(1 for o in open_orders if o.type == "LIMIT")

        log.debug("Position %s: open_orders=%d, sl_ok=%s, tp_orders=%d",
                  pos.pair, len(open_orders), sl_ok, tp_count)

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

    async def _close_position(self, pos: Position, reason: str,
                              closed_by: str = "MANUAL") -> bool:
        """Internal: close a position."""
        side = "SELL" if pos.direction == "LONG" else "BUY"
        try:
            if side == "SELL":
                order = await self.exchange.market_sell(pos.pair, pos.quantity)
            else:
                order = await self.exchange.market_buy(pos.pair, pos.quantity)

            if order.status in ("FILLED", "NEW"):
                entry_cost = pos.entry_price * pos.quantity
                exit_value = (order.avg_price or 0) * pos.quantity
                pnl = (exit_value - entry_cost) if pos.direction == "LONG" else (entry_cost - exit_value)

                self.position_repo.close_position(
                    pos.id, exit_price=order.avg_price or 0,
                    pnl=pnl, reason=reason, closed_by=closed_by,
                )
                log.info("Position closed: %s %s PnL=%.2f (%s)", pos.direction, pos.pair, pnl, closed_by)
                return True
            else:
                log.error("Failed to close %s: status=%s error=%s", pos.pair, order.status, order.error)
                return False
        except Exception as e:
            log.error("Failed to close %s: %s", pos.pair, e)
            return False

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
            log.debug("No close price for %s %s — skipping check", symbol, ps.timeframe)
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

        # Send notification
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
                    f"⚡ **Signal ready for manual entry or auto-trade!**"
                )
            except Exception:
                log.exception("Failed to notify condition trigger")
