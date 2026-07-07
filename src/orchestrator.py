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
from src.agent.ta_context import fetch_ta_context
from src.domain.models import (
    ConfigSnapshot,
    Event,
    ExecutionResult,
    LLMInteraction,
    PendingSignal,
    PositionEvent,
    TradeDecision,
    TradeLogEntry,
    TradeSignal,
)
from src.events.bus import EventBus
from src.exchange.base import Exchange
from src.execution.order_service import OrderService
from src.execution.position_manager import PositionManager
from src.notifier.telegram import TelegramNotifier
from src.state.repositories import (
    ConfigSnapshotRepository,
    DecisionRepository,
    EventRepository,
    LLMInteractionRepository,
    OrderRepository,
    PendingSignalRepository,
    PositionEventRepository,
    PositionRepository,
    SignalRepository,
    TradeLogRepository,
)

from src.api.webhook import emit_event

from src.exchange.symbol_registry import get_registry

log = logging.getLogger("orchestrator")

import hashlib
import json as json_mod
import uuid


def _generate_correlation_id() -> str:
    """Generate a unique correlation ID for a pipeline run."""
    return uuid.uuid4().hex[:12]


def _config_hash(config: dict) -> str:
    """Compute a hash of the effective config (excluding secrets)."""
    # Strip secrets for hashing
    safe = dict(config)
    if "exchange" in safe:
        safe["exchange"] = {k: v for k, v in safe["exchange"].items() if k != "binance"}
    return hashlib.sha256(json_mod.dumps(safe, sort_keys=True, default=str).encode()).hexdigest()[:16]


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
        llm_repo: LLMInteractionRepository | None = None,
        trade_log_repo: TradeLogRepository | None = None,
        position_event_repo: PositionEventRepository | None = None,
        config_snapshot_repo: ConfigSnapshotRepository | None = None,
        event_bus: EventBus | None = None,
        pending_signal_repo: PendingSignalRepository | None = None,
        app_version: str = "",
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
        self.llm_repo = llm_repo
        self.trade_log_repo = trade_log_repo
        self.position_event_repo = position_event_repo
        self.config_snapshot_repo = config_snapshot_repo
        self.event_bus = event_bus
        self.pending_signal_repo = pending_signal_repo
        self.app_version = app_version
        self._dry_run = not config.get("agent", {}).get("auto_trade", False)

    def _log(self, correlation_id: str, level: str, module: str,
             message: str, metadata: dict | None = None) -> None:
        """Write a structured trade log entry if the repo is available."""
        if self.trade_log_repo:
            self.trade_log_repo.log(correlation_id, level, module, message, metadata)

    def _save_llm_interaction(self, decision_db_id: int) -> None:
        """Save the last LLM interaction from the agent brain into the DB."""
        if not self.llm_repo:
            return
        li = getattr(self.agent, '_last_interaction', None)
        if not li:
            return
        self.llm_repo.save(LLMInteraction(
            decision_id=decision_db_id,
            model=li.get("model", ""),
            system_prompt=li.get("system_prompt", ""),
            user_prompt=li.get("user_prompt", ""),
            raw_response=li.get("raw_response", ""),
            parsed_decision_json=li.get("parsed_decision_json", "{}"),
            prompt_tokens=li.get("prompt_tokens", 0),
            completion_tokens=li.get("completion_tokens", 0),
            latency_ms=li.get("latency_ms", 0),
            success=li.get("success", True),
            error=li.get("error"),
        ))

    def _save_config_snapshot(self, correlation_id: str) -> int | None:
        """Save a snapshot of the current config. Returns snapshot ID or None."""
        if not self.config_snapshot_repo:
            return None
        cfg_hash = _config_hash(self.config)
        import yaml
        cfg_yaml = yaml.safe_dump(self.config, default_flow_style=False, sort_keys=False)
        snap_id = self.config_snapshot_repo.save(
            config_hash=cfg_hash,
            config_yaml=cfg_yaml,
            app_version=self.app_version,
        )
        self._log(correlation_id, "INFO", "orchestrator",
                  f"Config snapshot saved (id={snap_id}, hash={cfg_hash})",
                  {"snap_id": snap_id, "config_hash": cfg_hash, "version": self.app_version})
        return snap_id

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

        # Generate a unique correlation ID for this pipeline run
        correlation_id = _generate_correlation_id()
        result["correlation_id"] = correlation_id

        try:
            # ── Step 0: Idempotent claim ────────────────────────────────────
            # Prevent double-processing / double Telegram notifications when the
            # same message_id is delivered more than once (channel post + an
            # immediate edit event on the same id, a reconnect replay, etc.).
            # The first delivery claims the id and proceeds; all later ones are
            # skipped here — before any LLM call or notification.
            if not self.signal_repo.claim(message_id, raw_text):
                log.info("Message %s already claimed — skipping duplicate delivery", message_id)
                result["action"] = "duplicate"
                result["skipped"] = True
                return result

            # ── Step 1: Regex Pre-Parse ────────────────────────────────────
            signal = parse_signal(message_id, channel, raw_text, has_media)
            log.info("Parsed signal: pair=%s dir=%s entry=%s",
                     signal.pair, signal.direction, signal.entry_price)

            # Enrich with symbol metadata from SymbolRegistry
            sym_meta = {}
            if signal.pair:
                registry = get_registry()
                if registry and registry.is_ready:
                    info = registry.get_symbol_info(signal.pair)
                    if info:
                        sym_meta = {
                            "base_asset": info.base_asset,
                            "display_pair": info.display_symbol(),
                            "is_1000x": info.is_1000x,
                            "price_precision": info.price_precision,
                            "quantity_precision": info.quantity_precision,
                            "min_notional": info.min_notional,
                            "tick_size": info.tick_size,
                            "step_size": info.step_size,
                            "contract_type": info.contract_type,
                        }

            self._log(correlation_id, "INFO", "orchestrator",
                      f"Signal received: pair={signal.pair} dir={signal.direction}",
                      {"message_id": message_id, "pair": signal.pair,
                       "direction": signal.direction, "symbol_meta": sym_meta})

            if self.event_bus:
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
                self._log(correlation_id, "INFO", "gate1",
                          f"Gate1 rejected: {reason}",
                          {"reason": reason, "pair": signal.pair})
                self.signal_repo.mark_processed(message_id, raw_text)
                # Gate1 rejections are expected noise from unparseable/vague messages
                # — do NOT notify Telegram for these to avoid spam
                return result

            # ── Step 3: Collect context for skip notifications ─────────────────
            open_positions = [
                {"pair": p.pair, "direction": p.direction,
                 "entry_price": p.entry_price, "quantity": p.quantity}
                for p in self.position_repo.get_open_positions()
            ]
            pending_rows = []
            if self.pending_signal_repo:
                pending_rows = self.pending_signal_repo.get_pending()
            try:
                balance_info = await self.exchange.get_balance()
                balance = {"USDT": balance_info.free_usdt, "Total": balance_info.total_usdt}
            except Exception:
                balance = None
                log.warning("Failed to fetch balance, sizing blind")

            # ── Step 3b: Save signal ────────────────────────────────────────
            signal_db_id = self.signal_repo.save(signal)

            # ── Step 4: Agent Brain (1 LLM call) ────────────────────────────
            # Fetch technical context only when the signal has no SL/TP
            # so the LLM can anchor on real support/resistance/ATR levels
            # instead of guessing from training-data heuristics.
            ta_ctx: str | None = None
            if signal.sl_price is None or not signal.tp_prices:
                try:
                    ta_ctx = await fetch_ta_context(
                        self.exchange, signal.pair or "", timeframe="4h"
                    )
                    if ta_ctx:
                        log.info("TA context fetched for %s (%d chars)",
                                 signal.pair, len(ta_ctx))
                except Exception:
                    log.warning("Failed to fetch TA context for %s", signal.pair,
                                exc_info=True)

            decision = self.agent.decide(signal, open_positions, balance, ta_ctx)
            log.info("Decision: %s %s (conf=%.2f, reason=%s)",
                     decision.action, decision.pair, decision.confidence, decision.reason)
            self._log(correlation_id, "INFO", "orchestrator",
                      f"Decision: {decision.action} {decision.pair} conf={decision.confidence:.2f}",
                      {"action": decision.action, "pair": decision.pair,
                       "direction": decision.direction, "confidence": decision.confidence,
                       "reason": decision.reason})

            if self.event_bus:
                self.event_bus.emit(Event("DecisionCreated", {
                    "action": decision.action, "pair": decision.pair,
                }))

            # ── Step 4b: Save decision + LLM interaction ─────────────────
            decision_db_id = self.decision_repo.save(signal_db_id, decision)
            self._save_llm_interaction(decision_db_id)

            # ── Step 5: Handle MODIFY action (position management) ────────
            if decision.action == "MODIFY":
                log.info("Decision: MODIFY %s (SL=%s, TP=%s, reason=%s)",
                         decision.pair, decision.sl_price, decision.tp_prices, decision.reason)
                self._log(correlation_id, "INFO", "orchestrator",
                          f"MODIFY {decision.pair}: SL={decision.sl_price} TP={decision.tp_prices}",
                          {"action": "MODIFY", "pair": decision.pair,
                           "sl_price": decision.sl_price, "tp_prices": decision.tp_prices,
                           "reason": decision.reason})
                exec_result = await self.order_service.execute(
                    signal_db_id, decision, decision_id=decision_db_id)
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
                        exec_result.error or f"❌ **Modification failed — {decision.pair}**"
                    )
                self.signal_repo.mark_processed(message_id, raw_text)
                if self.event_bus:
                    self.event_bus.emit(Event("PositionModified", {
                        "pair": decision.pair, "reason": decision.reason,
                    }))
                return result

            # ── Step 5b: Handle CONDITIONAL action (setup/alert signals) ──
            if decision.action == "CONDITIONAL":
                log.info("Decision: CONDITIONAL %s (trigger=%.6f, reason=%s)",
                         decision.pair, decision.entry_price or 0, decision.reason)
                self._log(correlation_id, "INFO", "orchestrator",
                          f"CONDITIONAL {decision.pair} @ {decision.entry_price}",
                          {"action": "CONDITIONAL", "pair": decision.pair,
                           "trigger_price": decision.entry_price, "reason": decision.reason})
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
                if self.event_bus:
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
                self._log(correlation_id, "INFO", "orchestrator",
                          f"{decision.action} {decision.pair}: {decision.reason[:100]}",
                          {"action": decision.action, "pair": decision.pair,
                           "reason": decision.reason})
                if decision.action == "SKIP":
                    reason_text = decision.reason or "no reason provided"
                    pos_lines = "\n".join(
                        f"  • `{p['pair']}` {p['direction']} @ `{p['entry_price']}` × `{p['quantity']}`"
                        for p in open_positions
                    ) or "  (none)"
                    pend_lines = "\n".join(
                        f"  • `{ps.pair}` {ps.direction} `{ps.condition_type}` `{ps.trigger_price}` ({ps.timeframe})"
                        for ps in pending_rows
                    ) or "  (none)"
                    bal_str = f"`{balance['USDT']:.2f}` USDT" if balance else "(unavailable)"
                    skip_text = (
                        f"⏭️ **Skipped**\n"
                        f"Message: {raw_text[:300]}\n"
                        f"Reason to Skip: {reason_text}\n"
                        f"Current Positions:\n{pos_lines}\n"
                        f"Current Pendings:\n{pend_lines}\n"
                        f"Balance: {bal_str}"
                    )
                    await self.notifier.send_message(skip_text)
                elif decision.action == "CLOSE":
                    await self.notifier.send_message(
                        f"🔴 **Close signal received** `{signal.pair}` — processing"
                    )
                return result

            # ── Step 6: Safety Gate 2 (post-LLM) — only for ENTER decisions
            bal_for_gate = (balance or {}).get("USDT", None)
            allowed, reason, clamped_decision = self.gate2.check(decision, bal_for_gate)
            if not allowed:
                log.info("Gate2 rejected: %s", reason)
                result["action"] = "rejected"
                result["reason"] = reason
                self._log(correlation_id, "WARNING", "gate2",
                          f"Gate2 rejected {decision.pair}: {reason[:200]}",
                          {"action": "ENTER", "pair": decision.pair, "reason": reason})
                self.signal_repo.mark_processed(message_id, raw_text)
                pos_lines = "\n".join(
                    f"  • `{p['pair']}` {p['direction']} @ `{p['entry_price']}` × `{p['quantity']}`"
                    for p in open_positions
                ) or "  (none)"
                pend_lines = "\n".join(
                    f"  • `{ps.pair}` {ps.direction} `{ps.condition_type}` `{ps.trigger_price}` ({ps.timeframe})"
                    for ps in pending_rows
                ) or "  (none)"
                bal_str = f"`{balance['USDT']:.2f}` USDT" if balance else "(unavailable)"
                reject_text = (
                    f"🚫 **Trade Rejected**\n"
                    f"Message: {raw_text[:300]}\n"
                    f"Reason: {reason}\n"
                    f"Current Positions:\n{pos_lines}\n"
                    f"Current Pendings:\n{pend_lines}\n"
                    f"Balance: {bal_str}"
                )
                await self.notifier.send_message(reject_text)
                return result
            decision = clamped_decision
            self._log(correlation_id, "INFO", "gate2",
                      f"Gate2 passed: {decision.pair} qty={decision.quantity:.4f} lev={decision.leverage}x",
                      {"pair": decision.pair, "quantity": decision.quantity,
                       "leverage": decision.leverage, "sl": decision.sl_price,
                       "tp": decision.tp_prices})

            # ── Step 6b: Snapshot config on ENTER ─────────────────────────
            config_snap_id = self._save_config_snapshot(correlation_id)
            if config_snap_id is not None and self.position_repo:
                # Store config_snapshot_id for the upcoming position
                pass  # We'll update the position after it's created

            # ── Step 7: Notify decision ────────────────────────────────────
            await self.notifier.notify_decision(signal, decision)

            # ── Step 8: Execute via exchange ───────────────────────────────
            exec_result = await self.order_service.execute(
                signal_db_id, decision, decision_id=decision_db_id)
            result["execution"] = {
                "success": exec_result.success,
                "order_id": exec_result.order_id,
                "symbol": exec_result.symbol,
                "price": exec_result.avg_price,
                "quantity": exec_result.filled_quantity,
            }

            # ── Step 9: Notify execution result ────────────────────────────
            await self.notifier.notify_execution(signal, exec_result)

            if exec_result.success:
                result["action"] = "entered"
                if self.event_bus:
                    self.event_bus.emit(Event("PositionOpened", {
                        "pair": exec_result.symbol,
                        "price": exec_result.avg_price,
                        "quantity": exec_result.filled_quantity,
                    }))
                self._log(correlation_id, "INFO", "orchestrator",
                          f"Position opened: {decision.direction} {exec_result.symbol} "
                          f"qty={exec_result.filled_quantity:.4f} @ {exec_result.avg_price:.8f}",
                          {"pair": exec_result.symbol, "direction": decision.direction,
                           "quantity": exec_result.filled_quantity,
                           "price": exec_result.avg_price,
                           "order_id": exec_result.order_id,
                           "sl": decision.sl_price, "tp": decision.tp_prices,
                           "leverage": decision.leverage})
                # Log position event for the opened position
                if self.position_event_repo:
                    try:
                        open_pos = self.position_repo.get_open_by_pair(exec_result.symbol)
                        if open_pos and open_pos.id:
                            self.position_event_repo.save_event(
                                position_id=open_pos.id,
                                event_type="POSITION_OPENED",
                                details=f"Entered {decision.direction} @ {exec_result.avg_price:.8f} qty={exec_result.filled_quantity:.4f}",
                                metadata={
                                    "pair": exec_result.symbol,
                                    "direction": decision.direction,
                                    "entry_price": exec_result.avg_price,
                                    "quantity": exec_result.filled_quantity,
                                    "sl": decision.sl_price,
                                    "tp": decision.tp_prices,
                                    "leverage": decision.leverage,
                                    "config_snapshot_id": config_snap_id,
                                    "correlation_id": correlation_id,
                                },
                            )
                    except Exception as e:
                        log.debug("Failed to log position event: %s", e)

                # Emit webhook for position opened
                try:
                    await emit_event(
                        "TRADE_ENTERED",
                        {
                            "pair": exec_result.symbol,
                            "direction": decision.direction,
                            "entry_price": exec_result.avg_price,
                            "quantity": exec_result.filled_quantity,
                            "sl": decision.sl_price,
                            "tp": decision.tp_prices,
                            "leverage": decision.leverage,
                            "order_id": exec_result.order_id,
                        },
                        correlation_id=correlation_id,
                        config=self.config,
                    )
                except Exception as e:
                    log.debug("Webhook emit failed (non-blocking): %s", e)
            else:
                result["action"] = "failed"
                result["error"] = exec_result.error
                self._log(correlation_id, "ERROR", "orchestrator",
                          f"Order failed for {exec_result.symbol}: {exec_result.error}",
                          {"pair": exec_result.symbol, "error": exec_result.error,
                           "order_id": exec_result.order_id})

            # ── Step 10: Mark processed (idempotency) ──────────────────────
            self.signal_repo.mark_processed(message_id, raw_text)

            # ── Step 11: Log event ─────────────────────────────────────────
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
            # The trigger already has entry_price but may lack SL/TP —
            # fetch TA context using the pending signal's timeframe.
            ta_ctx: str | None = None
            if signal.sl_price is None or not signal.tp_prices:
                try:
                    ta_ctx = await fetch_ta_context(
                        self.exchange, signal.pair or "",
                        timeframe=pending.timeframe or "4h",
                    )
                    if ta_ctx:
                        log.info("TA context fetched for trigger %s (%d chars)",
                                 signal.pair, len(ta_ctx))
                except Exception:
                    log.warning("Failed to fetch TA context for trigger %s",
                                signal.pair, exc_info=True)

            decision = self.agent.decide(signal, open_positions, balance, ta_ctx)
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
                    exec_result.error or f"❌ **Trigger entry failed — {exec_result.symbol}**"
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
