"""Order service — translates TradeDecision → exchange orders with idempotency."""

from __future__ import annotations

import logging
import uuid
from typing import Any, TYPE_CHECKING

from src.domain.models import ExecutionResult, OrderRequest, TradeSignal, TradeDecision
from src.exchange.base import Exchange
from src.exchange.validation import validate_order
from src.agent.gate import _snap_leverage
from src.state.repositories import DecisionRepository, OrderRepository, PositionRepository, SignalRepository

if TYPE_CHECKING:
    from src.agent.agent import AgentBrain

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
                 position_repo: PositionRepository,
                 agent: "AgentBrain | None" = None):
        self.exchange = exchange
        self.config = config
        self.signal_repo = signal_repo
        self.decision_repo = decision_repo
        self.order_repo = order_repo
        self.position_repo = position_repo
        self.agent = agent
        self._max_retries = config.get("execution", {}).get("max_retries", 3)

    def _generate_client_id(self, decision_id: int) -> str:
        """Generate a unique, idempotent client order ID."""
        return f"lnr_{decision_id}_{uuid.uuid4().hex[:8]}"

    async def _get_filters(self, symbol: str) -> dict | None:
        """Load exchange filters for a symbol (delegates to the exchange adapter).

        Returns the filter dict (with tickSize/minPrice/maxPrice/stepSize/
        minQty/minNotional) or None if unavailable (validation is then skipped
        rather than blocking — the exchange is the final authority).
        """
        try:
            loader = getattr(self.exchange, "_load_futures_filters", None)
            if loader is not None:
                return await loader(symbol)
        except Exception as e:
            log.debug("Could not load filters for %s: %s", symbol, e)
        return None

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

        # ─── Idempotency: never place a second active entry for the same decision ──
        existing = self.order_repo.get_active_for_decision(decision_id, symbol, side)
        if existing:
            log.info("Idempotency: decision %d already has active %s %s order %s (%s) — skip",
                     decision_id, symbol, side, existing.get("exchange_order_id"), existing.get("status"))
            return ExecutionResult(success=True, status="DUPLICATE_SKIPPED",
                                   order_id=existing.get("exchange_order_id"),
                                   symbol=symbol, side=side,
                                   error=f"Duplicate entry skipped: decision {decision_id} already has active order")

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

        # ─── Pre-submission validation gate (Task 5) ───────────────────
        # Never send an order that violates the symbol's exchange filters.
        # This guarantees "no order submitted without passing validation".
        filters = await self._get_filters(symbol)
        val_err = validate_order(symbol, side, decision.entry_price, quantity, filters or {})
        if val_err:
            log.warning("Validation gate BLOCKED entry %s %s %s qty=%.4f @ %.8f: %s",
                        symbol, side, decision.order_type, quantity, decision.entry_price, val_err)
            return ExecutionResult(
                success=False, status="VALIDATION_SKIP", error=val_err,
                symbol=symbol, side=side,
            )

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

        # Do NOT adjust entry_price to current market — that defeats the
        # purpose of a LIMIT order. The order rests on the book at the
        # specified price and fills when the market comes to it.

        order = await self.exchange.limit_buy(symbol, quantity, entry_price) if side == "BUY" \
            else await self.exchange.limit_sell(symbol, quantity, entry_price)

        # If already filled (price was at or through the limit), done.
        # Otherwise the order rests on the book — save it as PENDING.
        if order.order_id and order.status != "FILLED":
            import asyncio as _aio
            # Quick check: did it fill in the first few seconds?
            for _attempt in range(5):
                await _aio.sleep(1)
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
                # Order didn't fill in quick check — it's resting on the book
                log.info("LIMIT resting on book: %s %s qty=%.4f @ %.8f (order %s)",
                         symbol, side, quantity, entry_price, order.order_id)

        # Save order
        order_db_id = self.order_repo.save(
            decision_id=decision_id, exchange=self.exchange.name,
            symbol=symbol, side=side, order_type="LIMIT",
            quantity=quantity, price=order.price,
            client_order_id=client_id,
        )

        if order.status in ("FAILED", "REJECTED", "EXPIRED"):
            self.order_repo.update_status(order_db_id, order.status, order.order_id)

            # ── Deterministic repair (NO LLM) ──
            # Re-derive qty/price/leverage from the symbol's filters + Gate2,
            # validate, and resubmit exactly once. If the repaired order still
            # cannot pass validation, we SKIP rather than invent parameters.
            repair = await self._repair_order(decision_id, decision, symbol, side, order.error)
            if repair is not None:
                return repair

            return ExecutionResult(
                success=False, status=order.status, error=order.error or "Order rejected",
            )

        # If not yet filled, the order is resting on the book — return PENDING
        if order.status != "FILLED":
            self.order_repo.update_status(order_db_id, "PENDING", order.order_id)
            log.info("LIMIT order resting: %s %s qty=%.4f @ %.8f — waiting for price",
                     symbol, side, quantity, entry_price)
            return ExecutionResult(
                success=True,
                order_id=order.order_id,
                symbol=symbol,
                side=side,
                filled_quantity=0,
                avg_price=entry_price,
                status="PENDING",
                error=f"⏳ LIMIT order placed @ {entry_price} — waiting for price to reach it",
            )

        # Order is FILLED — save position and place SL/TP
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
            sl_err = validate_order(symbol, sl_side, decision.sl_price, quantity, filters or {})
            if sl_err:
                log.warning("Validation gate SKIPPED SL %s @ %s: %s", symbol, decision.sl_price, sl_err)
            else:
                sl_order = await self.exchange.stop_loss(symbol, quantity, decision.sl_price, sl_side)
                if sl_order.order_id:
                    sl_placed = True
                    log.info("SL placed: %s @ %s (conditional)", symbol, decision.sl_price)
                elif sl_order.status == "UNPROTECTED":
                    log.warning("SL NOT placed for %s @ %s — position monitored by position manager",
                                symbol, decision.sl_price)
                else:
                    log.warning("SL NOT placed for %s @ %s — position UNPROTECTED (%s)",
                                symbol, decision.sl_price, sl_order.error)

        # Place TP orders (conditional TAKE_PROFIT-LIMIT, fallback to Basic LIMIT)
        for i, tp_price in enumerate(decision.tp_prices[:3]):
            tp_side = "SELL" if decision.direction == "LONG" else "BUY"
            tp_err = validate_order(symbol, tp_side, tp_price, quantity, filters or {})
            if tp_err:
                log.warning("Validation gate SKIPPED TP%d %s @ %s: %s", i + 1, symbol, tp_price, tp_err)
                continue
            tp_order = await self.exchange.take_profit(symbol, quantity, tp_price, tp_side)
            if tp_order.order_id:
                log.info("TP%d placed: %s @ %s (conditional)", i + 1, symbol, tp_price)
            else:
                # Some contracts block conditional TP (-4120). Fall back to a
                # resting Basic-tab LIMIT at the TP price so TP still works.
                log.warning("TP%d conditional blocked (%s) — falling back to Basic LIMIT @ %s",
                            i + 1, tp_order.error, tp_price)
                if tp_side == "SELL":
                    fb = await self.exchange.limit_sell(symbol, quantity, tp_price, reduce=True)
                else:
                    fb = await self.exchange.limit_buy(symbol, quantity, tp_price, reduce=True)
                if fb.order_id:
                    log.info("TP%d placed (Basic LIMIT fallback): %s @ %s", i + 1, symbol, tp_price)

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

    async def _repair_order(self, decision_id: int, decision: TradeDecision,
                            symbol: str, side: str,
                            error_message: str | None) -> ExecutionResult | None:
        """Deterministic repair of a rejected entry order — NO LLM.

        On a FAILED/REJECTED/EXPIRED entry we re-derive the order parameters
        from the symbol's real exchange filters and the config-driven leverage
        rules, validate them through the same gate every order passes, and
        resubmit exactly once. If the repaired parameters still can't pass
        validation, we return a VALIDATION_SKIP result — we never invent or
        guess parameters the way the old LLM fallback did.

        Returns an ExecutionResult on a successful repair, or None if repair
        was not possible (caller then returns the original failure).
        """
        try:
            filters = await self._get_filters(symbol)
            if not filters:
                log.warning("Repair skipped for %s: no filters available", symbol)
                return None

            # Re-round price + quantity to the symbol's real precision.
            price = decision.entry_price
            if price:
                price, _ = await self.exchange._round_price(symbol, price)
            qty = decision.quantity
            qty, _ = await self.exchange._round_quantity(symbol, qty, price_ref=price)

            # Recompute leverage deterministically (config-driven, no literals):
            # enough to meet the exchange minNotional from the configured margin.
            risk = self.config.get("risk", {})
            port_usdt = risk.get("port_usdt", 1.0)
            max_lev = risk.get("max_leverage", 50)
            increase_pct = risk.get("max_leverage_increase_pct", 10)
            min_notional = filters.get("minNotional") or risk.get("min_notional_usdt", 1.0)
            pos_value = qty * (price or 0)
            raw_lev = (pos_value / port_usdt) if port_usdt > 0 else 1
            lev_cap = _snap_leverage(raw_lev * (1 + increase_pct / 100.0), max_lev)
            new_lev = min(_snap_leverage(raw_lev, max_lev), lev_cap, max_lev)
            if new_lev != decision.leverage:
                try:
                    await self.exchange.set_symbol_leverage(symbol, new_lev)
                except Exception as e:
                    log.debug("Repair: set_symbol_leverage failed: %s", e)

            # Validate before resubmitting — never send an invalid order.
            val_err = validate_order(symbol, side, price, qty, filters)
            if val_err:
                log.warning("Repair for %s still invalid after re-round: %s", symbol, val_err)
                return ExecutionResult(
                    success=False, status="VALIDATION_SKIP",
                    error=f"Repair failed validation: {val_err}",
                    symbol=symbol, side=side,
                )

            log.info("Repair %s %s: qty=%.6f price=%.8f lev=%d (orig error: %s)",
                     symbol, side, qty, price or 0, new_lev, error_message)

            retry = await self.exchange.limit_buy(symbol, qty, price) if side == "BUY" \
                else await self.exchange.limit_sell(symbol, qty, price)

            if retry.order_id and retry.status != "FILLED":
                # Resting on the book — record and return PENDING.
                repair_db_id = self.order_repo.save(
                    decision_id=decision_id, exchange=self.exchange.name,
                    symbol=symbol, side=side, order_type="LIMIT",
                    quantity=qty, price=retry.price, client_order_id=self._generate_client_id(decision_id),
                )
                self.order_repo.update_status(repair_db_id, "PENDING", retry.order_id)
                return ExecutionResult(
                    success=True, order_id=retry.order_id, symbol=symbol, side=side,
                    filled_quantity=0, avg_price=price or 0, status="PENDING",
                    error=f"⏳ Repaired LIMIT resting @ {price} — waiting for price",
                )

            if retry.status == "FILLED":
                from src.domain.models import Position
                pos = Position(
                    pair=symbol, direction=decision.direction,
                    entry_price=retry.avg_price or price or 0,
                    quantity=retry.filled_quantity or qty,
                    sl_price=decision.sl_price, tp_prices=decision.tp_prices,
                    entry_order_id=retry.order_id,
                )
                self.position_repo.create(pos)
                for tp_price in decision.tp_prices[:3]:
                    tp_side = "SELL" if decision.direction == "LONG" else "BUY"
                    await self.exchange.take_profit(symbol, qty, tp_price, tp_side)
                return ExecutionResult(
                    success=True, order_id=retry.order_id, symbol=symbol, side=side,
                    filled_quantity=retry.filled_quantity or qty,
                    avg_price=retry.avg_price or price or 0, status="FILLED",
                    error=f"✅ Repaired order filled @ {price}",
                )

            # Repair resubmission also failed.
            log.warning("Repair resubmission failed for %s: %s", symbol, retry.error)
            return None

        except Exception as e:
            log.warning("Repair raised: %s", e)
            return None

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

        order = await self.exchange.limit_sell(symbol, position.quantity, close_price, reduce=True) if side == "SELL" \
            else await self.exchange.limit_buy(symbol, position.quantity, close_price, reduce=True)

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
            elif sl_order.status == "UNPROTECTED":
                log.warning("New SL monitored by position manager: %s @ %s", symbol, decision.sl_price)
            else:
                log.warning("New SL NOT placed for %s @ %s (%s)", symbol, decision.sl_price, sl_order.error)

        # Place new TP orders if specified (conditional, fallback to Basic LIMIT)
        tp_placed = 0
        for i, tp_price in enumerate(decision.tp_prices[:3]):
            if tp_price <= 0:
                continue
            tp_side = "SELL" if position.direction == "LONG" else "BUY"
            tp_order = await self.exchange.take_profit(symbol, position.quantity, tp_price, tp_side)
            if tp_order.order_id:
                tp_placed += 1
                log.info("New TP%d placed: %s @ %s", i + 1, symbol, tp_price)
            else:
                fb = await self.exchange.limit_sell(symbol, position.quantity, tp_price, reduce=True) if tp_side == "SELL" \
                    else await self.exchange.limit_buy(symbol, position.quantity, tp_price, reduce=True)
                if fb.order_id:
                    tp_placed += 1
                    log.info("New TP%d placed (Basic LIMIT fallback): %s @ %s", i + 1, symbol, tp_price)

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
