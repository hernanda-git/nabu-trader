# Setup Guide

## Prerequisites

- **OS**: Windows 10/11 (or Linux/Mac — see notes)
- **Python**: 3.11+
- **Telegram account**: with access to `@YOUR_SIGNAL_CHANNEL` channel (the signal source)
- **Binance account** (optional for paper trading): with Futures permissions for API key
- **Fly.io account** (optional for cloud deployment): `flyctl` CLI

---

## Step 1: Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/nabu-trader.git
cd nabu-trader
pip install -r requirements.txt
```

**Dependencies** (`requirements.txt`):
```
telethon>=1.44.0
httpx>=0.27.0
python-dotenv>=1.0.0
pyyaml>=6.0
fastapi>=0.115.0
uvicorn>=0.32.0
```

---

## Step 2: Configure Environment

### 2.1 Create `.env`

Copy `.env.example` to `.env` and fill in your credentials:

```ini
# --- Telegram (Required) ---
TG_API_ID=YOUR_API_ID
TG_API_HASH=YOUR_API_HASH
CHANNEL_USERNAME=YOUR_SIGNAL_CHANNEL
NOTIFY_CHAT_ID=YOUR_CHAT_ID
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather

# --- LLM (Required) ---
OPENCODE_GO_API_KEY=sk-your-opencode-key

# --- Binance (Optional — skip for paper trading) ---
BINANCE_API_KEY=your_binance_api_key
BINANCE_API_SECRET=your_binance_api_secret

# --- API Bridge (Optional) ---
API_KEY=your_api_bridge_key

# --- Webhook (Optional) ---
# WEBHOOK_URL=https://your-webhook.url
# WEBHOOK_HMAC_SECRET=your-hmac-secret
```

### 2.2 Get Telegram Credentials

1. Visit **my.telegram.org/apps**
2. Login with your phone number
3. Copy **api_id** and **api_hash** to `.env`

### 2.3 Create Telegram Bot

1. Message **@BotFather** on Telegram
2. Send `/newbot` and follow prompts
3. Copy the bot token to `.env` as `TELEGRAM_BOT_TOKEN`
4. Message your new bot at least once (to initialize the chat)
5. Copy your **chat ID** to `.env` as `NOTIFY_CHAT_ID`
   - To find chat ID: message `@userinfobot` on Telegram

### 2.4 Get Binance API Key (Optional)

1. Go to **Binance.com → API Management**
2. Create new API key
3. Enable **Futures** trading permission
4. Copy key + secret to `.env`
5. **Important**: Enable IP whitelist and add Fly.io egress IPs if deploying

---

## Step 3: Telegram Authentication (One-Time)

This links the Telethon client to your Telegram account so it can read the signal channel.

```bash
python auth.py +6281XXXXXXX
```

Replace `+6281XXXXXXX` with your phone number (including country code).

**What happens:**
1. Telegram sends a 5-digit code to your Telegram app
2. Enter the code when prompted
3. If 2FA is enabled, enter your password
4. Session is saved — future runs won't need re-auth

**Troubleshooting:**
| Problem | Solution |
|---------|----------|
| Code not received | Wait 10 minutes (Telegram rate-limits) |
| "Phone number invalid" | Check country code (e.g. `+62` for Indonesia) |
| "Session banned" | Your account may be restricted — wait 24h |
| Code expired | Request a new code by re-running auth.py |

**Extract SESSION_STRING for Fly.io:**
After successful auth, run:
```python
from telethon.sessions import StringSession
from telethon import TelegramClient
import os

# Load the session file and convert to string
client = TelegramClient('sessions/nabu', TG_API_ID, TG_API_HASH)
# The StringSession can be saved to env var for Fly.io
```

---

## Step 4: Configure Trading Settings

Edit `config.yaml`:

```yaml
# Start in dry-run mode first:
agent:
  auto_trade: false      # Analyze but don't trade

# Or paper trading:
exchange:
  active: paper           # Simulated fills

# For real trading:
exchange:
  active: binance         # Real Binance Futures
agent:
  auto_trade: true
```

---

## Step 5: Run

### Local Development

```bash
# Dry-run (safe — no real orders)
python src/main.py

# Paper trading
# Edit config.yaml → exchange.active: paper
python src/main.py

# Real trading (testnet)
# Edit config.yaml → exchange.active: binance_testnet
python src/main.py

# Real trading (mainnet)
# Edit config.yaml → exchange.active: binance
python src/main.py
```

**Expected Output:**
```
============================================================
Signal Listener starting...
Channel: @YOUR_SIGNAL_CHANNEL
Auto-trade: true
============================================================
Telegram notifier ready (chat_id=YOUR_CHAT_ID)
Position manager started (interval=10s)
Connected as: Hernanda (ID: YOUR_CHAT_ID)
Channel found: UNKNOWN TRADERS ACADEMY (ID: 1252615519)
Listening for new messages... (Ctrl+C to stop)
```

### Cloud (Fly.io)

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for full Fly.io deployment guide.

---

## Step 6: Verify

1. Send `/health` to your bot on Telegram
2. You should receive a full system health report
3. Send `/balance` to check futures account
4. Wait for a signal from `@YOUR_SIGNAL_CHANNEL` — the bot will:
   - Receive the message 📡
   - Parse and decide 🧠
   - Execute or skip ✅/⏭️
   - Notify you via bot

---

## Testing

### Run Test Suite

```bash
cd nabu-trader
python -m pytest tests/ -v
```

### Manual LLM Test

```bash
python -c "
import sys; sys.path.insert(0, '.')
from src.agent.agent import AgentBrain
from src.config.loader import load_config
from src.domain.models import TradeSignal

cfg = load_config()
brain = AgentBrain(cfg)
signal = TradeSignal(message_id=1, channel='test',
    raw_text='$BONK LONG Entry: 0.003105 SL: 0.003022 TP: 0.00333')
decision = brain.decide(signal)
print(f'{decision.action} {decision.pair} conf={decision.confidence}')
"
```

---

## File Layout

```
nabu-trader/
├── .env                     ← Secrets (NEVER commit)
├── .env.example            ← Template for .env
├── config.yaml             ← Trading configuration
├── src/
│   ├── main.py             ← Entry point
│   ├── listener.py         ← Telegram listener + commands
│   ├── orchestrator.py     ← Pipeline coordinator
│   ├── agent/              ← LLM agent + safety gates
│   ├── exchange/           ← Exchange adapters
│   ├── execution/          ← Order execution + position management
│   ├── state/              ← SQLite database + repositories
│   ├── api/                ← HTTP API bridge
│   ├── notifier/           ← Telegram notifications
│   ├── health/             ← Health reporting
│   ├── events/             ← Event bus
│   └── domain/             ← Data models
├── docs/                   ← Documentation
├── tests/                  ← Pytest suite
├── Dockerfile              ← Fly.io container
├── fly.toml                ← Fly.io config
└── requirements.txt        ← Python dependencies
```

---

## Windows vs Linux Notes

| | Windows | Linux |
|---|---|---|
| **Python** | `python` | `python3` |
| **Path** | `C:\...\nabu-trader` | `/home/user/nabu-trader` |
| **Run** | `python src/main.py` | `python3 src/main.py` |
| **Deploy CLI** | `YOUR_HOME\.fly\bin\flyctl.exe` | `/usr/local/bin/flyctl` |
| **Background** | Task Scheduler / `pythonw` | `systemd` / `screen` / `tmux` |

### Recommended VPS Specs

| Resource | Minimum |
|----------|---------|
| RAM | 512 MB |
| CPU | 1 vCPU |
| Disk | 1 GB |
| OS | Ubuntu 22.04+ / Debian 12+ |

The bot is lightweight — uses ~30-50 MB RAM idle, ~100 MB during active trading.
