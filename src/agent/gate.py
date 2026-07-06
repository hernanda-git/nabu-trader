"""Safety gates — non-negotiable hard limits the LLM cannot override.

Gate 1 (pre-LLM): idempotency, cooldown, pair whitelist → skip before LLM cost.
Gate 2 (post-LLM): clamp position size, max concurrent, daily loss,
                   min_notional scaling, dynamic leverage calculation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace as dc_replace
from datetime import datetime, timezone, timedelta

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
                elapsed = (datetime.now(timezone.utc) - last_trade.entry_time).total_seconds() / 60
                if elapsed < cooldown_min:
                    return False, f"Cooldown active for {signal.pair} ({elapsed:.1f}/{cooldown_min}m)"

        return True, ""


class SafetyGate2:
    """Post-LLM gate: hard limits + dynamic leverage + risk-based sizing.

    The LLM suggests SL/TP/direction; this gate computes the OPTIMAL quantity
    so that max loss (if SL hit) ≤ risk_per_trade_percent of balance.

    Sizing formula:
      max_loss     = balance × risk_per_trade_percent / 100
      sl_distance  = |entry - SL| / entry
      pos_value    = max_loss / sl_distance   ← can be much larger than balance
      leverage     = ceil(pos_value / (balance × margin_usage_pct / 100))
      quantity     = pos_value / entry

    This allows positions large enough to pass Binance min notional (~$5)
    while capping actual risk to 10% of balance. Leverage handles the gap
    between position value and available margin.

    Fallback (no SL or no balance): clamp to max_position_size_usdt.
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
        # ── RISK-BASED POSITION SIZING ──────────────────────────────────
        # Goal: max loss if SL hit ≤ risk_per_trade_percent of balance.
        # This lets position VALUE exceed balance (via leverage) while
        # keeping actual dollar risk capped at ~10% of balance.
        # ──────────────────────────────────────────────────────────────
        risk = self.config.get("risk", {})
        price = decision.entry_price or 0.0
        risk_pct = risk.get("risk_per_trade_percent", 10)
        max_size_hard = risk.get("max_position_size_usdt", 100)
        max_lev = risk.get("max_leverage", 20)
        margin_usage = risk.get("margin_usage_pct", 50)
        min_notional = risk.get("min_notional_usdt", 1.0)

        # 1. Max concurrent positions
        max_concurrent = risk.get("max_concurrent_positions", 2)
        open_count = self.position_repo.get_open_count()
        if open_count >= max_concurrent:
            return False, f"Max concurrent positions reached ({open_count}/{max_concurrent})", decision

        # 2. Daily loss limit
        daily_loss_pct = risk.get("daily_loss_limit_percent", 10)
        if daily_loss_pct > 0:
            daily_pnl = self.position_repo.get_daily_pnl()
            bal = balance_usdt if (balance_usdt is not None and balance_usdt > 0) else 1000
            if daily_pnl < 0 and abs(daily_pnl) / bal * 100 >= daily_loss_pct:
                return False, f"Daily loss limit reached ({abs(daily_pnl):.2f} USDT)", decision

        # 3. Compute quantity from risk (SL distance) + leverage
        sizing_reason = ""
        can_risk_size = (
            balance_usdt is not None
            and balance_usdt > 0
            and price > 0
            and decision.sl_price is not None
            and decision.sl_price > 0
        )

        if can_risk_size:
            # Risk-based: size so SL hit = max_loss
            max_loss = balance_usdt * risk_pct / 100  # e.g. $8.11 * 10% = $0.81
            sl_distance_pct = abs(price - (decision.sl_price or 0.0)) / price
            if sl_distance_pct > 0:
                # position value that makes loss = max_loss when SL hit
                # NO position-value cap — leverage handles the margin gap.
                # User constraint: "no need to clamp position size, just
                # make the loss no more than 10%, adjust leverage and portion"
                pos_value = max_loss / sl_distance_pct
                # ensure meets min notional (scale up, user accepts higher risk)
                if pos_value < min_notional:
                    pos_value = min_notional
                    sizing_reason += f"scaled to min notional ${min_notional:.2f}; "
                quantity = pos_value / price
                # leverage: enough so margin ≤ margin_usage% of balance
                margin_target = balance_usdt * margin_usage / 100
                if margin_target > 0 and pos_value > 0:
                    leverage = max(1, math.ceil(pos_value / margin_target))
                else:
                    leverage = 1
                leverage = min(leverage, max_lev)
                decision = dc_replace(
                    decision, quantity=quantity, leverage=leverage,
                    reason=(decision.reason or "") + f" [risk-sized: max_loss=${max_loss:.2f}, "
                            f"sl_dist={sl_distance_pct*100:.2f}%, pos_val=${pos_value:.2f}, lev={leverage}x]"
                )
                log.info("Gate2 risk-sizing: balance=$%.2f max_loss=$%.2f sl_dist=%.2f%% "
                         "pos_val=$%.2f qty=%.6f lev=%dx",
                         balance_usdt, max_loss, sl_distance_pct * 100,
                         pos_value, quantity, leverage)
            else:
                # SL == entry → zero distance, can't risk-size; fallback
                decision = dc_replace(decision, quantity=max_size_hard / price if price > 0 else 0)
                sizing_reason += "SL==entry (zero distance), fallback sizing; "
        else:
            # Fallback: no SL or no balance → clamp to max_size_hard
            fallback_qty = (max_size_hard / price) if price > 0 else 0
            decision = dc_replace(decision, quantity=fallback_qty)
            sizing_reason += "no SL/balance, fallback to max_position_size; "
            # Still calculate leverage if we have balance
            if balance_usdt is not None and balance_usdt > 0 and price > 0:
                pos_value = decision.quantity * price
                margin_target = balance_usdt * margin_usage / 100
                if margin_target > 0 and pos_value > 0:
                    leverage = max(1, math.ceil(pos_value / margin_target))
                    leverage = min(leverage, max_lev)
                    decision = dc_replace(decision, leverage=leverage)
                    sizing_reason += f"lev={leverage}x; "

        # 4. Validate quantity > 0
        if decision.quantity <= 0:
            log.info("Gate2 rejected: invalid quantity %.6f (reason: %s)", decision.quantity, decision.reason)
            return False, f"{decision.reason}", decision

        # 5. Validate SL is on correct side of entry price
        if decision.sl_price and decision.entry_price and decision.action == "ENTER":
            if decision.direction == "LONG" and decision.sl_price >= decision.entry_price:
                return False, f"SL {decision.sl_price} >= entry {decision.entry_price} for LONG", decision
            if decision.direction == "SHORT" and decision.sl_price <= decision.entry_price:
                return False, f"SL {decision.sl_price} <= entry {decision.entry_price} for SHORT", decision

        # Compose final reason
        gate_reason = sizing_reason.rstrip("; ") if sizing_reason else ""

        return True, gate_reason, decision
