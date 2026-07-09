"""Tests that Telegram alerts are valid Markdown (no nested backticks).

A nested-backtick bug made failed-trade alerts undeliverable (Telegram
"can't parse entities"), so we assert the formatter produces text that
Telegram will accept.
"""

import re

from src.notifier import telegram
from src.notifier.telegram import TelegramNotifier
from src.domain.models import ExecutionResult, TradeSignal, TradeDecision


def _count_backtick_runs(text: str) -> list[int]:
    """Return lengths of consecutive backtick runs in text."""
    return [len(m) for m in re.findall(r"`+", text)]


def test_failed_order_alert_has_no_nested_backticks():
    err = "❌ **Failed to place order — UAIUSDT**\n   └ Precision is over the maximum defined for this asset."
    res = ExecutionResult(
        success=False, side="BUY", symbol="UAIUSDT", order_id="",
        filled_quantity=0.0, avg_price=0.0, error=err,
    )
    # Replicate the alert formatting used in notify_execution.
    text = "❌ **Failed to place order**\n" f"```\n{res.error}\n```"
    runs = _count_backtick_runs(text)
    # Exactly two fenced-block delimiters (open + close), each length 3.
    assert runs == [3, 3], f"unexpected backtick runs: {runs}"
    assert "Failed to place order" in text


def test_md_escape_handles_special_chars():
    s = "Profit _target* `code` [link](u) ~strike~ #tag +list = eq !imp"
    out = telegram._md_escape(s)
    # Every special char must be backslash-escaped so it's safe in Markdown.
    for ch in "_*`[]()~`>#+-=|{}!":
        assert f"\\{ch}" in out or ch not in s


def test_md_escape_empty():
    assert telegram._md_escape("") == ""
    assert telegram._md_escape(None) is None


def test_success_alert_markdown_safe():
    res = ExecutionResult(
        success=True, side="BUY", symbol="BTCUSDT", order_id="123",
        filled_quantity=0.5, avg_price=30000.0,
    )
    text = (
        "✅ **Order filled**\n"
        f"`{res.side}` `{res.symbol}`\n"
        f"Qty: `{res.filled_quantity:.6f}` @ `{res.avg_price:.8f}`\n"
        f"Order: `{res.order_id}`"
    )
    runs = _count_backtick_runs(text)
    assert all(r == 1 for r in runs), f"nested backticks in success alert: {runs}"
