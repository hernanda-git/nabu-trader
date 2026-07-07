"""Telegram notifier — sends trade notifications via Bot API."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from src.domain.models import ExecutionResult, TradeDecision, TradeSignal

log = logging.getLogger("notifier.telegram")


class TelegramNotifier:
    """Sends formatted trade notifications to a Telegram chat via Bot API."""

    def __init__(self, bot_token: str, chat_id: str | int):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10)
        return self._client

    async def send_message(self, text: str) -> bool:
        """Send a raw message to the configured chat."""
        if not self.bot_token:
            log.info("[No bot token] %s", text[:100])
            return False

        client = await self._get_client()
        resp = await client.post(f"{self._base}/sendMessage", json={
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })
        if resp.status_code == 200:
            return True
        log.error("Telegram send failed: %s", resp.text)
        return False

    async def notify_signal_received(self, signal: TradeSignal):
        """Notify that a signal was received from the channel."""
        preview = signal.raw_text[:200] if signal.raw_text else "(media)"
        text = (
            f"📡 **Signal received from @{signal.channel}**\n\n"
            f"`{preview}`\n\n"
            f"🕐 _{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
        )
        await self.send_message(text)

    async def notify_decision(self, signal: TradeSignal, decision: TradeDecision):
        """Notify the agent's decision."""
        if decision.action == "SKIP":
            text = (
                f"⏭️ **Skipped** `{signal.pair or '?'}`\n"
                f"Reason: {decision.reason}\n"
                f"Confidence: {decision.confidence:.2f}"
            )
        elif decision.action == "CLOSE":
            text = (
                f"🔴 **CLOSE** `{decision.pair}`\n"
                f"Reason: {decision.reason}"
            )
        else:
            emoji = "🟢" if decision.direction == "LONG" else "🔴"
            tp_str = ", ".join(f"TP{i+1}=`{p}`" for i, p in enumerate(decision.tp_prices)) if decision.tp_prices else ""
            sl_str = f"SL=`{decision.sl_price}`" if decision.sl_price else ""
            lev_str = f"⚙️ {decision.leverage}x" if decision.leverage > 1 else ""
            text = (
                f"{emoji} **TRADE — {decision.pair}**\n"
                f"📊 {decision.direction} | {decision.order_type}\n"
                f"💰 Qty: `{decision.quantity:.6f}`\n"
            )
            if lev_str:
                text += f"{lev_str}\n"
            if sl_str:
                text += f"🛑 {sl_str}\n"
            if tp_str:
                text += f"🎯 {tp_str}\n"
            text += f"\n📝 {decision.reason[:200]}"

        await self.send_message(text)

    async def notify_execution(self, signal: TradeSignal, result: ExecutionResult):
        """Notify the result of an order execution."""
        if result.success:
            text = (
                f"✅ **Order filled**\n"
                f"`{result.side}` `{result.symbol}`\n"
                f"Qty: `{result.filled_quantity:.6f}` @ `{result.avg_price:.8f}`\n"
                f"Order: `{result.order_id}`"
            )
        else:
            text = (
                f"❌ **Order failed**\n"
                f"Error: `{result.error}`"
            )
        await self.send_message(text)

    async def notify_startup(self, version: str | None = None):
        """Notify that a new version has been deployed and is running."""
        lines = [
            "🚀 **Crypto Signal Auto-Trade • Online**",
        ]
        if version:
            lines.append(f"📦 Version: `{version}`")
        lines += [
            "",
            "The latest deployment has completed successfully.",
            "🟢 System Status: Operational",
            "⚡️ Ready to execute trades.",
            "",
            "Type / to access the available commands.",
        ]
        await self.send_message("\n".join(lines))

    async def set_commands(self):
        """Register bot slash commands so they appear in Telegram's command menu."""
        if not self.bot_token:
            return
        client = await self._get_client()
        await client.post(
            f"{self._base}/setMyCommands",
            json={
                "commands": [
                    {"command": "balance", "description": "Show futures account balance"},
                    {"command": "positions", "description": "Show all open futures positions"},
                    {"command": "setport", "description": "Set margin $ per trade (lev auto)"},
                    {"command": "getport", "description": "Show current margin per trade"},
                    {"command": "version", "description": "Show bot version"},
                    {"command": "help", "description": "Show available commands"},
                ],
                "scope": {"type": "default"},
            },
        )
