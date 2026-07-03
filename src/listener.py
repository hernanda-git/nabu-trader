#!/usr/bin/env python3
"""
Nabu Trader Channel Listener
Real-time Telethon listener that monitors @Nabu Trader
and forwards trade signals to Hermes Telegram chat.
"""

import asyncio
import os
import sys
import re
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import Channel, MessageMediaPhoto, MessageMediaDocument

# ─── Config ────────────────────────────────────────────────────────────────────
load_dotenv(Path(__file__).parent.parent / ".env")

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
CHANNEL = os.getenv("CHANNEL_USERNAME", "Nabu Trader")
NOTIFY_CHAT_ID = int(os.getenv("NOTIFY_CHAT_ID", "0"))
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

SESSION_DIR = Path(__file__).parent.parent / "sessions"
LOG_DIR = Path(__file__).parent.parent / "logs"
SESSION_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ─── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "listener.log"),
    ],
)
log = logging.getLogger("listener")

# ─── Signal Parser ─────────────────────────────────────────────────────────────
# Pattern-based extraction + full text pass-through for agent processing

PAIR_PATTERNS = [
    re.compile(r"\b(BTC|ETH|BNB|SOL|XRP|ADA|DOGE|AVAX|DOT|LINK|ATOM|UNI|SHIB|PEPE|FIL|LTC|BCH|ETC|NEAR|APT|ARB|OP|SUI|SEI|TIA|INJ|FET|RENDER|WIF|BONK|JUP|PYTH|ONDO|OMNI|ENS|AAVE|MKR|CRV|SNX)\s*/?\s*(USDT|USD|BUSD|USDC|BTC|ETH)\b", re.I),
    re.compile(r"\b([A-Z]{3,6})\s*(?:USDT|USD|BUSD)\b", re.I),
    re.compile(r"[#$](\w{2,20})\b", re.I),
    re.compile(r"\b([A-Z]{2,10})\b"),  # fallback: any all-caps word as ticker
]

DIRECTION_PATTERNS = [
    re.compile(r"\b(buy|long|bullish|upside|Call)\b", re.I),
    re.compile(r"\b(sell|short|bearish|downside|Put)\b", re.I),
]

ENTRY_PATTERNS = [
    re.compile(r"(?:entry|enter|open|buy\s*at|sell\s*at|limit|@\s*)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:zone|range|area)\s*[:\\-]?\s*([\d,]+\.?\d*)", re.I),
]

SL_PATTERNS = [
    re.compile(r"(?:sl|stop\s*(?:loss)?|stoploss)\s*[:\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:invalidat|invalid)\s*[:\-]?\s*([\d,]+\.?\d*)", re.I),
]

TP_PATTERNS = [
    re.compile(r"(?:tp\s*1?|take\s*profit\s*1?|target\s*1?|tgt\s*1?)\s*[:\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:tp\s*2|take\s*profit\s*2|target\s*2|tgt\s*2)\s*[:\-]?\s*([\d,]+\.?\d*)", re.I),
    re.compile(r"(?:tp\s*3|take\s*profit\s*3|target\s*3|tgt\s*3)\s*[:\-]?\s*([\d,]+\.?\d*)", re.I),
]


def parse_signal(text: str) -> dict:
    """Extract trade signal components from message text."""
    if not text:
        return {}

    signal = {"raw_text": text.strip()}

    # Pair
    for pat in PAIR_PATTERNS:
        m = pat.search(text)
        if m:
            signal["pair"] = m.group(0).strip().upper()
            break

    # Direction
    for pat in DIRECTION_PATTERNS:
        m = pat.search(text)
        if m:
            direction = m.group(1).lower()
            signal["direction"] = "LONG" if direction in ("buy", "long", "bullish", "upside", "call") else "SHORT"
            break

    # Entry
    for pat in ENTRY_PATTERNS:
        m = pat.search(text)
        if m:
            signal["entry"] = m.group(1).replace(",", "")
            break

    # Stop Loss
    for pat in SL_PATTERNS:
        m = pat.search(text)
        if m:
            signal["sl"] = m.group(1).replace(",", "")
            break

    # Take Profits
    tps = []
    for pat in TP_PATTERNS:
        m = pat.search(text)
        if m:
            tps.append(m.group(1).replace(",", ""))
    if tps:
        signal["tps"] = tps

    return signal


def format_signal(signal: dict, original_text: str, has_media: bool = False) -> str:
    """Format parsed signal for Telegram notification."""
    pair = signal.get("pair", "❓")
    direction = signal.get("direction", "❓")
    entry = signal.get("entry", "—")
    sl = signal.get("sl", "—")
    tps = signal.get("tps", [])

    # Emoji
    if direction == "LONG":
        emoji = "🟢"
        dir_text = "LONG (Buy)"
    elif direction == "SHORT":
        emoji = "🔴"
        dir_text = "SHORT (Sell)"
    else:
        emoji = "⚡"
        dir_text = "Unknown"

    # Build message
    lines = [
        f"{emoji} **SIGNAL — {pair}**",
        f"📊 **Direction:** {dir_text}",
        f"💰 **Entry:** `{entry}`",
        f"🛑 **Stop Loss:** `{sl}`",
    ]

    if tps:
        for i, tp in enumerate(tps, 1):
            lines.append(f"🎯 **TP{i}:** `{tp}`")

    if has_media:
        lines.append("📷 _Contains image/media_")

    lines.append("")
    lines.append(f"📝 _{original_text[:300]}_")
    lines.append(f"🕐 _{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")

    return "\n".join(lines)


# ─── Notifier ──────────────────────────────────────────────────────────────────

async def notify_hermes(message: str):
    """Send notification to Hermes Telegram chat via Bot API."""
    if not BOT_TOKEN:
        log.warning("No BOT_TOKEN set — printing to stdout only")
        print(f"\n{'='*60}\n{message}\n{'='*60}\n")
        return

    import httpx
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": NOTIFY_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            log.info("Notification sent to Hermes chat")
        else:
            log.error(f"Notification failed: {resp.status_code} — {resp.text}")


# ─── Main Listener ─────────────────────────────────────────────────────────────

# Store processed message IDs to avoid duplicates
seen_messages = set()

client = TelegramClient(
    str(SESSION_DIR / "nabu"),
    API_ID,
    API_HASH,
)


@client.on(events.NewMessage(chats=CHANNEL))
async def on_new_message(event):
    """Handle new messages from @Nabu Trader channel."""
    msg = event.message
    msg_id = msg.id

    # Dedup
    if msg_id in seen_messages:
        return
    seen_messages.add(msg_id)

    text = msg.text or msg.message or ""
    has_media = msg.media is not None
    is_forward = msg.forward is not None

    log.info(f"New message #{msg_id} | text={len(text)} chars | media={has_media} | forward={is_forward}")

    # Parse signal
    signal = parse_signal(text)

    # Check if it looks like a trade signal
    is_signal = any(k in signal for k in ("pair", "direction", "entry"))

    if is_signal:
        notification = format_signal(signal, text, has_media)
        log.info(f"Signal detected: {signal.get('pair', '?')} {signal.get('direction', '?')}")
    else:
        # Non-signal post — still forward as info
        preview = text[:200] if text else "(media/forwarded message)"
        notification = (
            f"📢 **New post from @Nabu Trader**\n\n"
            f"{preview}\n\n"
            f"🕐 _{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
        )
        log.info("Non-signal post forwarded")

    await notify_hermes(notification)


@client.on(events.MessageEdited(chats=CHANNEL))
async def on_message_edited(event):
    """Handle edited messages (sometimes signals get updated)."""
    msg = event.message
    text = msg.text or msg.message or ""

    log.info(f"Message #{msg.id} edited")

    signal = parse_signal(text)
    if any(k in signal for k in ("pair", "direction", "entry")):
        notification = (
            f"✏️ **SIGNAL UPDATED**\n\n"
            f"{format_signal(signal, text, msg.media is not None)}"
        )
        await notify_hermes(notification)


async def main():
    """Start the listener."""
    log.info("=" * 60)
    log.info("Nabu Trader Listener starting...")
    log.info(f"Channel: @{CHANNEL}")
    log.info(f"Notify chat: {NOTIFY_CHAT_ID}")
    log.info(f"Bot token: {'SET' if BOT_TOKEN else 'NOT SET (stdout only)'}")
    log.info("=" * 60)

    await client.start()
    me = await client.get_me()
    log.info(f"Connected as: {me.first_name} (ID: {me.id})")

    # Verify channel access
    try:
        channel = await client.get_entity(CHANNEL)
        log.info(f"Channel found: {channel.title} (ID: {channel.id})")
    except Exception as e:
        log.error(f"Cannot access @{CHANNEL}: {e}")
        log.info("Make sure the channel is public and accessible")
        return

    log.info("Listening for new messages... (Ctrl+C to stop)")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Listener stopped by user")
