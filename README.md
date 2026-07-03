# Nabu Trader Signal Listener — Auto-Trade Pipeline

Real-time Telegram channel monitor → LLM-powered signal analysis → Automated trading via Binance.

> **Branch:** `feature/auto-trade`
> **Architecture:** Hybrid LLM + Hard Safety Gates

---

## 📡 What It Does

```
@Gishbanda Telegram Channel
          │
          ▼
  ┌──────────────────┐
  │  Regex Pre-Parse │  1ms — extracts pair, direction, numbers
  └──────┬───────────┘
         │
  ┌──────▼───────────┐
  │  Safety Gate 1   │  Idempotency, cooldown, whitelist
  └──────┬───────────┘
         │
  ┌──────▼───────────┐
  │  Agent Brain     │  1 LLM call via OpenCode Go (deepseek-v4-flash)
  │  (LLM)           │  Parses, validates, risk-assesses, decides
  └──────┬───────────┘
         │
  ┌──────▼───────────┐
  │  Safety Gate 2   │  Hard position limits, daily loss cap
  └──────┬───────────┘
         │
  ┌──────▼───────────┐
  │  Order Service   │  → Paper Exchange / Binance Testnet / Binance Mainnet
  └──────┬───────────┘
         │
  ┌──────▼───────────┐
  │  Position Mgr    │  Background monitor: SL/TP, time-based exits
  └──────┬───────────┘
         │
         ▼
  Telegram Notification
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Telegram account (for the listener)
- OpenCode Go subscription (for the LLM brain)
- Binance API keys (optional — paper trading works without)

### 1. Install Dependencies

```bash
cd "C:\Working Folder\Research\nabu-trader"

# From WSL:
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/pip install -r requirements.txt
```

### 2. Configure `.env`

Create `.env` in the project root:

```ini
# Telegram API credentials (from my.telegram.org)
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH

# Channel to monitor
CHANNEL_USERNAME=gishbanda

# Your Telegram chat ID for notifications
NOTIFY_CHAT_ID=YOUR_CHAT_ID

# Telegram Bot Token (from @BotFather)
TELEGRAM_BOT_TOKEN=REMOVED_SECRET

# OpenCode Go API Key (for LLM brain)
OPENCODE_GO_API_KEY=sk-...
```

### 3. First-Time Telegram Auth

Only needed once — creates a session file for the Telethon client:

```bash
# From WSL:
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python auth.py +628****1995

# From Windows CMD:
cd /d "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python auth.py +628****1995
```

Enter the 5-digit code Telegram sends you.

### 4. Configure Trading Settings

Edit `config.yaml` to set your risk preferences:

```yaml
exchange:
  active: paper              # paper | binance_testnet | binance

agent:
  auto_trade: false           # false = dry-run (no real trades)
  confidence_threshold: 0.6   # minimum confidence to trade

risk:
  max_position_size_usdt: 100
  max_concurrent_positions: 2
  risk_per_trade_percent: 2.0
  min_cooldown_minutes: 5
```

### 5. Run

```bash
# ─── From WSL ──────────────────────────────────
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python src/main.py

# ─── From Windows CMD ──────────────────────────
cd /d "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python src\main.py

# ─── From Windows PowerShell ───────────────────
cd "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python src\main.py

# ─── Or use the wrapper script ─────────────────
./run.sh          # WSL / Linux
run.bat           # Windows (double-click)
```

---

## ⚙️ Configuration Reference

### `config.yaml`

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| exchange | `active` | `paper` | `paper` / `binance_testnet` / `binance` |
| exchange.binance | `testnet` | `true` | Use Binance testnet |
| risk | `max_position_size_usdt` | `100` | Max USDT per position |
| risk | `max_concurrent_positions` | `2` | Max open positions at once |
| risk | `risk_per_trade_percent` | `2.0` | % of balance to risk per trade |
| risk | `daily_loss_limit_percent` | `10.0` | Stop trading if daily loss exceeds |
| risk | `min_cooldown_minutes` | `5` | Min time between same-pair trades |
| agent | `auto_trade` | `false` | Master switch for live trading |
| agent | `confidence_threshold` | `0.6` | Min LLM confidence to act |
| agent.llm | `api_url` | (see below) | OpenCode Go endpoint |
| agent.llm | `model` | `deepseek-v4-flash` | LLM model name |
| monitoring | `check_interval_seconds` | `10` | Position manager poll interval |

Default LLM endpoint: `https://opencode.ai/zen/go/v1/chat/completions`

---

## 📁 Project Structure

```
nabu-trader/
├── src/
│   ├── agent/
│   │   ├── parser.py          Regex pre-parse (~1ms)
│   │   ├── agent.py           LLM brain (1 call via OpenCode Go)
│   │   └── gate.py            Safety Gates 1 & 2 (hard limits)
│   │
│   ├── exchange/
│   │   ├── base.py            Abstract exchange interface
│   │   ├── paper.py           Paper trading simulation
│   │   └── binance.py         Binance REST API (+ testnet)
│   │
│   ├── execution/
│   │   ├── order_service.py   Decision → exchange order + SL/TP
│   │   └── position_manager.py Background position monitor
│   │
│   ├── state/
│   │   ├── database.py        SQLite schema (8 tables)
│   │   └── repositories.py    Repository pattern (CRUD)
│   │
│   ├── domain/
│   │   └── models.py          Typed dataclasses
│   │
│   ├── events/
│   │   └── bus.py             In-process pub/sub
│   │
│   ├── notifier/
│   │   └── telegram.py        Telegram Bot API notifications
│   │
│   ├── config/
│   │   └── loader.py          Config.yaml + .env merger
│   │
│   ├── listener.py            Telethon listener
│   ├── orchestrator.py        Pipeline coordinator
│   └── main.py                Entry point
│
├── config.yaml                Trading configuration
├── .env                       Secrets (Telegram + API keys)
├── data/trades.db             SQLite database (auto-created)
├── PLAN.md                    Architecture plan & docs
├── logs/trading.log           Activity log
└── requirements.txt
```

---

## 🔄 Pipeline Flow (Detailed)

### Signal Processing

```
1. Telegram listener receives message from @gishbanda
2. Regex pre-parse (1ms): extracts pair, direction, entry, SL, TP
3. Safety Gate 1 (pre-LLM):
   - Idempotency check — already processed? → skip
   - Cooldown check — same pair traded <5 min ago? → skip
   - Pair whitelist — not allowed? → skip
4. Agent Brain — 1 LLM call via OpenCode Go:
   - Input: raw text + regex fields + open positions + balance
   - Output: structured TradeDecision JSON
   - LLM handles parsing, validation, risk assessment, and decision in one shot
5. Safety Gate 2 (post-LLM):
   - Clamp quantity to risk % of balance
   - Enforce max concurrent positions
   - Enforce daily loss limit
6. Order Service: execute via exchange
   - Place entry order (MARKET or LIMIT)
   - Place SL order (STOP_LOSS)
   - Place TP orders (LIMIT)
7. State: save to SQLite (signals → decisions → orders → positions)
8. Notifier: Telegram message with trade details
```

### Position Monitoring

```
Background loop (every 10s):
  For each open position:
    - Check if SL order was filled → close + log P&L
    - Check if TP order was filled → close + log P&L
    - Check position age → time-based exit if >24h
    - If new opposite-direction signal → trigger close + re-entry
```

---

## 🛡️ Safety Features

| Feature | Layer | Description |
|---------|-------|-------------|
| **Idempotency** | Gate 1 | `processed_signals` table prevents duplicate trades after crash/restart |
| **Cooldown** | Gate 1 | Prevents re-entry on the same pair within N minutes |
| **Pair Whitelist** | Gate 1 | Only trade allowed pairs (or `*` for all) |
| **Position Sizing** | Gate 2 | Clamps quantity based on `max_position_size_usdt` |
| **Max Concurrent** | Gate 2 | Limits number of open positions |
| **Daily Loss Limit** | Gate 2 | Stops trading if daily P&L exceeds threshold |
| **Dry-Run Mode** | Agent | `auto_trade: false` — LLM analyzes but doesn't execute |
| **Paper Exchange** | Exchange | Simulated fills — no real money at risk |
| **Testnet** | Exchange | Binance testnet — real API, fake money |

---

## 🧪 Testing

### Unit Tests

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python -m pytest tests/   # when tests are added
```

### Manual LLM Test

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'src')
from src.agent.agent import AgentBrain
from src.config.loader import load_config
from src.domain.models import TradeSignal

cfg = load_config()
brain = AgentBrain(cfg)
signal = TradeSignal(message_id=1, channel='test',
    raw_text='BUY BTCUSDT Entry: 65400 SL: 64800 TP: 66200')
decision = brain.decide(signal)
print(f'{decision.action} {decision.pair} conf={decision.confidence}')
"
```

### Pipeline Dry-Run

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python src/main.py
# Set auto_trade: false in config.yaml — monitors + analyzes but no trades
```

---

## 🗺️ Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| **Phase 1** — Foundation | ✅ Done | Config, domain models, SQLite, event bus |
| **Phase 2** — Trading Core | ✅ Done | Agent brain, exchange abstraction, safety gates |
| **Phase 3** — Pipeline | ✅ Done | Orchestrator, notifier, entry point, LLM connection |
| **Phase 4** — Hardening | ⏳ Future | Retry/circuit breaker, health monitoring, metrics, more exchanges |

---

## 📝 Logs

All activity is logged to:

| File | Location |
|------|----------|
| Trading log | `logs/trading.log` |
| Listener log | `logs/listener.log` |
| SQLite DB | `data/trades.db` |
| Telegram sessions | `sessions/` |

---

## ⚠️ Important Notes

1. **Start with `auto_trade: false`** — verify the LLM makes correct decisions first
2. **Use paper exchange first** — no real money until you're confident
3. **Binance testnet** — `exchange.active: binance_testnet` gives real API with fake money
4. **Only switch to mainnet** when you've verified the pipeline for days/weeks
5. **The LLM endpoint** — OpenCode Go at `opencode.ai/zen/go/v1` with `deepseek-v4-flash`
6. **Reasoning models** — `deepseek-v4-flash` is a reasoning model; the agent handles both `content` and `reasoning_content` response fields

---

## 🧑‍💻 From Your Terminal

### WSL (Linux on Windows)
```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python src/main.py
```

### Windows CMD
```cmd
cd /d "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python src\main.py
```

### Windows PowerShell
```powershell
cd "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python src\main.py
```

### Double-Click
Just open the folder and double-click `run.bat` — it launches the pipeline automatically.

---

*Built with Hermes Agent + OpenCode Go. Paper trading first, testnet second, mainnet never until proven safe.*
