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
from src.domain.models import Event, ExecutionResult, TradeDecision, TradeSignal
from src.events.bus import EventBus
from src.exchange.base import Exchange
from src.execution.order_service import OrderService
from src.execution.position_manager import PositionManager
from src.notifier.telegram import TelegramNotifier
from src.state.repositories import (
    DecisionRepository,
    EventRepository,
    OrderRepository,
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

            # ── Step 5: Early exit for SKIP (LLM declined or parse failure) ─
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
