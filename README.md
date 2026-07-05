# Nabu Trader Signal Listener — Auto-Trade Pipeline

Real-time Telegram channel monitor → LLM-powered signal analysis → **Automated Binance Futures trading**.

> **Branch:** `feature/auto-trade`  
> **Channel:** `@YOUR_SIGNAL_CHANNEL`  
> **Exchange:** Binance USDⓈ-M Futures (dynamic leverage)  
> **Brain:** OpenCode Go / deepseek-v4-flash  
> **Safety:** Hard-coded risk gates (LLM cannot override)

---

> **🤖 AI Agent?** Read [`AGENTS.md`](AGENTS.md) first — it has everything you need to understand, maintain, and deploy this project.

---

## What It Does

```
@YOUR_SIGNAL_CHANNEL Telegram Channel
         │
         ▼
 ┌──────────────────┐
 │  Regex Pre-Parse │  1ms — extracts pair, direction, prices
 └──────┬───────────┘
        │
 ┌──────▼───────────┐
 │  Safety Gate 1   │  Idempotency, cooldown, whitelist
 └──────┬───────────┘
        │
 ┌──────▼───────────┐
 │  Agent Brain     │  1 LLM call via OpenCode Go
 │  (LLM)           │  Parses, validates, risk-assesses
 └──────┬───────────┘
        │
 ┌──────▼───────────┐
 │  Safety Gate 2   │  Position clamp, min notional, leverage calc
 └──────┬───────────┘
        │
 ┌──────▼───────────┐
 │  Order Service   │  → Binance Futures (or Paper / Testnet)
 └──────┬───────────┘
        │
 ┌──────▼───────────┐
 │  Position Mgr    │  Background monitor: SL/TP, auto-close
 └──────┬───────────┘
        │
        ▼
 Telegram Notification
```

---

## Quick Start

### 1. Configure `.env`

```ini
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH
CHANNEL_USERNAME=YOUR_SIGNAL_CHANNEL
NOTIFY_CHAT_ID=YOUR_CHAT_ID
TELEGRAM_BOT_TOKEN=REMOVED_SECRET
OPENCODE_GO_API_KEY=sk-...
```

### 2. Authenticate Telegram (one-time)

```bash
/home/it26/.hermes/venvs/netra/bin/python auth.py +6281212345678
```

### 3. Run

```bash
/home/it26/.hermes/venvs/netra/bin/python src/main.py
```

See **[docs/SETUP.md](docs/SETUP.md)** for full step-by-step.

---

## Quick Config Reference

| Key | Current | Description |
|-----|---------|-------------|
| `exchange.active` | `binance` | `paper` / `binance_testnet` / `binance` |
| `exchange.binance.futures` | `true` | USDⓈ-M Futures (not Spot) |
| `risk.risk_per_trade_percent` | `10.0` | Risk 10% of balance per trade |
| `risk.max_position_size_usdt` | `5` | Hard cap on position value |
| `risk.max_leverage` | `20` | Dynamic leverage ceiling |
| `risk.margin_usage_pct` | `50` | Use ≤50% of balance as margin |
| `agent.auto_trade` | `true` | Live trading enabled |
| `agent.confidence_threshold` | `0.0` | YOLO mode — trust all signals |

Full reference: **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**

---

## Documentation

| Doc | Description |
|-----|-------------|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Full system architecture & data flow |
| **[docs/SETUP.md](docs/SETUP.md)** | Step-by-step setup guide |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | All config keys explained |
| **[docs/RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md)** | Safety gates & risk controls |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Fly.io & Docker deployment |
| **[docs/SIGNAL_PARSING.md](docs/SIGNAL_PARSING.md)** | Regex & LLM signal analysis |
| **[docs/API.md](docs/API.md)** | Exchange adapter API reference |

---

## Safety Architecture

Multiple non-negotiable layers prevent the LLM from causing losses:

| Layer | What it prevents |
|-------|-----------------|
| **Gate 1** | Duplicate signals, cooldown violations, off-whitelist pairs |
| **Gate 2** | Position > $5, >2 concurrent, >30% daily loss, SL on wrong side |
| **Position Manager** | Auto-closes positions after 48h, reconciles filled SL/TP |
| **Exchange Layer** | Min notional, leverage cap, isolated margin |

Full details: **[docs/RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md)**

---

## Project Structure

```
nabu-trader/
├── src/
│   ├── agent/
│   │   ├── parser.py          Regex pre-parse (~1ms)
│   │   ├── agent.py           LLM brain (OpenCode Go)
│   │   └── gate.py            Safety Gates 1 & 2
│   ├── exchange/
│   │   ├── base.py            Abstract exchange interface
│   │   ├── paper.py           Paper trading simulator
│   │   └── binance.py         Binance Futures/Spot API
│   ├── execution/
│   │   ├── order_service.py   Decision → exchange + SL/TP
│   │   └── position_manager.py Background monitor
│   ├── state/
│   │   ├── database.py        SQLite (10 tables, WAL mode)
│   │   └── repositories.py    Repository pattern CRUD
│   ├── domain/models.py       Typed dataclasses
│   ├── events/bus.py          In-process pub/sub
│   ├── notifier/telegram.py   Bot API notifications
│   ├── config/loader.py       Config.yaml + .env merger
│   ├── listener.py            Telethon listener
│   ├── orchestrator.py        Pipeline coordinator
│   └── main.py                Entry point
├── docs/                      Comprehensive documentation
├── config.yaml                Trading configuration
├── Dockerfile                 Fly.io deployment
├── fly.toml                   Fly.io config
└── .env                       Secrets (never commit)
```

---

## Testing

### Dry-Run Mode

```yaml
# config.yaml
agent:
  auto_trade: false        # analyzes but doesn't execute
  confidence_threshold: 0.0
```

The pipeline runs fully — signals are parsed, LLM makes decisions, Gate 2 clamps — but no orders are placed. Perfect for verification.

### Paper Trading

```yaml
exchange:
  active: paper              # simulated fills, fake money
agent:
  auto_trade: true
```

### Manual LLM Test

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python -c "
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

## Important Notes

1. **Futures, not Spot** — This system trades Binance USDⓈ-M Futures. Ensure your API key has Futures permissions enabled.
2. **API Key Security** — Never commit `.env`. Use `fly secrets set` or Docker env vars in production.
3. **Session File** — The Telethon session (`sessions/nabu.session`) is essential. Back it up.
4. **Start Small** — With $10.58 balance, max $5 positions, and dynamic leverage, the system is designed for safety.
5. **LLM Costs** — OpenCode Go charges per token. Each signal = ~1 LLM call. Monitor usage.

---

## 🚀 Deploy & Health (Fly.io)

> ⚠️ **Cross-platform auth**: This app is deployed via **Windows Fly CLI** (`YOUR_HOME\.fly\bin\flyctl.exe`).  
> The WSL Fly token does **NOT** have access. From WSL, always use:
> ```bash
> powershell.exe -NoProfile -Command "& flyctl <command> --app nabu-trader"
> ```

### Health Check

```powershell
# Windows PowerShell (or via WSL → powershell.exe bridge)
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail
```

| Check | WSL Command |
|-------|-------------|
| Status & machine | `powershell.exe -NoProfile -Command "& flyctl status --app nabu-trader"` |
| Live logs | `powershell.exe -NoProfile -Command "& flyctl logs --app nabu-trader"` |
| SSH console | `powershell.exe -NoProfile -Command "& flyctl ssh console --app nabu-trader"` |
| Secrets | `powershell.exe -NoProfile -Command "& flyctl secrets list --app nabu-trader"` |

### Quick Deploy

```powershell
cd C:\"Working Folder\Research\nabu-trader"
flyctl deploy --app nabu-trader
```

Full health reference: [`docs/Fly-health-check.md`](docs/Fly-health-check.md)

---

*Built with Hermes Agent + OpenCode Go. Binance USDⓈ-M Futures. Dynamic leverage. Hard safety gates.*
