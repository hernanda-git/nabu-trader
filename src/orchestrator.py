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
from src.agent.parser import parse_signal, parse_management_command, _MGMT_TP1_PARTIAL, _MGMT_FULL
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



# ── Management-command helpers ────────────────────────────────────────────

def _cmd_word(action: str) -> str:
    """Human-readable command word for user-facing skip messages."""
    return {
        "SL_ENTRY": "sl to entry",
        "TP": "tpN",
        "FULL": "full",
    }.get(action, action.lower())


def _resolve_tp_price(mgmt: "TradeSignal", position: "Position") -> float | None:
    """Resolve the TP price for a tpN command.

    Priority:
      1. Explicit price given in the text (e.g. 'tp1 5.2') — mgmt.tp_prices[0].
      2. The position's stored tp_prices[tp_index] from when it was opened.
    Returns None if neither is available.
    """
    if mgmt.tp_prices:
        return mgmt.tp_prices[0]
    idx = mgmt.tp_index or 0
    stored = position.tp_prices or []
    if 0 <= idx < len(stored) and stored[idx] is not None:
        return stored[idx]
    return None


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
                            raw_text: str, has_media: bool = False,
                            reply_to_message_id: int | None = None,
                            reply_pair: str | None = None) -> dict[str, Any]:
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

            # ── Step 0b: Management command (e.g. "sl to entry", "tp1 booked") ──
            # These are replies to a previous signal. They do NOT go through the
            # LLM — they modify the open position directly.
            mgmt = parse_management_command(message_id, channel, raw_text, reply_pair)
            if mgmt is not None:
                if mgmt.mgmt_action == _MGMT_TP1_PARTIAL:
                    return await self._handle_tp1_booked(
                        correlation_id, message_id, raw_text, mgmt, result
                    )
                if mgmt.mgmt_action == _MGMT_FULL:
                    return await self._handle_full_close(
                        correlation_id, message_id, raw_text, mgmt, result
                    )
                return await self._handle_management(
                    correlation_id, message_id, raw_text, mgmt, result
                )

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

            # ── Step 6: Handle SKIP / CLOSE ──────────────────────────
            if decision.action == "SKIP":
                self.signal_repo.mark_processed(message_id, raw_text)
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = decision.reason
                self._log(correlation_id, "INFO", "orchestrator",
                          f"SKIP {decision.pair}: {decision.reason[:100]}",
                          {"action": "SKIP", "pair": decision.pair,
                           "reason": decision.reason})
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
                return result

            elif decision.action == "CLOSE":
                self._log(correlation_id, "INFO", "orchestrator",
                          f"CLOSE {decision.pair}: {decision.reason[:100]}",
                          {"action": "CLOSE", "pair": decision.pair,
                           "reason": decision.reason})
                await self.notifier.send_message(
                    f"🔴 **Close signal received** `{decision.pair}` — executing..."
                )
                # EXECUTE the close — order_service._close_position handles it
                exec_result = await self.order_service.execute(
                    signal_db_id, decision, decision_id=decision_db_id)
                result["execution"] = {
                    "success": exec_result.success,
                    "order_id": exec_result.order_id,
                    "symbol": exec_result.symbol,
                    "price": exec_result.avg_price,
                    "quantity": exec_result.filled_quantity,
                }
                self.signal_repo.mark_processed(message_id, raw_text)
                if exec_result.success:
                    result["action"] = "closed"
                    await self.notifier.send_message(
                        f"✅ **Position closed** — `{exec_result.symbol}`\n"
                        f"   ├ Exit: `{exec_result.avg_price}`\n"
                        f"   └ PnL: `{exec_result.error or '—'}`"
                    )
                else:
                    result["action"] = "close_failed"
                    result["reason"] = exec_result.error
                    await self.notifier.send_message(
                        f"❌ **Close failed** — `{decision.pair}`\n`{exec_result.error}`"
                    )
                if self.event_bus:
                    self.event_bus.emit(Event("PositionClosed", {
                        "pair": decision.pair, "reason": decision.reason,
                    }))
                return result

            # ── Must be ENTER from here — proceed with Gate2 ────────

            # ── Step 6: Safety Gate 2 (post-LLM) — only for ENTER decisions
            bal_for_gate = (balance or {}).get("USDT", None)
            # Resolve the pair's own Binance max leverage (from the SymbolRegistry
            # that was populated at startup from exchangeInfo leverageBrackets).
            pair_max_leverage = None
            try:
                registry = get_registry()
                if registry and registry.is_ready and decision.pair:
                    info = registry.get_symbol_info(decision.pair)
                    if info is not None:
                        pair_max_leverage = getattr(info, "max_leverage", None)
            except Exception as e:  # noqa: BLE001 — registry lookup is best-effort
                log.debug("Gate2 pair-max-leverage lookup failed: %s", e)
            allowed, reason, clamped_decision = self.gate2.check(
                decision, bal_for_gate, pair_max_leverage=pair_max_leverage)
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
            # Persist the Gate2-clamped size to the decisions row. The row was
            # saved BEFORE Gate2 (step 3), so without this its quantity/leverage
            # stay 0 — breaking analytics and daily_stats. See DecisionRepository.update_quantity.
            try:
                self.decision_repo.update_quantity(
                    decision_db_id, decision.quantity, decision.leverage)
            except Exception as e:  # noqa: BLE001 — never let bookkeeping block trading
                log.warning("Failed to persist clamped quantity for decision %s: %s",
                            decision_db_id, e)
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

                # Notify user — include repair info if deterministically repaired
                if exec_result.error and exec_result.error.startswith("✅ Repaired") and exec_result.status == "FILLED":
                    await self.notifier.send_message(
                        f"✅ **Trade entered (repaired)** — {exec_result.symbol}\n"
                        f"   ├ Qty: `{exec_result.filled_quantity:,.0f}`\n"
                        f"   ├ Entry: `{exec_result.avg_price:.8f}`\n"
                        f"   ├ SL: `{decision.sl_price or '—'}`\n"
                        f"   ├ TP: `{decision.tp_prices or '—'}`\n\n"
                        f"{exec_result.error}"
                    )
                elif exec_result.status == "PENDING":
                    await self.notifier.send_message(
                        f"⏳ **LIMIT order placed — {exec_result.symbol}**\n"
                        f"   ├ Direction: `{decision.direction}`\n"
                        f"   ├ Qty: `{decision.quantity:,.0f}`\n"
                        f"   ├ Entry: `{exec_result.avg_price:.8f}`\n"
                        f"   ├ SL: `{decision.sl_price or '—'}`\n"
                        f"   ├ TP: `{decision.tp_prices or '—'}`\n"
                        f"   └ Waiting for price to reach entry level"
                    )

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

    # ── Management command handling ────────────────────────────────────────

    async def _handle_management(self, correlation_id: str, message_id: int,
                                 raw_text: str, mgmt: "TradeSignal",
                                 result: dict) -> dict:
        """Handle a position-management command (sl to entry / tpN / full).

        Resolves the target open position and acts on it directly — NO LLM call.
        The command kind comes from ``mgmt.mgmt_action``:

          * SL_ENTRY — move SL to the position's entry price (breakeven).
          * TP       — move/refresh take-profit level tpN (rest stays open).
          * FULL     — full market close of the position.

        A pair derived from the reply context or the message text is
        authoritative: if it has no open position we SKIP with a clear message
        and never touch a different position. Only SL_ENTRY keeps the legacy
        "no pair given -> single open position" fallback, because tpN/full are
        too destructive to apply to the wrong position by guess.
        """
        action = mgmt.mgmt_action or "SL_ENTRY"
        log.info("Management command: action=%s pair=%s", action, mgmt.pair)
        self._log(correlation_id, "INFO", "orchestrator",
                  f"Management command: {raw_text[:120]}",
                  {"action": action, "pair": mgmt.pair})

        resolved_pair = mgmt.pair

        def _resolve_open(sym: str):
            s = sym.replace("#", "").replace("$", "").upper()
            if not s.endswith("USDT"):
                s += "USDT"
            return self.position_repo.get_open_by_pair(s)

        if resolved_pair:
            position = _resolve_open(resolved_pair)
            if position is None:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = f"No open position for {resolved_pair}"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **{action}** — no open position found for `{resolved_pair}`.\n"
                    f"Reply to the specific signal, or include the pair "
                    f"(e.g. `#BTC {_cmd_word(action)}`)."
                )
                return result
        else:
            # Only SL_ENTRY falls back to the single open position when untargeted.
            if action == "SL_ENTRY":
                opens = self.position_repo.get_open_positions()
                position = opens[0] if len(opens) == 1 else None
            else:
                position = None
            if position is None:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = (
                    f"Could not determine which position '{_cmd_word(action)}' "
                    f"refers to (no pair in reply/text and 0 or >1 open positions)"
                )
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **{action}** — could not determine the position.\n"
                    f"Reply to the specific signal, or include the pair "
                    f"(e.g. `#BTC {_cmd_word(action)}`)."
                )
                return result

        # ── FULL: full market close ──────────────────────────────────────
        if action == "FULL":
            from src.domain.models import TradeDecision
            decision = TradeDecision(
                action="CLOSE",
                pair=position.pair,
                direction=position.direction,
                entry_price=position.entry_price,
                quantity=position.quantity,
                sl_price=position.sl_price,
                tp_prices=position.tp_prices or [],
                reason=f"full close (manual command) — {position.pair}",
            )
            signal_db_id = self.signal_repo.save(mgmt)
            exec_result = await self.order_service.execute(signal_db_id, decision)
            self.signal_repo.mark_processed(message_id, raw_text)
            result["action"] = "closed" if exec_result.success else "failed"
            result["reason"] = decision.reason
            if exec_result.success:
                await self.notifier.send_message(
                    f"🔴 **Position closed (full)** — `{position.pair}`\n"
                    f"   ├ Direction: `{position.direction}`\n"
                    f"   └ Qty: `{position.quantity}`"
                )
                self._log(correlation_id, "INFO", "orchestrator",
                          f"Full close executed for {position.pair}")
            else:
                await self.notifier.send_message(
                    f"❌ **full close failed** — {position.pair}\n"
                    f"`{exec_result.error or 'unknown error'}`"
                )
                result["error"] = exec_result.error
            return result

        # ── TP: move/refresh take-profit level tpN ───────────────────────
        if action == "TP":
            tp_price = _resolve_tp_price(mgmt, position)
            if tp_price is None:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = (
                    f"No TP price for tp{mgmt.tp_index + 1} on {position.pair} "
                    f"(position has no stored tp_prices and none given in text)"
                )
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **tp{mgmt.tp_index + 1}** — no TP price available for "
                    f"`{position.pair}`.\nInclude a price (e.g. `tp{mgmt.tp_index + 1} 5.2`) "
                    f"or set TP when opening the position."
                )
                return result
            from src.domain.models import TradeDecision
            decision = TradeDecision(
                action="MODIFY",
                pair=position.pair,
                direction=position.direction,
                entry_price=position.entry_price,
                quantity=position.quantity,
                sl_price=position.sl_price,
                tp_prices=[tp_price],
                reason=f"move TP{mgmt.tp_index + 1} to {tp_price} (rest of position stays open)",
            )
            signal_db_id = self.signal_repo.save(mgmt)
            exec_result = await self.order_service.execute(signal_db_id, decision)
            self.signal_repo.mark_processed(message_id, raw_text)
            result["action"] = "modified" if exec_result.success else "failed"
            result["reason"] = decision.reason
            if exec_result.success:
                await self.notifier.send_message(
                    f"🎯 **TP moved — {position.pair}**\n"
                    f"   ├ Level: `tp{mgmt.tp_index + 1}`\n"
                    f"   ├ New TP: `{tp_price}`\n"
                    f"   └ SL kept at `{position.sl_price}` (position stays open)"
                )
                self._log(correlation_id, "INFO", "orchestrator",
                          f"TP{mgmt.tp_index + 1} moved for {position.pair} -> {tp_price}")
            else:
                await self.notifier.send_message(
                    f"❌ **tp{mgmt.tp_index + 1} failed** — {position.pair}\n"
                    f"`{exec_result.error or 'unknown error'}`"
                )
                result["error"] = exec_result.error
            if self.event_bus:
                self.event_bus.emit(Event("PositionModified", {
                    "pair": position.pair, "tp": tp_price, "reason": decision.reason,
                }))
            return result

        # ── SL_ENTRY: move SL to breakeven (entry price) ─────────────────
        # mgmt.sl_price is the sentinel _SL_TO_ENTRY (-1.0); substitute entry.
        if mgmt.sl_price == _SL_TO_ENTRY:
            new_sl = position.entry_price
        else:
            new_sl = mgmt.sl_price

        from src.domain.models import TradeDecision
        decision = TradeDecision(
            action="MODIFY",
            pair=position.pair,
            direction=position.direction,
            entry_price=position.entry_price,
            quantity=position.quantity,
            sl_price=new_sl,
            tp_prices=position.tp_prices or [],
            reason="sl to entry — move stop loss to breakeven (entry price)",
        )

        # MODIFY still runs through Gate2 so size/portfolio limits stay valid
        # (Gate2 is a no-op on size for MODIFY but validates the SL side).
        bal_for_gate = None
        try:
            balance_info = await self.exchange.get_balance()
            bal_for_gate = balance_info.free_usdt
        except Exception:
            log.warning("Balance fetch failed in management path; proceeding")
        allowed, reason, decision = self.gate2.check(decision, bal_for_gate)
        # Gate2 clamps size but must NOT veto a management move. A zero-qty
        # rejection is pure fallout from the historically-missing entry_price;
        # now that it's populated, size is valid. Honor real safety gates
        # (concurrent positions / daily-loss limit) — just not size fallthrough.
        if not allowed and "Invalid quantity" in (reason or ""):
            allowed, reason = True, ""
        if not allowed:
            result["action"] = "rejected"
            result["reason"] = reason
            self.signal_repo.mark_processed(message_id, raw_text)
            await self.notifier.send_message(
                f"🚫 **sl to entry rejected** — {position.pair}\nReason: {reason}"
            )
            return result

        # Execute the modification via the existing MODIFY path.
        signal_db_id = self.signal_repo.save(mgmt)
        exec_result = await self.order_service.execute(
            signal_db_id, decision
        )

        self.signal_repo.mark_processed(message_id, raw_text)
        result["action"] = "modified"
        result["reason"] = decision.reason

        if exec_result.success:
            old_sl = position.sl_price if position.sl_price is not None else "—"
            await self.notifier.send_message(
                f"🔒 **SL → entry (breakeven)** — `{position.pair}`\n"
                f"   ├ Direction: `{position.direction}`\n"
                f"   ├ New SL: `{new_sl}` (was `{old_sl}`)\\n"
                f"   └ Entry: `{position.entry_price}`"
            )
            self._log(correlation_id, "INFO", "orchestrator",
                      f"SL moved to entry for {position.pair}: {position.sl_price} → {new_sl}")
        else:
            await self.notifier.send_message(
                f"❌ **sl to entry failed** — {position.pair}\n"
                f"`{exec_result.error or 'unknown error'}`"
            )
            result["action"] = "failed"
            result["error"] = exec_result.error

        if self.event_bus:
            self.event_bus.emit(Event("PositionModified", {
                "pair": position.pair, "sl": new_sl, "reason": decision.reason,
            }))
        return result

    # ── TP1 / partial-exit handler ──────────────────────────────────────────

    async def _handle_tp1_booked(self, correlation_id: str, message_id: int,
                                  raw_text: str, mgmt: "TradeSignal",
                                  result: dict) -> dict:
        """Handle a 'tp1 booked' / 'tp1 done' management command.

        Flow:
          1. Find the open position for the message's pair.
          2. Market-close 1/3 of the position (take partial profit).
          3. Move SL to entry price (breakeven).
          4. Update the remaining TP so the rest of the position runs.

        No LLM call is made — this is a deterministic management action.
        """
        log.info("TP1 management command: pair=%s text=%s", mgmt.pair, raw_text[:80])
        self._log(correlation_id, "INFO", "orchestrator",
                  f"TP1 command: {raw_text[:120]}", {"pair": mgmt.pair})

        # Resolve the target position
        sym = None
        if mgmt.pair:
            sym = mgmt.pair.replace("#", "").replace("$", "").upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
        position = self.position_repo.get_open_by_pair(sym) if sym else None

        if position is None:
            # If the message explicitly specified a pair, don't fall back to
            # a different open position — reject with a clear message instead.
            if mgmt.pair:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = f"No open position for {sym}"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **TP1 partial close** — no open position for `{sym}`.\n"
                    f"Command was for {sym} but that trade is already closed."
                )
                return result
            opens = self.position_repo.get_open_positions()
            if len(opens) == 1:
                position = opens[0]
                sym = position.pair
            elif len(opens) > 1:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = "Multiple open positions — specify pair for TP1"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    "⚠️ **TP1 partial close** — multiple positions open.\n"
                    "Include the pair (e.g. `#ENA tp1 booked`)."
                )
                return result
            else:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = f"No open position for {mgmt.pair}"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **TP1 partial close** — no open position for `{mgmt.pair}`."
                )
                return result

        if sym is None:
            sym = position.pair

        # ── Step 1: Calculate 1/2 partial close quantity ────────────────
        total_qty = position.quantity
        close_qty = total_qty / 2.0
        remain_qty = total_qty - close_qty

        # Round to exchange precision
        try:
            remain_qty, _ = await self.exchange._round_quantity(sym, remain_qty)
            close_qty = total_qty - remain_qty  # adjust close to match remainder
            if close_qty <= 0:
                close_qty, _ = await self.exchange._round_quantity(sym, total_qty * 0.33)
                remain_qty = total_qty - close_qty
        except Exception:
            pass  # use approximate values

        # Fetch current mark price for the close and new TP
        mark_price = None
        try:
            mark_price = await self.exchange.get_mark_price(sym)
        except Exception:
            log.warning("TP1: could not fetch mark price for %s", sym)

        # ── Step 2: Market-close 1/3 of position ────────────────────────
        close_side = "SELL" if position.direction == "LONG" else "BUY"
        partial_close_ok = False
        try:
            close_order = await self.exchange.market_close(sym, close_qty, close_side)
            if close_order.status in ("FILLED", "NEW", "PARTIALLY_FILLED"):
                partial_close_ok = True
                log.info("TP1 partial close: %s %.4f @ %s (status=%s)",
                         sym, close_qty, close_order.avg_price or mark_price, close_order.status)
            else:
                log.warning("TP1 partial close failed for %s: %s", sym, close_order.error)
        except Exception as e:
            log.warning("TP1 partial close exception for %s: %s", sym, e)

        # ── Step 3: Cancel old SL/TP, place new SL (breakeven) + TP for remainder ──
        if partial_close_ok and remain_qty > 0:
            # Cancel existing orders
            try:
                await self.exchange.cancel_all_orders(sym)
            except Exception as e:
                log.debug("TP1: cancel_all_orders warning: %s", e)

            # Update position in DB: reduce qty, set SL to entry, keep TP price
            new_tp = position.tp_prices[0] if position.tp_prices else None
            self.position_repo.update_sl_tp(
                position.id,
                sl_price=position.entry_price,  # breakeven
                tp_prices=[new_tp] if new_tp else [],
            )
            # Reduce the open position qty in DB
            self.position_repo.update_quantity(position.id, remain_qty)

            # Place new SL at entry (breakeven)
            sl_side = "SELL" if position.direction == "LONG" else "BUY"
            try:
                await self.exchange.stop_loss(sym, remain_qty, position.entry_price, sl_side)
                log.info("TP1: breakeven SL placed for %s @ %s", sym, position.entry_price)
            except Exception as e:
                log.warning("TP1: breakeven SL not placed for %s: %s", sym, e)

            # Place new TP for remaining position (at the original TP price)
            if new_tp and new_tp > 0:
                tp_side = "SELL" if position.direction == "LONG" else "BUY"
                try:
                    await self.exchange.take_profit(sym, remain_qty, new_tp, tp_side)
                    log.info("TP1: remaining TP placed for %s @ %s (qty=%.4f)",
                             sym, new_tp, remain_qty)
                except Exception as e:
                    log.warning("TP1: remaining TP not placed: %s", e)

            # Log position event
            if self.position_event_repo:
                self.position_event_repo.save_event(
                    position_id=position.id,
                    event_type="PARTIAL_EXIT",
                    details=f"TP1 partial close: {close_qty:.4f} @ {close_order.avg_price or mark_price or '?'}, "
                            f"remaining {remain_qty:.4f}, SL moved to entry",
                    metadata={
                        "pair": sym,
                        "direction": position.direction,
                        "close_qty": close_qty,
                        "remain_qty": remain_qty,
                        "close_price": close_order.avg_price or mark_price,
                        "entry_price": position.entry_price,
                        "remaining_tp": new_tp,
                    },
                )

            result["action"] = "tp1_partial"
            result["reason"] = f"Closed {close_qty:.4f} @ TP1, remaining {remain_qty:.4f} with SL at entry"

            await self.notifier.send_message(
                f"📊 **TP1 Partial Close — {sym}**\n"
                f"   ├ Closed: `{close_qty:.4f}` @ `{close_order.avg_price or mark_price or '?'}`\n"
                f"   ├ Remaining: `{remain_qty:.4f}`\n"
                f"   ├ SL → Entry: `{position.entry_price}` (breakeven)\n"
                f"   ├ TP: `{new_tp or '—'}`\n"
                f"   └ Entry: `{position.entry_price}`"
            )

            if self.event_bus:
                self.event_bus.emit(Event("TP1PartialExit", {
                    "pair": sym,
                    "direction": position.direction,
                    "close_qty": close_qty,
                    "close_price": close_order.avg_price or mark_price,
                    "remain_qty": remain_qty,
                    "sl_price": position.entry_price,
                }))
        else:
            result["action"] = "failed"
            result["error"] = "Partial close order was not filled"
            log.warning("TP1 partial close NOT executed for %s (ok=%s, remain=%.4f)",
                        sym, partial_close_ok, remain_qty)
            await self.notifier.send_message(
                f"⚠️ **TP1 partial close failed** — `{sym}`\n"
                f"Market close did not fill. Check Binance."
            )

        self.signal_repo.mark_processed(message_id, raw_text)
        return result

    # ── Full close handler ─────────────────────────────────────────────────

    async def _handle_full_close(self, correlation_id: str, message_id: int,
                                  raw_text: str, mgmt: "TradeSignal",
                                  result: dict) -> dict:
        """Handle a 'closed at entry' / 'full' management command.

        Market-closes the resolved position immediately without LLM.
        """
        log.info("Full close command: pair=%s text=%s", mgmt.pair, raw_text[:80])
        self._log(correlation_id, "INFO", "orchestrator",
                  f"Full close: {raw_text[:120]}", {"pair": mgmt.pair})

        # Resolve position
        sym = None
        if mgmt.pair:
            sym = mgmt.pair.replace("#", "").replace("$", "").upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
        position = self.position_repo.get_open_by_pair(sym) if sym else None

        if position is None:
            # If the message explicitly specified a pair, don't fall back to
            # a different open position — reject with a clear message instead.
            if mgmt.pair:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = f"No open position for {sym}"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **Close command** — no open position for `{sym}`.\n"
                    f"Command was for {sym} but that trade is already closed."
                )
                return result
            opens = self.position_repo.get_open_positions()
            if len(opens) == 1:
                position = opens[0]
                sym = position.pair
            else:
                result["action"] = "skipped"
                result["skipped"] = True
                result["reason"] = "No open position"
                self.signal_repo.mark_processed(message_id, raw_text)
                await self.notifier.send_message(
                    f"⚠️ **Close command** — could not resolve position.\n"
                    f"Specify a pair like `#ENA closed at entry`."
                )
                return result

        if sym is None:
            sym = position.pair

        # Execute via position manager (market close)
        ok = await self.position_manager.close_position(
            position, f"Management close: {raw_text[:60]}", closed_by="MANUAL"
        )

        self.signal_repo.mark_processed(message_id, raw_text)
        if ok:
            result["action"] = "closed"
            result["reason"] = "Position closed via management command"
            await self.notifier.send_message(
                f"✅ **Position closed** — `{sym}`\n"
                f"Management command: `{raw_text[:100]}`"
            )
        else:
            result["action"] = "failed"
            result["error"] = "Close order did not fill"
            await self.notifier.send_message(
                f"❌ **Close failed** — `{sym}`\nMarket close did not fill. Check Binance."
            )

        if self.event_bus:
            self.event_bus.emit(Event("PositionClosed", {
                "pair": sym, "reason": raw_text[:100],
            }))
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

                if exec_result.error and exec_result.error.startswith("✅ Repaired") and exec_result.status == "FILLED":
                    await self.notifier.send_message(
                        f"🚀 **Trigger ENTERED (repaired)** — {exec_result.symbol}\n"
                        f"   ├ Direction: `{decision.direction}`\n"
                        f"   ├ Entry: `{exec_result.avg_price:.8f}`\n"
                        f"   ├ Qty: `{exec_result.filled_quantity:,.0f}`\n"
                        f"   ├ SL: `{decision.sl_price}`\n"
                        f"   ├ TP: `{decision.tp_prices}`\n"
                        f"   ├ Leverage: `{decision.leverage}x`\n\n"
                        f"{exec_result.error}"
                    )
                else:
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
