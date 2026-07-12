"""Safety gates — non-negotiable hard limits the LLM cannot override.

Gate 1 (pre-LLM): idempotency, cooldown, pair whitelist → skip before LLM cost.
Gate 2 (post-LLM): clamp position size, max concurrent, daily loss,
                   portfolio notional cap, dynamic leverage calculation.

Key improvements over the original:
  - Position value defaults to a fixed PORTION of balance (`max_port_pct`, default 10%).
    No more tight-SL blowups — the position is always predictable.
  - SL distance is used ONLY as a risk check: if SL hit would lose > risk_pct
    of balance, the position is shrunk proportionally.
  - Leverage is snapped to Binance-valid values (1, 2, 3, 5, 7, 10, 15, 20,
    25, 30, 50, 75, 100, 125) — no more 17x or 43x.
  - Leverage is applied ONLY when needed to meet margin or min notional.
    At 1x, the full position stays in your margin budget.
  - A portfolio notional cap (`max_portfolio_leverage`) prevents over-leveraging
    the entire account across multiple open positions.
  - The fallback path (no SL / no balance) also computes valid leverage.
"""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from datetime import datetime, timezone
from typing import Any

from src.domain.models import TradeDecision, TradeSignal
from src.state.repositories import PositionRepository, SignalRepository

log = logging.getLogger("agent.gate")

# ─── Binance-compatible leverage values ──────────────────────────────────
# USDⓈ-M Futures supports these discrete levels only.
_BINANCE_VALID_LEVERAGES = sorted({1, 2, 3, 5, 7, 10, 15, 20, 25, 30, 50, 75, 100, 125})


def _snap_leverage(leverage: float, max_lev: int) -> int:
    """Snap a computed leverage to the nearest valid Binance level, rounding up.

    Example: 4 → 5, 6 → 7, 12 → 15, 17 → 20, 3.2 → 5
    """
    if leverage <= 1:
        return 1
    clamped = min(leverage, max_lev)
    for v in _BINANCE_VALID_LEVERAGES:
        if v >= clamped:
            return min(v, max_lev)
    return max_lev


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
    """Post-LLM gate: hard limits + dual-constraint sizing + portfolio cap.

    DUAL CONSTRAINT SIZING — two rules, both must be satisfied:

      🎯 Risk constraint (primary): position sized so SL loss ≤ risk_pct of balance
      🛑 Port cap (hard ceiling):   position never exceeds max_port_pct of balance

      The port cap is the WORST-CASE limit — it only activates when a very
      tight SL would make the risk-based position unacceptably large.
      For normal SL distances, the risk-based size wins.

    ── Formula Chain ──

      1. pos_risk     = max_loss / sl_distance         ← risk-based ideal
      2. pos_cap      = balance × max_port_pct / 100   ← 10% worst-case ceiling
      3. pos_value    = MIN(pos_risk, pos_cap)         ← never exceed either
      4. pos_value    = MAX(pos_value, min_notional)   ← but always meet min trade
      5. quantity     = pos_value / price
      6. leverage     = snap(pos_value / margin_target) only if > margin_target
      7. margin check: pos_value / leverage ≤ margin_target

    Portfolio cap (cross-position): existing_notional + pos_value ≤
      balance × max_portfolio_leverage.

    Fallback (no SL / no balance): use max_port_pct as the fallback.
    """

    def __init__(self, config: dict, position_repo: PositionRepository):
        self.config = config
        self.position_repo = position_repo

    def check(self, decision: TradeDecision, balance_usdt: float | None = None,
              filters: dict | None = None) -> tuple[bool, str, TradeDecision]:
        """Returns (allowed: bool, reason: str, clamped_decision).

        Args:
            decision: The trade decision to validate/clamp.
            balance_usdt: Current free balance in USDT (for sizing).
            filters: Optional exchange filter dict for the symbol (from
                     BinanceExchange._load_futures_filters). When provided, its
                     MIN_NOTIONAL is used as the floor instead of (or in addition
                     to) the config min_notional_usdt, so leverage is driven by the
                     REAL exchange minimum, not a hardcoded value.
        """
        risk = self.config.get("risk", {})
        price = decision.entry_price or 0.0

        # ── Read risk params ──────────────────────────────────────────────
        risk_pct = risk.get("risk_per_trade_percent", 10)
        max_port_pct = risk.get("max_port_pct", 10)            # hard ceiling % of balance
        max_size_hard = risk.get("max_position_size_usdt", 100)  # USD absolute ceiling
        max_lev = risk.get("max_leverage", 20)
        port_usdt = risk.get("port_usdt", 1.0)                # $ margin to use per trade
        margin_usage = risk.get("margin_usage_pct", 50)       # absolute cap % of balance
        min_notional = risk.get("min_notional_usdt", 1.0)
        max_portfolio_lev = risk.get("max_portfolio_leverage", 10)
        max_lev_increase_pct = risk.get("max_leverage_increase_pct", 10)
        # Use the REAL exchange MIN_NOTIONAL when available (Task 6).
        if filters:
            exchange_min = filters.get("minNotional")
            if exchange_min:
                min_notional = max(min_notional, float(exchange_min))
        # -------------------------------------------------------------------

        # ── 1. Max concurrent positions ───────────────────────────────────
        max_concurrent = risk.get("max_concurrent_positions", 2)
        open_count = self.position_repo.get_open_count()
        if open_count >= max_concurrent:
            return False, f"Max concurrent positions reached ({open_count}/{max_concurrent})", decision

        # ── 2. Daily loss limit ───────────────────────────────────────────
        daily_loss_pct = risk.get("daily_loss_limit_percent", 10)
        if daily_loss_pct > 0:
            daily_pnl = self.position_repo.get_daily_pnl()
            bal = balance_usdt if (balance_usdt is not None and balance_usdt > 0) else 1000
            if daily_pnl < 0 and abs(daily_pnl) / bal * 100 >= daily_loss_pct:
                return False, f"Daily loss limit reached ({abs(daily_pnl):.2f} USDT)", decision

        # ── 3. Portfolio notional exposure ────────────────────────────────
        if balance_usdt is not None and balance_usdt > 0 and max_portfolio_lev > 0:
            existing_notional = self.position_repo.get_total_notional_usdt()
            portfolio_budget = balance_usdt * max_portfolio_lev
            log.info("Gate2 portfolio: existing=$%.2f  budget=$%.2f (%.1fx)",
                     existing_notional, portfolio_budget, max_portfolio_lev)
        else:
            existing_notional = 0.0
            portfolio_budget = float("inf")

        # ── 4. Dual-constraint position sizing ────────────────────────────
        sizing_reason: list[str] = []
        can_risk_size = (
            balance_usdt is not None
            and balance_usdt > 0
            and price > 0
            and decision.sl_price is not None
            and decision.sl_price > 0
        )

        if can_risk_size:
            # ── Risk-budget inputs ──────────────────────────────────────
            max_allowed_loss = balance_usdt * risk_pct / 100.0    # e.g. $10
            sl = decision.sl_price or 0.0
            port_cap = balance_usdt * max_port_pct / 100.0        # e.g. $10

            # Direction-aware SL distance
            if decision.direction == "SHORT":
                sl_distance_pct = abs(sl - price) / price
            else:
                sl_distance_pct = abs(price - sl) / price

            if sl_distance_pct > 0:
                # ── Step A: risk-based ideal size ────────────────────
                pos_value_risk = max_allowed_loss / sl_distance_pct

                # ── Step B: hard ceiling (never exceed port_pct of balance) ──
                pos_value = min(pos_value_risk, port_cap)

                # Track which constraint won
                if pos_value < pos_value_risk:
                    sizing_reason.append(
                        f"capped by port {max_port_pct}% (risk=$"
                        f"{pos_value_risk:.2f} > port=${port_cap:.2f})"
                    )

                # ── Step C: effective loss after ceiling ─────────────
                effective_max_loss = pos_value * sl_distance_pct

                # ── Step D: min notional floor ──────────────────────
                if pos_value < min_notional:
                    pos_value = min_notional
                    effective_max_loss = pos_value * sl_distance_pct
                    sizing_reason.append(
                        f"floored to min notional ${min_notional:.2f} "
                        f"(effective risk ${effective_max_loss:.2f})"
                    )

                # ── Step E: portfolio notional ──────────────────────
                if existing_notional + pos_value > portfolio_budget:
                    pos_value = max(min_notional, portfolio_budget - existing_notional)
                    effective_max_loss = pos_value * sl_distance_pct
                    sizing_reason.append(
                        f"trimmed by portfolio cap (budget=${portfolio_budget:.2f}, "
                        f"used=${existing_notional:.2f})"
                    )

                # ── Step F: compute quantity ──────────────────────────
                quantity = pos_value / price

                # ── Step G: leverage = pos_value / port_usdt ────────────
                margin_budget = min(port_usdt, balance_usdt * margin_usage / 100.0)
                if margin_budget > 0 and pos_value > 0:
                    raw_lev = pos_value / margin_budget
                    # Cap any increase above the needed baseline at
                    # max_leverage_increase_pct (Task 6) — but never above max_lev.
                    lev_cap = _snap_leverage(raw_lev * (1 + max_lev_increase_pct / 100.0), max_lev)
                    leverage = min(_snap_leverage(raw_lev, max_lev), lev_cap, max_lev)
                else:
                    leverage = 1

                # ── Step H: margin sanity check ─────────────────────
                margin_needed = pos_value / leverage
                if margin_needed > margin_budget:
                    scale = margin_budget / margin_needed
                    quantity *= scale
                    pos_value = quantity * price
                    effective_max_loss = pos_value * sl_distance_pct
                    leverage = _snap_leverage(pos_value / max(margin_budget, 0.01), max_lev)
                    sizing_reason.append(
                        f"margin-adjusted after lev snap: qty×{scale:.3f}"
                    )

                decision = dc_replace(
                    decision, quantity=quantity, leverage=leverage,
                    reason=(decision.reason or "") + (
                        f" [dual-constrained: max_loss=${effective_max_loss:.2f}, "
                        f"sl_dist={sl_distance_pct*100:.2f}%, "
                        f"pos_val=${pos_value:.2f}, lev={leverage}x, "
                        f"margin_needed=${margin_needed:.2f}]"
                    ),
                )
                log.info(
                    "Gate2 dual-constraint: bal=$%.2f max_loss=$%.2f sl_dist=%.2f%% "
                    "pos_risk=$%.2f port_cap=$%.2f → pos=$%.2f qty=%.6f lev=%d",
                    balance_usdt, max_allowed_loss, sl_distance_pct * 100,
                    pos_value_risk, port_cap, pos_value, quantity, leverage,
                )
            else:
                # SL == entry → zero distance → fallback to port cap
                fallback_qty = port_cap / price if price > 0 else 0
                decision = dc_replace(decision, quantity=fallback_qty)
                sizing_reason.append("SL==entry (zero distance), fallback to port size")
        else:
            # ── Fallback: no SL / no balance → port cap ────────────────
            if balance_usdt is not None and balance_usdt > 0:
                port_cap = balance_usdt * max_port_pct / 100.0
            else:
                port_cap = max_size_hard
            fallback_qty = (port_cap / price) if price > 0 else 0
            decision = dc_replace(decision, quantity=fallback_qty)
            sizing_reason.append(f"no SL/balance, fallback to port=${port_cap:.2f}")

            # Still compute sensible leverage if we have balance
            if balance_usdt is not None and balance_usdt > 0 and price > 0:
                margin_budget = min(port_usdt, balance_usdt * margin_usage / 100.0)
                pos_value = decision.quantity * price
                if margin_budget > 0 and pos_value > 0:
                    raw_lev = pos_value / margin_budget
                    lev_cap = _snap_leverage(raw_lev * (1 + max_lev_increase_pct / 100.0), max_lev)
                    leverage = min(_snap_leverage(raw_lev, max_lev), lev_cap, max_lev)
                    decision = dc_replace(decision, leverage=leverage)
                    sizing_reason.append(f"lev={leverage}x")

        # ── 5. Validate quantity > 0 ─────────────────────────────────────
        if decision.quantity <= 0:
            log.info("Gate2 rejected: invalid quantity %.6f", decision.quantity)
            return False, f"Invalid quantity {decision.quantity:.6f}", decision

        # ── 6. Validate SL is on correct side of entry price ─────────────
        if decision.sl_price and decision.entry_price and decision.action == "ENTER":
            if decision.direction == "LONG" and decision.sl_price >= decision.entry_price:
                return False, f"SL {decision.sl_price} >= entry {decision.entry_price} for LONG", decision
            if decision.direction == "SHORT" and decision.sl_price <= decision.entry_price:
                return False, f"SL {decision.sl_price} <= entry {decision.entry_price} for SHORT", decision

        gate_reason = "; ".join(sizing_reason) if sizing_reason else ""
        return True, gate_reason, decision
