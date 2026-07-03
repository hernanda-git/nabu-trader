# Agentic Auto-Trade System — Plan

> Branch: `feature/auto-trade`
> Goal: Transform signal listener → agentic auto-trader with Binance integration

---

## 🧠 Architecture Overview

```
@Gishbanda Channel
    │
    ▼
┌─────────────────────────────┐
│  Signal Listener (Telethon) │  ◀── Existing, already runs
│  src/listener.py            │
└─────────┬───────────────────┘
          │ raw message text
          ▼
┌─────────────────────────────┐
│  Agent Brain                │  ◀── NEW: LLM-powered reasoning
│  src/agent.py               │
│                             │
│  • Parse ambiguous signals  │
│  • Validate trade setup     │
│  • Risk assessment          │
│  • Decide: enter / skip     │
└─────────┬───────────────────┘
          │ structured trade decision
          ▼
┌─────────────────────────────┐
│  Execution Engine           │  ◀── NEW: Binance API
│  src/executor.py            │
│                             │
│  • Market/limit entry       │
│  • Set SL (stop-limit)      │
│  • Set TP (limit orders)    │
│  • Close positions          │
│  • Query balances/positions │
└─────────┬───────────────────┘
          │ order confirmation
          ▼
┌─────────────────────────────┐
│  State Manager              │  ◀── NEW: Track everything
│  src/state.py               │
│                             │
│  • Open positions JSON      │
│  • Trade history SQLite     │
│  • P&L tracking             │
│  • Cooldown/dupe prevention │
└─────────┬───────────────────┘
          │ notification
          ▼
┌─────────────────────────────┐
│  Notifier (Telegram Bot)    │  ◀── Existing
│  src/listener.py            │
└─────────────────────────────┘
```

---

## 🧩 Phase Breakdown

### Phase A — Foundation (this branch)

| Component | File | What it does |
|-----------|------|-------------|
| Agent Brain | `src/agent.py` | LLM-powered reasoning: reads signal text, enriches missing fields, decides entry/exit |
| Execution Engine | `src/executor.py` | Binance REST + WebSocket: place orders, set SL/TP, cancel, close |
| State Manager | `src/state.py` | Track positions, trades, P&L via SQLite + in-memory cache |
| Orchestrator | `src/orchestrator.py` | Ties listener → agent → executor into one pipeline |
| Config | `config.yaml` | Binance keys, risk params, pair mapping, cooldowns |
| Env Update | `.env` | Add `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| Tests | `tests/` | Verify agent parsing, executor dry-run, state consistency |

### Phase B — Agentic Pipeline (included)

```
Signal arrives
    │
    ▼
Orchestrator receives message
    │
    ├──▶ Agent parses + enriches signal
    │      • Extract pair, direction, entry, SL, TP
    │      • If missing → infer from context / ask LLM
    │      • Validate: is entry price realistic? is SL reasonable?
    │
    ├──▶ Risk check
    │      • Position size = risk % of balance
    │      • Max concurrent positions
    │      • Check cooldown (avoid re-entry on same pair)
    │
    ├──▶ Execute trade
    │      • Market buy/sell at current price
    │      • Place SL order (stop-limit / stop-market)
    │      • Place TP limit orders
    │      • Log position in state
    │
    └──▶ Notify via Telegram bot
           • Trade opened: pair, size, entry, SL, TP
           • If error: full details
```

### Phase C — Position Management (included)

```
Background monitor (every N seconds)
    │
    ├──▶ Check open positions
    │      • WebSocket stream for real-time price
    │      • Or REST poll every 30s
    │
    ├──▶ Auto-close logic
    │      • SL hit → confirmed close
    │      • TP hit → confirmed close
    │      • Manual trigger via Telegram command
    │      • Time-based exit (if not closed in X hours)
    │
    ├──▶ Trigger close
    │      • If new signal arrives opposite direction on same pair
    │      • Close existing → open new
    │
    └──▶ Update state + notify
```

---

## 📡 Agent Brain — Design

The agent brain is the key "agentic" layer. Unlike simple regex parsing, it uses an LLM to:

1. **Parse any signal format** — human-written posts, structured lists, even memes with text
2. **Enrich incomplete signals** — if only pair + direction given, infer entry/TP/SL from market context
3. **Risk score** — reject low-quality signals, flag suspicious setups
4. **Decide timing** — enter now (market) or wait (limit order)

**Implementation**: Since we're already running via Hermes with DeepSeek, the agent brain will be a Python module that calls an LLM via Hermes' provider chain. But to keep it self-contained and fast, we'll implement a **local reasoning engine** in `src/agent.py` that:

- First pass: regex enrichment (already done in `listener.py`)
- Second pass: LLM call for ambiguous/edge cases (optional, can be turned on/off)
- Third pass: rule-based validation (risk limits, position size, cooldown)

The agent will return a structured `TradeDecision`:

```python
@dataclass
class TradeDecision:
    action: Literal["ENTER", "CLOSE", "SKIP"]
    pair: str
    direction: Literal["LONG", "SHORT"]
    entry_type: Literal["MARKET", "LIMIT"]
    entry_price: Optional[float]
    quantity: float  # in base asset
    sl_price: Optional[float]
    tp_prices: list[float]
    reason: str
    confidence: float  # 0.0 - 1.0
```

---

## ⚡ Execution Engine — Design

### Entry

| Strategy | Method | When |
|----------|--------|------|
| Market | `POST /api/v3/order` with `MARKET` | Signal says "entry @ market" or fast entry needed |
| Limit | `POST /api/v3/order` with `LIMIT` | Signal gives specific entry price |
| Stop-Limit | `POST /api/v3/order` with `STOP_LOSS_LIMIT` | For breakouts |

### TP / SL Placement

- **Stop Loss**: Place a `STOP_MARKET` or `STOP_LOSS_LIMIT` order immediately after entry
- **Take Profit**: Place `LIMIT` orders at TP1, TP2, TP3 levels
- If Binance doesn't support OCO (One-Cancels-Other) for futures, use separate orders

### Close

- **Market close**: `POST /api/v3/order` with `MARKET` in opposite direction
- **Trigger close**: When new signal comes in opposite direction on same pair → close first, then open new

### Risk Controls

- Position size = `account_balance * risk_percent / (entry - SL)`
- Max 1-3 concurrent positions (configurable)
- Daily loss limit: if total loss > X% today, stop trading
- Cooldown: minimum 5 minutes between trades on same pair

---

## 🗄️ State Manager — Design

### SQLite Schema (`data/trades.db`)

```sql
-- All executed trades
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,           -- message ID from Telegram
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    entry_order_id TEXT,
    entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sl_price REAL,
    sl_order_id TEXT,
    tp1_price REAL,
    tp1_order_id TEXT,
    tp2_price REAL,
    tp2_order_id TEXT,
    tp3_price REAL,
    tp3_order_id TEXT,
    exit_price REAL,
    exit_order_id TEXT,
    exit_time TIMESTAMP,
    pnl REAL,
    status TEXT DEFAULT 'OPEN',  -- OPEN, CLOSED, CANCELLED
    reason TEXT                  -- why it was closed
);

-- Agent decisions log
CREATE TABLE agent_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_text TEXT,
    decision TEXT,       -- JSON of TradeDecision
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### In-Memory Cache

```python
class TradeState:
    open_positions: dict[str, Position]  # key = pair
    balance: dict
    daily_pnl: float
    cooldowns: dict[str, datetime]
```

---

## 🔧 Config (`config.yaml`)

```yaml
binance:
  api_key: ""
  api_secret: ""
  testnet: true  # Start on testnet for safety
  recv_window: 5000

risk:
  max_position_size_usdt: 100
  max_concurrent_positions: 2
  risk_per_trade_percent: 2.0    # % of balance to risk
  daily_loss_limit_percent: 10.0
  min_cooldown_minutes: 5
  max_leverage: 1                 # spot only initially

agent:
  use_llm: false                  # Phase 2: enable LLM enrichment
  confidence_threshold: 0.6       # skip signals below this
  auto_trade: true                # master switch
  allowed_pairs:
    - "BTCUSDT"
    - "ETHUSDT"
    - "SOLUSDT"
    - "PUMPUSDT"                  # if available
  # wildcard: true                # allow any pair

monitor:
  check_interval_seconds: 10
  websocket: true                 # use Binance WS for real-time
```

---

## 📁 File Map

```
nabu-trader/
├── src/
│   ├── listener.py      ← existing (modified: forward to orchestrator)
│   ├── agent.py         ← NEW: LLM + rule-based trade decision engine
│   ├── executor.py      ← NEW: Binance API client
│   ├── state.py         ← NEW: SQLite state + in-memory cache
│   ├── orchestrator.py  ← NEW: pipeline coordinator
│   └── config.py        ← NEW: config loader
├── data/
│   └── trades.db        ← auto-created SQLite
├── config.yaml          ← NEW: risk, API, pair config
├── PLAN.md              ← this file
├── .env                 ← updated with Binance keys
├── tests/
│   ├── test_agent.py
│   ├── test_executor.py
│   ├── test_state.py
│   └── test_orchestrator.py
```

---

## 🧪 Testing Strategy

| Test | Approach |
|------|----------|
| Agent parsing | Unit test with real @gishbanda messages against `TradeDecision` schema |
| Executor | Binance testnet only (real API, fake money) |
| State | SQLite in-memory, verify CRUD + P&L calc |
| Orchestrator | Mock all deps, verify pipeline input→output |
| E2E dry-run | Run with `auto_trade: false`, verify decisions logged no real orders |

---

## ✅ Success Criteria

1. Signal from @gishbanda → parsed → decision → Binance testnet order placed ✅
2. TP/SL auto-placed on entry ✅
3. Opposite-direction signal triggers close → re-entry ✅
4. Position state persisted across restarts ✅
5. Telegram notification on every trade event ✅
6. Safety: configurable risk limits, testnet-first, manual override ✅

---

## 📅 Implementation Order

1. `config.yaml` + `config.py` — load all settings
2. `state.py` — SQLite schema + in-memory cache
3. `executor.py` — Binance API client (testnet)
4. `agent.py` — rule-based decision engine
5. `orchestrator.py` — wire listener → agent → executor → state
6. `tests/` — verify everything
7. Run live on testnet, observe, tune

---

*Plan v1.0 — Start with testnet, never mainnet until proven safe.*
