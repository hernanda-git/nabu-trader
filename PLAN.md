# Agentic Auto-Trade System — Plan

> **Branch:** `feature/auto-trade`
> **Goal:** Transform Telegram signal listener → agentic auto-trader with exchange abstraction
> **Status:** Planning — based on PLAN_REVISE.md review

---

## 🧠 Architecture Overview

```
  @Gishbanda Channel
        │
        ▼
┌───────────────────┐
│  Listener         │  Telethon — monitors channel in real-time
│  src/listener.py  │  (existing, modified to emit events)
└──────┬────────────┘
       │ raw message
       ▼
┌───────────────────┐
│  Signal Parser    │  Extracts pair, direction, entry, SL, TP
│  agent/parser.py  │  Regex + heuristics
└──────┬────────────┘
       │ TradeSignal
       ▼
┌───────────────────┐
│  Signal Validator │  Checks completeness, sanity, duplicates
│  agent/validator  │
└──────┬────────────┘
       │ validated signal
       ▼
┌───────────────────┐
│  Risk Engine      │  Position sizing, concurrent limits, cooldown
│  agent/risk.py    │
└──────┬────────────┘
       │ RiskAssessment
       ▼
┌───────────────────┐
│  Decision Engine  │  ENTER / SKIP / CLOSE decision
│  agent/decision.py│  Uses strategy + confidence threshold
└──────┬────────────┘
       │ TradeDecision
       ▼
┌───────────────────┐
│  Order Service    │  Translates decision → exchange order
│  execution/       │
└──────┬────────────┘
       │ OrderRequest
       ▼
┌──────────────────────────────────┐
│  Exchange Interface              │
│  exchange/base.py ← abstract     │
│  ├── exchange/paper.py           │  ← simulates fills
│  └── exchange/binance.py         │  ← real API + testnet
└────────┬─────────────────────────┘
         │ ExecutionResult
         ▼
┌───────────────────┐
│  Position Manager │  Owns SL/TP monitoring, time-based exits
│  position/        │
└──────┬────────────┘
       │ position events
       ▼
┌───────────────────┐
│  Event Bus        │  In-process pub/sub
│  events/bus.py    │  Loose coupling, multiple consumers
└──┬────┬────┬──────┘
   │    │    │
   ▼    ▼    ▼
Notify State Audit
```

---

## 📡 Component Breakdown

### Domain Layer (`domain/models.py`)

Strongly-typed immutable models — the backbone of every interaction:

```python
@dataclass(frozen=True)
class TradeSignal:
    """Raw parsed signal from Telegram."""
    message_id: int
    channel: str
    raw_text: str
    pair: str | None
    direction: Literal["LONG", "SHORT"] | None
    entry_price: float | None
    sl_price: float | None
    tp_prices: list[float]
    has_media: bool
    timestamp: datetime

@dataclass(frozen=True)
class RiskAssessment:
    allowed: bool
    quantity: float
    reason: str
    confidence: float

@dataclass(frozen=True)
class TradeDecision:
    action: Literal["ENTER", "CLOSE", "SKIP"]
    pair: str
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"]
    quantity: float
    entry_price: float | None
    sl_price: float | None
    tp_prices: list[float]
    reason: str
    confidence: float

@dataclass(frozen=True)
class OrderRequest:
    exchange: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP_LOSS_LIMIT"]
    quantity: float
    price: float | None
    stop_price: float | None
    client_order_id: str  # idempotency key

@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    order_id: str
    symbol: str
    side: str
    filled_quantity: float
    avg_price: float
    status: str
    error: str | None

@dataclass
class Position:
    id: int
    pair: str
    direction: str
    entry_price: float
    quantity: float
    sl_price: float | None
    tp_prices: list[float]
    entry_order_id: str
    entry_time: datetime
    exit_price: float | None
    exit_time: datetime | None
    status: Literal["OPEN", "CLOSED", "CANCELLED"]
    pnl: float | None
    reason: str | None
```

### Agent Layer (`agent/`)

| Component | File | Responsibility |
|-----------|------|---------------|
| Signal Parser | `agent/parser.py` | Regex extraction of pair, direction, entry, SL, TP from raw text |
| Signal Validator | `agent/validator.py` | Completeness check, pair whitelist, duplicate signal detection |
| Risk Engine | `agent/risk.py` | Position size = `balance * risk% / (entry - SL)`, max concurrent, daily loss limit, cooldown |
| Decision Engine | `agent/decision.py` | Orchestrates parser → validator → risk → final ENTER/SKIP/CLOSE decision |
| LLM Reasoner | `agent/llm.py` | *Optional* — calls Hermes provider chain for ambiguous signal enrichment |

### Exchange Layer (`exchange/`)

| File | Role |
|------|------|
| `exchange/base.py` | Abstract `Exchange` interface (place_order, cancel, get_position, get_balance) |
| `exchange/paper.py` | Simulated fills, fees, slippage. No API dependency |
| `exchange/binance.py` | Real Binance REST + WebSocket. Testnet toggle via config |

### Execution Layer (`execution/`)

| File | Role |
|------|------|
| `execution/order_service.py` | Translates TradeDecision → OrderRequest → Exchange. Retry + idempotency |
| `execution/position_manager.py` | Monitors open positions, checks SL/TP hit, handles time-based exits, trigger close |

### State Layer (`state/`)

Repository pattern — business logic never touches SQL directly:

| File | Role |
|------|------|
| `state/database.py` | SQLite connection management, schema creation, migrations |
| `state/repositories.py` | SignalRepository, DecisionRepository, OrderRepository, PositionRepository, EventRepository |

### Event System (`events/bus.py`)

Simple in-process pub/sub:

```python
Events: SignalReceived | DecisionCreated | OrderPlaced | OrderFilled |
        PositionOpened | PositionClosed | SLTriggered | TPTriggered |
        TradeRejected | Error
```

### Config (`config/`)

| File | Role |
|------|------|
| `config/loader.py` | Load `config.yaml` + `.env`, merge, validate |
| `config/validator.py` | Fail-fast on invalid API keys, risk params, pair whitelist |

---

## 🗄️ Database Schema

```sql
-- Every signal received from Telegram
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    pair TEXT,
    direction TEXT,
    entry_price REAL,
    sl_price REAL,
    tp_prices TEXT,             -- JSON array
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every decision made by the agent
CREATE TABLE decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES signals(id),
    action TEXT NOT NULL,        -- ENTER / CLOSE / SKIP
    pair TEXT,
    direction TEXT,
    quantity REAL,
    confidence REAL,
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every order sent to exchange
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER REFERENCES decisions(id),
    client_order_id TEXT UNIQUE,  -- idempotency key
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL,
    status TEXT DEFAULT 'PENDING',
    exchange_order_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Every fill / execution
CREATE TABLE executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER REFERENCES orders(id),
    filled_quantity REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL,
    fee_asset TEXT,
    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Active and historical positions
CREATE TABLE positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    sl_price REAL,
    tp_prices TEXT,              -- JSON array
    entry_order_id TEXT,
    entry_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    exit_price REAL,
    exit_order_id TEXT,
    exit_time TIMESTAMP,
    pnl REAL,
    status TEXT DEFAULT 'OPEN',   -- OPEN / CLOSED / CANCELLED
    reason TEXT,
    closed_by TEXT               -- 'SL' / 'TP' / 'MANUAL' / 'TRIGGER'
);

-- Idempotency: processed signal tracking
CREATE TABLE processed_signals (
    message_id INTEGER PRIMARY KEY,
    signal_hash TEXT NOT NULL,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Immutable audit trail
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,       -- JSON
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Daily aggregated metrics
CREATE TABLE daily_stats (
    date TEXT PRIMARY KEY,
    total_signals INTEGER DEFAULT 0,
    trades_opened INTEGER DEFAULT 0,
    trades_closed INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0.0,
    max_drawdown REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## ⚙️ Config (`config.yaml`)

```yaml
exchange:
  active: paper              # paper | binance | binance_testnet
  binance:
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
    testnet: true
    recv_window: 5000

risk:
  max_position_size_usdt: 100
  max_concurrent_positions: 2
  risk_per_trade_percent: 2.0
  daily_loss_limit_percent: 10.0
  min_cooldown_minutes: 5

agent:
  confidence_threshold: 0.6
  auto_trade: false           # start in dry-run mode!
  allowed_pairs:
    - "*"                     # wildcard: allow any pair

monitoring:
  check_interval_seconds: 10
  health_check_port: 9090
```

---

## 📁 Folder Structure

```
nabu-trader/
├── src/
│   ├── agent/
│   │   ├── parser.py         Signal parsing
│   │   ├── validator.py      Signal validation
│   │   ├── risk.py           Risk engine
│   │   ├── decision.py       Decision engine
│   │   └── llm.py           Optional LLM enrichment
│   │
│   ├── exchange/
│   │   ├── base.py           Abstract exchange interface
│   │   ├── paper.py          Paper trading simulation
│   │   └── binance.py        Binance API (testnet + mainnet)
│   │
│   ├── execution/
│   │   ├── order_service.py  Order placement + retry
│   │   └── position_manager.py  Position lifecycle
│   │
│   ├── state/
│   │   ├── database.py       SQLite connection + schema
│   │   └── repositories.py   Repository pattern
│   │
│   ├── domain/
│   │   └── models.py         Typed dataclasses
│   │
│   ├── events/
│   │   └── bus.py            In-process event bus
│   │
│   ├── notifier/
│   │   └── telegram.py       Telegram notifications
│   │
│   ├── config/
│   │   ├── loader.py         Config loader
│   │   └── validator.py      Config validation
│   │
│   ├── listener.py           Telethon listener (modified)
│   ├── orchestrator.py       Pipeline coordinator
│   └── main.py               Entry point
│
├── config.yaml               Trading configuration
├── .env                      Secrets (Binance keys)
├── data/
│   └── trades.db             SQLite database (auto-created)
├── PLAN.md                   This file
├── tests/
│   ├── test_parser.py
│   ├── test_validator.py
│   ├── test_risk.py
│   ├── test_decision.py
│   ├── test_paper_exchange.py
│   ├── test_order_service.py
│   ├── test_position_manager.py
│   └── test_orchestrator.py
└── requirements.txt
```

---

## 🔄 Pipeline Flow (Detailed)

```
1. Telegram listener receives message
2. ➡️ Event Bus emits: SignalReceived(signal)
3. Listeners:
   a. Signal Parser → TradeSignal
   b. State: saves to `signals` table
4. Validator checks: completeness, pair whitelist, idempotency
   ➡️ Event Bus emits: SignalValidated | SignalRejected
5. Risk Engine: position size, cooldown, daily limits
   ➡️ Event Bus emits: RiskAssessment
6. Decision Engine: ENTER / SKIP / CLOSE
   ➡️ Event Bus emits: DecisionCreated | TradeRejected
7. Order Service: translates decision → OrderRequest → Exchange
   ➡️ Event Bus emits: OrderPlaced
8. Exchange returns: order_id, filled, avg_price
   ➡️ Event Bus emits: OrderFilled | OrderFailed
9. Position Manager: records position, SL/TP orders placed
   ➡️ Event Bus emits: PositionOpened
10. Notifier: sends formatted Telegram message
11. Background loop:
    - Every N seconds, Position Manager checks SL levels
    - If hit → close → Emit: PositionClosed
    - If new opposite-direction signal → trigger close
```

---

## ✅ Success Criteria

1. Signal from @gishbanda → parsed → decision → paper trade executed ✅
2. TP/SL auto-placed on entry ✅
3. Opposite-direction signal triggers close → re-entry ✅
4. Idempotency: crash + restart doesn't duplicate trades ✅
5. Position state persisted + restorable across restarts ✅
6. Telegram notification on every event ✅
7. Configurable risk limits, testnet-first, dry-run mode ✅
8. Full audit trail: every signal, decision, order, and fill logged ✅

---

## 🛣️ Implementation Order

### Phase 1 — Foundation

| Step | Files | What |
|------|-------|------|
| 1 | `config/loader.py`, `config/validator.py`, `config.yaml` | Config system |
| 2 | `domain/models.py` | All typed dataclasses |
| 3 | `state/database.py`, `state/repositories.py` | SQLite + repositories |
| 4 | `events/bus.py` | In-process event bus |

### Phase 2 — Trading Core

| Step | Files | What |
|------|-------|------|
| 5 | `agent/parser.py` | Signal parsing (reuse from listener.py) |
| 6 | `agent/validator.py` | Validation rules |
| 7 | `agent/risk.py` | Position sizing, limits |
| 8 | `agent/decision.py` | Decision engine |
| 9 | `exchange/base.py`, `exchange/paper.py` | Exchange abstraction + paper |
| 10 | `exchange/binance.py` | Binance API (testnet) |
| 11 | `execution/order_service.py` | Order placement |
| 12 | `execution/position_manager.py` | Position lifecycle |

### Phase 3 — Pipeline

| Step | Files | What |
|------|-------|------|
| 13 | `orchestrator.py` | Wire everything together |
| 14 | `listener.py` (modified) | Emit events instead of direct calls |
| 15 | `notifier/telegram.py` | Telegram notifications |
| 16 | Tests | Verify every module |
| 17 | Live dry-run | `auto_trade: false` on testnet |

### Phase 4 — Hardening (future)

| Feature | Priority |
|---------|----------|
| LLM enrichment (`agent/llm.py`) | Medium |
| Retry + circuit breaker | High |
| Health monitoring | Medium |
| Metrics (win rate, drawdown) | Low |
| More exchange adapters (Bybit, Hyperliquid) | Low |

---

*Plan v2.0 — Merged with PLAN_REVISE.md recommendations. Paper trading first, testnet second, mainnet never until proven.*
