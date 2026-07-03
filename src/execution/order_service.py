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

    async def execute(self, signal_id: int, decision: TradeDecision) -> ExecutionResult:
        """Execute a trade decision and return the result."""
        # Save decision
        decision_id = self.decision_repo.save(signal_id, decision)

        if decision.action == "SKIP":
            return ExecutionResult(success=True, status="SKIPPED",
                                   error=f"Skipped: {decision.reason}")

        if decision.action == "CLOSE":
            return await self._close_position(decision_id, decision)

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

        # Set futures leverage before placing the entry order
        if decision.leverage > 1:
            await self.exchange.set_symbol_leverage(symbol, decision.leverage)
            await self.exchange.set_margin_type(symbol, "ISOLATED")

        # Validate minimum notional (Binance requirement)
        price_ref = decision.entry_price or 0
        min_notional = self.config.get("risk", {}).get("min_notional_usdt", 1.0)
        if quantity * max(price_ref, 1.0) < min_notional:
            return ExecutionResult(
                success=False, status="REJECTED",
                error=f"Order below min notional: {quantity} * {price_ref} < {min_notional} USDT",
            )

        # Place entry order
        if decision.order_type == "MARKET":
            order = await self.exchange.market_buy(symbol, quantity) if side == "BUY" \
                else await self.exchange.market_sell(symbol, quantity)
        else:
            price = decision.entry_price or 0
            order = await self.exchange.limit_buy(symbol, quantity, price) if side == "BUY" \
                else await self.exchange.limit_sell(symbol, quantity, price)

        # Save order
        order_db_id = self.order_repo.save(
            decision_id=decision_id, exchange=self.exchange.name,
            symbol=symbol, side=side, order_type=decision.order_type,
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
        order = await self.exchange.market_sell(symbol, position.quantity) if side == "SELL" \
            else await self.exchange.market_buy(symbol, position.quantity)

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
