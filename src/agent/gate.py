"""Safety gates — non-negotiable hard limits the LLM cannot override.

Gate 1 (pre-LLM): idempotency, cooldown, pair whitelist → skip before LLM cost.
Gate 2 (post-LLM): clamp position size, max concurrent, daily loss,
                   min_notional scaling, dynamic leverage calculation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace as dc_replace
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
        # 1. Idempotency — message_id already seen (blocks edited duplicates)
        if self.signal_repo.get_by_message_id(signal.message_id):
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
    """Post-LLM gate: hard limits + dynamic leverage.

    The LLM suggests quantity in asset units (e.g. 0.01 BTC).
    This gate:
      - Converts to position value (qty * entry_price) for USDT comparisons
      - Clamps position value to max_position_size_usdt
      - Scales up position if below min_notional (user accepts higher risk)
      - Calculates optimal futures leverage per trade

    All USDT comparisons use position_value = quantity * entry_price.
    """

    def __init__(self, config: dict, position_repo: PositionRepository):
        self.config = config
        self.position_repo = position_repo

    def check(self, decision: TradeDecision, balance_usdt: float | None = None) -> tuple[bool, str, TradeDecision]:
        """Returns (allowed: bool, reason: str, clamped_decision).

        If balance_usdt is provided, position sizing is dynamically
        clamped to balance * risk_per_trade_percent / 100, and
        leverage is calculated automatically.
        """
        risk = self.config.get("risk", {})
        price = decision.entry_price or 0.0

        # 1. Max concurrent positions
        max_concurrent = risk.get("max_concurrent_positions", 2)
        open_count = self.position_repo.get_open_count()
        if open_count >= max_concurrent:
            return False, f"Max concurrent positions reached ({open_count}/{max_concurrent})", decision

        # 2. Clamp position VALUE (in USDT) — compare position_value against max_size
        max_size = risk.get("max_position_size_usdt", 100)
        if balance_usdt is not None and balance_usdt > 0:
            risk_pct = risk.get("risk_per_trade_percent", 10)
            dynamic_max = balance_usdt * risk_pct / 100
            max_size = min(max_size, dynamic_max)

        clamped_reason = ""
        if price > 0 and decision.quantity * price > max_size:
            clamped_qty = max_size / price
            decision = dc_replace(
                decision,
                quantity=clamped_qty,
                reason=f"{decision.reason} (position ${decision.quantity * price:.2f} → ${max_size:.2f})",
            )
            clamped_reason = f"Position clamped to ${max_size:.2f}"
            log.info("Gate2: clamped position $%.2f → $%.2f (qty %.6f)",
                     decision.quantity * price, max_size, clamped_qty)

        # 3. Daily loss limit
        daily_loss_pct = risk.get("daily_loss_limit_percent", 10)
        if daily_loss_pct > 0:
            daily_pnl = self.position_repo.get_daily_pnl()
            bal = balance_usdt if (balance_usdt is not None and balance_usdt > 0) else 1000
            if daily_pnl < 0 and abs(daily_pnl) / bal * 100 >= daily_loss_pct:
                return False, f"Daily loss limit reached ({abs(daily_pnl):.2f} USDT)", decision

        # 4. Validate quantity > 0
        if decision.quantity <= 0:
            log.info("Gate2 rejected: invalid quantity %.6f (reason: %s)", decision.quantity, decision.reason)
            return False, f"{decision.reason}", decision

        # 5. Scale up position VALUE if below minimum notional
        scaled_reason = ""
        if decision.action == "ENTER" and price > 0:
            min_notional = risk.get("min_notional_usdt", 1.0)
            current_pos_value = decision.quantity * price
            if current_pos_value < min_notional:
                target_value = min_notional
                scaled_qty = target_value / price
                decision = dc_replace(
                    decision,
                    quantity=scaled_qty,
                    reason=f"{decision.reason} (scaled to meet min_notional: ${current_pos_value:.2f} → ${target_value:.2f})",
                )
                scaled_reason = f"Position scaled ${current_pos_value:.2f} → ${target_value:.2f} (min notional)"
                log.info("Gate2: scaled position $%.2f → $%.2f for min_notional",
                         current_pos_value, target_value)

        # 6. Calculate dynamic leverage for ENTER decisions
        lev_reason = ""
        if decision.action == "ENTER" and balance_usdt is not None and balance_usdt > 0:
            max_lev = risk.get("max_leverage", 20)
            margin_usage = risk.get("margin_usage_pct", 50)
            pos_value = (decision.quantity or 0) * (decision.entry_price or 1.0)

            # Target: use at most margin_usage% of balance as margin for this trade
            margin_target = balance_usdt * margin_usage / 100
            if margin_target > 0 and pos_value > 0:
                # Minimum leverage so margin_needed <= margin_target
                leverage = max(1, math.ceil(pos_value / margin_target))
                # Cap at max allowed
                leverage = min(leverage, max_lev)
            else:
                leverage = 1

            leverage = max(1, leverage)

            decision = dc_replace(decision, leverage=leverage)
            lev_reason = f"leverage: {leverage}x"
            log.info("Gate2: leverage %dx (pos=$%.2f, balance=$%.2f, margin_target=$%.2f)",
                     leverage, pos_value, balance_usdt, margin_target)

        # 7. Validate SL is on correct side of entry price
        if decision.sl_price and decision.entry_price and decision.action == "ENTER":
            if decision.direction == "LONG" and decision.sl_price >= decision.entry_price:
                return False, f"SL {decision.sl_price} >= entry {decision.entry_price} for LONG", decision
            if decision.direction == "SHORT" and decision.sl_price <= decision.entry_price:
                return False, f"SL {decision.sl_price} <= entry {decision.entry_price} for SHORT", decision

        # Compose final reason
        parts = [p for p in (clamped_reason, scaled_reason, lev_reason) if p]
        gate_reason = "; ".join(parts) if parts else ""

        return True, gate_reason, decision
