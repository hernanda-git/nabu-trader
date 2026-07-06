"""Telegram listener — monitors channel and feeds signals to the orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events

from src.exchange.base import Exchange
from src.config.loader import get_session_dir
from src.orchestrator import TradeOrchestrator

log = logging.getLogger("listener")


class SignalListener:
    """Telethon-based listener that forwards messages to the orchestrator."""

    def __init__(self, orchestrator: TradeOrchestrator, config: dict, exchange: Exchange | None = None):
        self.orchestrator = orchestrator
        self.config = config
        self.exchange = exchange or getattr(orchestrator, 'exchange', None)

        # Load Telegram API credentials
        load_dotenv(Path(__file__).parent.parent / ".env")
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        channel = os.getenv("CHANNEL_USERNAME", "YOUR_SIGNAL_CHANNEL")
        session_dir = get_session_dir()
        session_dir.mkdir(parents=True, exist_ok=True)

        self.channel = channel
        self.client = TelegramClient(str(session_dir / "nabu"), api_id, api_hash)

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

        # ── Register command handlers (private chat only) ─────────────
        @self.client.on(events.NewMessage(outgoing=True, pattern=r"^/"))
        async def on_command(event):
            cmd = (event.message.text or "").strip().lower()
            # Only respond in private chats (Saved Messages / bot DMs)
            if not event.is_private:
                return
            if cmd == "/positions":
                await self._handle_positions(event)
            elif cmd == "/balance":
                await self._handle_balance(event)
            elif cmd == "/help":
                await self._handle_help(event)

        await self.client.run_until_disconnected()

    async def stop(self):
        """Stop listening."""
        await self.client.disconnect()
        log.info("Listener stopped")

    # ── Command handlers ──────────────────────────────────────────────────

    async def _handle_positions(self, event):
        """Handle /positions — show open futures positions from exchange."""
        if not self.exchange:
            await event.reply("❌ No exchange configured.")
            return
        log.info("Command: /positions")
        try:
            positions = await self.exchange.get_positions()
            if not positions:
                await event.reply(
                    "📭 **No open positions**\n\n"
                    "No active futures positions found on Binance."
                )
                return
            lines = ["📊 **Binance Futures — Open Positions**\n"]
            for i, p in enumerate(positions, 1):
                emoji = "🟢" if p.direction == "LONG" else "🔴"
                pnl_emoji = "📈" if p.unrealized_pnl >= 0 else "📉"
                lines.append(
                    f"{emoji} **{i}. {p.symbol}**\n"
                    f"   ┣ Direction: `{p.direction}`\n"
                    f"   ┣ Size: `{p.size:.4f}` ({p.notional:.2f} USDT)\n"
                    f"   ┣ Entry: `{p.entry_price:.6f}`\n"
                    f"   ┣ Mark: `{p.mark_price:.6f}`\n"
                    f"   ┣ Liq: `{p.liquidation_price:.6f}`\n"
                    f"   ┣ Leverage: `{p.leverage}x`\n"
                    f"   ┗ {pnl_emoji} PnL: `{p.unrealized_pnl:+.2f} USDT`\n"
                )
            await event.reply("\n".join(lines))
        except Exception as e:
            log.exception("Failed to fetch positions")
            await event.reply(f"❌ **Error fetching positions:** `{e}`")

    async def _handle_balance(self, event):
        """Handle /balance — show account balance from exchange."""
        if not self.exchange:
            await event.reply("❌ No exchange configured.")
            return
        log.info("Command: /balance")
        try:
            bal = await self.exchange.get_balance()
            lines = [
                "💰 **Binance Futures — Account Balance**\n",
                f"   ┣ 💵 Free: `{bal.free_usdt:.2f} USDT`",
                f"   ┣ 💰 Total: `{bal.total_usdt:.2f} USDT`",
            ]
            if bal.assets:
                for asset, details in bal.assets.items():
                    if asset != "USDT":
                        continue
                    lines.append(f"   ┗ Unrealized PnL: `{details.get('unrealized_pnl', 0):+.2f} USDT`")
            await event.reply("\n".join(lines))
        except Exception as e:
            log.exception("Failed to fetch balance")
            await event.reply(f"❌ **Error fetching balance:** `{e}`")

    async def _handle_help(self, event):
        """Handle /help — list available commands."""
        await event.reply(
            "📋 **Available Commands**\n\n"
            "  /balance    — Show futures account balance\n"
            "  /positions  — Show all open futures positions\n"
            "  /help       — Show this message\n\n"
            "The bot automatically processes signals from @YOUR_SIGNAL_CHANNEL and\n"
            "executes trades on Binance Futures when conditions are met."
        )
