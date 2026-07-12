"""Tests for the /db admin command processor (src/state/db_admin.py).

Uses a throwaway in-memory SQLite DB — no Telegram, no network.
"""

import sqlite3

import pytest

from src.state.db_admin import run_db_command


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE signals ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "message_id INTEGER UNIQUE, pair TEXT, raw_text TEXT)"
    )
    c.execute(
        "CREATE TABLE no_pk (name TEXT, val INTEGER)"
    )
    c.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT)"
    )
    # seed
    for i in range(25):
        c.execute(
            "INSERT INTO signals (message_id, pair, raw_text) VALUES (?,?,?)",
            (1000 + i, f"BTCUSDT{i%3}", f"signal {i}"),
        )
    c.commit()
    return c


# ── read ops ──

def test_usage_on_empty(conn):
    out = run_db_command(conn, "")
    assert "/db — trade DB admin" in out
    assert "delete" in out


def test_tables_lists(conn):
    out = run_db_command(conn, "tables")
    assert "signals" in out
    assert "25 rows" in out


def test_schema(conn):
    out = run_db_command(conn, "schema signals")
    assert "id" in out and "🔑PK" in out
    assert "pair" in out


def test_schema_unknown_table(conn):
    assert "does not exist" in run_db_command(conn, "schema nope")


def test_list_first_page(conn):
    out = run_db_command(conn, "list signals")
    assert "page 1/3" in out  # 25 rows / 10 per page = 3 pages
    assert "signal 24" in out  # newest first (rowid DESC)


def test_list_second_page(conn):
    out = run_db_command(conn, "list signals 2")
    assert "page 2/3" in out
    assert "Next:" in out


def test_list_last_page_no_next(conn):
    out = run_db_command(conn, "list signals 3")
    assert "page 3/3" in out
    assert "end of table" in out


def test_list_unknown_table(conn):
    assert "does not exist" in run_db_command(conn, "list nope")


def test_get_row(conn):
    out = run_db_command(conn, "get signals 1")
    assert "`signals` #1" in out
    assert "pair" in out


def test_get_missing_row(conn):
    assert "not found" in run_db_command(conn, "get signals 999")


def test_get_table_without_pk(conn):
    # no_pk has no INTEGER PRIMARY KEY
    assert "no INTEGER PRIMARY KEY" in run_db_command(conn, "get no_pk foo")


# ── delete (confirmation gate) ──

def test_delete_preview_then_confirm(conn):
    # preview
    preview = run_db_command(conn, "delete signals 1")
    assert "DELETE preview" in preview
    assert "delete signals 1 !" in preview
    # still present
    assert run_db_command(conn, "get signals 1") != "not found"
    # confirm
    out = run_db_command(conn, "delete signals 1 !")
    assert "Deleted" in out
    assert "not found" in run_db_command(conn, "get signals 1")


def test_delete_missing_row(conn):
    assert "not found" in run_db_command(conn, "delete signals 999 !")


def test_delete_readonly_table_rejected(conn):
    # audit_log exists in the fixture but is not in WRITABLE_TABLES
    assert "read-only" in run_db_command(conn, "delete audit_log 1")


# ── update (confirmation gate) ──

def test_update_preview_then_confirm(conn):
    preview = run_db_command(conn, "update signals 2 pair=XRPUSDT")
    assert "UPDATE preview" in preview
    assert "pair='XRPUSDT'" in preview
    # confirm
    out = run_db_command(conn, "update signals 2 pair=XRPUSDT !")
    assert "Updated" in out
    row = conn.execute("SELECT pair FROM signals WHERE id=2").fetchone()
    assert row["pair"] == "XRPUSDT"


def test_update_multiple_cols(conn):
    out = run_db_command(conn, "update signals 3 pair=ETHUSDT raw_text='hi there' !")
    assert "Updated" in out
    row = conn.execute("SELECT pair, raw_text FROM signals WHERE id=3").fetchone()
    assert row["pair"] == "ETHUSDT"
    assert row["raw_text"] == "hi there"


def test_update_cannot_change_pk(conn):
    assert "primary key" in run_db_command(conn, "update signals 4 id=99 !")


def test_update_unknown_column(conn):
    assert "Unknown column" in run_db_command(conn, "update signals 4 nope=1 !")


def test_update_missing_row(conn):
    assert "not found" in run_db_command(conn, "update signals 999 pair=X !")


# ── insert (confirmation gate) ──

def test_insert_preview_then_confirm(conn):
    preview = run_db_command(conn, "insert signals (message_id=2000, pair=SOLUSDT)")
    assert "INSERT preview" in preview
    out = run_db_command(conn, "insert signals (message_id=2000, pair=SOLUSDT) !")
    assert "Inserted" in out
    row = conn.execute("SELECT pair FROM signals WHERE message_id=2000").fetchone()
    assert row["pair"] == "SOLUSDT"


def test_insert_value_coercion(conn):
    run_db_command(conn, "insert signals (message_id=3000, pair=ADAUSDT, raw_text=NULL) !")
    row = conn.execute("SELECT raw_text FROM signals WHERE message_id=3000").fetchone()
    assert row["raw_text"] is None


def test_insert_unknown_column(conn):
    assert "Unknown column" in run_db_command(
        conn, "insert signals (bogus=1) !"
    )


def test_insert_bad_syntax_missing_parens(conn):
    assert "Usage" in run_db_command(conn, "insert signals message_id=1")


# ── safety: SQL injection can't escape the parameterized VALUES ──

def test_insert_injection_is_parameterized(conn):
    # A value containing SQL should be stored verbatim, not executed.
    evil = "1); DROP TABLE signals; --"
    run_db_command(conn, f"insert signals (message_id=4000, raw_text='{evil}') !")
    row = conn.execute("SELECT raw_text FROM signals WHERE message_id=4000").fetchone()
    assert row["raw_text"] == evil
    # table still exists
    assert "signals" in run_db_command(conn, "tables")


def test_delete_requires_confirmation_no_accidental_run(conn):
    # Without `!`, nothing is deleted.
    before = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    run_db_command(conn, "delete signals 1")  # preview only
    after = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert before == after == 25
