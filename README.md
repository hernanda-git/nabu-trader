# Nabu Trader Signal Listener — Auto-Trade Pipeline

Real-time Telegram channel monitor → LLM-powered signal analysis → **Automated Binance Futures trading**.

> **Branch:** `feature/auto-trade`  
> **Channel:** `@YOUR_SIGNAL_CHANNEL`  
> **Exchange:** Binance USDⓈ-M Futures (dynamic leverage)  
| **LLM** | OpenCode Go / deepseek-v4-flash |
| **Symbol Registry** | Dynamic via Binance Futures exchangeInfo (auto-refreshed every 15m) |
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
│   │   ├── database.py        SQLite (14 tables, WAL mode, auto-migration)
│   │   └── repositories.py    Repository pattern (11 repos)
│   ├── api/
│   │   ├── __init__.py        API package
│   │   ├── auth.py            API key + HMAC auth + rate limiter
│   │   ├── server.py          FastAPI server (15 endpoints)
│   │   └── webhook.py         Trade event webhook emitter
│   ├── domain/models.py       Typed dataclasses (12 models)
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

## 🔌 API Bridge

The bot exposes a secure HTTP API on port 9090 for Hermes to query trades, LLM interactions, position events, and more.

```bash
# Stats dashboard
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/stats

# Full trade trace (includes LLM interaction, position events, trade logs)
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/trades/1

# Search LLM decisions
curl -s -H "X-API-Key: $API_KEY" \
  "https://nabu-trader.fly.dev/api/v1/llm/search?q=SKIP"

# Pipeline trace by correlation ID
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/logs/abc123def456
```

**Security**: API key auth (`X-API-Key` header), HMAC-SHA256 signing for write ops, rate limiting (30 req/min/IP).

**Setup**: `flyctl secrets set API_KEY=<openssl rand -hex 32> --app nabu-trader`

Full docs: [`src/api/server.py`](src/api/server.py), [`fly-trade-bridge` skill](https://hermes-agent.nousresearch.com/skills/fly-trade-bridge)

---

## 🚀 Deploy & Health (Fly.io)

> ⚠️ **Cross-platform auth**: This app is deployed via the **Windows Fly CLI**
> binary at `YOUR_HOME\.fly\bin\flyctl.exe`. Add it to PATH in git-bash/MSYS:
> `export PATH="$PATH:/c/Users/it26/.fly/bin"` — then run `flyctl` directly. Do
> **not** shell out through `powershell.exe` (nested quoting breaks `ssh console`
> heredocs). The `flyctl` binary (not the WSL `fly` token) owns this app.

### Health Check

```bash
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail
```

| Check | Command (git-bash) |
|-------|-------------|
| Status & machine | `flyctl status --app nabu-trader` |
| Live logs | `flyctl logs --app nabu-trader --no-tail` |
| SSH console | `flyctl ssh console --app nabu-trader` |
| Secrets | `flyctl secrets list --app nabu-trader` |

### Quick Deploy

```powershell
cd C:\"Working Folder\Research\nabu-trader"
flyctl deploy --app nabu-trader
```

Full health reference: [`docs/Fly-health-check.md`](docs/Fly-health-check.md)

---

*Built with Hermes Agent + OpenCode Go. Binance USDⓈ-M Futures. Dynamic leverage. Hard safety gates.*
