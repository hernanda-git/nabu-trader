"""Regression tests for tpN / full management commands.

These exercise the REAL TradeOrchestrator._handle_management routing with fakes
for the exchange/order_service/notifier and a real SafetyGate2. They prove the
"recent one failed" class of bugs is gone: text like "tp1" / "full" is now a
parser-level management command (no LLM) that actually executes.

Coverage:
- tpN on an open position -> MODIFY with that TP level, rest stays open.
- tpN with no available price (no stored tp_prices, none in text) -> SKIP.
- full on an open position -> CLOSE (full market close).
- tpN / full targeting a pair with NO open position -> SKIP, never mutates
  another open position (same authoritative-pair rule as sl-to-entry).
- tpN / full with no pair and >1 open position -> SKIP (too destructive to guess).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from src.agent.gate import SafetyGate2
from src.domain.models import Position, TradeSignal
from src.orchestrator import TradeOrchestrator, _SL_TO_ENTRY


# ─── Fakes ────────────────────────────────────────────────────────────────

class _FakeOrderService:
    """Routes CLOSE/MODIFY like the real OrderService but without the network,
    recording the decision and applying it to the (fake) position repo."""

    def __init__(self, position_repo):
        self.position_repo = position_repo
        self.last_decision = None
        self.calls = 0

    async def execute(self, signal_db_id, decision):
        self.last_decision = decision
        self.calls += 1

        if decision.action == "CLOSE":
            # Mimic _close_position: mark the position closed.
            self.position_repo.close_position(1, exit_price=decision.entry_price,
                                               pnl=0.0, reason="FULL")
            stat = "CLOSED"
        elif decision.action == "MODIFY":
            self.position_repo.update_sl_tp(
                1, sl_price=decision.sl_price, tp_prices=decision.tp_prices)
            stat = "MODIFIED"
        else:
            stat = "OK"

        @dataclass
        class _Res:
            success: bool = True
            order_id: str = "x"
            symbol: str = decision.pair
            side: str = ""
            filled_quantity: float = decision.quantity
            avg_price: float = decision.entry_price or 0.0
            status: str = stat
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
    def __init__(self, open_by_pair: dict[str, Position]):
        self._open_by_pair = open_by_pair
        self._closed = []
        self._updated = []

    def get_open_by_pair(self, pair: str) -> Position | None:
        return self._open_by_pair.get(pair.upper())

    def get_open_positions(self) -> list[Position]:
        return list(self._open_by_pair.values())

    def get_open_count(self) -> int:
        return len(self._open_by_pair)

    def get_daily_pnl(self) -> float:
        return 0.0

    def get_total_notional_usdt(self) -> float:
        return sum(p.entry_price * p.quantity for p in self._open_by_pair.values())

    def update_sl_tp(self, position_id, sl_price=None, tp_prices=None):
        self._updated.append((position_id, sl_price, tp_prices))

    def close_position(self, position_id, exit_price, pnl, reason="", closed_by="MANUAL"):
        self._closed.append((position_id, exit_price, pnl, reason))


class _SignalRepo:
    def __init__(self):
        self.processed = []

    def mark_processed(self, message_id, raw_text):
        self.processed.append(message_id)

    def save(self, signal):
        return 1


def _make_orchestrator(open_by_pair: dict[str, Position]):
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
    order_service = _FakeOrderService(pos_repo)
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


def _inj(tps=(5.139, 5.39)):
    return Position(id=1, pair="INJUSDT", direction="LONG",
                    entry_price=5.045, quantity=1.0, sl_price=4.935, tp_prices=list(tps))


def _mgmt(pair, action, tp_index=None, tp_price=None):
    tp_prices = [tp_price] if tp_price is not None else []
    return TradeSignal(
        message_id=-1, channel="manual", raw_text=f"{pair} {action}",
        pair=pair, direction=None,
        sl_price=_SL_TO_ENTRY if action == "SL_ENTRY" else None,
        tp_prices=tp_prices, mgmt_action=action, tp_index=tp_index,
    )


async def _run(orch, mgmt):
    return await orch._handle_management("corr", -1, mgmt.raw_text, mgmt, {"action": "unknown"})


# ─── Tests ────────────────────────────────────────────────────────────────

def test_tp1_refreshes_that_tp_level_and_keeps_position_open():
    inj = _inj()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT", "TP", tp_index=0)))

    assert result["action"] == "modified", result
    dec = orch.order_service.last_decision
    assert dec.action == "MODIFY"
    assert dec.tp_prices == [5.139], f"tp1 should be 5.139, got {dec.tp_prices}"
    # SL preserved; position NOT closed.
    assert dec.sl_price == 4.935
    assert orch.position_repo._closed == [], "position must stay open after tp1"
    assert orch.position_repo._updated, "update_sl_tp should have been called"


def test_tp2_uses_second_stored_tp_level():
    inj = _inj()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT", "TP", tp_index=1)))

    assert result["action"] == "modified"
    assert orch.order_service.last_decision.tp_prices == [5.39]


def test_tp_with_explicit_price_in_text_overrides_stored():
    inj = _inj()  # stored tp1 = 5.139
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT", "TP", tp_index=0, tp_price=5.25)))

    assert result["action"] == "modified"
    assert orch.order_service.last_decision.tp_prices == [5.25]


def test_tp_with_no_price_available_skips():
    inj = _inj(tps=[])  # no stored TPs
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT", "TP", tp_index=0)))

    assert result["action"] == "skipped", result
    assert orch.order_service.calls == 0, "should not execute without a TP price"


def test_full_closes_the_position():
    inj = _inj()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("INJUSDT", "FULL")))

    assert result["action"] == "closed", result
    dec = orch.order_service.last_decision
    assert dec.action == "CLOSE"
    assert orch.position_repo._closed, "position should be closed on FULL"


def test_tp_targeting_wrong_pair_skips_and_does_not_mutate_other():
    inj = _inj()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("CAKEUSDT", "TP", tp_index=0)))

    assert result["action"] == "skipped", result
    assert orch.order_service.calls == 0, "CAKE tp must not touch INJUSDT"
    assert orch.position_repo._updated == [] and orch.position_repo._closed == []
    assert inj.sl_price == 4.935 and inj.tp_prices == [5.139, 5.39]


def test_full_targeting_wrong_pair_skips_and_does_not_mutate_other():
    inj = _inj()
    orch = _make_orchestrator({"INJUSDT": inj})

    result = asyncio.run(_run(orch, _mgmt("CAKEUSDT", "FULL")))

    assert result["action"] == "skipped", result
    assert orch.order_service.calls == 0, "CAKE full must not close INJUSDT"
    assert orch.position_repo._closed == []


def test_untargeted_tp_with_multiple_opens_skips():
    inj = _inj()
    cake = Position(id=2, pair="CAKEUSDT", direction="LONG", entry_price=1.4,
                    quantity=10.0, sl_price=1.37, tp_prices=[1.535])
    orch = _make_orchestrator({"INJUSDT": inj, "CAKEUSDT": cake})

    # No pair given -> tpN must NOT guess; too destructive.
    result = asyncio.run(_run(orch, _mgmt(None, "TP", tp_index=0)))

    assert result["action"] == "skipped", result
    assert orch.order_service.calls == 0


def test_untargeted_full_with_multiple_opens_skips():
    inj = _inj()
    cake = Position(id=2, pair="CAKEUSDT", direction="LONG", entry_price=1.4,
                    quantity=10.0, sl_price=1.37, tp_prices=[1.535])
    orch = _make_orchestrator({"INJUSDT": inj, "CAKEUSDT": cake})

    result = asyncio.run(_run(orch, _mgmt(None, "FULL")))

    assert result["action"] == "skipped", result
    assert orch.order_service.calls == 0
    assert orch.position_repo._closed == []


if __name__ == "__main__":
    test_tp1_refreshes_that_tp_level_and_keeps_position_open()
    test_tp2_uses_second_stored_tp_level()
    test_tp_with_explicit_price_in_text_overrides_stored()
    test_tp_with_no_price_available_skips()
    test_full_closes_the_position()
    test_tp_targeting_wrong_pair_skips_and_does_not_mutate_other()
    test_full_targeting_wrong_pair_skips_and_does_not_mutate_other()
    test_untargeted_tp_with_multiple_opens_skips()
    test_untargeted_full_with_multiple_opens_skips()
    print("ALL TP/FULL MANAGEMENT REGRESSION TESTS PASSED")
