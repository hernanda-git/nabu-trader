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
from src.health.reporter import build_health_report
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
            reply_ctx = await self._resolve_reply_context(msg)
            await self.orchestrator.handle_signal(
                message_id=msg.id,
                channel=self.channel,
                raw_text=text,
                has_media=msg.media is not None,
                reply_to_message_id=reply_ctx.get("reply_to_message_id"),
                reply_pair=reply_ctx.get("reply_pair"),
            )

        @self.client.on(events.MessageEdited(chats=self.channel))
        async def on_edited(event):
            msg = event.message
            text = msg.text or msg.message or ""
            log.info("Edited message #%s", msg.id)
            reply_ctx = await self._resolve_reply_context(msg)
            await self.orchestrator.handle_signal(
                message_id=msg.id,
                channel=self.channel,
                raw_text=text,
                has_media=msg.media is not None,
                reply_to_message_id=reply_ctx.get("reply_to_message_id"),
                reply_pair=reply_ctx.get("reply_pair"),
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
            elif cmd == "/pending":
                await self._handle_pending(event)
            elif cmd.startswith("/cancel"):
                await self._handle_cancel(event)
            elif cmd == "/health":
                await self._handle_health(event)
            elif cmd.startswith("/close"):
                await self._handle_close(event)

        await self.client.run_until_disconnected()

    async def _resolve_reply_context(self, msg) -> dict:
        """Extract reply context so management commands (e.g. 'sl to entry')

        can be attributed to the trade they reply to.

        Returns ``{"reply_to_message_id": int|None, "reply_pair": str|None}``.
        The reply_pair is resolved by re-parsing the replied-to message text
        through the same SymbolRegistry pair resolver used for signals.
        """
        reply_to = getattr(msg, "reply_to", None)
        if not reply_to:
            return {"reply_to_message_id": None, "reply_pair": None}

        reply_id = getattr(reply_to, "reply_to_msg_id", None)
        reply_pair = None
        try:
            original = await msg.get_reply_message()
            if original:
                orig_text = original.text or original.message or ""
                if orig_text:
                    from src.agent.parser import _resolve_pair
                    reply_pair = _resolve_pair(orig_text)
        except Exception as e:  # noqa: BLE001
            log.debug("Failed to resolve reply context: %s", e)

        return {"reply_to_message_id": reply_id, "reply_pair": reply_pair}

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

            # Fetch balance for context
            try:
                bal = await self.exchange.get_balance()
                total_balance = bal.total_usdt
                free_balance = bal.free_usdt
            except Exception:
                total_balance = 0
                free_balance = 0

            # Fetch current prices for all position symbols in one call
            price_map: dict[str, float] = {}
            for p in positions:
                try:
                    price = await self.exchange.get_mark_price(p.symbol)
                    if price and price > 0:
                        price_map[p.symbol] = price
                except Exception:
                    pass

            # Build the message
            total_margin = 0.0
            total_pnl = 0.0
            lines = ["💼 **Open Positions**\n"]

            for i, p in enumerate(positions, 1):
                current_price = price_map.get(p.symbol, p.mark_price)
                direction_emoji = "🟢" if p.direction == "LONG" else "🔴"
                direction_label = "LONG ▲" if p.direction == "LONG" else "SHORT ▼"

                # Calculate margin in use
                margin_used = p.margin if p.margin > 0 else (p.notional / p.leverage if p.leverage > 0 else 0)
                total_margin += margin_used
                total_pnl += p.unrealized_pnl

                # PnL styling
                pnl_sign = "+" if p.unrealized_pnl >= 0 else ""
                pnl_emoji = "🟢" if p.unrealized_pnl >= 0 else "🔴"

                # Price change %
                if p.entry_price > 0 and current_price > 0:
                    if p.direction == "LONG":
                        pct_change = ((current_price - p.entry_price) / p.entry_price) * 100
                    else:
                        pct_change = ((p.entry_price - current_price) / p.entry_price) * 100
                    pct_str = f"{pnl_sign}{pct_change:.2f}%"
                else:
                    pct_str = "—"

                # Format price with appropriate precision
                def fmt_price(val):
                    if val <= 0:
                        return "—"
                    if val < 0.001:
                        return f"{val:.8f}"
                    if val < 1:
                        return f"{val:.6f}"
                    return f"{val:.4f}"

                lines.append(
                    f"{direction_emoji} **{p.symbol}** · `{direction_label}`\n"
                    f"   Entry `{fmt_price(p.entry_price)}` → Now `{fmt_price(current_price)}` ({pct_str})\n"
                    f"   Size `{p.size:,.0f}` · Lev `{p.leverage}x` · Margin `${margin_used:.2f}`\n"
                    f"   {pnl_emoji} PnL `${pnl_sign}{p.unrealized_pnl:.2f}`"
                )

            # Footer with account summary
            used_pct = (total_margin / total_balance * 100) if total_balance > 0 else 0
            lines.append("")
            lines.append(
                f"💰 Balance `${total_balance:.2f}` · "
                f"Free `${free_balance:.2f}` · "
                f"In use `${total_margin:.2f}` ({used_pct:.0f}%)"
            )
            if total_pnl != 0:
                tp = "+" if total_pnl >= 0 else ""
                te = "🟢" if total_pnl >= 0 else "🔴"
                lines.append(f"{te} Total PnL `${tp}{total_pnl:.2f}`")

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
            "  /pending    — List pending conditional signals\n"
            "  /cancel <id> — Cancel a pending signal\n"
            "  /cancel all — Cancel all pending signals\n"
            "  /health     — Run full system health check\n"
            "  /setport N  — Set margin per trade to $N\n"
            "  /getport    — Show current margin per trade\n"
            "  /version    — Show bot version\n"
            "  /help       — Show this message\n\n"
            "  /close <PAIR> — Immediately market-close an active trade (cancels its SL/TP)\n\n"
            f"Current port setting: `$ {port:.2f}` per trade\n\n"
            "The bot automatically processes signals from @YOUR_SIGNAL_CHANNEL and\n"
            "executes trades on Binance Futures when conditions are met."
        )

    async def _handle_health(self, event):
        """Handle /health — run a full system health check across all subsystems."""
        log.info("Command: /health")
        lines, n_ok, n_fail = await build_health_report(self)
        overall = "✅ ALL SYSTEMS OK" if n_fail == 0 else f"⚠️ {n_fail} ISSUE(S)"
        header = f"🩺 **Health Check** — {overall}"
        # build_health_report already prepends the header line; just reply
        await event.reply("\n".join(lines))

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

    async def _handle_pending(self, event):
        """Handle /pending — list all pending conditional signals."""
        psr = getattr(self.orchestrator, 'pending_signal_repo', None)
        if not psr:
            await event.reply("❌ Pending signal repository not configured.")
            return
        log.info("Command: /pending")
        try:
            pending = psr.get_pending()
            if not pending:
                await event.reply("📭 **No pending conditions**\n\nNo conditional signals waiting to trigger.")
                return
            lines = ["⏳ **Pending Conditional Signals**\n"]
            for ps in pending:
                emoji = "🟢" if ps.direction == "LONG" else "🔴"
                cond = "close >" if ps.condition_type == "close_above" else "close <"
                lines.append(
                    f"{emoji} **#{ps.id} — {ps.pair}**\n"
                    f"   ├ Direction: `{ps.direction}`\n"
                    f"   ├ Condition: `{cond}` `{ps.trigger_price:.8f}`\n"
                    f"   ├ Timeframe: `{ps.timeframe}`\n"
                    f"   └ Created: `{ps.created_at}`\n"
                )
            lines.append(f"Cancel: `/cancel <id>` or `/cancel all`")
            await event.reply("\n".join(lines))
        except Exception as e:
            log.exception("Failed to fetch pending signals")
            await event.reply(f"❌ **Error:** `{e}`")

    async def _handle_cancel(self, event):
        """Handle /cancel <id> or /cancel all — cancel pending signals."""
        psr = getattr(self.orchestrator, 'pending_signal_repo', None)
        if not psr:
            await event.reply("❌ Pending signal repository not configured.")
            return
        parts = (event.message.text or "").strip().split()
        if len(parts) < 2:
            await event.reply(
                "⚠️ **Usage:**\n"
                "  `/cancel <id>` — cancel a specific signal\n"
                "  `/cancel all` — cancel all pending signals"
            )
            return
        target = parts[1].lower()
        log.info("Command: /cancel %s", target)
        try:
            if target == "all":
                count = psr.cancel_all()
                await event.reply(f"✅ **Cancelled {count}** pending signal(s)." if count else "📭 No pending signals to cancel.")
            else:
                signal_id = int(target)
                ok = psr.cancel(signal_id)
                if ok:
                    await event.reply(f"✅ **Cancelled signal #{signal_id}**")
                else:
                    await event.reply(f"⚠️ Signal #{signal_id} not found or already processed.")
        except ValueError:
            await event.reply(f"❌ Invalid ID: `{parts[1]}`. Use `/cancel <id>` or `/cancel all`.")
        except Exception as e:
            log.exception("Failed to cancel signal")
            await event.reply(f"❌ **Error:** `{e}`")

    async def _handle_close(self, event):
        """Handle /close <PAIR> — manually close an active trade.

        Accepts a full pair (ENAUSDT), a bare base (#ENA / ENA), or a quoted
        pair. Cancels resting SL/TP orders, closes the position at market, and
        replies with the result (fill price, size, realised PnL).
        """
        if not self.exchange:
            await event.reply("❌ No exchange configured.")
            return
        parts = (event.message.text or "").strip().split()
        if len(parts) < 2:
            await event.reply(
                "⚠️ **Usage:** `/close <pair>`\n\n"
                "Examples:\n"
                "  `/close ENAUSDT` — close the ENAUSDT position\n"
                "  `/close ENA`     — same (auto-appends USDT)\n"
                "  `/close #ENA`    — same\n\n"
                "This immediately market-closes the position and cancels its\n"
                "resting SL/TP orders."
            )
            return
        symbol = parts[1].strip()
        log.info("Command: /close %s", symbol)

        pm = getattr(self.orchestrator, "position_manager", None)
        if pm is None:
            await event.reply("❌ Position manager not available.")
            return
        try:
            res = await pm.close_position_by_symbol(symbol, reason="Manual close (/close command)")
        except Exception as e:
            log.exception("Failed to close %s", symbol)
            await event.reply(f"❌ **Error closing {symbol}:** `{e}`")
            return

        if not res["ok"]:
            await event.reply(
                f"⚠️ **Could not close `{res['symbol']}`**\n\n"
                f"_{res['error']}_"
            )
            return

        side_emoji = "🟢" if res["side"] == "LONG" else "🔴"
        pnl = res["pnl"]
        pnl_str = f"`{pnl:+.2f}`" if pnl is not None else "—"
        pnl_emoji = "🟢" if (pnl or 0) >= 0 else "🔴"
        await event.reply(
            f"✅ **Position closed**\n\n"
            f"{side_emoji} `{res['symbol']}` `{res['side']}`\n"
            f"   Size: `{res['size']:,.4f}`\n"
            f"   {pnl_emoji} Realised PnL: `{pnl_str}`\n\n"
            f"_Closed manually via /close command._"
        )
