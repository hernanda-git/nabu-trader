# Changelog

All notable changes to this project are documented here.

## [Unreleased]

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
