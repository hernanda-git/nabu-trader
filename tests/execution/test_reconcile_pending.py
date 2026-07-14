"""Reconcile resting PENDING LIMIT entry orders that fill later.

Regression guard for the XPLUSDT incident: a LIMIT entry that fills after the
synchronous entry path already returned PENDING must still get a positions row
+ SL/TP. Without reconcile, the fill is orphaned (no DB record, no protection).
"""

import asyncio
import json

import pytest

from src.domain.models import Position
from src.execution.position_manager import PositionManager
from src.state.database import get_connection
from src.state.repositories import (
    DecisionRepository,
    OrderRepository,
    PositionEventRepository,
    PositionRepository,
    SignalRepository,
)
from src.exchange.base import OrderInfo


class FakeExchange:
    """Returns a FILLED order for the late-fill scenario and tracks placed SL/TP."""

    def __init__(self, avg_price, filled_qty):
        self.avg_price = avg_price
        self.filled_qty = filled_qty
        self.placed = []

    async def get_order(self, symbol, order_id):
        return OrderInfo(
            order_id=str(order_id),
            symbol=symbol,
            side="BUY",
            type="LIMIT",
            quantity=self.filled_qty,
            price=self.avg_price,
            status="FILLED",
            filled_quantity=self.filled_qty,
            avg_price=self.avg_price,
        )

    async def _load_futures_filters(self, symbol):
        return {"stepSize": 1.0, "tickSize": 0.0001, "minQty": 1.0,
                "minNotional": 1.0}

    async def get_open_orders(self, symbol=None):
        return []

    async def get_algo_open_orders(self, symbol=None):
        return []

    async def get_mark_price(self, symbol):
        return 0.089

    async def cancel_all_orders(self, symbol):
        return 0

    async def stop_loss(self, symbol, quantity, sl_price, side="SELL"):
        self.placed.append(("SL", symbol, sl_price, side, quantity))
        return OrderInfo(order_id="sl1", symbol=symbol, side=side, type="STOP",
                         quantity=quantity, price=sl_price, status="NEW")

    async def take_profit(self, symbol, quantity, tp_price, side="SELL"):
        self.placed.append(("TP", symbol, tp_price, side, quantity))
        return OrderInfo(order_id="tp1", symbol=symbol, side=side,
                         type="TAKE_PROFIT", quantity=quantity, price=tp_price,
                         status="NEW")


@pytest.fixture
def repos(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.row_factory = __import__("sqlite3").Row
    yield {
        "conn": conn,
        "order": OrderRepository(conn),
        "decision": DecisionRepository(conn),
        "position": PositionRepository(conn),
        "pevent": PositionEventRepository(conn),
    }
    conn.close()


def _seed(repos, symbol="XPLUSDT", side="BUY", qty=57.0, px=0.0892):
    c = repos["conn"]
    sid = c.execute(
        "INSERT INTO signals (message_id, channel, raw_text, pair, direction, "
        "sl_price, tp_prices) VALUES (1,'c','t',?,?,'0.08735',?)",
        (symbol, "LONG", json.dumps([0.0928])),
    ).lastrowid
    cid = c.execute(
        "INSERT INTO decisions (signal_id, action, pair, direction, quantity, "
        "confidence, reason) "
        "VALUES (?, 'ENTER', ?, ?, ?, 0.9, 't')",
        (sid, symbol, "LONG", 0.0),
    ).lastrowid
    oid = repos["order"].save(
        decision_id=cid, exchange="binance_futures", symbol=symbol,
        side=side, order_type="LIMIT", quantity=qty, price=px,
        client_order_id="lnr_x",
    )
    repos["order"].update_status(oid, "PENDING", "4008802163")
    return cid, oid


def test_reconcile_fills_late_limit_and_places_sl_tp(repos):
    cid, oid = _seed(repos)
    exch = FakeExchange(avg_price=0.0892, filled_qty=57.0)
    pm = PositionManager(
        exchange=exch, config={}, position_repo=repos["position"],
        order_repo=repos["order"], decision_repo=repos["decision"],
        position_event_repo=repos["pevent"], signal_repo=SignalRepository(repos["conn"]),
    )

    async def _run():
        await pm._reconcile_one({
            "id": oid, "symbol": "XPLUSDT", "side": "BUY", "order_type": "LIMIT",
            "decision_id": cid, "exchange_order_id": "4008802163",
        })

    asyncio.run(_run())

    # A positions row now exists with the filled size + SL/TP from the decision.
    pos = repos["position"].get_open_by_pair("XPLUSDT")
    assert pos is not None
    assert pos.quantity == 57.0
    assert pos.entry_price == 0.0892
    assert pos.sl_price == 0.08735
    assert pos.tp_prices == [0.0928]

    # SL + TP were placed via the exchange.
    kinds = {p[0] for p in exch.placed}
    assert "SL" in kinds and "TP" in kinds

    # Order row flipped PENDING -> FILLED (so it won't be reprocessed).
    cur = repos["conn"].execute("SELECT status FROM orders WHERE id=?", (oid,))
    assert cur.fetchone()["status"] == "FILLED"


def test_reconcile_is_idempotent(repos):
    cid, oid = _seed(repos)
    exch = FakeExchange(avg_price=0.0892, filled_qty=57.0)
    pm = PositionManager(
        exchange=exch, config={}, position_repo=repos["position"],
        order_repo=repos["order"], decision_repo=repos["decision"],
        position_event_repo=repos["pevent"], signal_repo=SignalRepository(repos["conn"]),
    )

    async def _run():
        row = {
            "id": oid, "symbol": "XPLUSDT", "side": "BUY", "order_type": "LIMIT",
            "decision_id": cid, "exchange_order_id": "4008802163",
        }
        # First pass creates the position.
        await pm._reconcile_one(row)
        # Second pass must be a no-op (position already open).
        await pm._reconcile_one(row)

    asyncio.run(_run())
    assert len(exch.placed) == 2  # exactly one SL + one TP placed total
    assert len(repos["position"].get_open_positions()) == 1


def test_reconcile_skips_still_resting(repos):
    cid, oid = _seed(repos)

    class RestingExchange(FakeExchange):
        async def get_order(self, symbol, order_id):
            return OrderInfo(order_id=str(order_id), symbol=symbol, side="BUY",
                             type="LIMIT", quantity=57.0, price=0.0892,
                             status="NEW", filled_quantity=0.0, avg_price=0.0)

    exch = RestingExchange(avg_price=0.0892, filled_qty=57.0)
    pm = PositionManager(
        exchange=exch, config={}, position_repo=repos["position"],
        order_repo=repos["order"], decision_repo=repos["decision"],
        position_event_repo=repos["pevent"], signal_repo=SignalRepository(repos["conn"]),
    )

    async def _run():
        await pm._reconcile_one({
            "id": oid, "symbol": "XPLUSDT", "side": "BUY", "order_type": "LIMIT",
            "decision_id": cid, "exchange_order_id": "4008802163",
        })

    asyncio.run(_run())
    assert repos["position"].get_open_by_pair("XPLUSDT") is None
    assert len(exch.placed) == 0


def test_self_heal_replaces_missing_protection(repos):
    """A position that has NO protection orders on the exchange (e.g. an
    XPLUSDT position reconciled before the Algo Order API existed, or whose
    conditional orders were cancelled externally) must have SL/TP (re)placed
    by the self-heal — and exactly once (tracked in _protection_placed)."""
    cid, oid = _seed(repos)
    exch = FakeExchange(avg_price=0.0892, filled_qty=57.0)
    pm = PositionManager(
        exchange=exch, config={}, position_repo=repos["position"],
        order_repo=repos["order"], decision_repo=repos["decision"],
        position_event_repo=repos["pevent"], signal_repo=SignalRepository(repos["conn"]),
    )

    from datetime import datetime, timezone
    # Pre-create the open position WITHOUT placing protection (simulates the
    # old state where the bot reconciled but left SL/TP only price-monitored).
    pos = Position(pair="XPLUSDT", direction="LONG", entry_price=0.0892,
                   quantity=57.0, sl_price=0.08735, tp_prices=[0.0928],
                   entry_time=datetime.now(timezone.utc).isoformat(),
                   status="OPEN")
    pid = repos["position"].create(pos)

    async def _run():
        await pm._check_position(pos)

    asyncio.run(_run())

    # Self-heal placed SL + TP via the exchange.
    kinds = {p[0] for p in exch.placed}
    assert "SL" in kinds and "TP" in kinds, f"self-heal placed: {exch.placed}"
    sl = next(p for p in exch.placed if p[0] == "SL")
    tp = next(p for p in exch.placed if p[0] == "TP")
    assert sl[2] == 0.08735
    assert tp[2] == 0.0928
    # Recorded so the next tick won't re-place.
    assert "XPLUSDT" in pm._protection_placed

    # Second tick must NOT place again (idempotent via _protection_placed).
    exch.placed.clear()
    asyncio.run(_run())
    assert len(exch.placed) == 0, f"self-heal re-placed on 2nd tick: {exch.placed}"
