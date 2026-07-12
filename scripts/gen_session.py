#!/usr/bin/env python3
"""Generate a Telethon StringSession for headless deployment.

Run this ONCE, interactively, on your LOCAL machine (not on Fly):

    python scripts/gen_session.py

It will prompt for your phone number, login code, and (if enabled) 2FA
password, then print a SESSION_STRING. Copy that value and set it as a
Fly secret:

    flyctl secrets set SESSION_STRING="<paste here>" --app nabu-trader

Why: the listener logs into Telegram as a *user account* to read the
channel. In a container there is no TTY, so an interactive login prompt
crashes the process. A StringSession bakes the auth into a secret so the
bot can connect headlessly without ever prompting.

Requires TG_API_ID and TG_API_HASH in your .env (they are already set as
Fly secrets for the deployed app).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
if not API_ID or not API_HASH:
    print("ERROR: TG_API_ID / TG_API_HASH not found in .env", file=sys.stderr)
    sys.exit(1)

# Empty StringSession() => generate a brand-new session via interactive login.
client = TelegramClient(StringSession(), API_ID, API_HASH)


async def main():
    print("Logging in interactively (you'll be prompted for phone/code)...")
    await client.start()  # prompts for phone, code, 2FA password
    me = await client.get_me()
    print(f"\nLogged in as: {me.first_name} (@{me.username}, id={me.id})")
    session_string = client.session.save()
    print("\n=== SESSION_STRING (copy everything between the lines) ===")
    print(session_string)
    print("=== end SESSION_STRING ===\n")
    print("Now run:")
    print('  flyctl secrets set SESSION_STRING="<the string above>" '
          "--app nabu-trader")
    await client.disconnect()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
