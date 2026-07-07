# AGENTS.md — nabu-trader

> **An AI agent's guide to this project.** Read this first before making any changes.

## 📌 Project Identity

**What:** Real-time Telegram signal listener → LLM-powered analysis → automated Binance Futures trading pipeline.  
**Owner:** Hernanda (YOUR_EMAIL@gmail.com)  
**Channel:** @YOUR_SIGNAL_CHANNEL (*UNKNOWN TRADERS ACADEMY*)  
**Exchange:** Binance USDⓈ-M Futures (mainnet)  
**LLM:** OpenCode Go / deepseek-v4-flash  
**Deployed:** Fly.io (Singapore, `sin`)  
**Status:** 🚀 YOLO auto-trade ($5 max pos, 10% risk, 30% daily loss limit)

---

## 🚀 Quick Start for AI Agents

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/nabu-trader.git
cd nabu-trader

# 2. Install deps
pip install -r requirements.txt

# 3. Copy env + configure
cp .env.example .env
# Edit .env with your Telegram API credentials, bot token, etc.

# 4. Run (paper mode for testing)
# Edit config.yaml: exchange.active: paper, agent.auto_trade: false
python src/main.py
```

---

## 🏗 Architecture

### Pipeline (one signal = one pass through all stages)

```
@YOUR_SIGNAL_CHANNEL ──► SignalListener ──► Orchestrator ──► Exchange
                      │                    │              │
                 Telethon          1. Regex parse       Binance
                 user session      2. Gate 1 (pre-LLM)  Futures API
                      │            3. Fetch balance     (fapi.binance.com)
                      │            4. LLM decide        httpx.AsyncClient
                      │            5. Gate 2 (clamp)
                      ▼            6. Execute order     ▼
                 Telegram Bot       7. Notify         SQLite DB
                 (notifier)                              │
                                                  /data/data/trades.db
```

### Directory Layout

```
src/
├── main.py                      # Entry point — wires everything
├── orchestrator.py              # Pipeline coordinator
├── listener.py                  # Telethon Telegram listener
├── agent/
│   ├── agent.py                 # LLM brain (OpenCode Go)
│   ├── gate.py                  # Safety Gate 1 + Gate 2
│   └── parser.py                # Regex signal parser
├── exchange/
│   ├── base.py                  # ABC (abstract interface)
│   ├── binance.py               # Real Binance REST API
│   └── paper.py                 # Simulated paper trading
├── execution/
│   ├── order_service.py         # Translate decision → orders
│   └── position_manager.py      # Background position monitor
├── notifier/
│   └── telegram.py              # Telegram Bot API notifications
├── state/
│   ├── database.py              # SQLite setup + schema
│   └── repositories.py          # DAO layer per entity
├── domain/
│   └── models.py                # Frozen dataclasses + Position
├── events/
│   └── bus.py                   # In-process pub/sub
└── config/
    ├── loader.py                # YAML + .env merge
    └── validator.py             # Fail-fast startup validation
```

---

## 📐 Code Conventions (Agents MUST Follow)

### Async Everywhere
- **All** I/O is `async def` — never `httpx.Client` (sync), always `httpx.AsyncClient`
- `await` every HTTP call, DB call, and exchange method
- Never block the event loop with sync requests

### Domain Models
- All inter-module data uses frozen dataclasses (`@dataclass(frozen=True)`)
- `TradeSignal` = raw parsed signal (immutable)
- `TradeDecision` = LLM output (immutable, includes leverage)
- `Position` = mutable (state changes over time)
- `ExecutionResult`, `OrderInfo`, `BalanceInfo` = exchange responses

### Error Handling
- **Binance 401/403** → `BinanceErrorCategory.AUTH` — fail-fast, don't retry
- **Binance 429** → `RATE_LIMIT` — retryable with backoff
- **Binance 5xx** → `SERVER` — retryable
- **Network errors** → `NETWORK` — return `PENDING` status (not FAILED)
- **Order failures** → return `OrderInfo(status="FAILED", error="[category] message")`

### Datetime Standard
- **Always** `datetime.now(timezone.utc)` — NEVER `datetime.utcnow()` (deprecated)
- All model default factories use `lambda: datetime.now(timezone.utc)`
- SQLite stores UTC via `CURRENT_TIMESTAMP`

### Gate Safety Philosophy
- **Gate 1** (pre-LLM): cheap checks that save LLM token cost — idempotency, whitelist, cooldown
- **Gate 2** (post-LLM): hard clamps the LLM cannot override — position size, leverage, daily loss
- Gate 2 runs ONLY for `ENTER` decisions; SKIP/CLOSE exit before Gate 2
- When adding a new safety rule, ALWAYS put it in the correct gate

### LLM Interaction
- Single call per signal — no multi-turn conversations
- Prompt includes: raw text, regex pre-parse, open positions, account balance
- `response_format: {"type": "json_object"}` enforces structured output
- Response parsed by `_parse_decision()` with 3 fallback strategies (direct JSON → code-fence → regex)
- Dry-run: `agent.auto_trade: false` logs decisions without executing

### Repository Pattern
- Business logic NEVER touches SQL directly
- Each entity has its own repository class
- All repositories accept an optional `sqlite3.Connection` in constructor
- One connection per process (created in `main.py`, passed to all repos)

---

## 🐛 Known Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| **Cross-platform Fly auth** | `flyctl status` returns "Could not find App" | Use Windows `flyctl` — WSL token doesn't have access |
| **Edited messages** | Duplicate signals from same message_id | Fixed: `get_by_message_id()` in Gate1 blocks edits |
| **LLM returns text instead of JSON** | "LLM parse error, skipping" | Fixed: `response_format: json_object` in API call |
| **Position manager stale orders** | SL/TP not placed | Check exchange balance + open orders manually |
| **Binance 401 on order** | "code:-2015 Invalid API-key" | API key missing Futures permission — check Binance API mgmt |
| **1000x symbol mapping** | `-1121 Invalid symbol` for BONKUSDT, PEPEUSDT, SHIBUSDT | Mapped automatically via `SYMBOL_MAP` in `binance.py` |
| **1000x quantity precision** | `-1111 Precision over maximum` | Auto-rounded to integer via fallback in `_place_order()` |
| **1000x STOP orders blocked** | `-4120 Use Algo Order API` | Binance limitation — SL handled by position manager monitoring (1m klines polling) |
| **1000x LIMIT SL fills instantly** | Position closed immediately | LIMIT SELL below market is NOT a valid SL — use position manager monitoring |
| **Windows/WSL out of sync** | Deploy pushes old code | Always `cp` changed files to Windows path before `flyctl deploy` |

### ⚠️ 1000x Contract Compatibility

Binance lists some low-price tokens (BONK, PEPE, SHIB, FLOKI) as "1000x" contracts on USDⓈ-M Futures. These have critical differences:

**Current mapped symbols:**
| User Symbol | Exchange Symbol | Base Asset |
|---|---|---|
| `BONKUSDT` | `1000BONKUSDT` | 1000BONK (price/tick in BONK scale) |
| `PEPEUSDT` | `1000PEPEUSDT` | 1000PEPE |
| `SHIBUSDT` | `1000SHIBUSDT` | 1000SHIB |
| `FLOKIUSDT` | `1000FLOKIUSDT` | 1000FLOKI |

**Semantics:**
- **Price:** Quoted in base asset price (e.g., 1 BONK = 0.0044 USDT). NOT multiplied by 1000.
- **Quantity:** In base asset tokens (7309 BONK). NOT divided by 1000.
- **LOT_SIZE:** `stepSize=1`, `minQty=1` (integer quantity only)
- **MIN_NOTIONAL:** `5 USDT` (enforced as `qty × price`)

**SL/TP Limitations (API):**
- `LIMIT SELL` ABOVE market → ✅ Take Profit (resting order, waits for price rise)
- `LIMIT SELL` BELOW market → ❌ NOT a valid Stop Loss (fills instantly as cheap ask)
- `STOP_MARKET`, `STOP`, `TAKE_PROFIT_MARKET`, `TAKE_PROFIT` → ❌ ALL blocked (-4120)
- **SL must be handled via position-manager monitoring** (polls 1m klines, closes via MARKET SELL on breach)

---

## 🔧 Agentic Workflows

### 1. Health Check
```bash
# From WSL (Windows Fly CLI bridge):
powershell.exe -NoProfile -Command "& flyctl status --app nabu-trader"
powershell.exe -NoProfile -Command "& flyctl logs --app nabu-trader --no-tail"

# Key things to check:
# - Machine state == "started"
# - Latest logs show "Listening for new messages..."
# - No 401 errors from Binance
# - No "LLM returned invalid JSON" warnings
```

### 2. Deploy New Code

**⚠️ CRITICAL: Windows/WSL sync.** The Fly.io deploy MUST run from the Windows path. Always:
1. Make changes in WSL (`/home/it26/learnornoearner-listener/`)
2. Copy changed files to Windows: `cp src/exchange/binance.py "/mnt/c/Working Folder/Research/learnornoearner-listener/src/exchange/binance.py"`
3. Commit and deploy from Windows:

```bash
# From WSL — copy changed files
cp -u /home/it26/nabu-trader/src/*.py "/mnt/c/Working Folder/Research/learnornoearner-listener/src/"
# Or for specific file:
cp /home/it26/nabu-trader/src/exchange/binance.py "/mnt/c/Working Folder/Research/learnornoearner-listener/src/exchange/binance.py"

# Then deploy from Windows
cd /mnt/c/"Working Folder/Research/learnornoearner-listener"
git add -A && git commit -m "description"
powershell.exe -NoProfile -Command "flyctl deploy --app learnornoearner-listener --detach --image-label v<NEXT>"
```

### 3. Add a New Signal Format
1. Edit `src/agent/parser.py` — add regex pattern + test
2. The parser returns `TradeSignal(pair, direction, entry_price, sl_price, tp_prices)`

### 4. Add a New Exchange
1. Create `src/exchange/<name>.py` implementing `Exchange` ABC
2. Add config key in `config.yaml` → `exchange.active`
3. Add import + instantiation in `src/main.py`

### 5. Debug a Failed Trade
1. Check Fly logs for the error category (`[auth]`, `[rate]`, `[server]`, `[network]`)
2. Auth errors → regenerate Binance API key
3. Network errors → usually transient, check again
4. Gate rejections → check config limits vs account balance

### 6. Set Binance API Key
```bash
# From Windows PowerShell:
flyctl secrets set BINANCE_API_KEY=xxx BINANCE_API_SECRET=xxx

# Verify:
flyctl secrets list --app nabu-trader
```

### 7. Check Binance Futures Balance
```bash
powershell.exe -NoProfile -Command "& flyctl ssh console --app nabu-trader -C 'python -c \"import os; os.chdir(\"/app\"); from src.exchange.binance import BinanceExchange; import asyncio; e=BinanceExchange(os.environ[\"BINANCE_API_KEY\"], os.environ[\"BINANCE_API_SECRET\"], futures=True, testnet=False); b=asyncio.run(e.get_balance()); print(f\"Free: \${b.free_usdt}, Total: \${b.total_usdt}\")\"'"
```

---

## 📋 Configuration Reference

See [`config.yaml`](config.yaml) for all settings.

| Key | Default | Description |
|-----|---------|-------------|
| `exchange.active` | `paper` | `paper`, `binance`, or `binance_testnet` |
| `exchange.binance.futures` | `true` | Use USDⓈ-M Futures |
| `risk.max_position_size_usdt` | `5` | Hard cap per position |
| `risk.risk_per_trade_percent` | `10` | % of balance to risk per trade |
| `risk.max_concurrent_positions` | `2` | Max open positions at once |
| `risk.daily_loss_limit_percent` | `30` | Stop trading if daily loss exceeds this |
| `risk.max_leverage` | `20` | Cap for dynamic leverage |
| `agent.auto_trade` | `true` | YOLO mode — enable real trading |
| `agent.llm.model` | `deepseek-v4-flash` | LLM model via OpenCode Go |
| `monitoring.check_interval_seconds` | `10` | Position manager loop interval |

---

## 📦 Deployment (Fly.io)

### Architecture
- **Region:** Singapore (`sin`)
- **Machine:** 1x shared-cpu-1x, 256MB RAM
- **Volume:** `/data` (persistent — survives restarts)
- **Port:** 9090 (health check only — app is Telegram-based, not HTTP)

### Secrets (set via `flyctl secrets set`)
```
TG_API_ID, TG_API_HASH, TELEGRAM_BOT_TOKEN, NOTIFY_CHAT_ID
BINANCE_API_KEY, BINANCE_API_SECRET, OPENCODE_GO_API_KEY
CHANNEL_USERNAME
```

### Persistent Data
| Path | Contents |
|------|----------|
| `/data/data/trades.db` | SQLite (signals, decisions, orders, positions) |
| `/data/sessions/nabu.session` | Telethon session (auth persistence) |
| `/data/logs/trading.log` | Debug logs |

### ⚠️ Cross-Platform Auth
The app is deployed via **Windows Fly CLI** (`YOUR_HOME\.fly\bin\flyctl.exe`).  
The WSL Fly token (`b12017ec-...`) does **NOT** have access to this app.  
Always use PowerShell bridge from WSL:
```bash
powershell.exe -NoProfile -Command "& flyctl <command> --app nabu-trader"
```

---

## 🧪 Testing

```bash
# Manual LLM test (dry-run)
cd /mnt/c/"Working Folder/Research/nabu-trader"
python -c "
import sys; sys.path.insert(0, '.')
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

---

## 🔗 Quick Links

| File | Purpose |
|------|---------|
| [`README.md`](README.md) | Project overview, features, quick start |
| [`config.yaml`](config.yaml) | All configuration |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Full architecture + data flow |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Fly.io + local deployment |
| [`docs/RISK_MANAGEMENT.md`](docs/RISK_MANAGEMENT.md) | Safety gates + risk rules |
| [`docs/SIGNAL_PARSING.md`](docs/SIGNAL_PARSING.md) | Regex patterns for signal formats |
| [`docs/Fly-health-check.md`](docs/Fly-health-check.md) | Quick health check reference |
| [`PLAN.md`](PLAN.md) | Original design plan |
| [`PLAN_REVISE.md`](PLAN_REVISE.md) | Revised plan |
