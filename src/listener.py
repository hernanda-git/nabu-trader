"""Telegram listener — monitors channel and feeds signals to the orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

from src.orchestrator import TradeOrchestrator

log = logging.getLogger("listener")


class SignalListener:
    """Telethon-based listener that forwards messages to the orchestrator."""

    def __init__(self, orchestrator: TradeOrchestrator, config: dict):
        self.orchestrator = orchestrator
        self.config = config

        # Load Telegram API credentials
        load_dotenv(Path(__file__).parent.parent / ".env")
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        channel = os.getenv("CHANNEL_USERNAME", "gishbanda")
        session_dir = Path(__file__).parent.parent / "sessions"
        session_dir.mkdir(exist_ok=True)

        self.channel = channel
        self.client = TelegramClient(str(session_dir / "nabu"), api_id, api_hash)
        self._seen_messages: set[int] = set()

    async def start(self):
        """Start listening."""
        log.info("=" * 60)
        log.info("Signal Listener starting...")
        log.info("Channel: @%s", self.channel)
        log.info("Auto-trade: %s", self.config.get("agent", {}).get("auto_trade", False))
        log.info("=" * 60)

        await self.client.start()
        me = await self.client.get_me()
        log.info("Connected as: %s (ID: %s)", me.first_name, me.id)

        # Verify channel access
        try:
            channel = await self.client.get_entity(self.channel)
            log.info("Channel found: %s (ID: %s)", channel.title, channel.id)
        except Exception as e:
            log.error("Cannot access @%s: %s", self.channel, e)
            return

        # Register handlers
        @self.client.on(events.NewMessage(chats=self.channel))
        async def on_new_message(event):
            msg = event.message
            if msg.id in self._seen_messages:
                return
            self._seen_messages.add(msg.id)
            text = msg.text or msg.message or ""
            log.info("New message #%s | %d chars", msg.id, len(text))
            await self.orchestrator.handle_signal(
                message_id=msg.id,
                channel=self.channel,
                raw_text=text,
                has_media=msg.media is not None,
            )

        @self.client.on(events.MessageEdited(chats=self.channel))
        async def on_edited(event):
            msg = event.message
            text = msg.text or msg.message or ""
            log.info("Edited message #%s", msg.id)
            await self.orchestrator.handle_signal(
                message_id=msg.id,
                channel=self.channel,
                raw_text=text,
                has_media=msg.media is not None,
            )

        log.info("Listening for new messages... (Ctrl+C to stop)")
        await self.client.run_until_disconnected()

    async def stop(self):
        """Stop listening."""
        await self.client.disconnect()
        log.info("Listener stopped")
