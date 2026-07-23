# Architecture & End-to-End Trade Flow

> Companion to [AGENTS.md](../AGENTS.md) (runbook) and [CHANGELOG.md](../CHANGELOG.md).  
> Describes how the `nabu-trader` auto-trader is wired together and what happens from the moment a Telegram message arrives to the moment an order fills on Binance Futures.

---

## 1. System Architecture

### 1.1 Component Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Telegram (User)                              │
│  ┌──────────────────────┐    ┌──────────────────────────────┐   │
│  │ @YOUR_SIGNAL_CHANNEL Channel │    │  Bot (private messages)      │   │
│  │ (signals)            │    │  /commands, notifications    │   │
│  └──────────┬───────────┘    └────────▲─────────────────────┘   │
└─────────────┼─────────────────────────┼─────────────────────────┘
              │                         │
    ┌─────────▼─────────────────────────┴─────────┐
    │              src/listener.py                  │
    │  TelethonClient (user session)                │
    │  + Bot API (bot token)                        │
    │  - Reads channel messages                     │
    │  - Handles /commands from user                │
    │  - Sends notifications via bot                │
    └─────────┬─────────────────────────────────────┘
              │ raw signal text
              ▼
    ┌───────────────────────────────────────────────┐
    │           src/orchestrator.py                   │
    │  Pipeline coordinator — the central brain      │
    │                                                 │
    │  handle_signal(signal):                         │
    │    1. Regex Pre-Parse (parser.py)               │
    │    2. Safety Gate 1 (gate.py → pre-LLM)        │
    │    3. LLM Agent (agent.py → decide)            │
    │    4. Safety Gate 2 (gate.py → post-LLM)       │
    │    5. Management Commands (tp1, sl, close)     │
    │    6. Order Execution (order_service.py)        │
    │    7. SL/TP Placement                           │
    │    8. Telegram Notification                     │
    └───────┬────────────────────────────────┬───────┘
            │                                │
            ▼                                ▼
    ┌───────────────┐              ┌──────────────────┐
    │   agent/      │              │  execution/       │
    │  parser.py    │              │  order_service.py │
    │  agent.py     │              │  position_mgr.py  │
    │  gate.py      │              └────────┬─────────┘
    └───────────────┘                       │
                                            ▼
                                  ┌──────────────────┐
                                  │   exchange/      │
                                  │  base.py (ABC)   │
                                  │  binance.py      │
                                  │  paper.py        │
                                  └────────┬─────────┘
                                           │ Binance REST
                                           ▼
                                  ┌──────────────────┐
                                  │  Binance USDⓈ-M  │
                                  │  Futures          │
                                  └──────────────────┘
```

### 1.2 State & Persistence

```
┌─────────────────────┐    ┌──────────────────────┐
│   state/database.py │    │  state/repositories  │
│   SQLite (WAL mode) │    │  Repository Pattern  │
│   /data/trades.db   │───▶│  - SignalRepository  │
│                      │    │  - DecisionRepo     │
│   Tables:            │    │  - OrderRepository  │
│   - signals          │    │  - PositionRepo     │
│   - decisions        │    │  - EventRepo        │
│   - orders           │    │  - PendingSignal    │
│   - positions        │    │  - LLMInteraction   │
│   - events           │    │  - ConfigSnapshot   │
│   - llm_interactions │    └──────────────────────┘
│   - config_snapshots │
│   - pending_signals  │    ┌──────────────────────┐
│   - processed_msgs   │    │  events/bus.py       │
│   - position_events  │    │  In-process pub/sub  │
│   - signal_metadata  │    │  - SignalReceived    │
│   - daily_stats      │    │  - DecisionCreated   │
└─────────────────────┘    │  - OrderPlaced       │
                            │  - PositionOpened    │
                            │  - PositionClosed    │
                            └──────────────────────┘
```

---

## 2. End-to-End Signal Flow

### 2.1 Signal Reception

1. **Telethon** (`listener.py`) receives a new message from `@YOUR_SIGNAL_CHANNEL`
2. Regex pre-parse runs immediately to extract basic fields:
   - `$SYMBOL` mentions → pair resolution via `SymbolRegistry`
   - Direction keywords (LONG/SHORT/BUY/SELL)
   - Price patterns (ENTRY, SL, TP)
   - Management commands (`tp1 booked`, `sl to entry`, `closed at entry`)
3. A `TradeSignal` domain object is created
4. The signal is forwarded to `orchestrator.handle_signal()`

### 2.2 Gate 1 — Pre-LLM Safety (gate.py)

Fast checks before any LLM cost:

| Check | Logic |
|-------|-------|
| **Idempotency** | Same `message_id` already processed? → SKIP |
| **Cooldown** | Same pair traded within last `min_cooldown_minutes`? → SKIP |
| **Pair Whitelist** | Pair in `allowed_pairs` list? (default `*` = anything) |
| **Media Check** | Message has media but no text? → SKIP |

### 2.3 Agent Brain (agent.py)

A single LLM call to OpenCode Go (`deepseek-v4-flash`) with:

**Input context:**
- Raw signal text
- Regex pre-parse fields (pair, direction, prices)
- Currently open positions
- Account balance
- Recent trade history (last 5 decisions)

**Output:** Structured JSON (`TradeDecision`):
```json
{
  "action": "ENTER" | "CLOSE" | "SKIP",
  "pair": "1000BONKUSDT",
  "direction": "LONG",
  "order_type": "LIMIT",
  "quantity": 1610.3,
  "entry_price": 0.003105,
  "sl_price": 0.003022,
  "tp_prices": [0.00333],
  "reason": "Signal with clear support level",
  "confidence": 0.85
}
```

### 2.4 Gate 2 — Post-LLM Hard Limits (gate.py)

Non-negotiable checks after the LLM decides:

| Check | Limit |
|-------|-------|
| **Position Size** | Clamped to `port_usdt` ($1.00 default) / mark price |
| **Max Concurrent** | ≤ 2 open positions |
| **Daily Loss Limit** | Stops trading if >30% daily drawdown |
| **Min Notional** | ≥ $5.00 (Binance requirement) |
| **Portfolio Leverage** | Total notional / balance ≤ 10× |
| **SL Direction** | SL must be below entry for LONG, above for SHORT |
| **Margin Usage** | ≤ 80% of available balance |
| **Leverage Ceiling** | ≤ `max_leverage` (20× default) |

### 2.5 Management Command Detection (orchestrator.py)

Before routing to the LLM, the orchestrator checks if the message is a **management command** — these bypass LLM entirely and execute directly:

| Pattern | Action | Handler |
|---------|--------|---------|
| `tp1 booked` / `tp1 done` / `partial` | Close 50% → SL to entry → TP on remainder | `_handle_tp1_booked` |
| `sl to entry` / `breakeven` | Move stop loss to entry price | `_handle_sl_to_entry` |
| `closed at entry` / `cutting it here` / `full` | Full market close | `_handle_full_close` |
| `tp1 <price>` / `tp2 <price>` | Modify TP level | `_handle_tp_modify` |

If a management message names a specific pair (`$ENA sl to entry`), it applies only to that pair. If no pair is named, it applies to the most recently opened position. **If the named pair has no open position, the command is rejected with a Telegram warning** (fix v101).

### 2.6 Order Execution (order_service.py)

For `ENTER` decisions, the flow is:

1. **Calculate quantity**: `margin_usdt / mark_price` → adjusted for step size
2. **Generate client_order_id**: `lnr_{decision_id}_{uuid4}` (idempotency key)
3. **Place entry order**: LIMIT or MARKET via exchange adapter
4. **If filled immediately**:
   - Create `positions` row in DB
   - Place SL order (conditional STOP-LIMIT, reduce-only)
   - Place TP order (conditional TAKE_PROFIT-LIMIT, reduce-only)
   - Send Telegram notification
5. **If PENDING** (resting LIMIT on book):
   - Save as `PENDING` order
   - Position manager polls for fill every 10s
   - On late fill: reconcile position + attach SL/TP

For `CLOSE` decisions:
1. Market close with reduceOnly flag
2. Cancel any resting SL/TP orders
3. Record PnL → DB + Telegram notification

### 2.7 Position Monitoring (position_manager.py)

Runs as a background loop every `check_interval_seconds` (default 10s):

```
_monitor_loop()
    ├── _reconcile_pending_orders()  — Check PENDING LIMIT fills
    ├── _check_positions()           — For each open position:
    │   ├── Cancel any Orphan SL/TP on wrong side
    │   ├── Check exchange open orders
    │   ├── Price-based SL check (via mark price)
    │   │   └── If price <= SL (LONG) or price >= SL (SHORT) → close
    │   ├── Self-heal: replace missing SL/TP orders
    │   ├── Orphan detection: no orders >30min → verify exchange → close
    │   └── Time-based exit: >max_position_hold_hours (48h) → close
    └── _check_pending_conditions() — Evaluate price-trigger conditions
```

On any position close, a **Telegram notification** is sent with PnL, direction, exit price, and reason (fix v103).

### 2.8 SL/TP Management

**Stop Loss:**
- Primary: conditional `STOP_LOSS_LIMIT` order (reduceOnly) on Binance
- Backup: **price monitoring** via position manager (mark price polled every 10s)
  - For 1000× contracts where conditional orders are blocked (`-4120`)
  - Falls back to `UNPROTECTED` status → monitored every tick

**Take Profit:**
- Primary: conditional `TAKE_PROFIT_LIMIT` order (reduceOnly)
- Fallback: resting Basic-tab LIMIT order at TP price

**Self-Heal:** On each monitoring tick, if SL/TP orders are missing from the exchange but the position is still open, the position manager re-places them automatically (once per position per session, tracked in `_protection_placed`).

---

## 3. Database Schema

### 3.1 Core Tables

**`signals`** — Every message received from Telegram:
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `message_id` | INTEGER UNIQUE | Telegram message ID |
| `channel` | TEXT | Source channel |
| `raw_text` | TEXT | Full message text |
| `pair` | TEXT | Resolved trading pair |
| `direction` | TEXT | LONG/SHORT |
| `entry_price` | REAL | Entry level |
| `sl_price` | REAL | Stop loss |
| `tp_prices` | TEXT | JSON array of TP levels |
| `created_at` | TIMESTAMP | When received |
| `correlation_id` | TEXT | Trace UUID |

**`decisions`** — LLM or management command decisions:
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `signal_id` | INTEGER FK | → signals.id |
| `action` | TEXT | ENTER/CLOSE/SKIP/MODIFY |
| `pair` | TEXT | Target pair |
| `direction` | TEXT | LONG/SHORT |
| `quantity` | REAL | Calculated quantity |
| `confidence` | REAL | LLM confidence 0-1 |
| `reason` | TEXT | Human-readable reason |
| `created_at` | TIMESTAMP | When decided |

**`orders`** — Orders sent to exchange:
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `decision_id` | INTEGER FK | → decisions.id |
| `client_order_id` | TEXT UNIQUE | Idempotency key |
| `exchange` | TEXT | binance_futures |
| `symbol` | TEXT | e.g. 1000BONKUSDT |
| `side` | TEXT | BUY/SELL |
| `order_type` | TEXT | LIMIT/MARKET/STOP/TAKE_PROFIT |
| `quantity` | REAL | Order quantity |
| `price` | REAL | Order price |
| `status` | TEXT | PENDING/FILLED/CANCELED/FAILED |
| `exchange_order_id` | TEXT | Binance order ID |

**`positions`** — Trade positions:
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `pair` | TEXT | Trading pair |
| `direction` | TEXT | LONG/SHORT |
| `entry_price` | REAL | Fill price |
| `quantity` | REAL | Position size |
| `sl_price` | REAL | Initial SL |
| `tp_prices` | TEXT | JSON array |
| `status` | TEXT | OPEN/CLOSED |
| `entry_time` | TIMESTAMP | When opened |
| `exit_time` | TIMESTAMP | When closed |
| `exit_price` | REAL | Close fill price |
| `pnl` | REAL | Realized PnL (USDT) |
| `reason` | TEXT | Close reason |
| `closed_by` | TEXT | SL/TP/MANUAL/SYSTEM/TRIGGER |

**`llm_interactions`** — Full LLM request/response audit:
| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `decision_id` | INTEGER FK | → decisions.id |
| `system_prompt` | TEXT | Prompt sent to LLM |
| `user_message` | TEXT | Signal context sent |
| `raw_response` | TEXT | Raw LLM response |
| `parsed_decision` | TEXT | JSON parsed from response |
| `latency_ms` | INTEGER | LLM response time |
| `model` | TEXT | Model name |
| `created_at` | TIMESTAMP | When called |

### 3.2 Additional Tables

- **`position_events`** — Immutable event log per position (OPENED, SL_HIT, TP_HIT, CLOSED, MODIFIED)
- **`pending_signals`** — Conditional price-trigger signals waiting for activation
- **`processed_messages`** — Idempotency tracking (message_id → hash)
- **`config_snapshots`** — Periodic snapshots of `config.yaml` for audit
- **`daily_stats`** — Aggregated daily metrics (signals, trades, PnL)
- **`signal_metadata`** — Extra metadata per signal (exchange info, resolved pair details)

---

## 4. Notification System

### 4.1 Telegram Notifications

The bot sends notifications to your Telegram via the Bot API (`@YOUR_BOT_USERNAME`):

| Event | Format |
|-------|--------|
| Signal Received | 📡 **Signal received from @YOUR_SIGNAL_CHANNEL** |
| Decision (ENTER) | 🟢 **TRADE — 1000BONKUSDT** LONG \| LIMIT, Qty, SL, TP, Reason |
| Decision (CLOSE) | 🔴 **CLOSE** pair — Reason |
| Decision (SKIP) | ⏭️ **Skipped** pair — Reason |
| Order Filled | ✅ **Order filled** — side, symbol, qty, price |
| Order Failed | ❌ **Failed to place order** — error details |
| Position Closed | {🟢/🔴} **Position Closed** — pair, entry/exit, PnL, reason, closed by |
| Startup | 🚀 **Online** — version, status |
| Health Report | 🩺 **Health Check** — subsystem status |
| Management Warning | ⚠️ **message** — e.g. "no open position for pair" |

All dynamic content is `_md_escape()`'d to prevent Telegram Markdown parse failures (fix v103).

### 4.2 Health Reports

The `HealthReporter` runs every 6 hours and checks:
- 🤖 Telegram Bot API connection
- 👤 Telethon user session + channel access
- 🧠 LLM Provider (OpenCode Go ping)
- 💱 Exchange connection + balance
- 💼 Portfolio (open positions, margin/trade)
- 🔗 Margin mode
- ⚙️ Leverage ceiling
- 🔎 Symbol Registry
- 🗄️ Database reachability

---

## 5. API Bridge

The bot exposes a FastAPI server on port 9090 for external queries:

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/health` | GET | None | Simple health check |
| `/api/v1/health` | GET | API Key | Full system health |
| `/api/v1/stats` | GET | API Key | Trading statistics |
| `/api/v1/positions` | GET | API Key | Open positions |
| `/api/v1/positions/{id}` | GET | API Key | Position details |
| `/api/v1/trades` | GET | API Key | Trade list |
| `/api/v1/trades/{id}` | GET | API Key | Full trade trace |
| `/api/v1/llm/search` | GET | API Key | Search LLM decisions |
| `/api/v1/llm/{id}` | GET | API Key | LLM interaction detail |
| `/api/v1/config` | GET | API Key | Current config snapshot |
| `/api/v1/config/update` | POST | API+HMAC | Update config |
| `/api/v1/logs/{correlation_id}` | GET | API Key | Pipeline trace |
| `/api/v1/balance` | GET | API Key | Account balance |
| `/api/v1/close` | POST | API+HMAC | Close position |
| `/api/v1/events` | GET | API Key | Event stream |

---

## 6. File Layout

```
nabu-trader/
├── src/
│   ├── __init__.py
│   ├── main.py                        # Entry point: inits all components
│   ├── listener.py                    # Telethon listener + Telegram command handlers
│   ├── orchestrator.py                # Pipeline coordinator (signal → LLM → execute)
│   ├── version.py                     # Version string (e.g. "v103")
│   │
│   ├── agent/
│   │   ├── parser.py                  # Regex pre-parse (1ms signal extraction)
│   │   ├── agent.py                   # LLM brain (OpenCode Go chat completion)
│   │   └── gate.py                    # Safety Gates 1 (pre-LLM) & 2 (post-LLM)
│   │
│   ├── exchange/
│   │   ├── base.py                    # Abstract Exchange ABC
│   │   ├── binance.py                 # Binance REST API adapter
│   │   ├── paper.py                   # Paper trading simulator
│   │   ├── validation.py              # Order validation (min notional, step size)
│   │   └── symbol_registry.py         # Dynamic symbol cache via exchangeInfo
│   │
│   ├── execution/
│   │   ├── order_service.py           # Decision → exchange order + SL/TP placement
│   │   └── position_manager.py        # Background monitoring loop + SL/TP/Telegram
│   │
│   ├── state/
│   │   ├── database.py                # SQLite: connection, schema, migrations
│   │   └── repositories.py            # Repository pattern (10+ repos)
│   │
│   ├── api/
│   │   ├── server.py                  # FastAPI server (15+ endpoints)
│   │   ├── auth.py                    # API key auth + HMAC + rate limiter
│   │   └── webhook.py                 # Trade event webhook emitter
│   │
│   ├── domain/
│   │   └── models.py                  # Typed dataclasses (12 models)
│   │
│   ├── events/
│   │   └── bus.py                     # In-process pub/sub event bus
│   │
│   ├── health/
│   │   └── reporter.py                # Periodic health checks + Telegram report
│   │
│   ├── notifier/
│   │   └── telegram.py                # Bot API notifications (Markdown-safe)
│   │
│   └── config/
│       └── loader.py                  # Config.yaml + .env merger
│
├── docs/                              # Full documentation
├── tests/                             # Pytest suite
├── data/                              # SQLite DB, logs (created at runtime)
├── scripts/                           # Utility scripts
├── config.yaml                        # Trading configuration
├── Dockerfile                         # Fly.io container
├── fly.toml                           # Fly.io app config
├── deploy.sh                          # Deploy + version bump script
├── .env                               # Secrets (never committed)
├── requirements.txt                   # Python dependencies
└── .dockerignore
```

---

## 7. Deployment Architecture (Fly.io)

```
┌──────────────────────────────────────────────────────┐
│                    Fly.io                             │
│  Region: sin (Singapore)                              │
│  Machine: YOUR_MACHINE_ID                              │
│                                                       │
│  ┌──────────────────────────────────────────────┐     │
│  │  Docker Container (python:3.11-slim)          │     │
│  │                                               │     │
│  │  ┌──────────────────┐  ┌──────────────────┐   │     │
│  │  │ main.py          │  │ API Server       │   │     │
│  │  │ (Telethon +      │  │ (FastAPI :9090)  │   │     │
│  │  │  Position Mgr)   │  │                  │   │     │
│  │  └──────────────────┘  └──────────────────┘   │     │
│  │                                               │     │
│  │  ┌──────────────────────────────────────┐     │     │
│  │  │ /data/ (persistent volume)           │     │     │
│  │  │  ├── trades.db                       │     │     │
│  │  │  ├── sessions/                       │     │     │
│  │  │  └── logs/                           │     │     │
│  │  └──────────────────────────────────────┘     │     │
│  └──────────────────────────────────────────────┘     │
│                                                       │
│  Secrets (fly secrets set):                           │
│  - TELEGRAM_BOT_TOKEN                                 │
│  - OPENCODE_GO_API_KEY                                │
│  - BINANCE_API_KEY / BINANCE_API_SECRET               │
│  - SESSION_STRING (Telethon string session)           │
│  - API_KEY (for HTTP API bridge)                      │
└──────────────────────────────────────────────────────┘
```

**Volumes:** Persistent data lives at `/data/` (Fly volume `data_vol`):
- `/data/trades.db` — SQLite database (survives deploys/restarts)
- `/data/sessions/` — Telethon session string backup
- `/data/logs/` — Application logs

**State Restore:** On each startup, the bot:
1. Connects to SQLite at `/data/trades.db` (creates if absent)
2. Loads Telethon session from `SESSION_STRING` env var (no re-auth)
3. Fetches open positions from Binance (source of truth)
4. Resumes position monitoring from the last saved state

---

## 8. Risk & Safety Architecture

```
                    ┌─────────────────────────┐
                    │      Gate 1 (pre-LLM)    │
                    │  ┌───────────────────┐   │
                    │  │ Idempotency       │   │
                    │  │ Cooldown          │   │  Fast rejects
                    │  │ Pair Whitelist    │   │  (no LLM cost)
                    │  └───────────────────┘   │
                    └───────────┬─────────────┘
                                │ pass
                                ▼
                    ┌─────────────────────────┐
                    │      LLM Agent           │
                    │  Decides: ENTER/CLOSE/   │
                    │  SKIP with size, SL, TP  │
                    └───────────┬─────────────┘
                                │ decision
                                ▼
                    ┌─────────────────────────┐
                    │    Gate 2 (post-LLM)    │
                    │  ┌───────────────────┐   │
                    │  │ Size Clamp ($1)   │   │  Hard limits
                    │  │ Max Concurrent(2) │   │  (cannot override)
                    │  │ Daily Loss (30%)  │   │
                    │  │ Min Notional ($5) │   │
                    │  │ SL Direction      │   │
                    │  └───────────────────┘   │
                    └───────────┬─────────────┘
                                │ pass
                                ▼
                    ┌─────────────────────────┐
                    │   Position Manager       │
                    │  ┌───────────────────┐   │
                    │  │ SL Monitor (10s)  │   │  Runtime safety
                    │  │ TP Monitor        │   │
                    │  │ Time Exit (48h)   │   │
                    │  │ Orphan Detection  │   │
                    │  │ Self-Heal Orders  │   │
                    │  │ Telegram Notify   │   │
                    │  └───────────────────┘   │
                    └─────────────────────────┘
```

See **[RISK_MANAGEMENT.md](RISK_MANAGEMENT.md)** for complete details on each layer.
