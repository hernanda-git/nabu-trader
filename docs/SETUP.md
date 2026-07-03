# Setup Guide

## Prerequisites

| Requirement | Details |
|-------------|---------|
| **Python** | 3.11+ (tested on 3.11/3.12) |
| **Telegram account** | For the Telethon listener (user session, not a bot) |
| **OpenCode Go key** | LLM brain API (or any OpenAI-compatible endpoint) |
| **Binance account** | Optional — paper trading works without |
| **WSL / Linux / macOS** | for running the listener |

## 1. Clone & Install

```bash
git clone <repo-url> nabu-trader
cd nabu-trader

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### From WSL (Windows)

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/pip install -r requirements.txt
```

## 2. Configure Environment

Create `.env` in the project root:

```ini
# ─── Telegram API credentials (from https://my.telegram.org) ───
TG_API_ID=12345678
TG_API_HASH=your_api_hash_here

# ─── Channel to monitor ───
CHANNEL_USERNAME=YOUR_SIGNAL_CHANNEL

# ─── Your Telegram chat ID for notifications (find via @userinfobot) ───
NOTIFY_CHAT_ID=YOUR_CHAT_ID

# ─── Telegram Bot Token (from @BotFather) ───
TELEGRAM_BOT_TOKEN=REMOVED_SECRET

# ─── Binance API keys (from https://binance.com/en/my/settings/api) ───
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# ─── OpenCode Go API Key (for LLM brain) ───
OPENCODE_GO_API_KEY=sk-opencode-go-key-here
```

### Getting Telegram API Credentials
1. Go to https://my.telegram.org
2. Log in with your phone number
3. Go to **API Development Tools**
4. Create a new application → copy `api_id` and `api_hash`

### Getting a Bot Token
1. Open Telegram, search for [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow instructions
3. Copy the token (format: `123456:ABC-DEF...`)

### Finding Your Chat ID
1. Open Telegram, search for [@userinfobot](https://t.me/userinfobot)
2. Send `/start` → it replies with your chat ID

## 3. First-Time Telegram Authentication

> ⚠️ **Only needed once.** This creates a session file so the listener can connect to Telegram as your user account.

```bash
# From WSL:
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python auth.py +6281212345678

# From Windows CMD:
cd /d "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python auth.py +6281212345678
```

Replace `+6281212345678` with your phone number (including country code).

You'll receive a 5-digit code on Telegram — enter it when prompted. The session is saved to `sessions/nabu.session`.

## 4. Configure Trading

Edit `config.yaml`:

```yaml
# Start with paper trading + dry-run to verify the pipeline:
exchange:
  active: paper              # paper | binance_testnet | binance

agent:
  auto_trade: false          # false = analyze only, no trades
  confidence_threshold: 0.0  # 0.0 = YOLO mode (all signals)
```

See [CONFIGURATION.md](CONFIGURATION.md) for the full reference.

## 5. Run the Pipeline

```bash
# From WSL:
cd /mnt/c/"Working Folder/Research/nabu-trader"
/home/it26/.hermes/venvs/netra/bin/python src/main.py

# Or use the wrapper script:
./run.sh
```

The pipeline will:
1. Connect to Telegram as your user
2. Find the target channel
3. Listen for new messages
4. Process each signal through the full pipeline

## 6. Verify It's Working

Check the logs:

```bash
tail -f logs/trading.log
```

You should see:
```
Signal Listener starting...
Channel: @YOUR_SIGNAL_CHANNEL
Auto-trade: true
Connected as: Your Name (ID: 123456789)
Channel found: UNKNOWN TRADERS ACADEMY / FATTYFAT CLUB (ID: 123456789)
```

When a signal arrives:
```
Parsed signal: pair=BTCUSDT dir=LONG entry=65400
Decision: ENTER BTCUSDT (conf=0.85, reason=Clear entry with tight SL)
Gate2: leverage 3x (pos=$21.16, balance=$10.58, margin_target=$5.29)
Position opened: LONG BTCUSDT MARKET qty=0.0003
```

## Deployment Options

### Local (WSL/Linux)
- Run with `systemd` or `screen`/`tmux` for persistence
- Keep `.env` and `sessions/` backed up

### Fly.io (Cloud)
See [DEPLOYMENT.md](DEPLOYMENT.md) for full instructions.

### Docker
```bash
docker build -t learner-listener .
docker run -d \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/sessions:/app/sessions \
  -v $(pwd)/logs:/app/logs \
  learner-listener
```
