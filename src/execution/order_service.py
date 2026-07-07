"""Order service — translates TradeDecision → exchange orders with idempotency."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from src.domain.models import ExecutionResult, OrderRequest, TradeDecision
from src.exchange.base import Exchange
from src.state.repositories import DecisionRepository, OrderRepository, PositionRepository, SignalRepository

log = logging.getLogger("execution.order_service")


class OrderService:
    """Bridge between TradeDecision and the exchange.

    Generates idempotent client_order_ids, places orders via the exchange,
    and records everything in state.
    """

    def __init__(self, exchange: Exchange, config: dict,
                 signal_repo: SignalRepository,
                 decision_repo: DecisionRepository,
                 order_repo: OrderRepository,
                 position_repo: PositionRepository):
        self.exchange = exchange
        self.config = config
        self.signal_repo = signal_repo
        self.decision_repo = decision_repo
        self.order_repo = order_repo
        self.position_repo = position_repo
        self._max_retries = config.get("execution", {}).get("max_retries", 3)

    def _generate_client_id(self, decision_id: int) -> str:
        """Generate a unique, idempotent client order ID."""
        return f"lnr_{decision_id}_{uuid.uuid4().hex[:8]}"

    async def execute(self, signal_id: int, decision: TradeDecision,
                      decision_id: int | None = None) -> ExecutionResult:
        """Execute a trade decision and return the result.

        Args:
            signal_id: The DB ID of the signal triggering this decision.
            decision: The trade decision to execute.
            decision_id: Optional pre-saved decision ID. If None, the decision
                         is saved internally and a new ID is created.
        """
        # Save decision if not pre-saved
        if decision_id is None:
            decision_id = self.decision_repo.save(signal_id, decision)

        if decision.action == "SKIP":
            return ExecutionResult(success=True, status="SKIPPED",
                                   error=f"Skipped: {decision.reason}")

        if decision.action == "CLOSE":
            return await self._close_position(decision_id, decision)

        if decision.action == "MODIFY":
            return await self._modify_position(decision_id, decision)

        return await self._enter_position(decision_id, decision)

    async def _enter_position(self, decision_id: int,
                              decision: TradeDecision) -> ExecutionResult:
        """Open a new position."""
        symbol = decision.pair.replace("#", "").replace("$", "").upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        quantity = decision.quantity
        client_id = self._generate_client_id(decision_id)
        side = "BUY" if decision.direction == "LONG" else "SELL"

        # Set futures leverage and margin type before placing the entry order
        if decision.leverage > 1:
            await self.exchange.set_symbol_leverage(symbol, decision.leverage)
            margin_type = self.config.get("risk", {}).get("margin_type", "ISOLATED")
            await self.exchange.set_margin_type(symbol, margin_type)

        # ─── Pre-flight: leverage sanity check ──────────────────────────
        # Gate2 already validates this, but double-check before hitting the API
        if decision.leverage > 1:
            try:
                bal = await self.exchange.get_balance()
                margin_budget = bal.free_usdt * (
                    self.config.get("risk", {}).get("margin_usage_pct", 50) / 100.0
                )
                notional_value = decision.quantity * (decision.entry_price or 0)
                margin_needed = notional_value / decision.leverage
                if margin_needed > margin_budget * 1.1:  # 10% tolerance
                    log.warning(
                        "Pre-flight margin check: needed=$%.2f budget=$%.2f (%.1f%% over)",
                        margin_needed, margin_budget,
                        (margin_needed / margin_budget - 1) * 100,
                    )
            except Exception as e:
                log.debug("Pre-flight balance check skipped: %s", e)

        # Validate minimum notional (Binance requirement)
        price_ref = decision.entry_price or 0
        min_notional = self.config.get("risk", {}).get("min_notional_usdt", 1.0)
        notional_check = quantity * price_ref
        if notional_check < min_notional and notional_check > 0:
            # For low-price coins, scale quantity up to meet min notional
            scale = min_notional / notional_check
            quantity = quantity * scale
            log.info("Scaled quantity by %.4fx to meet min notional $%.2f (was $%.2f)",
                     scale, min_notional, notional_check)

        # ─── Entry order (always LIMIT, never MARKET) ───────────────────
        entry_price = decision.entry_price
        if not entry_price or entry_price <= 0:
            # No entry price provided — use current mark price
            try:
                entry_price = await self.exchange.get_mark_price(symbol)
            except Exception:
                entry_price = None
            if not entry_price or entry_price <= 0:
                return ExecutionResult(
                    success=False, status="FAILED",
                    error=f"Cannot determine entry price for {symbol}",
                )

        # Ensure LIMIT price is fillable: for BUY, price must be >= current;
        # for SELL, price must be <= current. Otherwise the order waits forever.
        try:
            current = await self.exchange.get_mark_price(symbol)
            if current and current > 0:
                if side == "BUY" and entry_price < current:
                    log.info("Entry price %.8f < current %.8f — adjusting to current",
                             entry_price, current)
                    entry_price = current
                elif side == "SELL" and entry_price > current:
                    log.info("Entry price %.8f > current %.8f — adjusting to current",
                             entry_price, current)
                    entry_price = current
        except Exception:
            pass

        order = await self.exchange.limit_buy(symbol, quantity, entry_price) if side == "BUY" \
            else await self.exchange.limit_sell(symbol, quantity, entry_price)

        # Wait for LIMIT fill (up to 30s, poll every 2s)
        if order.order_id and order.status != "FILLED":
            import asyncio as _aio
            for _attempt in range(15):
                await _aio.sleep(2)
                try:
                    check = await self.exchange.get_order(symbol, order.order_id)
                    if check.status == "FILLED":
                        order = check
                        break
                    if check.status in ("CANCELED", "EXPIRED", "REJECTED"):
                        return ExecutionResult(
                            success=False, status=check.status,
                            error=f"Entry order {check.status}: {check.error or ''}",
                        )
                except Exception:
                    pass
            else:
                # Timeout — cancel and fail
                try:
                    await self.exchange.cancel_order(symbol, order.order_id)
                except Exception:
                    pass
                return ExecutionResult(
                    success=False, status="TIMEOUT",
                    error=f"Entry LIMIT not filled within 30s for {symbol} @ {entry_price}",
                )

        # Save order
        order_db_id = self.order_repo.save(
            decision_id=decision_id, exchange=self.exchange.name,
            symbol=symbol, side=side, order_type="LIMIT",
            quantity=quantity, price=order.price,
            client_order_id=client_id,
        )

        if order.status in ("FAILED", "REJECTED", "EXPIRED"):
            self.order_repo.update_status(order_db_id, order.status, order.order_id)
            return ExecutionResult(
                success=False, status=order.status, error=order.error or "Order rejected",
            )

        self.order_repo.update_status(order_db_id, "FILLED", order.order_id)

        # Save position
        from src.domain.models import Position
        pos = Position(
            pair=symbol,
            direction=decision.direction,
            entry_price=order.avg_price or decision.entry_price or 0,
            quantity=order.filled_quantity or quantity,
            sl_price=decision.sl_price,
            tp_prices=decision.tp_prices,
            entry_order_id=order.order_id,
        )
        pos_id = self.position_repo.create(pos)

        # Place SL order
        sl_placed = False
        if decision.sl_price:
            sl_side = "SELL" if decision.direction == "LONG" else "BUY"
            sl_order = await self.exchange.stop_loss(symbol, quantity, decision.sl_price, sl_side)
            if sl_order.order_id:
                sl_placed = True
                log.info("SL placed: %s @ %s", symbol, decision.sl_price)
            else:
                log.warning("SL NOT placed for %s @ %s — position is UNPROTECTED", symbol, decision.sl_price)

        # Place TP orders (only if SL placed or no SL configured)
        for i, tp_price in enumerate(decision.tp_prices[:3]):
            tp_side = "SELL" if decision.direction == "LONG" else "BUY"
            if tp_side == "SELL":
                tp_order = await self.exchange.limit_sell(symbol, quantity, tp_price)
            else:
                tp_order = await self.exchange.limit_buy(symbol, quantity, tp_price)
            if tp_order.order_id:
                log.info("TP%d placed: %s @ %s", i + 1, symbol, tp_price)

        log.info("Position opened: %s %s %s qty=%.4f", decision.direction, symbol, decision.order_type, quantity)

        return ExecutionResult(
            success=True,
            order_id=order.order_id,
            symbol=symbol,
            side=side,
            filled_quantity=order.filled_quantity or quantity,
            avg_price=order.avg_price or decision.entry_price or 0,
            status="FILLED",
        )

    async def _close_position(self, decision_id: int,
                              decision: TradeDecision) -> ExecutionResult:
        """Close an open position."""
        symbol = decision.pair.replace("#", "").replace("$", "").upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        position = self.position_repo.get_open_by_pair(symbol)
        if not position:
            return ExecutionResult(success=False, status="NOT_FOUND",
                                   error=f"No open position for {symbol}")

        side = "SELL" if position.direction == "LONG" else "BUY"

        # Always LIMIT close — fetch current price for the LIMIT
        try:
            close_price = await self.exchange.get_mark_price(symbol)
        except Exception:
            close_price = None
        if not close_price or close_price <= 0:
            return ExecutionResult(
                success=False, status="FAILED",
                error=f"Cannot determine close price for {symbol}",
            )

        order = await self.exchange.limit_sell(symbol, position.quantity, close_price) if side == "SELL" \
            else await self.exchange.limit_buy(symbol, position.quantity, close_price)

        # Wait for close fill (up to 30s)
        if order.order_id and order.status != "FILLED":
            import asyncio as _aio
            for _ in range(15):
                await _aio.sleep(2)
                try:
                    check = await self.exchange.get_order(symbol, order.order_id)
                    if check.status == "FILLED":
                        order = check
                        break
                    if check.status in ("CANCELED", "EXPIRED", "REJECTED"):
                        return ExecutionResult(
                            success=False, status=check.status,
                            error=f"Close order {check.status}",
                        )
                except Exception:
                    pass
            else:
                try:
                    await self.exchange.cancel_order(symbol, order.order_id)
                except Exception:
                    pass
                return ExecutionResult(
                    success=False, status="TIMEOUT",
                    error=f"Close LIMIT not filled within 30s for {symbol}",
                )

        # Calculate P&L
        entry_cost = position.entry_price * position.quantity
        exit_value = (order.avg_price or 0) * position.quantity
        pnl = (exit_value - entry_cost) if position.direction == "LONG" else (entry_cost - exit_value)

        self.position_repo.close_position(
            position.id, exit_price=order.avg_price or 0,
            pnl=pnl, reason=decision.reason, closed_by="TRIGGER",
        )

        log.info("Position closed: %s %s PnL=%.2f", position.direction, symbol, pnl)

        return ExecutionResult(
            success=True,
            order_id=order.order_id,
            symbol=symbol,
            side=side,
            filled_quantity=position.quantity,
            avg_price=order.avg_price or 0,
            status="CLOSED",
        )

    async def _modify_position(self, decision_id: int,
                               decision: TradeDecision) -> ExecutionResult:
        """Modify an existing position — cancel old SL/TP, place new ones."""
        symbol = decision.pair.replace("#", "").replace("$", "").upper()
        if not symbol.endswith("USDT"):
            symbol += "USDT"

        position = self.position_repo.get_open_by_pair(symbol)
        if not position:
            return ExecutionResult(
                success=False, status="NOT_FOUND",
                error=f"No open position for {symbol} to modify",
            )

        log.info("Modifying %s %s: cancel old SL/TP → place new", position.direction, symbol)

        # Cancel existing SL/TP orders
        cancelled = await self.exchange.cancel_all_orders(symbol)
        log.info("Cancelled %d existing orders for %s", cancelled, symbol)

        # Place new SL order if specified
        sl_placed = False
        if decision.sl_price and decision.sl_price > 0:
            sl_side = "SELL" if position.direction == "LONG" else "BUY"
            sl_order = await self.exchange.stop_loss(
                symbol, position.quantity, decision.sl_price, sl_side
            )
            if sl_order.order_id:
                sl_placed = True
                log.info("New SL placed: %s @ %s", symbol, decision.sl_price)
            else:
                log.warning("New SL NOT placed for %s @ %s", symbol, decision.sl_price)

        # Place new TP orders if specified
        tp_placed = 0
        for i, tp_price in enumerate(decision.tp_prices[:3]):
            if tp_price <= 0:
                continue
            tp_side = "SELL" if position.direction == "LONG" else "BUY"
            if tp_side == "SELL":
                tp_order = await self.exchange.limit_sell(symbol, position.quantity, tp_price)
            else:
                tp_order = await self.exchange.limit_buy(symbol, position.quantity, tp_price)
            if tp_order.order_id:
                tp_placed += 1
                log.info("New TP%d placed: %s @ %s", i + 1, symbol, tp_price)

        # Update position SL/TP in database
        self.position_repo.update_sl_tp(
            position.id,
            sl_price=decision.sl_price,
            tp_prices=decision.tp_prices,
        )

        pnl_after = decision.sl_price or position.sl_price or 0
        log.info("Position modified: %s %s SL→%.6f TP→%s",
                 position.direction, symbol, pnl_after, decision.tp_prices)

        return ExecutionResult(
            success=True,
            symbol=symbol,
            status="MODIFIED",
            error=(
                f"SL placed={'✅' if sl_placed else '❌'}, "
                f"TP placed={tp_placed}/{len(decision.tp_prices) if decision.tp_prices else 0}"
            ),
        )
