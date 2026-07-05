# 🚀 nabu-trader — Health Check Guide

## Quick Health Check

```powershell
# From Windows (PowerShell) — the app is on Windows Fly CLI auth:
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail
```

```bash
# From WSL — use PowerShell bridge:
powershell.exe -NoProfile -Command "& flyctl status --app nabu-trader"
powershell.exe -NoProfile -Command "& flyctl logs --app nabu-trader --no-tail"
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
| **Persistent volume** | `/data` (source: `data_volume`) |
| **Health port** | 9090 |

## Important: Cross-Platform Auth

> ⚠️ The WSL Fly.io token (`b12017ec-...`) does **NOT** have access to this app.
> The Windows Fly CLI (`YOUR_HOME\.fly\bin\flyctl.exe`) has the correct auth.
>
> Always use `powershell.exe -NoProfile -Command "& flyctl ..."` from WSL,
> or run flyctl directly in Windows PowerShell.

## Check Machine Logs

```powershell
flyctl logs --app nabu-trader
```

## SSH into machine (debugging)

```powershell
flyctl ssh console --app nabu-trader
```

## Check secrets are set

```powershell
flyctl secrets list --app nabu-trader
```

## Health endpoint (if app has HTTP)

```powershell
curl https://nabu-trader.fly.dev/health
```
