import asyncio
import sys
import json
from telethon import TelegramClient
from pathlib import Path

SESSION_DIR = Path(__file__).parent / "sessions"

async def main(code):
    with open(SESSION_DIR / "hash.json") as f:
        data = json.load(f)
    
    client = TelegramClient(str(SESSION_DIR / "nabu"), YOUR_API_ID, "YOUR_API_HASH")
    await client.connect()
    await client.sign_in(data["phone"], code, phone_code_hash=data["hash"])
    me = await client.get_me()
    print(f"\nAuthenticated as: {me.first_name} (ID: {me.id})")
    print("Session saved. Run: python src\\listener.py")
    await client.disconnect()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python complete_auth.py CODE")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
