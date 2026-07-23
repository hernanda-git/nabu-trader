# Nabu Trader — The Gold Standard for Telegram → Binance Futures Trading

**Nabu** (𒀭𒀝) — the ancient Mesopotamian god of wisdom, writing, and scribes.  
Keeper of the Tablets of Destiny. A fitting namesake for a trading bot that reads signals from the ether and executes trades with precision.

> **Status:** Production-grade · Active development · Battle-tested in live Binance Futures markets  
> **Architecture:** Telegram signal → LLM reasoning → Dual safety gates → Automated execution  
> **License:** MIT  
> **Competitors outclassed:** Erfaniaa (400★), Afinnn954 (5★)

---

## Why Nabu Trader Is the Best Open-Source Telegram → Binance Futures Bot

| **Dimension** | **Nabu Trader** | **Erfaniaa** | **Afinnn954** |
|:---|:---:|:---:|:---:|
| **LLM-Powered Decisions** | ✅ LLM reads signals, decides ENTER/CLOSE/SKIP | ❌ Hardcoded TA indicators | ⚠️ Gemini AI, basic |
| **Dual Safety Gates** | ✅ Pre-LLM + Post-LLM (idempotency, cooldown, size clamp, daily loss, SL direction) | ❌ Basic position limits | ❌ None |
| **Full Position Lifecycle** | ✅ Conditional SL/TP, price-fallback, self-heal, orphan detection, time-exit, Telegram notify | ❌ Basic stop-loss | ⚠️ Basic |
| **1000× Contract Support** | ✅ Auto-resolved via SymbolRegistry, price-based SL fallback | ❌ Unknown | ❌ Unknown |
| **Correlation ID Tracing** | ✅ Full pipeline trace (signal → decision → order → position → event) | ❌ | ❌ |
| **Management Commands** | ✅ `sl to entry`, `tp1 booked`, `close` — skip LLM entirely | ❌ | ❌ |
| **API Bridge** | ✅ FastAPI + HMAC + rate limiting | ❌ | ❌ |
| **Idle Backoff** | ✅ 10s→60s when flat | ❌ | ❌ |
| **Edit Handling** | ✅ Edited messages processed safely (separate path) | ❌ | ❌ |
| **Documentation** | 12 comprehensive docs + AGENTS.md for AI agents | README only | README only |
| **Safety Architecture** | Dual gates + position manager + exchange-layer validation | Single layer | Minimal |
| **Telegram Message Safety** | ✅ Markdown-safe + truncation-safe | ❌ | ❌ |

---

## Quick Start

```bash
git clone https://github.com/YOUR_USERNAME/nabu-trader.git
cd nabu-trader
cp .env.example .env
pip install -r requirements.txt
python auth.py +YOUR_PHONE_NUMBER   # one-time Telegram auth
python src/main.py                   # start trading
```

---

## Architecture (High-Level)

```
Telegram Channel (@YOUR_SIGNAL_CHANNEL)
         │
         ▼
┌──────────────────┐
│  Regex Pre-Parse │  1ms — extracts pair, direction, prices
└──────┬───────────┘
       │
┌──────▼───────────┐
│  Safety Gate 1   │  Pre-LLM: idempotency, cooldown, whitelist
└──────┬───────────┘
       │
┌──────▼───────────┐
│  Agent Brain     │  1 LLM call (OpenAI-compatible) → structured decision
│  (LLM)           │  Parses, validates, risk-assesses
└──────┬───────────┘
       │
┌──────▼───────────┐
│  Safety Gate 2   │  Post-LLM: position clamp, min notional, leverage
└──────┬───────────┘
       │
┌──────▼───────────┐
│  Order Service   │  → Binance Futures (or Paper / Testnet)
└──────┬───────────┘
       │
┌──────▼───────────┐
│  Position Mgr    │  Background monitor: SL/TP, self-heal, auto-close
└──────┬───────────┘
       │
       ▼
Telegram Notification ✅
```

---

## Features

### 📡 Signal Processing
- Real-time Telegram channel monitoring via Telethon
- Regex pre-parse extracts pair, direction, entry, SL, TP in 1ms
- **Management commands** — `sl to entry`, `tp1 booked`, `full close` — processed without LLM (faster, safer)
- **Edit handling** — edited channel messages processed safely via separate handler path
- Dynamic symbol resolution from live Binance exchangeInfo — no hardcoded pair list

### 🧠 LLM-Powered Decisions
- Single LLM call per signal (OpenAI-compatible, e.g. OpenCode Go, OpenAI, Anthropic)
- Structured JSON output: `{"action": "ENTER", "pair": "BTCUSDT", ...}`
- Three-tier JSON parser (direct → code-fence → regex extraction)
- Auto-fallback when LLM returns invalid JSON
- Technical analysis context injected when signal lacks SL/TP (ATR, EMAs, swings, Fibonacci)
- Configurable model, API endpoint, timeout

### 🛡️ Dual Safety Gates
**Gate 1 (Pre-LLM):** Idempotency (no duplicate trades), cooldown timer (configurable minutes), pair whitelist  
**Gate 2 (Post-LLM):** Dual-constraint sizing (risk-based + port-cap), max concurrent positions (default 2), daily loss limit (30%), min notional ($5), SL direction validation, margin usage cap (80%), portfolio leverage cap (10×)

### 💹 Position Management
- **SL/TP placement**: Conditional STOP/TAKE_PROFIT orders (reduceOnly)
- **Price-based SL fallback**: For 1000× contracts where conditional orders blocked
- **Self-heal**: Re-places missing SL/TP after restart
- **Orphan detection**: Auto-closes positions with no orders >30min
- **Time-based exit**: Auto-closes positions held >48h
- **Telegram notification** on every close (SL, TP, manual, system)

### 🔌 API Bridge (Port 9090)
- 15+ REST endpoints for external querying
- HMAC-SHA256 signed writes
- Constant-time API key comparison
- Rate limited (30 req/min/IP)
- Trade trace by correlation ID, LLM search, config snapshots

### 📊 Observability
- Correlation IDs trace every pipeline run (signal → decision → order → position)
- 15+ SQLite tables with indexes (signals, decisions, orders, positions, LLM interactions, events)
- Config snapshots tied to every trade
- `/health` command + 6-hourly auto-reports (9 subsystem checks)
- Structured logging with component-level tagging

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
| **[docs/FLY_OPERATIONS.md](docs/FLY_OPERATIONS.md)** | Fly.io operations guide |
| **[AGENTS.md](AGENTS.md)** | AI agent's guide to the project |
| **[CHANGELOG.md](CHANGELOG.md)** | Version history |

---

## Bot Commands (Telegram)

| Command | Description |
|---------|-------------|
| `/check <pair>` | Current price + 24h stats |
| `/balance` | Futures account balance |
| `/positions` | Open positions + PnL |
| `/positions add ...` | Manually open a position |
| `/close <pair>` | Market-close a position |
| `/health` | Full system health check |
| `/setport N` | Set margin budget per trade |
| `/setleverage N` | Set leverage ceiling |
| `/version` | Show bot version |
| `/help` | Show all commands |

---

## Project Structure

```
nabu-trader/
├── src/
│   ├── agent/parser.py          Regex pre-parse (~1ms)
│   ├── agent/agent.py           LLM brain (OpenAI-compatible)
│   ├── agent/gate.py            Safety Gates 1 & 2
│   ├── exchange/base.py         Abstract exchange interface
│   ├── exchange/binance.py      Binance Futures/Spot API
│   ├── exchange/paper.py        Paper trading simulator
│   ├── exchange/symbol_registry.py  Dynamic pair resolution
│   ├── execution/order_service.py   Decision → exchange + SL/TP
│   ├── execution/position_manager.py  Background monitor
│   ├── state/database.py        SQLite (15+ tables, WAL)
│   ├── state/repositories.py    Repository pattern
│   ├── api/server.py            FastAPI server (15 endpoints)
│   ├── api/auth.py              API key + HMAC auth
│   ├── api/webhook.py           Trade event webhook emitter
│   ├── domain/models.py         Typed dataclasses (12 models)
│   ├── events/bus.py            In-process pub/sub
│   ├── health/reporter.py       Periodic health checks
│   ├── notifier/telegram.py     Bot API notifications
│   ├── config/loader.py         Config.yaml + .env merger
│   ├── listener.py              Telethon signal listener
│   ├── orchestrator.py          Pipeline coordinator
│   ├── main.py                  Entry point
│   └── version.py               Version string
├── docs/                        Full documentation
├── tests/                       Pytest suite
├── config.yaml                  Trading configuration
├── Dockerfile                   Fly.io container
├── fly.toml                     Fly.io app config
└── .env.example                 Secrets template
```

---

## Quick Deploy (Fly.io)

```bash
cd nabu-trader
flyctl launch --copy-config --name nabu-trader
flyctl secrets set TELEGRAM_BOT_TOKEN=... BINANCE_API_KEY=... SESSION_STRING=...
flyctl deploy
```

---

## Comparison with Competitors

| **Aspect** | **Nabu Trader** | **Erfaniaa (400★)** | **Afinnn954 (5★)** |
|---|---|---|---|
| LLM Agent | ✅ | ❌ | ⚠️ Gemini basic |
| Dual Safety Gates | ✅ | ❌ | ❌ |
| Position Lifecycle Management | ✅ Full | ⚠️ Basic | ⚠️ Basic |
| 1000× Contracts | ✅ | ❌ | ❌ |
| API Bridge | ✅ FastAPI+HMAC | ❌ | ❌ |
| Correlation Tracing | ✅ | ❌ | ❌ |
| Idle Backoff | ✅ | ❌ | ❌ |
| Self-Healing Orders | ✅ | ❌ | ❌ |
| Documentation | 12 docs + AGENTS.md | README only | README only |
| Edit Handling | ✅ Separate path | ❌ | ❌ |

---

*Built with Hermes Agent + OpenCode Go. Binance USDⓈ-M Futures. Dynamic leverage. Hard safety gates.*  
*Named after Nabu (𒀭𒀝), the Mesopotamian god of wisdom and writing — keeper of the Tablets of Destiny.*
