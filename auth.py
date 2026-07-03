#!/usr/bin/env python3
"""
Auth for Nabu Trader Listener.
Usage: python auth.py +6281280031995
"""

import asyncio
import sys
from telethon import TelegramClient
from pathlib import Path

API_ID = YOUR_API_ID
API_HASH = "YOUR_API_HASH"
SESSION_DIR = Path(__file__).parent / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

async def main(phone):
    client = TelegramClient(str(SESSION_DIR / "nabu"), API_ID, API_HASH)
    await client.start(phone=phone)
    me = await client.get_me()
    print(f"\nAuthenticated as: {me.first_name} (ID: {me.id})")
    print("Session saved. You can now run: python src\\listener.py")
    await client.disconnect()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python auth.py +6281280031995")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
