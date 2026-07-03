"""Safety gates — non-negotiable hard limits the LLM cannot override.

Gate 1 (pre-LLM): idempotency, cooldown, pair whitelist → skip before LLM cost.
Gate 2 (post-LLM): clamp position size, max concurrent, daily loss → reject.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.domain.models import TradeDecision, TradeSignal
from src.state.repositories import PositionRepository, SignalRepository

log = logging.getLogger("agent.gate")


class SafetyGate1:
    """Pre-LLM gate: fast checks that skip before any LLM cost."""

    def __init__(self, config: dict, signal_repo: SignalRepository, position_repo: PositionRepository):
        self.config = config
        self.signal_repo = signal_repo
        self.position_repo = position_repo
        self._cooldowns: dict[str, datetime] = {}

    def check(self, signal: TradeSignal) -> tuple[bool, str]:
        """Returns (allowed: bool, reason: str).

        If allowed=False, the signal should be skipped immediately (no LLM call).
        """
        # 1. Idempotency — already processed?
        if self.signal_repo.is_processed(signal.message_id, signal.raw_text):
            return False, "Duplicate signal (already processed)"

        # 2. Pair whitelist
        allowed_pairs = self.config.get("agent", {}).get("allowed_pairs", ["*"])
        if "*" not in allowed_pairs and signal.pair:
            pair_clean = signal.pair.replace("#", "").replace("$", "").upper()
            if not any(p.upper() in pair_clean or pair_clean in p.upper() for p in allowed_pairs):
                return False, f"Pair {signal.pair} not in whitelist"

        # 3. Cooldown — same pair traded recently?
        if signal.pair:
            pair_key = signal.pair.upper().replace("#", "").replace("$", "") + "USDT"
            last_trade = self.position_repo.get_open_by_pair(pair_key)
            if last_trade:
                cooldown_min = self.config.get("risk", {}).get("min_cooldown_minutes", 5)
                elapsed = (datetime.utcnow() - last_trade.entry_time).total_seconds() / 60
                if elapsed < cooldown_min:
                    return False, f"Cooldown active for {signal.pair} ({elapsed:.1f}/{cooldown_min}m)"

        return True, ""


class SafetyGate2:
    """Post-LLM gate: hard limits on what the LLM decided.

    The LLM can suggest any position size, but this gate clamps it.
    """

    def __init__(self, config: dict, position_repo: PositionRepository):
        self.config = config
        self.position_repo = position_repo

    def check(self, decision: TradeDecision) -> tuple[bool, str, TradeDecision]:
        """Returns (allowed: bool, reason: str, clamped_decision)."""
        risk = self.config.get("risk", {})

        # 1. Max concurrent positions
        max_concurrent = risk.get("max_concurrent_positions", 2)
        open_count = self.position_repo.get_open_count()
        if open_count >= max_concurrent:
            return False, f"Max concurrent positions reached ({open_count}/{max_concurrent})", decision

        # 2. Clamp position size
        max_size = risk.get("max_position_size_usdt", 100)
        if decision.quantity > max_size:
            clamped = TradeDecision(
                action=decision.action,
                pair=decision.pair,
                direction=decision.direction,
                order_type=decision.order_type,
                quantity=max_size,
                entry_price=decision.entry_price,
                sl_price=decision.sl_price,
                tp_prices=decision.tp_prices,
                reason=f"{decision.reason} (quantity clamped from {decision.quantity} to {max_size})",
                confidence=decision.confidence,
            )
            log.info("Gate2: clamped quantity %.4f → %.4f", decision.quantity, max_size)
            return True, f"Quantity clamped to {max_size}", clamped

        # 3. Daily loss limit
        daily_loss_pct = risk.get("daily_loss_limit_percent", 10)
        if daily_loss_pct > 0:
            daily_pnl = self.position_repo.get_daily_pnl()
            balance_usdt = 1000  # TODO: fetch from exchange
            if daily_pnl < 0 and abs(daily_pnl) / balance_usdt * 100 >= daily_loss_pct:
                return False, f"Daily loss limit reached ({abs(daily_pnl):.2f} USDT)", decision

        return True, "", decision
