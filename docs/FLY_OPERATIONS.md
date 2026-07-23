# 🚀 Fly.io Operations Guide

## Quick Health Check

```bash
# From git-bash/MSYS (the Hermes terminal runs bash). Add flyctl to PATH:
export PATH="$PATH:YOUR_HOME/.fly/bin"
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail
```

## App Details

| Field | Value |
|-------|-------|
| **App Name** | `nabu-trader` |
| **Machine ID** | `YOUR_MACHINE_ID` |
| **Region** | `sin` (Singapore) |
| **Image** | `nabu-trader:deployment-01KY691TB9WB3KD9F0R5D5A3X8` |
| **Primary Port** | `9090` (API bridge) |
| **Volume** | `data_vol` → `/data` |

## CLI Reference

### Status & Monitoring

| Command | Description |
|---------|-------------|
| `flyctl status --app nabu-trader` | App status, machine state, version |
| `flyctl logs --app nabu-trader --no-tail` | Recent logs (last 500 lines) |
| `flyctl logs --app nabu-trader` | Stream live logs |
| `flyctl machine list --app nabu-trader` | List all machines |
| `flyctl scale count 1 --app nabu-trader` | Scale to 1 machine |

### Logs

The `--no-tail` flag dumps recent logs and exits. Without it, logs stream continuously.

```bash
# Recent error logs
flyctl logs --app nabu-trader --no-tail | grep -i "error\|fail\|exception\|traceback"

# Recent SL/close events
flyctl logs --app nabu-trader --no-tail | grep -i "position closed\|SL HIT\|TP HIT"

# Recent signals
flyctl logs --app nabu-trader --no-tail | grep -i "signal received\|decision"
```

### SSH Console

Run commands directly on the Fly machine:

```bash
# Open interactive shell
flyctl ssh console --app nabu-trader

# Run one command
flyctl ssh console --app nabu-trader -C "python3 -c 'import sys; sys.path.insert(0,\"/app\"); from src.version import __version__; print(__version__)'"

# Check database
flyctl ssh console --app nabu-trader -C "sqlite3 /data/trades.db 'SELECT id,pair,status,pnl,closed_by FROM positions ORDER BY id DESC LIMIT 10;'"

# Check disk usage
flyctl ssh console --app nabu-trader -C "df -h /data"

# Check running processes
flyctl ssh console --app nabu-trader -C "ps aux"
```

### Remote Command Execution (Machines API)

For scripts >1 line, use the Machines API exec (no SSH session needed):

```bash
TOKEN=$(flyctl auth token 2>/dev/null | head -1)
MID=YOUR_MACHINE_ID
B64=$(base64 -w0 /path/to/script.py)
curl -s -X POST "https://api.machines.dev/v1/apps/nabu-trader/machines/$MID/exec" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"command\":[\"sh\",\"-c\",\"echo '\$B64' | base64 -d | python3\"],\"timeout\":30}"
```

### Secrets Management

```bash
# List all secrets
flyctl secrets list --app nabu-trader

# Set a secret
flyctl secrets set MY_SECRET=value --app nabu-trader

# Remove a secret
flyctl secrets unset MY_SECRET --app nabu-trader

# Deploy is NOT needed after secrets change — Fly restarts the app automatically
```

### Deploy

```bash
# Standard deploy (build + deploy)
flyctl deploy --app nabu-trader

# Deploy from specific directory
cd "/c/Working Folder/Research/nabu-trader"
flyctl deploy --app nabu-trader

# Deploy with version bump (via deploy.sh)
bash deploy.sh
```

### Volumes

```bash
# List volumes
flyctl volumes list --app nabu-trader

# Show volume details
flyctl volumes show data_vol --app nabu-trader

# Extend volume (if running out of space)
flyctl volumes extend data_vol --app nabu-trader --size 3
```

### Machine Management

```bash
# List all machines
flyctl machine list --app nabu-trader

# Stop machine
flyctl machine stop YOUR_MACHINE_ID --app nabu-trader

# Start machine
flyctl machine start YOUR_MACHINE_ID --app nabu-trader

# Restart machine
flyctl machine restart YOUR_MACHINE_ID --app nabu-trader
```

## Database Operations

The bot uses SQLite at `/data/trades.db`. Here's how to inspect it:

### SSH Console SQL

```bash
# Recent positions
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'SELECT id, pair, direction, entry_price, quantity, status, pnl, closed_by FROM positions ORDER BY id DESC LIMIT 10;'"

# Recent signals
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'SELECT id, pair, raw_text, created_at FROM signals ORDER BY id DESC LIMIT 10;'"

# Recent decisions
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'SELECT d.id, s.pair, d.action, d.reason FROM decisions d JOIN signals s ON s.id=d.signal_id ORDER BY d.id DESC LIMIT 10;'"

# Open positions
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'SELECT id, pair, direction, entry_price, quantity, status FROM positions WHERE status=\"OPEN\";'"

# Account stats
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'SELECT COUNT(*) as trades, SUM(pnl) as total_pnl, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as losses FROM positions WHERE status=\"CLOSED\";'"
```

### Python Remote Inspection

For complex queries, pipe Python:

```bash
flyctl ssh console --app nabu-trader -C "python3 -c '
import sqlite3, json
c = sqlite3.connect(\"/data/trades.db\")
c.row_factory = sqlite3.Row
for r in c.execute(\"SELECT id, pair, direction, entry_price, exit_price, pnl, closed_by, reason FROM positions ORDER BY id DESC LIMIT 5\"):
    print(dict(r))
c.close()
'"
```

## Health Alerts

The bot has two health mechanisms:

### 1. `/health` Command (on-demand)

Send `/health` to the bot on Telegram. Returns a full system status report:
- 🤖 Telegram Bot API
- 👤 Telethon session + channel
- 🧠 LLM Provider (OpenCode Go)
- 💱 Exchange connection + balance
- 💼 Portfolio (open positions, margin/trade)
- 🗄️ Database

### 2. Periodic Health Reports (auto, every 6h)

The `HealthReporter` posts a health summary to Telegram automatically every 6 hours. Same format as `/health`.

### 3. Fly.io Health Checks

Fly.io periodically checks `:9090/health` (HTTP 200 = healthy). If the app fails 3 checks, Fly restarts the machine.

## Troubleshooting

### Bot not responding to commands

```bash
# 1. Check if machine is running
flyctl status --app nabu-trader

# 2. Check recent logs for errors
flyctl logs --app nabu-trader --no-tail | tail -50

# 3. Try SSH console
flyctl ssh console --app nabu-trader -C "ps aux | grep python"

# 4. Restart if stuck
flyctl machine restart YOUR_MACHINE_ID --app nabu-trader
```

### Position not closing (old bug, fixed in v92)

If a close trigger produces no action, verify the fix is deployed:
```bash
flyctl ssh console --app nabu-trader -C \
  "python3 -c 'import sys; sys.path.insert(0,\"/app\"); from src.version import __version__; print(__version__)'"
```
Expected: `v92` or higher.

### SQLite database issues

```bash
# Check DB integrity
flyctl ssh console --app nabu-trader -C \
  "sqlite3 /data/trades.db 'PRAGMA integrity_check;'"

# Check disk space
flyctl ssh console --app nabu-trader -C "df -h /data"

# Backup database
flyctl ssh console --app nabu-trader -C \
  "cp /data/trades.db /data/trades.backup.db"
```

### LLM not responding

```bash
# Check if OpenCode Go endpoint is reachable
flyctl ssh console --app nabu-trader -C \
  "curl -s -o /dev/null -w '%{http_code}' https://opencode.ai/zen/go/v1/models"

# Check API key is set
flyctl secrets list --app nabu-trader | grep OPENCODE
```

## Version History

| Version | Date | Key Changes |
|---------|------|-------------|
| v103 | 2026-07-23 | Close Telegram notification + markdown fix |
| v102 | 2026-07-22 | Hotfix rebuild |
| v101 | 2026-07-22 | Reject management cmds for wrong pair |
| v92 | 2026-07-14 | Fix CLOSE no-op in orchestrator |
| v91 | 2026-07-14 | Previous state |
