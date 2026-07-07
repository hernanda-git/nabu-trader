# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Added
- **Startup notification**: On every deployment/restart, the bot sends `🟢 LearnerNoLearner — Online` to Telegram so you know a new version is running.
- **Bot command menu**: Registered `/balance`, `/positions`, and `/help` via Telegram Bot API `setMyCommands` — these appear when you type `/` in your chat.
- **/help command**: In-chat fallback listing all available commands.
- **1000x symbol mapping**: `SYMBOL_MAP` + `_resolve_futures_symbol()` in `binance.py` auto-maps user-facing symbols (BONKUSDT) to actual exchange symbols (1000BONKUSDT) for 1000x contracts. Price and quantity remain in base asset scale.
- **Price-based SL monitoring**: `PositionManager._check_position()` now polls 1m klines and closes via MARKET order if price breaches SL level for 1000x contracts (where exchange STOP orders are blocked).
- **Symbol status field in exchangeInfo**: Stored in filter cache for debugging.
- **Critical review document**: `docs/CRITICAL_REVIEW_SESSION_20260707.md` documenting all issues found during the BONK trade session.

### Fixed
- **BONK klines 400 loop**: `get_klines_close` in `exchange/binance.py` now catches `httpx.HTTPStatusError` and returns `None` (instead of warning) for 400 responses — preventing symbol/timeframe unavailability from spamming logs.
- **Pending signal infinite retry**: `PositionManager._evaluate_condition` now calls `pending_signal_repo.mark_expired()` when klines are unavailable, transitioning the signal from `PENDING → EXPIRED` on first failure instead of retrying forever.
- **Binance Futures 1000x symbol -1121**: BONKUSDT now correctly resolves to 1000BONKUSDT on Binance Futures via `_resolve_futures_symbol()`.
- **Quantity precision -1111 on 1000x**: Quantity is auto-rounded to integer for 1000x contracts (LOT_SIZE stepSize=1) with multiple fallback layers in `_place_order()`.
- **Invalid symbol cache**: `_invalid_symbols` now caches the resolved symbol name to prevent redundant exchangeInfo lookups.
- **Symbol resolution in all exchange methods**: `set_symbol_leverage`, `set_margin_type`, `_load_futures_filters`, `_preflight_symbol`, `_place_order`, `get_klines_close`, `cancel_order`, `get_order`, `get_open_orders` all use `_resolve_futures_symbol()` for consistent symbol mapping.
- **Deploy process**: AGENTS.md now documents the Windows/WSL sync requirement.

### Changed
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
- **`_round_quantity()` return signature**: Now returns `(rounded_qty, rules_or_None)` for better fallback handling.
- **`stop_loss()` for 1000x contracts**: Returns `UNPROTECTED` status with a clear message instead of attempting blocked STOP_MARKET orders, enabling position manager to activate monitoring-based SL.
- **AGENTS.md updated**: Comprehensive section on 1000x contract quirks, SL/TP compatibility matrix, and deploy process.
