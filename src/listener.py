"""Telegram listener — monitors channel and feeds signals to the orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from src.exchange.base import Exchange
from src.config.loader import get_session_dir
from src.orchestrator import TradeOrchestrator

log = logging.getLogger("listener")


class SignalListener:
    """Telethon-based listener that forwards messages to the orchestrator."""

    def __init__(self, orchestrator: TradeOrchestrator, config: dict, exchange: Exchange | None = None, version: str | None = None, notifier: "TelegramNotifier | None" = None):
        self.orchestrator = orchestrator
        self.config = config
        self.exchange = exchange or getattr(orchestrator, 'exchange', None)
        self.version = version

        # Load Telegram API credentials
        load_dotenv(Path(__file__).parent.parent / ".env")
        api_id = int(os.getenv("TG_API_ID", "0"))
        api_hash = os.getenv("TG_API_HASH", "")
        channel = os.getenv("CHANNEL_USERNAME", "YOUR_SIGNAL_CHANNEL")
        session_dir = get_session_dir()
        session_dir.mkdir(parents=True, exist_ok=True)

        self.channel = channel
        self.notifier = notifier
        self.version = version

        # Session: prefer a StringSession secret (for headless/container
        # deployments) so we never fall back to an interactive login prompt,
        # which crashes the process when there is no TTY.
        session_str = os.getenv("SESSION_STRING", "")
        if session_str:
            session = StringSession(session_str)
            log.info("Using StringSession from SESSION_STRING secret")
        else:
            session = str(session_dir / "nabu")
            log.info("Using file session at %s", session)
        self.client = TelegramClient(session, api_id, api_hash)

    async def start(self):
        """Start listening."""
        log.info("=" * 60)
        log.info("Signal Listener starting...")
        log.info("Channel: @%s", self.channel)
        log.info("Auto-trade: %s", self.config.get("agent", {}).get("auto_trade", False))
        log.info("=" * 60)

        # Connect WITHOUT an interactive prompt (headless-safe). Then verify
        # the session is actually authorized; if not, fail fast with a clear
        # message instead of crashing on a hidden login prompt.
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is NOT authorized. Set the SESSION_STRING Fly "
                "secret (generate once via `python scripts/gen_session.py`)."
            )
        me = await self.client.get_me()
        log.info("Connected as: %s (ID: %s)", me.first_name, me.id)

        # Announce "Online" ONLY after a successful connection, so a crash
        # during startup can never spam the deployment notification.
        if self.notifier is not None:
            await self.notifier.notify_startup(version=self.version)

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
            elif cmd == "/version":
                await self._handle_version(event)
            elif cmd.startswith("/setport"):
                await self._handle_setport(event)
            elif cmd == "/getport":
                await self._handle_getport(event)

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
        port = self.config.get("risk", {}).get("port_usdt", 1.0)
        await event.reply(
            "📋 **Available Commands**\n\n"
            "  /balance    — Show futures account balance\n"
            "  /positions  — Show all open futures positions\n"
            "  /setport N  — Set margin per trade to $N (e.g. /setport 2)\n"
            "  /getport    — Show current margin per trade\n"
            "  /version    — Show bot version\n"
            "  /help       — Show this message\n\n"
            f"Current port setting: `$ {port:.2f}` per trade\n\n"
            "The bot automatically processes signals from @YOUR_SIGNAL_CHANNEL and\n"
            "executes trades on Binance Futures when conditions are met."
        )

    async def _handle_version(self, event):
        """Handle /version — show bot version."""
        ver = self.version or "unknown"
        await event.reply(
            f"📦 **Crypto Signal Auto-Trade**\n\n"
            f"Version: `{ver}`\n"
            f"Mode: `{'🚀 YOLO auto-trade' if self.config.get('agent', {}).get('auto_trade') else '🔍 Dry run (no trades)'}`\n\n"
            f"_Deployed on Fly.io (Singapore)_"
        )

    async def _handle_setport(self, event):
        """Handle /setport N — change margin per trade to $N."""
        parts = (event.message.text or "").strip().split()
        if len(parts) < 2:
            current = self.config.get("risk", {}).get("port_usdt", 1.0)
            await event.reply(
                f"⚠️ **Usage:** `/setport <value>`\n"
                f"Current port: `$ {current:.2f}` per trade\n\n"
                f"Example: `/setport 2` → use $2 margin per trade"
            )
            return
        try:
            new_port = float(parts[1])
            if new_port <= 0:
                await event.reply("❌ Port must be a positive number (e.g. `/setport 1`).")
                return
            if new_port > 100:
                await event.reply("⚠️ Port > $100 is unusually high. Set it again to confirm.")
                return
            self.config.setdefault("risk", {})["port_usdt"] = new_port
            log.info("Port per trade changed to $%.2f", new_port)
            await event.reply(
                f"✅ **Port updated**\n"
                f"Margin per trade: `$ {new_port:.2f}`\n\n"
                f"Leverage will auto-adjust on the next trade:\n"
                f"  `pos_value / ${new_port:.2f} = lev`"
            )
        except ValueError:
            await event.reply(f"❌ Invalid number: `{parts[1]}`. Use e.g. `/setport 2`.")

    async def _handle_getport(self, event):
        """Handle /getport — show current margin per trade."""
        port = self.config.get("risk", {}).get("port_usdt", 1.0)
        await event.reply(
            f"💰 **Current Margin Per Trade**\n\n"
            f"Port: `$ {port:.2f}`\n\n"
            f"This is the margin budget per trade.\n"
            f"Leverage = position value ÷ ${port:.2f}\n\n"
            f"Change it with `/setport <value>`"
        )
