# Deployment Guide

## Fly.io (Cloud — 24/7)

The system is designed to run 24/7 on Fly.io's free tier (Singapore region).

### Prerequisites

```bash
# Install flyctl (Fly CLI)
curl -L https://fly.io/install.sh | sh

# Login
fly auth login
```

### Deploy

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"

# Create the app (first time only)
fly launch --name nabu-trader --now --region sin --no-deploy

# Set secrets (never commit .env!)

### Core Secrets

```bash
fly secrets set \
  TG_API_ID=12345678 \
  TG_API_HASH=abc123... \
  TELEGRAM_BOT_TOKEN=123:ABC... \
  NOTIFY_CHAT_ID=YOUR_CHAT_ID \
  BINANCE_API_KEY=xxx \
  BINANCE_API_SECRET=xxx \
  OPENCODE_GO_API_KEY=sk-... \
  CHANNEL_USERNAME=YOUR_SIGNAL_CHANNEL
```

### API Bridge Secrets

```bash
fly secrets set \
  API_KEY="$(openssl rand -hex 32)" \
  API_HMAC_SECRET="$(openssl rand -hex 32)"
```

### Webhook (optional)

```bash
fly secrets set \
  WEBHOOK_URL="https://your-hermes-endpoint/webhook" \
  WEBHOOK_HMAC_SECRET="$(openssl rand -hex 32)"
```

### Upload Telegram Session

```bash
flyctl sftp upload sessions/nabu.session /data/sessions/
```

### Deploy

```bash
fly deploy
```

### The Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends sqlite3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY .env.example .env.example
COPY config.yaml .
COPY src/ src/
RUN mkdir -p /data/sessions /data/logs
ENV FLY_MODE=1 DATA_ROOT=/data PYTHONUNBUFFERED=1
CMD ["python", "src/main.py"]
```

### Persistent Storage

- Database: `/data/trades.db` (SQLite WAL mode) — **corrected 2026-07-12** from the old nested `/data/data/trades.db` (`get_data_dir()` bug fixed).
- Sessions: `/data/sessions/nabu.session`
- Logs: `/data/logs/trading.log`

Fly.io mounts a persistent volume at `/data` so data survives restarts.

### Check the App

```bash
# From git-bash/MSYS — NOT PowerShell/cmd. Add flyctl to PATH first:
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl logs --app nabu-trader --no-tail
flyctl status --app nabu-trader
flyctl ssh console --app nabu-trader        # interactive shell in /app
```

### Updating

```bash
export PATH="$PATH:/c/Users/it26/.fly/bin"
cd "/c/Working Folder/Research/nabu-trader"
source .venv/Scripts/activate
python -m pytest -q            # 1. verify green
git add src/... tests/...      # 2. stage specific paths (NOT -A)
git commit -m "..." && git push origin fix/code-review-fixes
flyctl deploy                  # 3. rolling deploy (~1-2 min)
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://nabu-trader.fly.dev/health  # expect 200
flyctl logs --no-tail          # confirm "Listening for new messages..."
```

### Stopping

```bash
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl machine stop <machine-id>
```

## Local (WSL / Linux)

### With systemd (auto-restart)

Create `/etc/systemd/system/learner-listener.service`:

```ini
[Unit]
Description=Nabu Trader Signal Listener
After=network.target

[Service]
Type=simple
User=it26
WorkingDirectory=/mnt/c/Working Folder/Research/nabu-trader
ExecStart=/home/it26/.hermes/venvs/netra/bin/python src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable learner-listener
sudo systemctl start learner-listener
```

### With screen/tmux (simple)

```bash
cd /mnt/c/"Working Folder/Research/nabu-trader"
screen -S learner-listener
/home/it26/.hermes/venvs/netra/bin/python src/main.py
# Ctrl+A, D to detach
# screen -r learner-listener to reattach
```

## Docker (Anywhere)

```bash
docker build -t learner-listener .

docker run -d \
  --name learner-listener \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/sessions:/app/sessions \
  -v $(pwd)/logs:/app/logs \
  learner-listener
```

### Docker Compose

```yaml
version: "3.8"
services:
  listener:
    build: .
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./sessions:/app/sessions
      - ./logs:/app/logs
```

## Important Notes

1. **Session file is critical** — without it, you'll need to re-authenticate on every deploy. Back it up.
2. **Never commit `.env`** — secrets are set via `fly secrets set` or Docker env vars.
3. **One machine per region** — Fly.io free tier includes 3 shared-cpu-1x machines. One is enough.
4. **SQLite concurrency** — WAL mode handles single-writer fine. No need for PostgreSQL.
5. **Cross-platform auth** — this app uses the Windows Fly CLI binary (`/c/Users/it26/.fly/bin/flyctl.exe`). Run it directly from git-bash/MSYS via `export PATH="$PATH:/c/Users/it26/.fly/bin"` — do not shell out through `powershell.exe` (nested quoting breaks `ssh console` heredocs).
