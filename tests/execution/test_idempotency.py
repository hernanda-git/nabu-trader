"""Task 4 — idempotent entry: a given decision can only ever have one active
entry LIMIT. OrderRepository.get_active_for_decision returns the active order
(if any) so OrderService can skip duplicates.
"""

import pytest

from src.state.repositories import OrderRepository
from src.state.database import get_connection


@pytest.fixture
def order_repo(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.row_factory = __import__("sqlite3").Row
    yield OrderRepository(conn)
    conn.close()


def test_get_active_for_decision_finds_pending(order_repo):
    sid = order_repo.conn.execute(
        "INSERT INTO signals (message_id, channel, raw_text, pair, direction) "
        "VALUES (1,'c','t','APEUSDT','LONG')"
    ).lastrowid
    cid = order_repo.conn.execute(
        "INSERT INTO decisions (signal_id, action, pair, direction, quantity, confidence, reason) "
        "VALUES (?,'ENTER','APEUSDT','LONG',31,0.9,'t')", (sid,)
    ).lastrowid
    oid = order_repo.save(
        decision_id=cid, exchange="binance_futures", symbol="APEUSDT",
        side="BUY", order_type="LIMIT", quantity=31, price=0.162,
        client_order_id="lnr_7_abc",
    )
    order_repo.update_status(oid, "PENDING", "12345")
    active = order_repo.get_active_for_decision(cid, "APEUSDT", "BUY")
    assert active is not None
    assert active["exchange_order_id"] == "12345"
    assert active["status"] == "PENDING"


def test_get_active_for_decision_none_when_terminal(order_repo):
    sid = order_repo.conn.execute(
        "INSERT INTO signals (message_id, channel, raw_text, pair, direction) "
        "VALUES (2,'c','t','APEUSDT','LONG')"
    ).lastrowid
    cid = order_repo.conn.execute(
        "INSERT INTO decisions (signal_id, action, pair, direction, quantity, confidence, reason) "
        "VALUES (?,'ENTER','APEUSDT','LONG',31,0.9,'t')", (sid,)
    ).lastrowid
    oid = order_repo.save(
        decision_id=cid, exchange="binance_futures", symbol="APEUSDT",
        side="BUY", order_type="LIMIT", quantity=31, price=0.162,
        client_order_id="lnr_8_abc",
    )
    # A CANCELED/REJECTED entry is terminal -> safe to re-enter (no active order)
    order_repo.update_status(oid, "CANCELED", "999")
    assert order_repo.get_active_for_decision(cid, "APEUSDT", "BUY") is None
