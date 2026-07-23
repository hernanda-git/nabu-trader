# Nabu Trader Signal Listener — Auto-Trade Pipeline

Real-time Telegram channel monitor → LLM-powered signal analysis → **Automated Binance Futures trading**.

> **Channel:** `@YOUR_SIGNAL_CHANNEL` (UNKNOWN TRADERS ACADEMY)  
> **Exchange:** Binance USDⓈ-M Futures (isolated margin, dynamic leverage)  
> **LLM:** OpenCode Go / `deepseek-v4-flash`  
> **Deploy:** Fly.io (Singapore, machine `YOUR_MACHINE_ID`)  
> **Version:** `v103` (see [CHANGELOG.md](CHANGELOG.md))

---

## Quick Overview

```
Telegram Channel (@YOUR_SIGNAL_CHANNEL)
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
│  Position Mgr    │  Background monitor: SL/TP, auto-close, Telegram notify
└──────┬───────────┘
       │
       ▼
Telegram Notification to your bot
```

---

## Quick Start

### Prerequisites
- Python 3.11+
- Telegram account with access to `@YOUR_SIGNAL_CHANNEL`
- Binance API key (Futures-enabled) or use paper trading
- Fly.io account (for deployment)

### 1. Setup

```bash
git clone https://github.com/YOUR_USERNAME/nabu-trader.git
cd nabu-trader
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys (see .env.example)
```

### 2. Telegram Auth (one-time)

```bash
python auth.py +6281XXXXXXX
# Enter the 5-digit code Telegram sends you
```

### 3. Run (dry-run first)

```yaml
# config.yaml — set auto_trade: false for dry-run
agent:
  auto_trade: false
```

```bash
python src/main.py
```

See **[docs/SETUP.md](docs/SETUP.md)** for full step-by-step.

---

## Bot Commands (Telegram)

All commands are sent as private messages to the bot. Type `/` to see the registered menu.

| Command | Description |
|---------|-------------|
| `/check <pair>` | Current price + 24h stats. e.g. `/check btc`, `/check #ena` |
| `/balance` | Futures account balance |
| `/positions` | All open futures positions + PnL |
| `/positions add [LONG\|SHORT] <pair> <margin> <lev> <price\|market> [tp] [sl]` | Open a position manually |
| `/pending` | Pending conditional signals |
| `/cancel <id>` / `/cancel all` | Cancel pending signal(s) |
| `/close <pair>` | Market-close an open position |
| `/health` | Full system health check |
| `/setport N` / `/getport` | Margin budget per trade |
| `/setleverage N` / `/leverage` | Default leverage ceiling |
| `/setmargintype isolated\|cross` | Margin mode |
| `/version` | Show bot version |
| `/db tables\|list\|get\|delete\|update\|insert` | Browse/edit DB |
| `/help` | Show available commands |

### `/positions add` Examples

```
# LONG limit at 60000 with TP/SL
/positions add btcusdt 10 20 60000 65000 58000

# Market-long ETH with TP/SL
/positions add LONG ethusdt 5 10 market 3500 3200

# Market-short SOL, no TP/SL
/positions add SHORT solusdt 2 5 market
```

---

## Safety Architecture

Multiple non-negotiable layers protect against LLM errors:

| Layer | What it prevents |
|-------|-----------------|
| **Gate 1** (pre-LLM) | Duplicate signals, cooldown violations, off-whitelist pairs |
| **Gate 2** (post-LLM) | Position > $5, >2 concurrent, >30% daily loss, SL on wrong side |
| **Position Manager** | Background monitor: SL hit, time-based auto-close (48h max), orphan detection, Telegram notification |
| **Exchange Layer** | Min notional enforcement, step size rounding, isolated margin, reduce-only protection |

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
│   │   ├── binance.py         Binance Futures/Spot API
│   │   └── symbol_registry.py Symbol resolution cache
│   ├── execution/
│   │   ├── order_service.py   Decision → exchange + SL/TP placement
│   │   └── position_manager.py Background monitor (SL/TP, time exit, Telegram notify)
│   ├── state/
│   │   ├── database.py        SQLite (15+ tables, WAL mode, auto-migration)
│   │   └── repositories.py    Repository pattern (Signal, Decision, Order, Position, etc.)
│   ├── api/
│   │   ├── server.py          FastAPI server (15+ endpoints)
│   │   ├── auth.py            API key + HMAC auth + rate limiter
│   │   └── webhook.py         Trade event webhook emitter
│   ├── domain/
│   │   └── models.py          Typed dataclasses (TradeSignal, TradeDecision, Position, etc.)
│   ├── events/
│   │   └── bus.py             In-process pub/sub event bus
│   ├── health/
│   │   └── reporter.py        Periodic health checks + Telegram reports
│   ├── notifier/
│   │   └── telegram.py        Bot API notifications (Markdown-safe)
│   ├── config/
│   │   └── loader.py          Config.yaml + .env merger
│   ├── listener.py            Telethon signal listener
│   ├── orchestrator.py        Pipeline coordinator (signal → LLM → execute)
│   ├── main.py                Entry point
│   └── version.py             Version tracking
├── docs/                      Full documentation
├── tests/                     Pytest suite
├── config.yaml                Trading configuration
├── Dockerfile                 Fly.io deployment image
├── fly.toml                   Fly.io app config
├── deploy.sh                  Deployment script
└── .env                       Secrets (never commit)
```

---

## Documentation Index

| Doc | Description |
|-----|-------------|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | Full system architecture & data flow |
| **[docs/SETUP.md](docs/SETUP.md)** | Step-by-step setup guide |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | All config keys explained |
| **[docs/RISK_MANAGEMENT.md](docs/RISK_MANAGEMENT.md)** | Safety gates & risk controls |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Fly.io & Docker deployment |
| **[docs/SIGNAL_PARSING.md](docs/SIGNAL_PARSING.md)** | Regex & LLM signal analysis |
| **[docs/API.md](docs/API.md)** | Exchange adapter API reference |
| **[docs/FLY_OPERATIONS.md](docs/FLY_OPERATIONS.md)** | Fly.io operations, health checks, logs, SSH |
| **[CHANGELOG.md](CHANGELOG.md)** | Version history |
| **[AGENTS.md](AGENTS.md)** | AI agent maintenance guide |

---

## Testing

### Dry-Run Mode
```yaml
# config.yaml
agent:
  auto_trade: false        # analyzes but doesn't execute
```

### Paper Trading
```yaml
exchange:
  active: paper              # simulated fills, fake money
agent:
  auto_trade: true
```

### Run Tests
```bash
cd nabu-trader
python -m pytest tests/ -v
```

---

## Important Notes

1. **Futures, not Spot** — trades Binance USDⓈ-M Futures. API key needs Futures permissions.
2. **API Key Security** — Never commit `.env`. Use `fly secrets set` in production.
3. **Session File** — The Telethon session is essential. Back it up.
4. **Start Small** — Default max position is $5, dynamic leverage.
5. **LLM Costs** — OpenCode Go charges per token. Each signal ≈ 1 LLM call.

---

## API Bridge

The bot exposes a secure HTTP API on port 9090 for Hermes or external tools:

```bash
# Stats dashboard
curl -s -H "X-API-Key: <key>" \
  https://nabu-trader.fly.dev/api/v1/stats

# Full trade trace (LLM interactions, position events, logs)
curl -s -H "X-API-Key: <key>" \
  https://nabu-trader.fly.dev/api/v1/trades/1

# Pipeline trace by correlation ID
curl -s -H "X-API-Key: <key>" \
  https://nabu-trader.fly.dev/api/v1/logs/<correlation_id>
```

**Security**: API key auth (`X-API-Key`), HMAC-SHA256 for writes, rate limited (30 req/min/IP).

Full docs: **[docs/API.md](docs/API.md)**

---

*Built with Hermes Agent + OpenCode Go. Binance USDⓈ-M Futures. Dynamic leverage. Hard safety gates.*
