# Changelog

All notable changes to this project are documented here.

## [Unreleased]

### Fixed
- **BONK klines 400 loop**: `get_klines_close` in `exchange/binance.py` now catches `httpx.HTTPStatusError` and returns `None` (instead of warning) for 400 responses — preventing symbol/timeframe unavailability from spamming logs.
- **Pending signal infinite retry**: `PositionManager._evaluate_condition` now calls `pending_signal_repo.mark_expired()` when klines are unavailable, transitioning the signal from `PENDING → EXPIRED` on first failure instead of retrying forever.

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
