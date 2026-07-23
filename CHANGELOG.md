# Changelog

All notable changes to this project are documented here.

## v103 — 2026-07-23

### Added
- **Position close Telegram notification**: `_finalize_close()` now sends a Telegram message whenever a position closes (SL, TP, MANUAL, SYSTEM). Shows direction, entry/exit price, PnL, reason, and closed-by label. (Fixes missing "SL hit" notification)

### Fixed
- **Health report markdown escaping**: Dynamic detail fields in health checks now pass through `_md_escape()` to prevent Telegram "can't parse entities" errors on `/health` and periodic reports.

### Changed
- `_finalize_close()` converted from sync to async (to support Telegram notification)
- Notification sends before returning True, non-blocking on failure

## v102 — 2026-07-22

Hotfix rebuild (same changes as v101, fresh deploy).

## v101 — 2026-07-22

### Fixed
- **Management command applied to wrong pair**: When channel posted `$ENA tp1 booked` but ENA was already closed, the bot silently applied the TP1 close to the only remaining open position (ONDO). Now rejects with `⚠️ TP1 partial close — no open position for ENAUSDT.` (See issue ONDO #18 wrong close)

### Changed
- Both `_handle_tp1_booked` and `_handle_full_close` now check that the message-specified pair (if any) has an open position before executing

---

## v92 — 2026-07-14

### Fixed
- **CRITICAL: CLOSE action never executed** — The orchestrator's `handle_signal()` treated CLOSE as a no-op (only sent Telegram "processing" message and returned). MODIFY/CONDITIONAL correctly called `order_service.execute()` but CLOSE didn't. Now CLOSE actually places a market close order.
- **Missing /close Telegram command**: Added `/close <symbol>` command that flattens any open position. `/close` with no symbol lists open positions. Added to `/help`.

### Changed
- Deployed via `flyctl deploy` (not deploy.sh), so v92 was not git-committed

---

## v91 — 2026-07-14

Previous state before critical CLOSE fix. Everything below is reconstructed from changelog fragments.

## v81 — 2026-07-13

### Fixed
- **ReduceOnly regression (P0)**: Earlier fix (v80) removed `reduceOnly` from all LIMIT orders, but LIMIT is used for both entries AND exits. Exit paths (maker-close, TP-fallback) lost their reduce-only protection, risking accidental flips. Fixed with explicit `reduce=` flag per call.
- v81 entries pass `reduce=False` (no `-2022` error), exits pass `reduce=True` (no flip risk).

### Added
- 95 regression tests covering all order paths

## v72 — 2026-07-12

### Added
- Management patterns for direct command execution (skip LLM):
  - `sl to entry` / `breakeven` — Move SL to entry price
  - `tp1 booked` / `tp1 done` / `partial` — Close 50% + breakeven + TP remainder
  - `closed at entry` / `cutting it here` — Full market close
- `_MGMT_CLOSE_RE` regex pattern for management close detection
- Manual WLD close via remote exec script (closed 14 WLD @ 0.383)

### Fixed
- Version pipeline now reports real deployed commit from Dockerfile build args
- Build argument injection: version baked into image at `flyctl deploy` time

## v71 — 2026-07-12

### Fixed
- TP1 partial close now correctly handles target pair when position is already loaded
- LLM confidence threshold removal (set to 0.0) — always trust the signal

## v70 — 2026-07-12

### Added
- TP1 booking handler: `_handle_tp1_booked()` — closes 50% at market, moves SL to breakeven, sets TP on remaining position
- Close handler: `_handle_full_close()` — full market close with pair detection

## v69 — 2026-07-11

### Added
- Position manager self-heal: if SL/TP orders are missing from exchange, re-place them once per session
- Orphan detection: positions with zero orders and age >30min are verified against exchange and auto-closed if flat
- Reconciler for late-fill LIMIT orders (position created when pending LIMIT fills)

## v68 — 2026-07-10

### Added
- Config snapshot system: periodic snapshots of config.yaml recorded in DB for audit
- LLM interaction logging: full prompt/response/latency stored per decision
- Signal metadata table: exchange-level pair validation data per signal

## v67 — 2026-07-09

### Fixed
- Unmatched `[` in f-string causing syntax error on remote exec queries
- Telegram markdown in error messages: escaped backticks within fenced code blocks

### Added
- API bridge: FastAPI server with 15 endpoints for Hermes integration
  - Trade trace by ID (includes LLM interactions, position events, logs)
  - LLM decision search
  - Config snapshots + live update
  - Pipeline trace by correlation ID
  - Balance query, position close, event stream

## v66 — 2026-07-08

### Added
- Gateway proxy support: route all Binance calls through a signed relay (avoids embedding API key)
- HMAC-SHA256 authentication on API write endpoints
- Rate limiting: 30 req/min per IP on API bridge

## v65 — 2026-07-07

### Fixed
- **1000x coin pair resolution**: SymbolRegistry now handles `1000×` prefix for small-cap coins like BONK, PEPE, SHIB. Binance lists these as `1000BONKUSDT` but the channel posts `$BONK`.
- Invalid symbol error (`-1121`) for all low-cap coins resolved.

### Changed
- Moved from 40 hardcoded coin symbols to dynamic SymbolRegistry (fetches all ~300+ pairs from exchangeInfo)
- Pair regex patterns removed from parser.py — now delegates entirely to registry

## v64 — 2026-07-06

### Added
- Multiple TP level support (TP1, TP2)
- Conditional TAKE_PROFIT_LIMIT orders with reduceOnly
- Position monitoring loop: checks mark price vs SL every 10s

## v63 — 2026-07-05

### Added
- Safety Gate 2: post-LLM clamp (size, concurrent, daily loss, SL direction)
- Dynamic leverage calculation: `leverage = position_value / port_usdt`
- Portfolio leverage cap: total notional / balance ≤ 10×

## v62 — 2026-07-04

### Added
- Safety Gate 1: idempotency, cooldown, pair whitelist
- Management command detection: `tp1`, `sl to entry`, `close` patterns skip LLM
- Event bus: in-process pub/sub for loose coupling

## v61 — 2026-07-03

### Added
- Initial auto-trade pipeline (feature/auto-trade branch):
  - Agent brain: single LLM call via OpenCode Go → structured JSON decision
  - Order service: decision → exchange order + SL/TP placement
  - Position manager: background monitoring loop
  - Paper trading and dry-run modes
  - Full SQLite schema (signals, decisions, orders, positions, events)
  - Repository pattern for data access
  - Telegram notification system

## v0 — Initial Listener
- Telethon-based channel monitor
- Regex signal parsing
- Forward parsed signals to Telegram
- Single-file listener (`listener.py`)
