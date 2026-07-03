"""Position manager — monitors open positions, handles SL/TP and time-based exits."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.domain.models import Position
from src.exchange.base import Exchange
from src.state.repositories import PositionRepository

log = logging.getLogger("execution.position_manager")


class PositionManager:
    """Manages the lifecycle of open positions.

    Runs a background loop that periodically checks:
    - Are any SL/TP levels hit? (via exchange open orders)
    - Should any positions be closed due to time-out?
    """

    def __init__(self, exchange: Exchange, config: dict,
                 position_repo: PositionRepository):
        self.exchange = exchange
        self.config = config
        self.position_repo = position_repo
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
        """Background loop that checks open positions."""
        while self._running:
            try:
                await self._check_positions()
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

        # Time-based exit — close if position has been open too long
        max_hold_hours = self.config.get("risk", {}).get("max_position_hold_hours", 24)
        if max_hold_hours > 0:
            from datetime import datetime, timezone
            age_hours = (datetime.now(timezone.utc) - pos.entry_time.replace(tzinfo=timezone.utc)).total_seconds() / 3600
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
