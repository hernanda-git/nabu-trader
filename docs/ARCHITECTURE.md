# Architecture

## System Overview

The nabu-trader is a **real-time Telegram signal listener → LLM-powered analysis → automated Binance Futures trading** pipeline. It monitors a Telegram channel, parses trade signals using regex, analyzes them via an LLM (OpenCode Go), applies hard safety gates, and executes trades on Binance USDⓈ-M Futures.

```
┌─────────────────────────────────────────────────────────────┐
│                    Telegram Channel                          │
│                    @YOUR_SIGNAL_CHANNEL                             │
└─────────────────────────┬───────────────────────────────────┘
                          │ raw message
                          ▼
┌─────────────────────────────────────────────────────────────┐
│  SignalListener (Telethon)                                   │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  • Connects to Telegram as user session               │   │
│  │  • Listens for new + edited messages                  │   │
│  │  • Forwards to orchestrator.handle_signal()           │   │
│  └────────────────────┬─────────────────────────────────┘   │
└───────────────────────┼─────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│  TradeOrchestrator (Pipeline Coordinator)                    │
│                                                              │
│  1. Regex Pre-Parse (1ms)                                    │
│     └─ parser.py → TradeSignal                              │
│                                                              │
│  2. Safety Gate 1 (pre-LLM)                                 │
│     └─ Idempotency, cooldown, whitelist                     │
│                                                              │
│  3. Fetch Balance from Exchange                              │
│                                                              │
│  4. Agent Brain (1 LLM call)                                 │
│     └─ agent.py → TradeDecision via OpenCode Go              │
│                                                              │
│  5. Safety Gate 2 (post-LLM)                                 │
│     └─ Clamp, scale, leverage, daily loss                   │
│                                                              │
│  6. Order Service (execute)                                  │
│     └─ order_service.py → Exchange order + SL/TP            │
│                                                              │
│  7. Notify via Telegram Bot                                  │
│     └─ notifier/telegram.py                                  │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### 1. Listener (`src/listener.py`)
- Uses **Telethon** (MTProto, not Bot API) to monitor a Telegram channel
- Default channel: `@YOUR_SIGNAL_CHANNEL` (configurable via `CHANNEL_USERNAME` env var)
- Two event handlers: `NewMessage` + `MessageEdited` (for signal updates)
- Deduplication via DB `processed_signals` table + Gate1 `get_by_message_id()` check
- Requires a user account session (not a bot) — authenticated via Telegram client

### 2. Orchestrator (`src/orchestrator.py`)
The central pipeline coordinator. Wires all components together:
```
handle_signal(msg) →
  1. Regex parse → TradeSignal
  2. Gate 1 check (fast reject)
  3. Save signal to DB
  4. Fetch balance from exchange
  5. Agent Brain → TradeDecision
  6. Gate 2 check (clamp + leverage)
  7. Notify decision
  8. Execute via OrderService
  9. Notify result
 10. Emit events
```

### 3. Agent Brain (`src/agent/agent.py`)
- Single LLM call via **OpenCode Go** (`deepseek-v4-flash` or compatible)
- System prompt: instructs the LLM to calculate position sizing based on risk%
- Prompt includes: raw signal text, regex pre-parse fields, open positions, account balance
- Output: structured JSON (`TradeDecision`) with action, pair, direction, quantity, prices
- Supports reasoning models (handles both `content` and `reasoning_content` response fields)
- Dry-run mode: `auto_trade: false` logs without executing

### 4. Safety Gates (`src/agent/gate.py`)

**Gate 1 (pre-LLM)** — lightweight checks that skip before the LLM call:
| Check | Purpose |
|-------|---------|
| Idempotency | Prevents re-processing signals after restart/crash |
| Pair whitelist | Only trade allowed pairs (`*` = all) |
| Cooldown | Prevents re-entry on same pair within N minutes |

**Gate 2 (post-LLM)** — hard limits the LLM cannot override:
| Check | Purpose |
|-------|---------|
| Max concurrent | Limits number of open positions |
| Position value clamp | Caps position in USDT (dynamic via balance) |
| Daily loss limit | Stops trading if daily P&L exceeds threshold |
| Quantity validation | Rejects zero/negative quantities |
| Min notional scaling | Scales up to exchange minimum if needed |
| Leverage calculation | Dynamic per-trade leverage for margin efficiency |
| SL sanity | Rejects SL on wrong side of entry (would trigger immediately) |

### 5. Exchange Layer (`src/exchange/`)

| Adapter | Use Case |
|---------|----------|
| `base.py` | Abstract interface (ABC) with all exchange methods |
| `paper.py` | Simulated trading — no real money, instant fills |
| `binance.py` | Real Binance REST API — supports Spot + USDⓈ-M Futures |

**Binance Futures features:**
- Dynamic leverage per symbol (set before each order)
- Isolated margin mode
- STOP_MARKET orders for stop-loss
- Leverage endpoint (`/fapi/v1/leverage`)
- Balance from futures wallet (`/fapi/v2/account`)

### 6. Order Service (`src/execution/order_service.py`)
Translates a `TradeDecision` into actual exchange orders:
1. Sets futures leverage + margin type
2. Places entry order (MARKET or LIMIT)
3. Places STOP_MARKET (stop-loss) order
4. Places LIMIT take-profit orders
5. Records everything to DB (idempotency keys)

### 7. Position Manager (`src/execution/position_manager.py`)
Background loop that monitors open positions:
- Fills from SL/TP orders (via Binance reconciliation)
- Time-based auto-close (>48h configurable)
- Stale position cleanup
- Event-driven notifications

### 8. State Layer (`src/state/`)
- **SQLite** database with 10 tables
- Repository pattern for each entity (Signal, Decision, Order, Position, Event)
- WAL mode for concurrent reads
- Auto-created at first run

## Data Flow (Detailed)

### Signal → Trade

```
Message on @YOUR_SIGNAL_CHANNEL
  │
  ▼
SignalListener.on_new_message()
  │ message_id, channel, raw_text, has_media
  ▼
TradeOrchestrator.handle_signal()
  │
  ├─ 1. parse_signal(raw_text) → TradeSignal
  │      Regex: pair, direction, entry, SL, TP
  │      ~1ms, no network
  │
  ├─ 2. SafetyGate1.check(signal)
  │      ├─ Already processed?       → SKIP
  │      ├─ Pair whitelisted?        → SKIP
  │      └─ Cooldown active?         → SKIP
  │
  ├─ 3. exchange.get_balance() → BalanceInfo
  │      Futures wallet balance (USDT)
  │
  ├─ 4. AgentBrain.decide(signal, positions, balance)
  │      ├─ Build prompt with context
  │      ├─ LLM call (OpenCode Go)
  │      └─ Parse response → TradeDecision
  │
  ├─ 5. SafetyGate2.check(decision, balance)
  │      ├─ Clamp position value to max_size
  │      ├─ Scale up for min_notional if needed
  │      ├─ Calculate dynamic leverage
  │      └─ Validate SL side
  │
  ├─ 6. OrderService.execute(decision)
  │      ├─ Set leverage & margin type
  │      ├─ Place entry order
  │      ├─ Place SL order (STOP_MARKET)
  │      ├─ Place TP orders (LIMIT)
  │      └─ Save position to DB
  │
  └─ 7. TelegramNotifier (decision + execution)
```

### Dynamic Leverage Calculation

```
                  ┌─────────────────────┐
                  │ balance = $10.58    │
                  │ risk_pct = 10%      │
                  │ max_loss = $1.058   │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────────┐
                  │ LLM calculates qty  │
                  │ qty = max_loss /    │
                  │   abs(entry - sl)   │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────────┐
                  │ Gate 2 clamps       │
                  │ pos_val <= max_size │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────────┐
                  │ Scale up for        │
                  │ min_notional if     │
                  │ pos_val < $1        │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────────┐
                  │ Leverage = ceil(    │
                  │   pos_val /         │
                  │   (balance × 50%)   │
                  │ ) capped to 20x     │
                  └────────┬────────────┘
                           │
                  ┌────────▼────────────┐
                  │ Margin = pos_val /  │
                  │ leverage            │
                  │ ≈ 50% of balance    │
                  └─────────────────────┘
```

## Event System

The event bus (`src/events/bus.py`) provides in-process pub/sub:
- Events are emitted at each pipeline stage
- EventRepository logs all events to DB
- Future: could drive metrics, alerts, external triggers

## Database Schema

14 tables in `data/trades.db`:

### Core Pipeline

| Table | Records | Key Columns |
|-------|---------|-------------|
| `signals` | Raw Telegram signals | message_id (unique), pair, direction, entry_price, sl_price, tp_prices, correlation_id |
| `decisions` | LLM outputs | signal_id (FK), action, pair, quantity, confidence, reason, correlation_id, llm_interaction_id |
| `orders` | Exchange orders | decision_id (FK), symbol, side, type, quantity, status, exchange_order_id, correlation_id |
| `positions` | Open/closed positions | pair, direction, entry_price, quantity, sl_price, tp_prices, pnl, correlation_id, config_snapshot_id |
| `executions` | Order fills | order_id (FK), filled_quantity, price, fee |

### Traceability (New)

| Table | Records | Key Columns |
|-------|---------|-------------|
| `llm_interactions` | Full LLM request/response | decision_id (FK), model, system_prompt, user_prompt, raw_response, prompt_tokens, completion_tokens, latency_ms |
| `trade_logs` | Structured pipeline logs | correlation_id (indexed), level, module, message, metadata_json |
| `position_events` | Position lifecycle events | position_id (FK), event_type (SL_HIT, POSITION_OPENED, etc.), details, metadata_json |
| `config_snapshots` | Point-in-time config | config_hash, config_yaml (full dump), app_version |

### Support

| Table | Purpose |
|-------|---------|
| `processed_signals` | Idempotency tracking (message_id + hash) |
| `events` | Generic audit trail |
| `daily_stats` | Daily aggregated metrics |
| `pending_signals` | Conditional signals awaiting price triggers |

### Migrations

All new columns are added via `_run_migrations()` using `try/except` for `ALTER TABLE ADD COLUMN`, so existing databases are upgraded automatically without data loss.

## API Bridge (`src/api/`)

A FastAPI server running on port 9090 provides secure remote access to the database:

### Auth Flow

```
Client                          Fly.io Edge                     API Server
  │                                │                               │
  ├─ HTTPS GET /api/v1/stats ─────►├─ Forward to :9090 ──────────►│
  │   X-API-Key: <key>            │  (TLS terminated)             │  ├─ Constant-time key compare
  │                                │                               │  ├─ Rate limit check (30/min)
  │                                │                               │  └─ Return JSON
  │◄──── JSON response ◄──────────┤◄──────────────────────────────┤
```

For POST/PUT/DELETE: additional `X-Signature: <HMAC-SHA256>` header required.

### 15 Query Endpoints

| Endpoint | Returns |
|----------|---------|
| `GET /health` | Health check (no auth) |
| `GET /api/v1/auth/verify` | API key validation |
| `GET /api/v1/stats` | Dashboard (PnL, positions, signals, LLM tokens) |
| `GET /api/v1/trades` | Paginated trade list |
| `GET /api/v1/trades/{id}` | Full trace (trade + decision + LLM + events + logs) |
| `GET /api/v1/positions` | Open positions with lifecycle events |
| `GET /api/v1/llm/recent` | Recent LLM interactions |
| `GET /api/v1/llm/search?q=` | Semantic search across LLM prompts/responses |
| `GET /api/v1/logs/{correlation_id}` | Full pipeline trace |
| `GET /api/v1/config/snapshots` | Config version history |
| `GET /api/v1/events/recent` | Position lifecycle events |
| `GET /api/v1/signals/recent` | Recent signals with decisions |
| `GET /api/v1/search/trades?q=` | Trade search by pair/reason |
| `GET /api/v1/exchange/balance` | Live Binance balance (proxy) |

### Webhook (Push Notifications)

When a trade executes, the orchestrator calls `emit_event()` which POSTs to a configurable URL:

```json
{
  "event_type": "TRADE_ENTERED",
  "correlation_id": "a1b2c3d4e5f6",
  "data": { "pair": "BTCUSDT", "direction": "LONG", "entry_price": 65000, ... }
}
```

Headers: `X-Signature: <hmac-sha256>` (if secret configured)
