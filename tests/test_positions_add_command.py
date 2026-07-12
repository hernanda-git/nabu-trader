"""Tests for the manual /positions add command parser and pair normalizer.

These cover the pure, side-effect-free logic in src.listener so the command
grammar and validation are locked down without needing Telegram or an exchange.
"""
import pytest

from src.listener import normalize_pair, parse_positions_add


# ─── normalize_pair ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("btcusdt", "BTCUSDT"),
    ("#eth", "ETHUSDT"),
    ("$SOL", "SOLUSDT"),
    ("ETHUSDT", "ETHUSDT"),
    ("pepe", "PEPEUSDT"),          # bare base → USDT appended
    ("", ""),
])
def test_normalize_pair(raw, expected):
    assert normalize_pair(raw) == expected


def test_normalize_pair_lowercase_handled():
    # Case is normalized internally.
    assert normalize_pair("BtcUsDt") == "BTCUSDT"


# ─── parse_positions_add: valid plans ────────────────────────────────────────

def test_parse_minimal_no_side_limit():
    plan = parse_positions_add(["btcusdt", "10", "20", "60000", "65000", "58000"])
    assert plan["ok"] is True
    assert plan["side"] is None            # inferred from price vs market
    assert plan["pair"] == "BTCUSDT"
    assert plan["margin_usdt"] == 10.0
    assert plan["leverage"] == 20
    assert plan["price"] == 60000.0
    assert plan["market"] is False
    assert plan["tp"] == 65000.0
    assert plan["sl"] == 58000.0


def test_parse_explicit_long_market_with_tp_only():
    plan = parse_positions_add(["LONG", "ethusdt", "5", "10", "market", "3500"])
    assert plan["ok"] is True
    assert plan["side"] == "LONG"
    assert plan["market"] is True
    assert plan["price"] is None
    assert plan["tp"] == 3500.0
    assert plan["sl"] is None


def test_parse_short_market_no_tp_sl():
    plan = parse_positions_add(["SHORT", "solusdt", "2", "5", "market"])
    assert plan["ok"] is True
    assert plan["side"] == "SHORT"
    assert plan["market"] is True
    assert plan["tp"] is None
    assert plan["sl"] is None


def test_parse_leverage_as_float_intoed():
    # "20.0" leverage is accepted and coerced to int 20.
    plan = parse_positions_add(["btcusdt", "10", "20.0", "60000"])
    assert plan["ok"] is True
    assert plan["leverage"] == 20


# ─── parse_positions_add: validation failures ────────────────────────────────

def test_parse_missing_arguments():
    assert parse_positions_add([])["ok"] is False
    assert parse_positions_add(["btcusdt", "10", "20"])["ok"] is False


def test_parse_bad_margin():
    assert parse_positions_add(["btcusdt", "abc", "20", "60000"])["ok"] is False


def test_parse_negative_margin_rejected():
    assert parse_positions_add(["btcusdt", "-5", "20", "60000"])["ok"] is False


def test_parse_oversized_margin_rejected():
    # > $100k guard against fat-finger.
    assert parse_positions_add(["btcusdt", "200000", "20", "60000"])["ok"] is False


def test_parse_bad_leverage():
    assert parse_positions_add(["btcusdt", "10", "0x", "60000"])["ok"] is False


def test_parse_leverage_too_low():
    assert parse_positions_add(["btcusdt", "10", "0", "60000"])["ok"] is False


def test_parse_leverage_too_high():
    assert parse_positions_add(["btcusdt", "10", "200", "60000"])["ok"] is False


def test_parse_bad_price():
    assert parse_positions_add(["btcusdt", "10", "20", "notaprice"])["ok"] is False


def test_parse_negative_price():
    assert parse_positions_add(["btcusdt", "10", "20", "-1"])["ok"] is False


def test_parse_invalid_tp():
    assert parse_positions_add(["btcusdt", "10", "20", "60000", "abc"])["ok"] is False


def test_parse_invalid_sl():
    assert parse_positions_add(["btcusdt", "10", "20", "60000", "65000", "abc"])["ok"] is False


def test_parse_negative_sl():
    assert parse_positions_add(["btcusdt", "10", "20", "60000", "65000", "-5"])["ok"] is False


def test_parse_side_must_be_long_or_short():
    # A non-side first token is treated as the pair → missing following args.
    assert parse_positions_add(["BUY", "btcusdt", "10", "20"])["ok"] is False
