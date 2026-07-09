# Changelog

All notable changes to this project are documented here.

- **Failure alert now delivers**: the "Failed to place order" alert was wrapped in nested backticks, which Telegram rejected with *"can't parse entities"* — so failed-trade alerts were silently **undelivered** in prod. Now uses a fenced code block for the raw error. Added markdown-safety tests.

- **Telegram alerts now render reliably**: added `_md_escape()` and applied it to all dynamic content (signal preview, decision reason, etc.) so raw signal/agent text with `*`, `_`, `` ` `` no longer breaks Markdown parsing. This eliminates the pre-existing "can't parse entities" delivery failures across *all* notifications, not just the failure alert.

- **Guaranteed alert delivery**: `send_message` now retries as **plain text** if Telegram rejects the Markdown ("can't parse entities"). No notification is ever silently dropped again — this was the root cause of failed-trade alerts not reaching you.

## [v59] — 2026-07-09

### Fixed
- **Root-cause `-1111` precision rejection (the lost UAIUSDT trade)**: outgoing LIMIT/STOP/TP **prices** are now rounded to the pair's `PRICE_FILTER.tickSize` (and clamped to `minPrice`/`maxPrice`), not just quantity. `_place_order` now calls a new `_round_price()`. Previously only `quantity` was rounded, so a price with too many decimals (e.g. `0.413` on a 4-decimal pair) was rejected by Binance with `-1111` and the trade was silently dropped. Deterministic tests prove the wire values now align to exchange precision.
- **LLM fallback crash on empty reasoning-model response**: `_llm_fallback` no longer calls `json.loads("")` when the model returns empty `content` (deepseek-v4-flash occasionally does). It now warns and returns `None` (trade left, per policy — no retry), instead of throwing `Expecting value` and crashing the safety net. Added a one-shot JSON-only retry on a malformed-but-non-empty response.
- **Reasoning-model `max_tokens` raised** `2048 → 4096` in `agent._call_llm` to avoid truncated/empty completions from deepseek-v4-flash.
- **Failure notification copy**: "❌ Order failed" → "❌ Failed to place order" (still no retry; signal is left after normal pipeline + LLM fallback both fail, since the price may no longer be relevant).

### Added
- **Gateway proxy-awareness** (default OFF): `exchange.binance.proxy` config lets the listener route ALL Binance REST calls through a signed relay (the `binance-gateway` Fly app) so the Binance key lives only on the gateway. Prod stays direct (key in its own Fly secrets). `config.yaml` documents the block.



### Added
- **Conditional SL/TP orders**: `stop_loss()` now places a `STOP` (stop-limit) order → Binance **Conditional tab**, fills as LIMIT (no slippage). New `take_profit()` method places `TAKE_PROFIT` (tp-limit) → Conditional tab, LIMIT fill. Both appear in the Conditional tab, exactly as the user requested.
- **Per-contract fallbacks** when a contract blocks `STOP`/`TAKE_PROFIT` (`-4120`): SL → position-manager mark-price monitoring (closes via LIMIT on breach); TP → resting Basic-tab `LIMIT` at the TP price. No order type silently drops — TP always works.
- **LIMIT-order resting policy (user correction)**: Entry LIMIT now rests on the book at the specified price and waits for the market to reach it. No auto-adjustment to current price, no timeout cancellation. Returns `PENDING` status + "⏳ waiting for price" notification.
- **LLM fallback on order failure (v52)**: `OrderService` calls `_llm_fallback()` on `FAILED`/`REJECTED`/`EXPIRED` — sends full error context (code, qty, price, notional, leverage, balance) to the LLM, which returns corrected params or `ABORT`. On success, notifies user with root cause + fix applied. Wired via `agent=` param in `main.py`.
- **TA context for decisions (v46)**: New `src/agent/ta_context.py` fetches 100 candles and computes ATR(14), swing highs/lows, EMA20/50, Fib, round levels — injected into the LLM prompt **only when a signal lacks SL/TP**.
- **Slash commands `/pending` and `/cancel`** (v50): list pending conditional signals, cancel one or all.
- **API endpoints for manual trade + pending management (v50)**: `POST /api/v1/trade` (auto pair-resolves ticker → Binance symbol, auto-sizes leverage to meet $5 min notional, LIMIT entry/TP), `GET /api/v1/pending`, `POST /api/v1/pending/{id}/cancel`, `POST /api/v1/pending/cancel_all`.
- **Improved `/positions` display (v51)**: live mark price, entry→now %, margin in use, account summary.
- **User-friendly Binance error formatting (v49)**: maps error codes to human-readable messages with full context (symbol, side, qty, price, notional, leverage); strips raw URL/signature.
- **Double-notification fix (v45)**: `content_hash` dedup on startup + Telegram `setMyCommands` so the menu doesn't re-trigger the handler.

### Changed
- **LIMIT-only order policy (HARD RULE)**: entry, close, SL, TP all LIMIT. No MARKET orders anywhere.
- `stop_loss()` signature now accepts `stop_price` and emits `STOP` instead of `STOP_LOSS`/`STOP_MARKET`.
- `OrderService` constructor accepts optional `agent` for LLM fallback.
- `_enter_position` returns `PENDING` (not failure) when the LIMIT hasn't filled yet.

### Fixed
- `_row_to_position` crash on extra DB columns (v48) — now filters to `Position` dataclass fields.

## [v44 and earlier]

### Added
- **Comprehensive trade DB**: 5 new tables (`llm_interactions`, `trade_logs`, `position_events`, `config_snapshots`) + correlation_id columns on existing tables
- **Full LLM request/response capture**: Every LLM call records prompt, response, token counts, latency, and model to `llm_interactions` table
- **Structured pipeline logging**: `trade_logs` table with correlation IDs, levels, module names, and JSON metadata for every pipeline step
- **Position lifecycle events**: `position_events` table tracks POSITION_OPENED, SL_HIT, TP_HIT, TIME_EXIT, AUTO_DETECTED_CLOSE per position
- **Config snapshots**: Point-in-time config.yaml dump tied to trades via `config_snapshots` table
- **Correlation ID tracing**: Every pipeline run gets a unique correlation_id that flows through signal → decision → order → position → logs → events
- **Secure API bridge** (`src/api/`): FastAPI server on port 9090 with:
  - API key authentication (constant-time HMAC comparison)
  - HMAC-SHA256 request signing for write operations
  - Rate limiting (30 req/min per IP)
  - 15 read-only query endpoints (stats, trades, positions, LLM search, logs, config, events)
  - Live Binance balance proxy endpoint
  - Auto-generated OpenAPI docs at /docs
- **Webhook emitter** (`src/api/webhook.py`): Push trade events (TRADE_ENTERED, etc.) to configurable HTTP endpoint with HMAC signing
- **Hermes skill** (`fly-trade-bridge`): `mlops/fly-trade-bridge` skill with query workflows, security setup, and Machines API exec fallback
- **Auto-migration**: `_run_migrations()` safely adds new columns to existing databases without data loss
- **Background API server**: uvicorn starts as asyncio task alongside the trading bot
- **deploy.sh**: Auto-versioning WSL→Windows sync and Fly.io deploy script
- **Startup notification**: Versioned deploy message sent to Telegram on each restart
- **/version command**: Telegram slash command showing bot version and trade mode
- **setMyCommands registration**: Bot commands appear in Telegram command menu

### Fixed
- **Multiple `len` bugs in API server**: Replaced bare `len` function references with `len(rows)` across all paginated endpoints
- **Dockerfile accuracy**: Updated docs to match actual Dockerfile (system deps, COPY patterns, ENV vars)

### Changed
- **Agent brain** (`agent.py`): `_call_llm` now returns `(text, prompt_tokens, completion_tokens, latency_ms)`; `_last_interaction` dict stored on instance
- **Orchestrator** (`orchestrator.py`): Early decision save + LLM interaction capture + structured trade logging + config snapshot on ENTER + webhook emission
- **Order service** (`order_service.py`): `execute()` accepts optional `decision_id` parameter to support pre-saved decisions
- **Position manager** (`position_manager.py`): Accepts `PositionEventRepository`, logs lifecycle events on all close paths
- **State layer**: `repositories.py` has 11 repos (4 new), `database.py` has 14 tables + migrations
- **Domain models** (`models.py`): 4 new dataclasses (LLMInteraction, TradeLogEntry, PositionEvent, ConfigSnapshot)
- **config.yaml**: Added `api` and `webhook` configuration sections
- **requirements.txt**: Added `fastapi>=0.110` and `uvicorn>=0.29`
- **Documentation**: AGENTS.md, README.md, ARCHITECTURE.md, DEPLOYMENT.md, CHANGELOG.md all updated
  Message: {raw signal text}
  Reason to Skip: {reason}
  Current Positions: {list or "(none)"}
  Current Pendings: {list or "(none)"}
  Balance: {balance}
  ```
- **Gate1 rejections silenced**: Pre-LLM safety gate rejections (vague signal, no direction, duplicate) are no longer sent to Telegram — expected noise from channels posting unparseable content. Previously these generated spam like `⏭️ Skipped (Signal unclear...)`.
- **Empty skip reason handling**: When the LLM returns a SKIP action with no `reason` field, the notification now shows `⏭️ Skipped (no reason provided)` instead of `⏭️ Skipped ()`.
- **AGENTS.md updated**: Comprehensive section on 1000x contract quirks, SL/TP compatibility matrix, and deploy process.
