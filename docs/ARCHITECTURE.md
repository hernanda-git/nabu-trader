# Architecture & End-to-End Trade Flow

> Companion to `AGENTS.md` (runbook) and `CHANGELOG.md`. Describes how the
> `nabu-trader` auto-trader is wired together and exactly what
> happens from the moment a Telegram message arrives to the moment an order is
> filled on Binance Futures.

---

## 1. System Architecture

### 1.1 Components

```
                         ┌─────────────────────────────────────────────────────────┐
                         │                      Fly.io (Singapore)                    │
                         │                                                             │
  @YOUR_SIGNAL_CHANNEL  ───────▶│  Telegram API                                              │
  (signal channel)       │       │                                                     │
                         │       ▼                                                     │
                         │  ┌──────────────────┐    message_id + raw_text            │
                         │  │   SignalListener  │──────────────────────────┐         │
                         │  │  (Telethon user   │                           │         │
                         │  │   session, private│                           │         │
                         │  │   chat = commands)│                           │         │
                         │  └──────────────────┘                           │         │
                         │          │  handle_signal()                     │         │
                         │          ▼                                      │         │
                         │  ┌──────────────────────────────────────────────────────┐ │
                         │  │              TradeOrchestrator (core pipeline)        │ │
                         │  │  claim → parse → Gate1 → AgentBrain → Gate2 → execute │ │
                         │  └───────────────┬───────────────┬──────────────┬────────┘ │
                         │                  │               │              │          │
                         │         ┌────────▼───┐   ┌───────▼──────┐ ┌──────▼───────┐  │
                         │         │ AgentBrain │   │ SafetyGate1  │ │ SafetyGate2  │  │
                         │         │ (LLM via    │   │ (pre-LLM     │ │ (post-LLM    │  │
                         │         │  OpenCode Go)│   │  regex/risk) │ │  sizing/lev) │  │
                         │         └────────────┘   └──────────────┘ └──────────────┘  │
                         │                  │                                        │
                         │         ┌────────▼───────────────┐                         │
                         │         │     OrderService       │  execute()              │
                         │         │  (idempotent entry,     │                         │
                         │         │   validate_order gate,  │                         │
                         │         │   _repair_order, SL/TP)  │                         │
                         │         └────────┬───────────────┘                         │
                         │                  │                                        │
                         │         ┌────────▼───────────────┐   ┌─────────────────┐   │
                         │         │    BinanceExchange      │◀─▶│ SymbolRegistry   │   │
                         │         │ (futures REST+WS, proxy) │   │ (live exchangeInfo│  │
                         │         └────────┬───────────────┘   │  cache, 15m refresh│ │
                         │                  │                   └─────────────────┘   │
                         │         ┌────────▼───────────────┐                         │
                         │         │   PositionManager       │  triggers, close, SL/TP│ │
                         │         └────────┬───────────────┘                         │
                         │                  │                                        │
                         │  ┌───────────────▼───────────────┐   ┌─────────────────┐   │
                         │  │   Repositories (SQLite:         │   │ TelegramNotifier│   │
                         │  │    signals/decisions/orders/    │   │ (bot → notify   │   │
                         │  │    positions/pending/events/    │   │  chat + /health)│   │
                         │  │    llm_interactions/logs/       │   └────────┬────────┘   │
                         │  │    config_snapshots)            │            │            │
                         │  └────────────────────────────────┘            │            │
                         │                                                │            │
                         │  ┌──────────────────────────────────────┐      │            │
                         │  │ HealthReporter (every 6h → /health)   │      │            │
                         │  └──────────────────────────────────────┘      │            │
                         │                                                │            │
                         │  ┌──────────────────────────────────────┐      │            │
                         │  │ API bridge (FastAPI, :9090, optional) │      │            │
                         │  └──────────────────────────────────────┘      │            │
                         └────────────────────────────────────────────────┘
                                  ▲                                          │
                                  │            Telegram bot notifications    │
                                  └──────────────────────────────────────────┘
```

### 1.2 Module map (`src/`)

| Module | Responsibility |
|--------|---------------|
| `main.py` | Entry point. Loads config + `.env`, wires every component, starts the listener/health/API tasks. |
| `listener.py` | Telethon client. Monitors `@YOUR_SIGNAL_CHANNEL`; forwards new/edited messages to the orchestrator; implements the private-chat slash commands (`/help`, `/balance`, `/positions`, `/pending`, `/cancel`, `/health`, `/setport`, `/getport`, `/version`). |
| `orchestrator.py` | `TradeOrchestrator`. The core pipeline (steps below). Also handles reply-based management ("sl to entry", modify) and `execute_trigger()` for conditional signals. |
| `agent/agent.py` | `AgentBrain`. Builds the prompt and calls the LLM (OpenCode Go endpoint) once per signal to produce a `TradeDecision` (ENTER / SKIP / CLOSE / MODIFY / CONDITIONAL). |
| `agent/parser.py` | Regex pre-parse of raw signal text into a `TradeSignal` (pair, direction, entry, SL, TP). Symbol resolution via `SymbolRegistry`. |
| `agent/gate.py` | `SafetyGate1` (pre-LLM structural checks) and `SafetyGate2` (post-LLM sizing + leverage cap from exchange `minNotional`). |
| `exchange/binance.py` | `BinanceExchange`. Futures REST + WS, optional proxy, real exchangeInfo filters (`_load_futures_filters`), `_round_price`/`_round_quantity`, order placement. |
| `exchange/symbol_registry.py` | Live `exchangeInfo` cache (seeded from `data/symbols_seed.json`, refreshed every 15 min). Source of truth for price/quantity precision and `minNotional`. |
| `exchange/validation.py` | `validate_order(symbol, side, price, qty, filters)` — pre-submission gate (Tasks 1–5). Returns `(ok, error)`. |
| `exchange/paper.py` | Simulated exchange for `paper` mode. |
| `execution/order_service.py` | `OrderService`. Idempotent entry (dedup via `get_active_for_decision` + `client_order_id` UNIQUE), validation gate, deterministic `_repair_order`, SL/TP placement. |
| `execution/position_manager.py` | Monitors open positions, fires conditional triggers, manages SL/TP and closes. |
| `notifier/telegram.py` | `TelegramNotifier`. Bot → notify chat: startup, decisions, execution, health, alerts. Registers slash commands via `setMyCommands`. |
| `health/reporter.py` | `build_health_report()` (shared by `/health` and the 6h scheduler) + `HealthReporter` loop. |
| `state/database.py` | SQLite (`trades.db`, on a Fly volume at `/data` in prod). Schema: `signals, decisions, orders, executions, positions, processed_signals, events, daily_stats, pending_signals, llm_interactions, trade_logs, position_events, config_snapshots`. |
| `state/repositories.py` | One repository class per table. |
| `api/server.py` | Optional FastAPI bridge (port 9090) for external status hooks. |
| `config/loader.py`, `config/validator.py` | Load + validate `config.yaml`. |

### 1.3 Data stores & state

- **SQLite `trades.db`** — persistent state: every signal, decision, order, position, pending trigger, LLM prompt/response, and a config snapshot at each entry.
- **`processed_signals`** — idempotent claim so a message delivered twice (post + immediate edit, or reconnect replay) is processed once.
- **`pending_signals`** — conditional (setup/alert) signals awaiting their price trigger.
- **`.env`** (gitignored) — `TELEGRAM_BOT_TOKEN`, `NOTIFY_CHAT_ID`, `TG_API_ID`, `TG_API_HASH`, `CHANNEL_USERNAME`, `OPENCODE_GO_API_KEY`, `SESSION_STRING`.

---

## 2. End-to-End Trade Flow

Triggered by a new/edited message in `@YOUR_SIGNAL_CHANNEL`, forwarded by `SignalListener.on_new_message` → `TradeOrchestrator.handle_signal(message_id, channel, raw_text, …)`.

### 2.1 Signal → Decision pipeline

| # | Step | Component | Notes |
|---|------|-----------|-------|
| 0 | **Idempotent claim** | `signal_repo.claim(message_id)` | Duplicate delivery (edit/reconnect) is dropped *before* any LLM call or notification. |
| 0b | **Management command?** | `parse_management_command` | Replies like "sl to entry" bypass the LLM and mutate the open position directly (see §2.4). |
| 1 | **Regex pre-parse** | `parse_signal` + `SymbolRegistry` | Raw text → `TradeSignal` (pair resolved, precision/`minNotional` attached as `sym_meta`). |
| 2 | **Safety Gate 1** | `SafetyGate1.check` | Pre-LLM structural/risk check. Rejections are expected noise → **no Telegram spam**, signal marked processed. |
| 3 | **Context gather** | orchestrator | Fetches open positions, pending signals, and futures balance (for sizing). |
| 3b | **Save signal** | `signal_repo.save` | Persisted for audit/traceability. |
| 4 | **Agent Brain (1 LLM call)** | `AgentBrain.decide` | Technical context (`ta_context`) is fetched **only** when the signal lacks SL/TP, so the LLM anchors on real support/resistance/ATR. Returns a `TradeDecision`. |
| 4b | **Save decision + LLM** | `decision_repo.save`, `_save_llm_interaction` | Persisted. |
| 5 / 5b | **Branch on action** | orchestrator | `MODIFY` → execute modification. `CONDITIONAL` → save a `pending_signal` (await trigger). `SKIP`/`CLOSE` → notify (SKIP) and exit (no Gate 2). |
| 6 | **Safety Gate 2** | `SafetyGate2.check(decision, balance, filters)` | **ENTER only.** Clamps size, computes leverage from exchange `minNotional` (Task 6), never above `max_leverage` (`50`) and capped at `max_leverage_increase_pct` (`10`) over the needed baseline. |
| 7 | **Notify decision** | `notifier.notify_decision` | "thinking / decision" card to the notify chat. |
| 8 | **Execute** | `order_service.execute` | See §2.2. |
| 9 | **Notify execution** | `notifier.notify_execution` | `LIMIT order placed` / `Trade entered (repaired)` / reject / skip. |

### 2.2 Order execution (`OrderService.execute`)

1. **Idempotency dedup** — `get_active_for_decision(decision_id, side)`; if an active order exists → return `DUPLICATE_SKIPPED` (UNIQUE `client_order_id` is the DB backstop).
2. **Pre-submission validation gate** — `validate_order(symbol, side, price=entry, qty, filters)` (Tasks 1–5). If invalid → `VALIDATION_SKIP`, **no API call**.
3. **Place entry** — `LIMIT` buy/sell at the rounded entry price, quantity rounded to the symbol's real precision and `minNotional` (Tasks 2–3).
4. **On rejection** (e.g. still `-1111`/`-4164`) — deterministic `_repair_order` reloads filters, re-rounds, re-validates, recomputes leverage, and **resubmits exactly once**. No LLM involved. Success → `✅ Repaired entry …`; failure → `VALIDATION_SKIP`.
5. **SL / TP legs** — placed after entry; each is validated; an invalid leg is skipped with a warning (does not abort the position).
6. **Notify** — `LIMIT order placed` (PENDING) or `Trade entered (repaired)` (FILLED).

### 2.3 Conditional signals & triggers

- A `CONDITIONAL` decision saves a `pending_signal` (`close_above` for LONG, `close_below` for SHORT, at the trigger price, with a timeframe).
- `PositionManager` watches price; when the condition fires it calls `orchestrator.execute_trigger()`, which re-runs the LLM with trigger context, applies **Gate 2** sizing, and enters via `OrderService`.
- Manage pending entries with `/pending` and `/cancel <id>` / `/cancel all`.

### 2.4 Reply-based position management (no LLM)

Replying to a previous signal/message with a management command (e.g. "sl to entry", "modify") is routed by `_resolve_reply_context` (which re-parses the replied-to message to recover the `reply_pair`) into `_handle_management`. These mutate the open position directly and still pass through Gate 2 for SL validity.

---

## 3. Telegram surface

### 3.1 Slash commands (private chat with the bot)

| Command | Purpose |
|---------|---------|
| `/help` | List commands + current `port` setting |
| `/balance` | Binance Futures account balance |
| `/positions` | All open futures positions |
| `/pending` | Pending conditional signals |
| `/cancel <id>` | Cancel one pending signal |
| `/cancel all` | Cancel all pending signals |
| `/health` | Full system health check (also includes **💼 Portfolio: N open · margin/trade $X**) |
| `/setport N` | Set margin per trade to $N (leverage auto-derived) |
| `/getport` | Show current margin per trade (`port_usdt`) |
| `/version` | Bot version + mode (YOLO auto-trade vs dry run) |

> The pre-written Telegram menu (`setMyCommands`) lists the 7 arg-free commands: `balance, positions, health, setport, getport, version, help`.

### 3.2 Notifications sent by the bot

`notify_startup` · `notify_decision` · `notify_execution` (`LIMIT order placed` / `Trade entered (repaired)`) · `Skipped` / `Trade Rejected` / `Close signal received` · conditional-saved · position modified · periodic `/health` report (every 6h, includes portfolio/port).

---

## 4. Deployment

- **Platform:** Fly.io (Singapore). `main.py` runs as the single process; the listener blocks on `run_until_disconnected()`, so the HealthReporter and API tasks are started *before* it.
- **Volumes:** SQLite lives on a persistent volume (`/data` in `FLY_MODE`); local dev uses the repo root.
- **Config:** `config.yaml` (`exchange.active`, `risk.port_usdt`, `risk.max_leverage`, `risk.max_leverage_increase_pct`, `agent.auto_trade`, `monitoring.health_report_hours`, `api.*`).
- **Secrets:** set via Fly secrets / `.env` (never committed — `.env` is gitignored and was scrubbed from git history).
- **No schema migrations** are required for the reliability pass (tables already had `status DEFAULT 'PENDING'` and `client_order_id UNIQUE`); a rolling `fly deploy` is safe.
- **Post-deploy check:** Telegram should show `LIMIT order placed`, `Trade entered (repaired)`, or `VALIDATION_SKIP` — never an unvalidated fill.
