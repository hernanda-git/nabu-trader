"""Admin DB command processor for the Telegram `/db` command.

This module is intentionally free of Telegram/async concerns so it can be unit
tested against a throwaway in-memory SQLite DB. The listener passes the raw
command text + a live connection; the function returns a Markdown-formatted
reply string (or a plain-text-safe string).

Safety model
------------
- Destructive operations (`delete`/`update`/`insert`) are gated behind a
  confirmation step: the first call shows a preview and the exact confirmation
  command; the second call with the `!` suffix actually executes.
- A small allow-list of tables is editable; everything is readable.
- SQL injection in DELETE/UPDATE/INSERT is prevented by binding VALUES
  parameters positionally (`?`) — never string-formatting user input into SQL.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# Tables the user is allowed to modify. Signals/decisions/orders are the trade
# history; pending_signals is the conditional queue. Core audit tables
# (trade_logs, position_events, llm_interactions) are read-only here.
WRITABLE_TABLES = {
    "signals", "decisions", "orders", "executions", "positions",
    "pending_signals", "processed_signals", "events", "daily_stats",
    "config_snapshots",
}

# Page size for /db list.
PAGE_SIZE = 10

# Confirmation sentinel appended to a destructive command.
CONFIRM = "!"

_TOKEN_RE = re.compile(
    r"""\w+=(?:"[^"]*"|'[^']*'|`[^`]*`)   # key=quoted_value (kept whole)
       | "(?:[^"]*)" | '(?:[^']*)' | `(?:[^`]*)`   # standalone quoted strings
       | [^,\s]+""",                              # bare tokens
    re.VERBOSE,
)


def _split_args(text: str) -> list[str]:
    """Split on whitespace but keep quoted strings (single/double/backtick) intact."""
    return _TOKEN_RE.findall(text)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _pk_of(conn: sqlite3.Connection, table: str) -> str | None:
    """Return the first INTEGER PRIMARY KEY column name for a table, else None."""
    cur = conn.execute(f"PRAGMA table_info('{table}')")
    for r in cur.fetchall():
        if (r["pk"] == 1) and ("INTEGER" in (r["type"] or "").upper()):
            return r["name"]
    # fall back to first pk column
    cur = conn.execute(f"PRAGMA table_info('{table}')")
    for r in cur.fetchall():
        if r["pk"] == 1:
            return r["name"]
    return None


def _coerce(value: str) -> Any:
    """Best-effort coercion of a VALUE token to int/float/None, else str."""
    if value == "NULL":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _render_rows(rows: list[sqlite3.Row], columns: list[str]) -> str:
    """Render a set of rows as a compact, Markdown-safe table/text block."""
    if not rows:
        return "_(no rows)_"
    lines = []
    for i, row in enumerate(rows, 1):
        parts = []
        for col in columns:
            v = row[col]
            if isinstance(v, str) and len(v) > 60:
                v = v[:57] + "..."
            elif isinstance(v, float):
                v = f"{v:.6g}"
            parts.append(f"{col}={v}")
        lines.append(f"{i}. " + " | ".join(parts))
    return "\n".join(lines)


def _truncate(text: str, limit: int = 3800) -> str:
    """Telegram messages must stay under ~4096 chars; leave headroom."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… _(truncated, {len(text) - limit} more chars)_"


def run_db_command(conn: sqlite3.Connection, raw: str) -> str:
    """Execute a `/db` subcommand and return a reply string.

    `raw` is the text AFTER the leading `/db`. SQL/table/column names are
    matched case-sensitively (SQLite treats them as such); the subcommand verb
    is matched case-insensitively. Returns a human-readable Markdown reply.
    """
    text = (raw or "").strip()
    if not text:
        return _usage()

    parts = _split_args(text)
    verb = parts[0].lower()
    args = parts[1:]

    try:
        if verb in ("tables", "ls"):
            return _cmd_tables(conn)
        if verb == "schema":
            if not args:
                return "⚠️ Usage: `/db schema <table>`"
            return _cmd_schema(conn, args[0])
        if verb == "list":
            return _cmd_list(conn, args)
        if verb == "get":
            return _cmd_get(conn, args)
        if verb == "delete":
            return _cmd_delete(conn, args)
        if verb == "update":
            return _cmd_update(conn, args)
        if verb == "insert":
            return _cmd_insert(conn, args)
        return _usage()
    except sqlite3.Error as e:
        return f"❌ **SQL error:** `{e}`"
    except (ValueError, IndexError) as e:
        return f"❌ **Bad argument:** `{e}`"


# ── Subcommands ──────────────────────────────────────────────────────────────

def _usage() -> str:
    return (
        "🗄 **/db — trade DB admin**\n\n"
        "  `/db tables` — list all tables + row counts\n"
        "  `/db schema <table>` — show columns of a table\n"
        "  `/db list <table> [page]` — page through rows (10/page)\n"
        "  `/db get <table> <id>` — show one row by primary key\n"
        "  `/db delete <table> <id>` — DELETE a row (needs `!` confirm)\n"
        "  `/db update <table> <id> <col>=<val> [...]` — UPDATE (needs `!`)\n"
        "  `/db insert <table> (<col>=<val>, ...)` — INSERT (needs `!`)\n\n"
        "⚠️ **Destructive ops preview first, then re-run with `!` to confirm.**\n"
        "Example: `/db delete pending_signals 5` → preview → `/db delete pending_signals 5 !`"
    )


def _cmd_tables(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    lines = ["📋 **Tables**"]
    for r in rows:
        name = r["name"]
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM '{name}'").fetchone()[0]
        except sqlite3.Error:
            n = "?"
        flag = "" if name in WRITABLE_TABLES else "  _(read-only)_"
        lines.append(f"  • `{name}` — {n} rows{flag}")
    return _truncate("\n".join(lines))


def _cmd_schema(conn: sqlite3.Connection, table: str) -> str:
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    rows = conn.execute(f"PRAGMA table_info('{table}')").fetchall()
    lines = [f"🧬 **Schema: `{table}`**"]
    for r in rows:
        pk = " 🔑PK" if r["pk"] == 1 else ""
        nn = " NOT NULL" if r["notnull"] == 1 else ""
        dflt = f" DEFAULT {r['dflt_value']}" if r["dflt_value"] is not None else ""
        lines.append(f"  • `{r['name']}` `{r['type']}{nn}{dflt}{pk}`")
    return _truncate("\n".join(lines))


def _cmd_list(conn: sqlite3.Connection, args: list[str]) -> str:
    if not args:
        return "⚠️ Usage: `/db list <table> [page]`"
    table = args[0]
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    page = 1
    if len(args) > 1:
        try:
            page = int(args[1])
        except ValueError:
            return f"❌ Invalid page: `{args[1]}`"
    if page < 1:
        page = 1
    offset = (page - 1) * PAGE_SIZE

    total = conn.execute(f"SELECT COUNT(*) FROM '{table}'").fetchone()[0]
    cols = [c["name"] for c in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
    rows = conn.execute(
        f"SELECT * FROM '{table}' ORDER BY rowid DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, offset),
    ).fetchall()

    last_page = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    header = f"📄 **`{table}`** — page {page}/{last_page} ({total} rows total)"
    if not rows:
        return f"{header}\n\n_(empty page)_"
    body = _render_rows(rows, cols)
    nav = f"\n\nNext: `/db list {table} {page + 1}`" if page < last_page else "\n\n_end of table_"
    return _truncate(f"{header}\n\n{body}{nav}")


def _cmd_get(conn: sqlite3.Connection, args: list[str]) -> str:
    if len(args) < 2:
        return "⚠️ Usage: `/db get <table> <id>`"
    table = args[0]
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    pk = _pk_of(conn, table)
    if pk is None:
        return f"❌ Table `{table}` has no INTEGER PRIMARY KEY — use `/db list`."
    try:
        rid = int(args[1])
    except ValueError:
        return f"❌ Invalid id: `{args[1]}`"

    row = conn.execute(f"SELECT * FROM '{table}' WHERE \"{pk}\"=?", (rid,)).fetchone()
    if row is None:
        return f"⚠️ `{table}` #{rid} not found."
    cols = [c["name"] for c in conn.execute(f"PRAGMA table_info('{table}')").fetchall()]
    lines = [f"🔎 **`{table}` #{rid}** (PK `{pk}`)"]
    for col in cols:
        v = row[col]
        if isinstance(v, float):
            v = f"{v:.6g}"
        elif isinstance(v, str) and len(v) > 400:
            v = v[:397] + "..."
        lines.append(f"  • `{col}`: `{v}`")
    return _truncate("\n".join(lines))


def _parse_col_eq(tokens: list[str]) -> dict[str, Any]:
    """Parse `col=value` tokens into a dict, coercing value types."""
    out: dict[str, Any] = {}
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"expected col=value, got `{tok}`")
        col, val = tok.split("=", 1)
        col = col.strip().strip("`").strip('"').strip("'")
        val = val.strip()
        # strip surrounding quotes from the value too
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'`":
            val = val[1:-1]
        out[col] = _coerce(val)
    return out


def _cmd_delete(conn: sqlite3.Connection, args: list[str]) -> str:
    if len(args) < 2:
        return "⚠️ Usage: `/db delete <table> <id>`  (add ` !` to confirm)"
    table = args[0]
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    if table not in WRITABLE_TABLES:
        return f"🚫 Table `{table}` is read-only via /db."
    pk = _pk_of(conn, table)
    if pk is None:
        return f"❌ Table `{table}` has no INTEGER PRIMARY KEY — cannot target by id."

    # confirmation?
    confirmed = args[-1] == CONFIRM
    if confirmed:
        args = args[:-1]
    if len(args) < 2:
        return "⚠️ Usage: `/db delete <table> <id>`  (add ` !` to confirm)"
    try:
        rid = int(args[1])
    except ValueError:
        return f"❌ Invalid id: `{args[1]}`"

    exists = conn.execute(f"SELECT 1 FROM '{table}' WHERE \"{pk}\"=?", (rid,)).fetchone()
    if not exists:
        return f"⚠️ `{table}` #{rid} not found."

    if not confirmed:
        return (
            f"🗑 **DELETE preview** — `{table}` #{rid}\n\n"
            f"_This will permanently remove the row._\n"
            f"Confirm with: `/db delete {table} {rid} !`"
        )

    conn.execute(f"DELETE FROM '{table}' WHERE \"{pk}\"=?", (rid,))
    conn.commit()
    return f"✅ **Deleted** `{table}` #{rid}"


def _cmd_update(conn: sqlite3.Connection, args: list[str]) -> str:
    if len(args) < 3:
        return "⚠️ Usage: `/db update <table> <id> <col>=<val> [<col>=<val> ...]`  (add ` !`)"
    table = args[0]
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    if table not in WRITABLE_TABLES:
        return f"🚫 Table `{table}` is read-only via /db."
    pk = _pk_of(conn, table)
    if pk is None:
        return f"❌ Table `{table}` has no INTEGER PRIMARY KEY — cannot target by id."

    confirmed = args[-1] == CONFIRM
    work = args[1:] if not confirmed else args[1:-1]
    if len(work) < 2:
        return "⚠️ Usage: `/db update <table> <id> <col>=<val> [...]  (add ` !`)"
    try:
        rid = int(work[0])
    except ValueError:
        return f"❌ Invalid id: `{work[0]}`"

    try:
        updates = _parse_col_eq(work[1:])
    except ValueError as e:
        return f"❌ {e}"

    if pk in updates:
        return f"🚫 Cannot update the primary key column `{pk}`."
    valid_cols = {c["name"] for c in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    bad = set(updates) - valid_cols
    if bad:
        return f"❌ Unknown column(s): {', '.join('`'+b+'`' for b in sorted(bad))}"

    exists = conn.execute(f"SELECT 1 FROM '{table}' WHERE \"{pk}\"=?", (rid,)).fetchone()
    if not exists:
        return f"⚠️ `{table}` #{rid} not found."

    if not confirmed:
        preview = ", ".join(f"{k}={updates[k]!r}" for k in updates)
        return (
            f"✏️ **UPDATE preview** — `{table}` #{rid}\n"
            f"  SET {preview}\n\n"
            f"Confirm with: `/db update {table} {rid} "
            + " ".join(f"{k}={updates[k]!r}" for k in updates)
            + " !`"
        )

    set_clause = ", ".join(f'"{k}"=?' for k in updates)
    params = list(updates.values()) + [rid]
    conn.execute(f"UPDATE '{table}' SET {set_clause} WHERE \"{pk}\"=?", params)
    conn.commit()
    return f"✅ **Updated** `{table}` #{rid}: {len(updates)} column(s)"


def _cmd_insert(conn: sqlite3.Connection, args: list[str]) -> str:
    if len(args) < 1:
        return "⚠️ Usage: `/db insert <table> (<col>=<val>, <col>=<val> ...)`  (add ` !`)"
    table = args[0]
    if not _table_exists(conn, table):
        return f"❌ Table `{table}` does not exist."
    if table not in WRITABLE_TABLES:
        return f"🚫 Table `{table}` is read-only via /db."

    confirmed = args[-1] == CONFIRM
    work = args[1:] if not confirmed else args[1:-1]

    # Expect the values wrapped in parentheses: (col=val, col=val)
    joined = " ".join(work)
    m = re.match(r"^\((.+)\)$", joined.strip(), re.DOTALL)
    if not m:
        return "⚠️ Usage: `/db insert <table> (<col>=<val>, <col>=<val> ...)`  (add ` !`)"
    inner = m.group(1)
    toks = [t for t in _TOKEN_RE.findall(inner) if t.strip()]
    try:
        values = _parse_col_eq(toks)
    except ValueError as e:
        return f"❌ {e}"
    if not values:
        return "❌ No column=value pairs supplied."

    valid_cols = {c["name"] for c in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
    bad = set(values) - valid_cols
    if bad:
        return f"❌ Unknown column(s): {', '.join('`'+b+'`' for b in sorted(bad))}"

    if not confirmed:
        preview = ", ".join(f"{k}={values[k]!r}" for k in values)
        return (
            f"➕ **INSERT preview** — into `{table}`\n"
            f"  ({preview})\n\n"
            f"Confirm with: `/db insert {table} ({preview}) !`"
        )

    cols = list(values)
    placeholders = ", ".join("?" for _ in cols)
    col_clause = ", ".join(f'"{c}"' for c in cols)
    params = list(values.values())
    cur = conn.execute(
        f"INSERT INTO '{table}' ({col_clause}) VALUES ({placeholders})", params
    )
    conn.commit()
    new_id = cur.lastrowid
    return f"✅ **Inserted** into `{table}` → new rowid #{new_id}"
