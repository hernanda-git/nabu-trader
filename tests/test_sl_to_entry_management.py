"""Regression tests for the position-management ('sl to entry') path.

Two bugs this guards against:

Bug A — silent misroute (CAKE -> INJUSDT):
  A management command resolved to an explicit pair (e.g. via reply context:
  "close cake at entry" -> CAKEUSDT) was, when that pair had no open position,
  silently retargeted to the single open position (INJUSDT) and mutated it.
  Fix: an explicit/reply-derived pair is authoritative. If it has no open
  position we SKIP with a clear message and never touch a different position.

Bug B — zero-quty Gate2 rejection:
  The MODIFY TradeDecision was built without entry_price/quantity, so Gate2
  computed price=0 -> fallback_qty = port_cap/0 -> 0 -> "Invalid quantity
  0.000000" rejection of a perfectly valid command. Fix: populate
  entry_price/quantity from the open position; a residual zero-qty vet can no
  longer block a management move (real safety gates still apply).

These tests exercise the REAL TradeOrchestrator._handle_management with fakes
for the exchange/order_service/notifier and a real SafetyGate2 over an
in-memory config.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.agent.gate import SafetyGate2
from src.domain.models import Position, TradeSignal
from src.orchestrator import TradeOrchestrator, _SL_TO_ENTRY


# ─── Fakes ────────────────────────────────────────────────────────────────

class _FakeOrderService:
    """Records the decision that would be executed; never hits the network."""

    def __init__(self):
        self.last_decision = None
        self.calls = 0

    async def execute(self, signal_db_id, decision):
        self.last_decision = decision
        self.calls += 1

        @dataclass
        class _Res:
            success: bool = True
            order_id: str = "x"
            symbol: str = decision.pair
            side: str = ""
            filled_quantity: float = decision.quantity
            avg_price: float = decision.entry_price or 0.0
            status: str = "MODIFIED"
            error: str | None = None

        return _Res()


class _FakeExchange:
    async def get_balance(self):
        @dataclass
        class _Bal:
            total_usdt: float = 100.0
            free_usdt: float = 100.0
        return _Bal()


class _FakeNotifier:
    def __init__(self):
        self.messages = []

    async def send_message(self, text):
        self.messages.append(text)


class _PosRepo:
    """Returns whatever open position map we configure; supports the methods
    _handle_management actually calls."""

    def __init__(self, open_by_pair: dict[str, Position]):
        self._open_by_pair = open_by_pair
        self.updated = []

    def get_open_by_pair(self, pair: str) -> Position | None:
        return self._open_by_pair.get(pair.upper())

    def get_open_positions(self) -> list[Position]:
        return list(self._open_by_pair.values())

    def get_open_count(self) -> int:
        return len(self._open_by_pair)

    def get_daily_pnl(self) -> float:
        return 0.0  # no losses -> daily-loss gate stays open

    def get_total_notional_usdt(self) -> float:
        return sum(p.entry_price * p.quantity for p in self._open_by_pair.values())

    def update_sl_tp(self, position_id, sl_price=None, tp_prices=None):
        self.updated.append((position_id, sl_price, tp_prices))


class _SignalRepo:
    def __init__(self):
        self.processed = []

    def mark_processed(self, message_id, raw_text):
        self.processed.append(message_id)

    def save(self, signal):
        return 1  # fake PK


def _make_orchestrator(open_by_pair: dict[str, Position]):
    """Build a TradeOrchestrator backed by fakes + a real SafetyGate2."""
    config = {
        "risk": {
            "risk_per_trade_percent": 10,
            "max_port_pct": 10,
            "max_position_size_usdt": 100,
            "max_leverage": 20,
            "port_usdt": 1.0,
            "margin_usage_pct": 80,
            "min_notional_usdt": 5.0,
            "max_portfolio_leverage": 10,
            "max_leverage_increase_pct": 10,
            "max_concurrent_positions": 2,
            "daily_loss_limit_percent": 30,
        }
    }
    pos_repo = _PosRepo(open_by_pair)
    signal_repo = _SignalRepo()
    gate2 = SafetyGate2(config, pos_repo)
    order_service = _FakeOrderService()
    notifier = _FakeNotifier()

    orch = TradeOrchestrator.__new__(TradeOrchestrator)
    orch.config = config
    orch.exchange = _FakeExchange()
    orch.gate2 = gate2
    orch.order_service = order_service
    orch.notifier = notifier
    orch.signal_repo = signal_repo
    orch.position_repo = pos_repo
    orch.event_bus = None
    orch.trade_log_repo = None
    orch.pending_signal_repo = None
    orch.event_repo = None
    orch.decision_repo = None
    orch.order_repo = None
    orch.llm_repo = None
    orch.position_event_repo = None
    orch.config_snapshot_repo = None
    orch.agent = None
    orch.gate1 = None
    orch.position_manager = None
    orch.app_version = "test"
    return orch


def _inj_position() -> Position:
    return Position(
        id=1, pair="INJUSDT", direction="LONG",
        entry_price=5.045, quantity=1.0, sl_price=4.935,
        tp_prices=[5.139],
    )


def _mgmt(pair: str | None) -> TradeSignal:
    return TradeSignal(
        message_id=-1, channel="manual", raw_text=f"#{pair} sl to entry" if pair else "sl to entry",
        pair=pair, direction=None, sl_price=_SL_TO_ENTRY,
    )


async def _run(orch, mgmt) -> dict:
    result = {"action": "unknown"}
    return await orch._handle_management("corr", -1, mgmt.raw_text, mgmt, result)


# ─── Tests ────────────────────────────────────────────────────────────────

def test_sl_to_entry_on_open_position_modifies_and_uses_entry_price():
    """Bug B: a real open position must MODIFY with SL == entry, no rejection."""
    inj = _inj_position()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT")))

    assert result["action"] == "modified", f"expected modified, got {result}"
    # Order service received the decision with the breakeven SL.
    dec = orch.order_service.last_decision
    assert dec is not None, "order_service.execute was never called"
    assert dec.sl_price == 5.045, f"SL should move to entry 5.045, got {dec.sl_price}"
    # Bug B: the decision must carry a valid entry_price (so Gate2 no longer
    # divides by zero) and a positive quantity (so it is never rejected).
    assert dec.entry_price == 5.045, "decision must carry entry_price (Bug B)"
    assert dec.quantity > 0, f"decision quantity must be > 0 (Bug B), got {dec.quantity}"
    # No rejection message sent.
    assert not any("rejected" in m for m in orch.notifier.messages), orch.notifier.messages


def test_explicit_pair_with_no_open_position_skips_and_does_not_mutate_other():
    """Bug A: CAKE command must NOT retarget/skip onto the open INJUSDT."""
    inj = _inj_position()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("CAKEUSDT")))

    assert result["action"] == "skipped", f"expected skipped, got {result}"
    assert result.get("skipped") is True
    # Critically: it must NOT have called order_service (no mutation of INJ).
    assert orch.order_service.calls == 0, "CAKE command must not execute on INJUSDT"
    assert "CAKEUSDT" in (result.get("reason") or ""), result.get("reason")
    # INJUSDT position object is untouched.
    assert inj.sl_price == 4.935, "INJUSDT SL must remain 4.935 (not mutated)"


def test_untargeted_command_with_single_open_position_still_resolves():
    """The legit fallback (no pair given, exactly one open) must still work."""
    inj = _inj_position()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt(None)))

    assert result["action"] == "modified", f"untargeted single-position should modify, got {result}"
    assert orch.order_service.last_decision.sl_price == 5.045


if __name__ == "__main__":
    test_sl_to_entry_on_open_position_modifies_and_uses_entry_price()
    test_explicit_pair_with_no_open_position_skips_and_does_not_mutate_other()
    test_untargeted_command_with_single_open_position_still_resolves()
    print("ALL SL-TO-ENTRY REGRESSION TESTS PASSED")
