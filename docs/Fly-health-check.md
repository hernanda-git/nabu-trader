# 🚀 nabu-trader — Health Check Guide

## Quick Health Check

```bash
# From git-bash/MSYS (the Hermes terminal runs bash). Add flyctl to PATH:
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail
```

## App Details

| Field | Value |
|-------|-------|
| **App** | nabu-trader |
| **Region** | Singapore (`sin`) |
| **Hostname** | `nabu-trader.fly.dev` |
| **Exchange** | Binance Futures (auto-trade = 🚀 YOLO) |
| **Channel** | @YOUR_SIGNAL_CHANNEL |
| **Telegram notifier** | Chat ID: YOUR_CHAT_ID |
| **Persistent volume** | `/data` (source: `data_volume`) — **DB at `/data/trades.db`** (corrected 2026-07-12 from the old nested `/data/data/trades.db`) |
| **Health port** | 9090 |

## Important: Cross-Platform Auth

> ⚠️ This app uses the **Windows Fly CLI** binary (`YOUR_HOME\.fly\bin\flyctl.exe`).
> The WSL `fly` token does **NOT** have access. Run `flyctl` directly from git-bash/MSYS
> via `export PATH="$PATH:/c/Users/it26/.fly/bin"` — do **not** shell out
> through `powershell.exe` (nested quoting breaks `ssh console` heredocs).

## Check Machine Logs

```bash
flyctl logs --app nabu-trader
```

## SSH into machine (debugging)

```bash
# Pipe a heredoc (NOT -C 'python -c ...' — nested quotes break):
flyctl ssh console --app nabu-trader <<'PY'
python - <<'PY'
import os
os.chdir("/app")
from src.state.database import DB_PATH
print("DB_PATH:", DB_PATH)
PY
exit
PY
```

## Check secrets are set

```bash
flyctl secrets list --app nabu-trader
```

## Health endpoint (if app has HTTP)

```bash
curl https://nabu-trader.fly.dev/health
```
