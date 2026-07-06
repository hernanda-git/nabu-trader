"""Orchestrator — wires listener → agent → executor → state into one pipeline.

On each signal:
  1. Receive raw message
  2. Regex pre-parse → TradeSignal
  3. Safety Gate 1 (pre-LLM): idempotency, cooldown, whitelist
  4. Agent Brain: 1 LLM call → TradeDecision
  5. Safety Gate 2 (post-LLM): clamp size, enforce limits
  6. Order Service: execute via exchange
  7. State: save everything
  8. Notifier: Telegram
  9. Event Bus: emit events
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from src.agent.agent import AgentBrain
from src.agent.gate import SafetyGate1, SafetyGate2
from src.agent.parser import parse_signal
from src.domain.models import Event, ExecutionResult, PendingSignal, TradeDecision, TradeSignal
from src.events.bus import EventBus
from src.exchange.base import Exchange
from src.execution.order_service import OrderService
from src.execution.position_manager import PositionManager
from src.notifier.telegram import TelegramNotifier
from src.state.repositories import (
    DecisionRepository,
    EventRepository,
    OrderRepository,
    PendingSignalRepository,
    PositionRepository,
    SignalRepository,
)

log = logging.getLogger("orchestrator")


class TradeOrchestrator:
    """Central pipeline coordinator for the auto-trade system."""

    def __init__(
        self,
        config: dict,
        exchange: Exchange,
        agent: AgentBrain,
        gate1: SafetyGate1,
        gate2: SafetyGate2,
        order_service: OrderService,
        position_manager: PositionManager,
        notifier: TelegramNotifier,
        signal_repo: SignalRepository,
        decision_repo: DecisionRepository,
        order_repo: OrderRepository,
        position_repo: PositionRepository,
        event_repo: EventRepository,
        event_bus: EventBus,
        pending_signal_repo: PendingSignalRepository | None = None,
    ):
        self.config = config
        self.exchange = exchange
        self.agent = agent
        self.gate1 = gate1
        self.gate2 = gate2
        self.order_service = order_service
        self.position_manager = position_manager
        self.notifier = notifier
        self.signal_repo = signal_repo
        self.decision_repo = decision_repo
        self.order_repo = order_repo
        self.position_repo = position_repo
        self.event_repo = event_repo
        self.event_bus = event_bus
        self.pending_signal_repo = pending_signal_repo
        self._dry_run = not config.get("agent", {}).get("auto_trade", False)

    async def handle_signal(self, message_id: int, channel: str,
                            raw_text: str, has_media: bool = False) -> dict[str, Any]:
        """Process a single signal through the entire pipeline.

        Returns a summary dict of what happened.
        """
        result: dict[str, Any] = {
            "message_id": message_id,
            "channel": channel,
            "action": "unknown",
            "skipped": False,
            "error": None,
        }

        try:
            # ── Step 1: Regex Pre-Parse ────────────────────────────────────
            signal = parse_signal(message_id, channel, raw_text, has_media)
            log.info("Parsed signal: pair=%s dir=%s entry=%s",
                     signal.pair, signal.direction, signal.entry_price)

            self.event_bus.emit(Event("SignalReceived", {
                "message_id": message_id, "pair": signal.pair,
            }))

            # ── Step 2: Safety Gate 1 (pre-LLM) ────────────────────────────
            allowed, reason = self.gate1.check(signal)
            if not allowed:
                log.info("Gate1 rejected: %s", reason)
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = reason
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⏭️ **Skipped** ({reason})"
                )
                return result

            # ── Step 3: Save signal ────────────────────────────────────────
            signal_db_id = self.signal_repo.save(signal)

            # ── Step 4: Fetch balance & Agent Brain (1 LLM call) ────────────
            open_positions = [
                {"pair": p.pair, "direction": p.direction,
                 "entry_price": p.entry_price, "quantity": p.quantity}
                for p in self.position_repo.get_open_positions()
            ]
            # Fetch real balance from exchange for dynamic sizing
            try:
                balance_info = await self.exchange.get_balance()
                balance = {"USDT": balance_info.free_usdt, "Total": balance_info.total_usdt}
            except Exception:
                balance = None
                log.warning("Failed to fetch balance, sizing blind")
            decision = self.agent.decide(signal, open_positions, balance)
            log.info("Decision: %s %s (conf=%.2f, reason=%s)",
                     decision.action, decision.pair, decision.confidence, decision.reason)

            self.event_bus.emit(Event("DecisionCreated", {
                "action": decision.action, "pair": decision.pair,
            }))

            # ── Step 5: Handle MODIFY action (position management) ────────
            if decision.action == "MODIFY":
                log.info("Decision: MODIFY %s (SL=%s, TP=%s, reason=%s)",
                         decision.pair, decision.sl_price, decision.tp_prices, decision.reason)
                exec_result = await self.order_service.execute(signal_db_id, decision)
                result["action"] = "modified"
                result["reason"] = decision.reason
                if exec_result.success:
                    await self.notifier.send_message(
                        f"🔄 **Position modified — {decision.pair}**\n"
                        f"📋 {exec_result.error}\n"
                        f"📝 {decision.reason[:200]}"
                    )
                else:
                    await self.notifier.send_message(
                        f"❌ **Modification failed — {decision.pair}**\n"
                        f"Error: {exec_result.error}"
                    )
                self.signal_repo.mark_processed(message_id, raw_text)
                self.event_bus.emit(Event("PositionModified", {
                    "pair": decision.pair, "reason": decision.reason,
                }))
                return result

            # ── Step 5b: Handle CONDITIONAL action (setup/alert signals) ──
            if decision.action == "CONDITIONAL":
                log.info("Decision: CONDITIONAL %s (trigger=%.6f, reason=%s)",
                         decision.pair, decision.entry_price or 0, decision.reason)
                # Determine condition type from direction + reason
                cond_type = "close_above" if decision.direction == "LONG" else "close_below"
                # Extract timeframe from reason or default to 4h
                import re as _re
                tf_match = _re.search(r'(\d+)([mhdw])', decision.reason or "")
                timeframe = f"{tf_match.group(1)}{tf_match.group(2)}" if tf_match else "4h"
                if self.pending_signal_repo:
                    pending = PendingSignal(
                        pair=decision.pair,
                        direction=decision.direction,
                        condition_type=cond_type,
                        trigger_price=decision.entry_price or 0,
                        timeframe=timeframe,
                        raw_text=raw_text,
                        message_id=message_id,
                    )
                    db_id = self.pending_signal_repo.save(pending)
                    log.info("Pending signal saved (id=%d): %s %s @ %.6f on %s close",
                             db_id, decision.direction, decision.pair, pending.trigger_price, timeframe)
                    await self.notifier.send_message(
                        f"⏳ **Conditional signal saved — {decision.pair}**\n"
                        f"   ├ Direction: `{decision.direction}`\n"
                        f"   ├ Condition: `{cond_type}` `{pending.trigger_price:.8f}`\n"
                        f"   ├ Timeframe: `{timeframe}`\n"
                        f"   └ Reason: {decision.reason[:200]}"
                    )
                else:
                    log.warning("PendingSignalRepository not available — cannot save conditional signal")
                    await self.notifier.send_message(
                        f"⚠️ **Conditional signal detected but not saved**\n"
                        f"PendingSignalRepository not configured.\n"
                        f"`{decision.pair}` `{decision.direction}` trigger={decision.entry_price}"
                    )
                self.signal_repo.mark_processed(message_id, raw_text)
                self.event_bus.emit(Event("ConditionalSignalSaved", {
                    "pair": decision.pair, "trigger": decision.entry_price, "timeframe": timeframe,
                }))
                return result

            # ── Step 6: Early exit for SKIP / CLOSE (LLM declined) ─────
            if decision.action != "ENTER":  # SKIP / CLOSE — no gate needed
                self.signal_repo.mark_processed(message_id, raw_text)
                result["action"] = decision.action.lower()
                result["skipped"] = True
                result["reason"] = decision.reason
                if decision.action == "SKIP" and "Failed to parse" in decision.reason:
                    await self.notifier.send_message(
                        f"🤖 **LLM parse error, skipping** ({decision.reason})"
                    )
                elif decision.action == "SKIP":
                    await self.notifier.send_message(
                        f"⏭️ **Skipped** ({decision.reason})"
                    )
                return result

            # ── Step 6: Safety Gate 2 (post-LLM) — only for ENTER decisions
            bal_for_gate = (balance or {}).get("USDT", None)
            allowed, reason, clamped_decision = self.gate2.check(decision, bal_for_gate)
            if not allowed:
                log.info("Gate2 rejected: %s", reason)
                result["action"] = "rejected"
                result["reason"] = reason
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"🚫 **Trade rejected** ({reason})"
                )
                return result
            decision = clamped_decision

            # ── Step 7: Notify decision ────────────────────────────────────
            await self.notifier.notify_decision(signal, decision)

            # ── Step 7: Execute via exchange ───────────────────────────────
            exec_result = await self.order_service.execute(signal_db_id, decision)
            result["execution"] = {
                "success": exec_result.success,
                "order_id": exec_result.order_id,
                "symbol": exec_result.symbol,
                "price": exec_result.avg_price,
                "quantity": exec_result.filled_quantity,
            }

            # ── Step 8: Notify execution result ────────────────────────────
            await self.notifier.notify_execution(signal, exec_result)

            if exec_result.success:
                result["action"] = "entered"
                self.event_bus.emit(Event("PositionOpened", {
                    "pair": exec_result.symbol,
                    "price": exec_result.avg_price,
                    "quantity": exec_result.filled_quantity,
                }))
            else:
                result["action"] = "failed"
                result["error"] = exec_result.error

            # ── Step 9: Mark processed (idempotency) ───────────────────────
            self.signal_repo.mark_processed(message_id, raw_text)

            # ── Step 10: Log event ─────────────────────────────────────────
            self.event_repo.save_event("PipelineComplete", result)

        except Exception as e:
            log.exception("Pipeline error for message %s", message_id)
            result["action"] = "error"
            result["error"] = str(e)

        return result

    # ── Trigger execution ──────────────────────────────────────────────────

    async def execute_trigger(self, pending: PendingSignal) -> dict:
        """Execute a trade when a conditional signal triggers.

        Calls the LLM with trigger context, applies Gate2 sizing (10% risk),
        places the order via exchange, and notifies the result.
        """
        result: dict = {
            "pair": pending.pair,
            "direction": pending.direction,
            "action": "unknown",
            "error": None,
        }
        try:
            # Build a synthetic signal for the agent
            signal = TradeSignal(
                message_id=pending.message_id or 0,
                channel="condition_trigger",
                raw_text=pending.raw_text,
                pair=pending.pair,
                direction=pending.direction,
                entry_price=pending.trigger_price,
            )

            # Fetch balance + open positions for LLM context
            open_positions = [
                {"pair": p.pair, "direction": p.direction,
                 "entry_price": p.entry_price, "quantity": p.quantity}
                for p in self.position_repo.get_open_positions()
            ]
            try:
                balance_info = await self.exchange.get_balance()
                balance = {"USDT": balance_info.free_usdt, "Total": balance_info.total_usdt}
            except Exception:
                balance = None
                log.warning("Failed to fetch balance for trigger entry")

            # LLM decides (ENTER / SKIP / CLOSE with sizing)
            decision = self.agent.decide(signal, open_positions, balance)
            log.info("Trigger decision: %s %s (conf=%.2f, reason=%s)",
                     decision.action, decision.pair, decision.confidence, decision.reason)

            if decision.action != "ENTER":
                await self.notifier.send_message(
                    f"⏭️ **Trigger skipped** — {decision.pair}\n"
                    f"LLM declined entry: {decision.reason}"
                )
                result["action"] = "skipped"
                result["reason"] = decision.reason
                return result

            # Gate2 enforces 10% risk / position sizing
            bal_for_gate = (balance or {}).get("USDT", None)
            allowed, reason, clamped_decision = self.gate2.check(decision, bal_for_gate)
            if not allowed:
                log.info("Gate2 rejected trigger: %s", reason)
                await self.notifier.send_message(
                    f"🚫 **Trigger rejected by safety gate**\n{reason}"
                )
                result["action"] = "rejected"
                result["reason"] = reason
                return result
            decision = clamped_decision

            # Save a synthetic signal + decision to DB
            signal_db_id = self.signal_repo.save(signal)

            # Execute
            exec_result = await self.order_service.execute(signal_db_id, decision)
            result["execution"] = {
                "success": exec_result.success,
                "order_id": exec_result.order_id,
                "symbol": exec_result.symbol,
                "price": exec_result.avg_price,
                "quantity": exec_result.filled_quantity,
            }

            if exec_result.success:
                result["action"] = "entered"
                await self.notifier.send_message(
                    f"🚀 **Trigger ENTERED — {exec_result.symbol}**\n"
                    f"   ├ Direction: `{decision.direction}`\n"
                    f"   ├ Entry: `{exec_result.avg_price:.8f}`\n"
                    f"   ├ Qty: `{exec_result.filled_quantity:.6f}`\n"
                    f"   ├ SL: `{decision.sl_price}`\n"
                    f"   ├ TP: `{decision.tp_prices}`\n"
                    f"   ├ Leverage: `{decision.leverage}x`\n"
                    f"   └ Reason: {decision.reason[:200]}"
                )
                self.event_bus.emit(Event("TriggerEntered", {
                    "pair": exec_result.symbol, "price": exec_result.avg_price,
                    "quantity": exec_result.filled_quantity,
                }))
            else:
                result["action"] = "failed"
                result["error"] = exec_result.error
                await self.notifier.send_message(
                    f"❌ **Trigger entry failed** — {exec_result.symbol}\n"
                    f"Error: {exec_result.error}"
                )

            self.event_repo.save_event("TriggerExecuted", result)

        except Exception as e:
            log.exception("Trigger execution error for %s", pending.pair)
            result["action"] = "error"
            result["error"] = str(e)
            await self.notifier.send_message(
                f"❌ **Trigger execution error** — {pending.pair}\n`{e}`"
            )

        return result
