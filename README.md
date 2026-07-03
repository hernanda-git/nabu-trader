# Nabu Trader Signal Listener

Real-time Telegram channel monitor → Signal parser → Auto-notify via bot.

## Setup (run once)

```bash
cd "C:\Working Folder\Research\nabu-trader"

# Activate venv (or use full path)
/home/it26/.hermes/venvs/netra/bin/python auth.py
```

It will ask:
1. **Phone number** → enter `+6281280031995`
2. **Code** → check your Telegram app for the 5-digit code

## Run Listener

```bash
./run.sh
```

Or directly:
```bash
/home/it26/.hermes/venvs/netra/bin/python src/listener.py
```

## What Happens

1. Listener connects to Telegram as your account
2. Monitors `@Nabu Trader` in real-time
3. Parses signals (pair, direction, entry, SL, TP)
4. Sends formatted signal to your Telegram via @YOUR_BOT_USERNAME

## Files

- `.env` — credentials (do not share)
- `auth.py` — one-time Telegram auth
- `src/listener.py` — main listener
- `run.sh` — start listener
- `sessions/` — Telegram session (auto-created)
- `logs/` — listener.log
