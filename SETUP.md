# Nabu Trader Signal Listener — Setup Guide

## Overview

This tool monitors the `@Nabu Trader` Telegram channel in real-time,
parses trade signals (pair, direction, entry, SL, TP), and sends them
to your Telegram via the @YOUR_BOT_USERNAME.

---

## Prerequisites

- Windows 10/11
- Python 3.11+ (already installed via Hermes)
- Telegram account with phone number `+628****1995`
- Access to `@Nabu Trader` channel (public)

---

## Step 1: Install Dependencies

Open **PowerShell** or **CMD** and run:

```
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\pip install telethon httpx python-dotenv
```

Verify installation:
```
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python -c "import telethon; print(telethon.__version__)"
```

Expected output: `1.44.0` or similar.

---

## Step 2: Telegram Authentication (One-Time)

This links the listener to your Telegram account so it can read the channel.

### 2.1 Open Terminal

```
cd "C:\Working Folder\Research\nabu-trader"
```

### 2.2 Run Auth

```
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python auth.py +6281280031995
```

### 2.3 Enter the Code

- Telegram will send a **5-digit code** to your Telegram app
- Check the **"Telegram"** chat at the top of your chat list
- Enter the code when prompted

### 2.4 Success

You should see:
```
Authenticated as: Hernanda (ID: xxxxxxxx)
Session saved. Run: python src\listener.py
```

### Troubleshooting

| Problem | Solution |
|---------|----------|
| Code not received | Wait 10 minutes, Telegram rate-limits after multiple attempts |
| "Phone number invalid" | Double-check the number with country code |
| "Session banned" | Your account may be temporarily restricted — wait 24 hours |

---

## Step 3: Configure Bot Token

The bot token is already configured in `.env`:
```
TELEGRAM_BOT_TOKEN=REMOVED_SECRET
```

This connects to **@YOUR_BOT_USERNAME** which sends notifications to your chat.

---

## Step 4: Start the Listener

### Option A: Double-click `run.bat`

### Option B: Manual in terminal

```
cd "C:\Working Folder\Research\nabu-trader"
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\python src\listener.py
```

### Expected Output

```
2025-01-01 15:00:00 [INFO] ============================================================
2025-01-01 15:00:00 [INFO] Nabu Trader Listener starting...
2025-01-01 15:00:00 [INFO] Channel: @Nabu Trader
2025-01-01 15:00:00 [INFO] Notify chat: YOUR_CHAT_ID
2025-01-01 15:00:00 [INFO] Bot token: SET
2025-01-01 15:00:00 [INFO] ============================================================
2025-01-01 15:00:01 [INFO] Connected as: Hernanda (ID: xxxxxxxx)
2025-01-01 15:00:01 [INFO] Channel found: Nabu Trader (ID: xxxxxxxx)
2025-01-01 15:00:01 [INFO] Listening for new messages... (Ctrl+C to stop)
```

---

## Step 5: Test It

1. Keep the listener running
2. Ask someone to post in `@Nabu Trader`, or wait for a new post
3. You should receive a notification in Telegram via @YOUR_BOT_USERNAME

---

## Signal Format

The listener auto-detects signals from any format. It looks for:

| Field | Patterns Detected |
|-------|-------------------|
| **Pair** | BTC, ETH, SOL, BNB, etc. + USDT/USD |
| **Direction** | buy, long, sell, short, bullish, bearish |
| **Entry** | entry, enter, open, @, zone |
| **Stop Loss** | sl, stop loss, stoploss, invalidation |
| **Take Profit** | tp1, tp2, target, tgt |

### Example Messages Detected

```
🟢 BUY BTCUSDT
Entry: 65000
SL: 64000
TP1: 66000
TP2: 67000
```

```
SOL/USDT Long Zone 140-142
Invalidation below 138
Target 155
```

```
🔴 Short ETH at 3500
Stop: 3600
TP: 3300
```

All formats are auto-parsed and forwarded.

---

## Files

```
nabu-trader/
├── .env                  ← Credentials (DO NOT SHARE)
├── auth.py               ← One-time Telegram auth
├── complete_auth.py      ← Complete auth with code
├── run.bat               ← Start listener (double-click)
├── run.sh                ← Start listener (Linux/Mac)
├── src/
│   └── listener.py       ← Main listener + signal parser
├── sessions/
│   └── nabu.session  ← Telegram session (auto-created)
├── logs/
│   └── listener.log      ← Activity log
├── SETUP.md              ← This file
└── README.md             ← Quick reference
```

---

## Running in Background (Optional)

To run the listener as a background service:

### Method 1: Pythonw (no console window)

```
YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw src\listener.py
```

### Method 2: Task Scheduler

1. Open Task Scheduler
2. Create Basic Task
3. Trigger: At log on
4. Action: Start a program
   - Program: `YOUR_HOME\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe`
   - Arguments: `C:\Working Folder\Research\nabu-trader\src\listener.py`
   - Start in: `C:\Working Folder\Research\nabu-trader`

---

## Stopping the Listener

Press `Ctrl+C` in the terminal window, or end the process in Task Manager.

---

## Phase 2: Auto-Trade (Future)

This will add Binance integration:
- Parse signal → Create order via Binance API
- Configure: pair mapping, position size, risk management
- Paper trading mode by default

Setup will require:
```
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
```

---

## Support

If something isn't working:
1. Check `logs/listener.log` for errors
2. Verify bot works: the test message from @YOUR_BOT_USERNAME should be in your Telegram
3. Re-run auth if session expires

---

## Linux / VPS Setup

The same code works on Linux. Here's how to deploy it.

### Prerequisites

- Python 3.11+
- pip
- SSH access to your VPS

### 1. Copy Project to VPS

```bash
# From Windows (PowerShell)
scp -r "C:\Working Folder\Research\nabu-trader" user@your-vps:/home/user/

# Or from any machine
scp -r nabu-trader/ user@your-vps:/home/user/
```

### 2. SSH into VPS

```bash
ssh user@your-vps
cd nabu-trader
```

### 3. Install Dependencies

```bash
pip3 install telethon httpx python-dotenv
```

### 4. Auth (One-Time)

```bash
python3 auth.py +6281280031995
```

Enter the code from Telegram when prompted.

### 5. Run

```bash
python3 src/listener.py
```

### 6. Run in Background

```bash
# Option A: nohup
nohup python3 src/listener.py > logs/listener.log 2>&1 &

# Option B: screen
screen -S lnr
python3 src/listener.py
# Ctrl+A, D to detach

# Option C: tmux
tmux new -s lnr
python3 src/listener.py
# Ctrl+B, D to detach
```

### 7. Auto-Restart with systemd (Recommended)

Create `/etc/systemd/system/nabu.service`:

```ini
[Unit]
Description=Nabu Trader Signal Listener
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user/nabu-trader
ExecStart=/usr/bin/python3 src/listener.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable nabu
sudo systemctl start nabu

# Check status
sudo systemctl status nabu

# View logs
sudo journalctl -u nabu -f
```

### 8. Verify

```bash
ps aux | grep listener.py
tail -f logs/listener.log
```

---

## Windows vs Linux

| | Windows | Linux/VPS |
|---|---|---|
| Python | `C:\...\python.exe` | `python3` |
| Run | `python src\listener.py` | `python3 src/listener.py` |
| Background | `pythonw` / Task Scheduler | `nohup` / `screen` / `systemd` |
| Logs | `logs\listener.log` | `logs/listener.log` or `journalctl` |
| Auto-restart | Task Scheduler | `systemd` |

### Recommended VPS Specs

| Resource | Minimum |
|----------|---------|
| RAM | 512 MB |
| CPU | 1 vCPU |
| Disk | 1 GB |
| OS | Ubuntu 22.04+ / Debian 12+ |

The listener is lightweight — uses ~30-50 MB RAM.
