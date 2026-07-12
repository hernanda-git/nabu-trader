"""Health report must surface the margin-per-trade ("port") and open-position count.

Regression guard for the `/health` portfolio line: both the on-demand command
and the periodic reporter share ``build_health_report``, so one test covers both.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.health.reporter import build_health_report


class _FakePosition:
    def __init__(self, symbol):
        self.symbol = symbol


def _make_listener(port_usdt: float, n_positions: int):
    exchange = SimpleNamespace(
        get_balance=lambda: __import__("asyncio").sleep(0, result=SimpleNamespace(total_usdt=100.0)),
        get_positions=lambda: __import__("asyncio").sleep(
            0, result=[_FakePosition(f"SYM{i}") for i in range(n_positions)]
        ),
    )
    orchestrator = SimpleNamespace(config={"risk": {"port_usdt": port_usdt}})
    notifier = SimpleNamespace(
        check_connection=lambda: __import__("asyncio").sleep(0, result={"status": "ok", "bot": "bot"}),
    )
    client = SimpleNamespace(
        is_user_authorized=lambda: __import__("asyncio").sleep(0, result=True),
        get_me=lambda: SimpleNamespace(first_name="me"),
        get_entity=lambda c: SimpleNamespace(username="ch"),
    )
    return SimpleNamespace(
        notifier=notifier, client=client, channel="ch",
        orchestrator=orchestrator, exchange=exchange,
    )


@pytest.mark.asyncio
async def test_health_reports_port_and_position_count():
    listener = _make_listener(port_usdt=2.0, n_positions=3)
    lines, n_ok, n_fail = await build_health_report(listener)

    portfolio_line = next((l for l in lines if "💼 Portfolio" in l), None)
    assert portfolio_line is not None, "Portfolio line missing from health report"
    assert "3 open" in portfolio_line
    assert "$2.00" in portfolio_line


@pytest.mark.asyncio
async def test_health_reports_unset_port_without_crash():
    # No risk/port_usdt configured at all -> still renders, no exception.
    listener = _make_listener(port_usdt=None, n_positions=0)
    lines, n_ok, n_fail = await build_health_report(listener)
    portfolio_line = next((l for l in lines if "💼 Portfolio" in l), None)
    assert portfolio_line is not None
    assert "0 open" in portfolio_line
    assert "unset" in portfolio_line
