# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- **Startup notification**: On every deployment/restart, the bot sends `🟢 LearnerNoLearner — Online` to Telegram so you know a new version is running.
- **Bot command menu**: Registered `/balance`, `/positions`, and `/help` via Telegram Bot API `setMyCommands` — these appear when you type `/` in your chat.
- **/help command**: In-chat fallback listing all available commands.
- **1000x symbol mapping**: `SYMBOL_MAP` + `_resolve_futures_symbol()` in `binance.py` auto-maps user-facing symbols (BONKUSDT) to actual exchange symbols (1000BONKUSDT) for 1000x contracts. Price and quantity remain in base asset scale.
- **Price-based SL monitoring**: `PositionManager._check_position()` now polls mark price and 1m klines, closing via MARKET order if price breaches SL level for 1000x contracts (where exchange STOP orders are blocked).
- **`get_mark_price()` exchange method**: New method on `BinanceExchange` (and `Exchange` base class) that fetches current `lastPrice` from the 24hr ticker endpoint. Faster than klines for real-time price checks.
- **1000x contract documentation**: Updated `_resolve_futures_symbol()` and `stop_loss()` docstrings with accurate 1000x semantics. Added `1000x Contract Compatibility` section + updated pitfalls in `AGENTS.md`.
- **Critical review document**: `docs/CRITICAL_REVIEW_SESSION_20260707.md` documenting all issues found during the BONK trade session.

### Fixed
- **BONK klines 400 loop**: `get_klines_close` in `exchange/binance.py` now catches `httpx.HTTPStatusError` and returns `None` (instead of warning) for 400 responses — preventing symbol/timeframe unavailability from spamming logs.
- **Pending signal infinite retry**: `PositionManager._evaluate_condition` now calls `pending_signal_repo.mark_expired()` when klines are unavailable, transitioning the signal from `PENDING → EXPIRED` on first failure instead of retrying forever.
- **Binance Futures 1000x symbol -1121**: BONKUSDT now correctly resolves to 1000BONKUSDT on Binance Futures via `_resolve_futures_symbol()`.
- **1000x STOP order blocked gracefully**: `stop_loss()` returns `UNPROTECTED` status for 1000x contracts (1000BONKUSDT etc.) instead of crashing with `-4120 Use Algo Order API`. The position manager monitors price and closes via MARKET if SL breached.
- **_round_quantity filter cache miss**: `_round_quantity()` is now async — lazy-loads exchange filters on cache miss and falls back to integer rounding for low-price coins when filters are unavailable.
- **Filter cache in all exchange methods**: `set_symbol_leverage`, `set_margin_type`, `_load_futures_filters`, `_preflight_symbol`, `_place_order`, `get_klines_close`, `cancel_order`, `get_order`, `get_open_orders` all use `_resolve_futures_symbol()` for consistent symbol mapping.
- **Duplicate `_filters` init in `__init__`**: Removed redundant duplicate `self._filters` and `self._invalid_symbols` assignment in constructor.

### Changed
- **_place_order rounding simplified**: Removed triple-fallback rounding chain (integer fallback + stepSize check + hard safety). Replaced with single `_round_quantity()` call that handles cache miss, stepSize rounding, and integer fallback internally.
- **Skip/reject notifications restructured**: All `⏭️ Skipped` and `🚫 Trade Rejected` messages sent to Telegram now follow a consistent format:
  ```
  ⏭️ Skipped
  Message: {raw signal text}
  Reason to Skip: {reason}
  Current Positions: {list or "(none)"}
  Current Pendings: {list or "(none)"}
  Balance: {balance}
  ```
- **Gate1 rejections silenced**: Pre-LLM safety gate rejections (vague signal, no direction, duplicate) are no longer sent to Telegram — expected noise from channels posting unparseable content. Previously these generated spam like `⏭️ Skipped (Signal unclear...)`.
- **Empty skip reason handling**: When the LLM returns a SKIP action with no `reason` field, the notification now shows `⏭️ Skipped (no reason provided)` instead of `⏭️ Skipped ()`.
- **AGENTS.md updated**: Comprehensive section on 1000x contract quirks, SL/TP compatibility matrix, and deploy process.
