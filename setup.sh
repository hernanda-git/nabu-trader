#!/bin/bash
# Setup script for Nabu Trader Listener
set -e

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON="/home/it26/.hermes/venvs/netra/bin/python"

echo "╔══════════════════════════════════════════════════╗"
echo "║   Nabu Trader Listener Setup                ║"
echo "╚══════════════════════════════════════════════════╝"

# 1. Create .env from example if not exists
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[1/4] Creating .env from .env.example..."
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo "      ✅ .env created — fill in TELEGRAM_BOT_TOKEN"
else
    echo "[1/4] .env already exists"
fi

# 2. Install dependencies
echo "[2/4] Checking dependencies..."
$PYTHON -c "import telethon" 2>/dev/null && echo "      ✅ telethon OK" || {
    echo "      ❌ telethon not found — install in netra venv"
    exit 1
}
$PYTHON -c "import httpx" 2>/dev/null && echo "      ✅ httpx OK" || {
    echo "      Installing httpx..."
    /home/it26/.hermes/venvs/netra/bin/pip install httpx -q
}
$PYTHON -c "import dotenv" 2>/dev/null && echo "      ✅ python-dotenv OK" || {
    echo "      Installing python-dotenv..."
    /home/it26/.hermes/venvs/netra/bin/pip install python-dotenv -q
}

# 3. Create directories
echo "[3/4] Creating directories..."
mkdir -p "$PROJECT_DIR/sessions" "$PROJECT_DIR/logs"
echo "      ✅ sessions/ and logs/ ready"

# 4. Test auth (first run needs phone number)
echo "[4/4] Testing Telegram connection..."
cd "$PROJECT_DIR"
$PYTHON -c "
import asyncio
from telethon import TelegramClient
from pathlib import Path

async def test():
    client = TelegramClient(
        str(Path('sessions/nabu')),
        YOUR_API_ID,
        'YOUR_API_HASH'
    )
    await client.start()
    me = await client.get_me()
    print(f'      ✅ Connected as: {me.first_name} (ID: {me.id})')
    await client.disconnect()

asyncio.run(test())
" 2>&1

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   Setup complete!                               ║"
echo "║                                                 ║"
echo "║   To run:  ./run.sh                             ║"
echo "║   To stop: Ctrl+C                               ║"
echo "╚══════════════════════════════════════════════════╝"
