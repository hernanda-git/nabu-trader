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
from src.config.loader import get_session_dir, DEFAULT_CONFIG_PATH
from src.health.reporter import build_health_report
from src.orchestrator import TradeOrchestrator
from src.state.database import get_connection
from src.state.db_admin import run_db_command

log = logging.getLogger("listener")


def normalize_pair(raw: str) -> str:
    """Normalize a user-supplied pair to a full futures symbol.

    Accepts a bare base (``BTC``), a base with ``#``/``$`` prefix
    (``#BTC``), or an already-quoted pair (``BTCUSDT``). Appends ``USDT``
    for bare bases, using the live SymbolRegistry when available (so e.g.
    ``PEPE`` → ``1000PEPEUSDT`` resolves correctly). Returns ``""`` for
    empty input.
    """
    s = (raw or "").strip().upper().lstrip("#").lstrip("$").strip()
    if not s:
        return ""
    if s.endswith(("USDT", "USD", "BUSD", "USDC")):
        return s
    try:
        from src.exchange.symbol_registry import get_registry
    except ImportError:
        get_registry = None
    registry = get_registry() if get_registry else None
    if registry and registry.is_ready:
        resolved, _ = registry.resolve(s)
        if resolved:
            return resolved
    return f"{s}USDT"


def parse_positions_add(args: list[str]) -> dict:
    """Parse ``/positions add`` arguments into a structured, validated plan.

    Pure and side-effect-free so it can be unit-tested without Telegram or
    an exchange connection.

    Accepted grammar (the optional side may be omitted — it is then
    inferred from the limit price vs. market):

        /positions add [LONG|SHORT] <pair> <margin_usdt> <leverage> \\
                       <price|market> [tp] [sl]

    Returns ``{"ok": True, "side", "pair", "margin_usdt", "leverage",
    "price", "market", "tp", "sl"}`` on success, or
    ``{"ok": False, "error": <str>}`` on any parse/validation failure.
    ``side`` is ``None`` when it must be inferred at runtime; ``price`` is
    ``None`` for a ``market`` entry; ``tp``/``sl`` are ``None`` when omitted.
    """
    if not args:
        return {"ok": False, "error": "missing arguments"}
    toks = [a for a in args if a != ""]
    # Optional leading side token.
    side: str | None = None
    if toks and toks[0].upper() in ("LONG", "SHORT"):
        side = toks[0].upper()
        toks = toks[1:]
    if len(toks) < 4:
        return {
            "ok": False,
            "error": (
                "expected: add [LONG|SHORT] <pair> <margin> <leverage> "
                "<price|market> [tp] [sl]"
            ),
        }
    pair_raw, margin_tok, lev_tok, price_tok = toks[0], toks[1], toks[2], toks[3]
    opt = toks[4:]

    pair = normalize_pair(pair_raw)
    if not pair:
        return {"ok": False, "error": f"invalid pair: {pair_raw!r}"}

    # Margin (USDT) — positive, with a sane upper guard against fat-fingers.
    try:
        margin = float(margin_tok)
    except ValueError:
        return {"ok": False, "error": f"invalid margin: {margin_tok!r} (expected a number)"}
    if margin <= 0:
        return {"ok": False, "error": "margin must be > 0"}
    if margin > 100_000:
        return {"ok": False, "error": "margin > $100k rejected (use a smaller value)"}

    # Leverage.
    try:
        leverage = int(float(lev_tok))
    except ValueError:
        return {"ok": False, "error": f"invalid leverage: {lev_tok!r} (expected an integer)"}
    if leverage < 1:
        return {"ok": False, "error": "leverage must be >= 1"}
    if leverage > 125:
        return {"ok": False, "error": "leverage must be <= 125 (Binance max)"}

    # Entry price or market.
    is_market = price_tok.lower() == "market"
    price: float | None = None
    if not is_market:
        try:
            price = float(price_tok)
        except ValueError:
            return {
                "ok": False,
                "error": f"invalid price: {price_tok!r} (expected a number or 'market')",
            }
        if price <= 0:
            return {"ok": False, "error": "price must be > 0"}

    # Optional TP / SL.
    tp: float | None = None
    sl: float | None = None
    if len(opt) >= 1:
        try:
            tp = float(opt[0])
        except ValueError:
            return {"ok": False, "error": f"invalid tp: {opt[0]!r} (expected a number)"}
        if tp <= 0:
            return {"ok": False, "error": "tp must be > 0"}
    if len(opt) >= 2:
        try:
            sl = float(opt[1])
        except ValueError:
            return {"ok": False, "error": f"invalid sl: {opt[1]!r} (expected a number)"}
        if sl <= 0:
            return {"ok": False, "error": "sl must be > 0"}

    return {
        "ok": True,
        "side": side,
        "pair": pair,
        "margin_usdt": margin,
        "leverage": leverage,
        "price": price,
        "market": is_market,
        "tp": tp,
        "sl": sl,
    }


def fmt_price(val: float) -> str:
    """Format a price with sensible precision for display."""
    if val <= 0:
        return "—"
    if val < 0.001:
        return f"{val:.8f}"
    if val < 1:
        return f"{val:.6f}"
    return f"{val:.4f}"


class SignalListener:
    """Telethon-based listener that forwards messages to the orchestrator."""

    def __init__(self, orchestrator: TradeOrchestrator, config: dict, exchange: Exchange | None = None, version: str | None = None, notifier: "TelegramNotifier | None" = None, config_path: "str | Path | None" = None):
        self.orchestrator = orchestrator
        self.config = config
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
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
            elif cmd.startswith("/setmargintype"):
                await self._handle_setmargintype(event)
            elif cmd == "/getmargintype":
                await self._handle_getmargintype(event)
            elif cmd.startswith("/setleverage"):
                await self._handle_setleverage(event)
            elif cmd == "/leverage":
                await self._handle_getleverage(event)
            elif cmd == "/pending":
                await self._handle_pending(event)
            elif cmd.startswith("/cancel"):
                await self._handle_cancel(event)
            elif cmd == "/health":
                await self._handle_health(event)
            elif cmd.startswith("/close"):
                await self._handle_close(event)
            elif cmd.startswith("/db"):
                await self._handle_db(event)
            elif cmd.startswith("/check"):
                await self._handle_check(event)

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

    async def _handle_check(self, event):
        """Handle /check <pair> — show current price + 24h stats for a pair.

        Usage:
            /check btcusdt
            /check #ETH
            /check pepe           (auto-resolves to 1000PEPEUSDT)
        """
        if not self.exchange:
            await event.reply("❌ No exchange configured.")
            return
        parts = (event.message.text or "").strip().split()
        if len(parts) < 2:
            await event.reply(
                "⚠️ **Usage:** `/check <pair>`\n\n"
                "Examples:\n"
                "  `/check btcusdt`\n"
                "  `/check #eth`\n"
                "  `/check pepe`\n\n"
                "Shows the current mark price, 24h change, high/low and volume."
            )
            return
        raw = parts[1]
        symbol = normalize_pair(raw)
        log.info("Command: /check %s → %s", raw, symbol)
        try:
            ticker = await self.exchange.get_ticker(symbol)
        except Exception as e:
            log.exception("Failed to fetch ticker for %s", symbol)
            await event.reply(f"❌ **Error fetching {symbol}:** `{e}`")
            return
        if ticker is None:
            await event.reply(f"❌ Price feed not available for `{symbol}`.")
            return
        if ticker.error:
            await event.reply(
                f"❌ **Could not fetch `{symbol}`**\n`{ticker.error}`"
            )
            return

        change = ticker.change_pct_24h
        up = change >= 0
        emoji = "🟢" if up else "🔴"
        sign = "+" if up else ""
        await event.reply(
            f"💹 **{symbol}**\n\n"
            f"   ├ Last: `{fmt_price(ticker.last_price)}`\n"
            f"   ├ Mark: `{fmt_price(ticker.mark_price)}`\n"
            f"   ├ 24h: {emoji} `{sign}{change:.2f}%`\n"
            f"   ├ High: `{fmt_price(ticker.high_24h)}`\n"
            f"   ├ Low:  `{fmt_price(ticker.low_24h)}`\n"
            f"   └ Vol:  `${ticker.volume_24h:,.0f}` (24h)"
        )

    async def _handle_positions_add(self, event):
        """Handle ``/positions add ...`` — open a new futures position.

        Grammar:

            /positions add [LONG|SHORT] <pair> <margin_usdt> <leverage> \\
                           <price|market> [tp] [sl]

        - Side is required when using ``market`` (a market order needs to know
          whether to buy or sell). For a limit ``price`` the side is *inferred*
          from the book: a buy-limit below market = LONG, a sell-limit above
          market = SHORT (rejected if the price is on the wrong side of market).
        - Margin is the USDT margin budget; position notional = margin × leverage.
        - On a market fill, SL/TP conditional orders are attached automatically.
        - A resting limit order is placed and left on the book (no DB position
          until it fills) so the position manager never spuriously closes it.
        """
        if not self.exchange:
            await event.reply("❌ No exchange configured.")
            return
        # text after "/positions"
        rest = (event.message.text or "")[len("/positions"):].strip()
        toks = rest.split()
        # First token MUST be "add".
        if not toks or toks[0].lower() != "add":
            await event.reply(
                "⚠️ **Usage:** `/positions add [LONG|SHORT] <pair> <margin> "
                "<leverage> <price|market> [tp] [sl]`\n\n"
                "Examples:\n"
                "  `/positions add btcusdt 10 20 60000 65000 58000`\n"
                "  `/positions add LONG ethusdt 5 10 market 3500 3200`\n"
                "  `/positions add pepe 5 20 0.000012 market`"
            )
            return

        plan = parse_positions_add(toks[1:])
        if not plan["ok"]:
            await event.reply(f"⚠️ **Invalid command:** {plan['error']}")
            return

        symbol = plan["pair"]
        margin = plan["margin_usdt"]
        leverage = plan["leverage"]
        is_market = plan["market"]
        price = plan["price"]
        log.info(
            "Command: /positions add %s margin=$%.2f lev=%dx market=%s price=%s",
            symbol, margin, leverage, is_market, price,
        )

        # ── Resolve side (explicit, or inferred from limit price vs market) ──
        side = plan["side"]
        mark_price = None
        if side is None:
            if is_market:
                await event.reply(
                    "⚠️ **Side required for a market entry.**\n"
                    "Use LONG or SHORT, e.g. `/positions add LONG btcusdt 10 20 market`."
                )
                return
            # Limit order — infer side from price vs. current market.
            try:
                mark_price = await self.exchange.get_mark_price(symbol)
            except Exception as e:
                log.warning("Could not fetch mark price for %s: %s", symbol, e)
            if not mark_price or mark_price <= 0:
                await event.reply(
                    f"⚠️ **Cannot infer side for {symbol}** — no market price "
                    f"available. Use an explicit LONG/SHORT side."
                )
                return
            if price < mark_price:
                side = "LONG"        # buy the dip
            elif price > mark_price:
                side = "SHORT"       # sell the rip
            else:
                await event.reply(
                    f"⚠️ Limit price `{fmt_price(price)}` equals mark "
                    f"`{fmt_price(mark_price)}` — use `market` or a clearly "
                    f"different limit price."
                )
                return

        exchange_side = "BUY" if side == "LONG" else "SELL"
        notional = margin * leverage
        # Position size in base asset = notional / entry price.
        entry_ref = price if not is_market else (mark_price or 0)
        if entry_ref <= 0:
            try:
                entry_ref = await self.exchange.get_mark_price(symbol) or 0
            except Exception:
                entry_ref = 0
        if entry_ref <= 0:
            await event.reply(
                f"❌ **Cannot size {symbol}** — no reference price available."
            )
            return
        quantity = notional / entry_ref

        # ── Pre-flight: symbol readiness + balance ────────────────────────
        if not is_market:
            if not await self.exchange._preflight_symbol(symbol):  # noqa: SLF001
                await event.reply(
                    f"❌ **{symbol}** is not available for futures trading."
                )
                return
        try:
            bal = await self.exchange.get_balance()
            free = bal.free_usdt
        except Exception:
            free = None
        if free is not None and margin > free:
            await event.reply(
                f"❌ **Insufficient margin:** need `${margin:.2f}` but only "
                f"`${free:.2f}` free. Reduce margin or leverage."
            )
            return

        # ── Set leverage + margin type, then place the entry order ──────────
        try:
            if getattr(self.exchange, "futures", False):
                await self.exchange.set_symbol_leverage(symbol, leverage)
                margin_type = self.config.get("risk", {}).get("margin_type", "ISOLATED")
                await self.exchange.set_margin_type(symbol, margin_type)

            entry_order: OrderInfo
            if is_market:
                entry_order = (
                    await self.exchange.market_buy(symbol, quantity)
                    if exchange_side == "BUY"
                    else await self.exchange.market_sell(symbol, quantity)
                )
            else:
                entry_order = (
                    await self.exchange.limit_buy(symbol, quantity, price)
                    if exchange_side == "BUY"
                    else await self.exchange.limit_sell(symbol, quantity, price)
                )
        except Exception as e:
            log.exception("Failed to place entry order for %s", symbol)
            await event.reply(
                f"❌ **Failed to place order — {symbol}**\n`{e}`"
            )
            return

        if entry_order.status in ("FAILED", "REJECTED", "EXPIRED"):
            await event.reply(
                f"❌ **Order rejected — {symbol}**\n"
                f"```{entry_order.error or 'unknown error'}```"
            )
            return

        # ── Handle outcomes ───────────────────────────────────────────────
        if is_market or entry_order.status == "FILLED":
            # Filled (market, or limit that immediately filled).
            fill_price = entry_order.avg_price or entry_order.price or entry_ref
            filled_qty = entry_order.filled_quantity or quantity

            # Attach SL/TP conditional orders (mirrors the signal pipeline).
            tp_placed = sl_placed = False
            if plan["sl"]:
                sl_side = "SELL" if side == "LONG" else "BUY"
                sl_o = await self.exchange.stop_loss(symbol, filled_qty, plan["sl"], sl_side)
                sl_placed = bool(sl_o.order_id)
            if plan["tp"]:
                tp_side = "SELL" if side == "LONG" else "BUY"
                tp_o = await self.exchange.take_profit(symbol, filled_qty, plan["tp"], tp_side)
                if not tp_o.order_id:
                    # Some contracts block conditional TP — fall back to Basic LIMIT.
                    if tp_side == "SELL":
                        tp_o = await self.exchange.limit_sell(symbol, filled_qty, plan["tp"], reduce=True)
                    else:
                        tp_o = await self.exchange.limit_buy(symbol, filled_qty, plan["tp"], reduce=True)
                tp_placed = bool(tp_o.order_id)

            # Register the position in the local DB so /close and /positions track it.
            pm = getattr(self.orchestrator, "position_repo", None)
            if pm is not None:
                try:
                    from src.domain.models import Position
                    pm.create(Position(
                        pair=symbol,
                        direction=side,
                        entry_price=fill_price,
                        quantity=filled_qty,
                        sl_price=plan["sl"],
                        tp_prices=[plan["tp"]] if plan["tp"] else [],
                        entry_order_id=entry_order.order_id,
                    ))
                    log.info("Registered position %s in DB (manual entry)", symbol)
                except Exception as e:
                    log.warning("DB position registration failed for %s: %s", symbol, e)

            await event.reply(
                f"✅ **Position opened — {symbol}**\n"
                f"   ├ Side: `{side}`\n"
                f"   ├ Entry: `{fmt_price(fill_price)}`\n"
                f"   ├ Size: `{filled_qty:,.4f}`\n"
                f"   ├ Margin: `${margin:.2f}` @ `{leverage}x`\n"
                f"   ├ Notional: `${notional:,.2f}`\n"
                + (f"   ├ 🛡 SL: `{fmt_price(plan['sl'])}`\n" if plan["sl"] else "")
                + (f"   └ 🎯 TP: `{fmt_price(plan['tp'])}`\n" if plan["tp"] else "")
                + f"   └ Order: `{entry_order.order_id}`"
            )
            return

        # Resting limit order (not yet filled) — leave it on the book.
        await event.reply(
            f"⏳ **Limit order placed — {symbol}**\n"
            f"   ├ Side (inferred): `{side}`\n"
            f"   ├ Limit: `{fmt_price(price)}`\n"
            f"   ├ Size: `{quantity:,.4f}`\n"
            f"   ├ Margin: `${margin:.2f}` @ `{leverage}x`\n"
            f"   └ Order: `{entry_order.order_id}`\n\n"
            f"_Waiting for price to reach the limit. It will fill automatically; "
            f"no position is recorded until then._"
        )

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
            "  /check <pair> — Show current price + 24h stats for a pair\n"
            "  /balance    — Show futures account balance\n"
            "  /positions  — Show all open futures positions\n"
            "  /positions add [LONG|SHORT] <pair> <margin> <lev> <price|market> [tp] [sl] — Open a position\n"
            "  /pending    — List pending conditional signals\n"
            "  /cancel <id> — Cancel a pending signal\n"
            "  /cancel all — Cancel all pending signals\n"
            "  /health     — Run full system health check\n"
            "  /setport N  — Set margin per trade to $N\n"
            "  /getport    — Show current margin per trade\n"
            "  /setmargintype <isolated|cross> — Set margin mode for new trades\n"
            "  /getmargintype — Show current margin mode (isolated/cross)\n"
            "  /setleverage <N> — Set default leverage ceiling (pair max clamps)\n"
            "  /leverage — Show current default leverage ceiling\n"
            "  /version    — Show bot version\n"
            "  /help       — Show this message\n\n"
            "  /close <PAIR> — Immediately market-close an active trade (cancels its SL/TP)\n\n"
            "  /db <cmd> — Browse/edit the trade DB. Subcommands:\n"
            "    /db tables — list tables + row counts\n"
            "    /db list <table> [page] — page through rows (10/page)\n"
            "    /db get <table> <id> — one row by primary key\n"
            "    /db delete <table> <id> — remove a row (needs `!` confirm)\n"
            "    /db update <table> <id> <col>=<val> [...] — edit (needs `!`)\n"
            "    /db insert <table> (<col>=<val>, ...) — add (needs `!`)\n\n"
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
        """Handle /version — show bot version (plain integer)."""
        ver = self.version or "0"
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

    async def _handle_getmargintype(self, event):
        """Handle /getmargintype — show current margin mode (isolated/cross)."""
        raw = self.config.get("risk", {}).get("margin_type", "ISOLATED")
        mode = self._normalize_margin_type(raw)
        emoji = "🔗" if mode == "CROSSED" else "🧱"
        label = "CROSS" if mode == "CROSSED" else "ISOLATED"
        await event.reply(
            f"{emoji} **Margin Mode**\n\n"
            f"Current: `{label}`\n\n"
            f"New positions use this margin type on Binance Futures.\n"
            f"Change it with `/setmargintype isolated` or `/setmargintype cross`."
        )

    async def _handle_setmargintype(self, event):
        """Handle /setmargintype <isolated|cross> — change margin mode for new trades."""
        parts = (event.message.text or "").strip().split()
        current = self._normalize_margin_type(
            self.config.get("risk", {}).get("margin_type", "ISOLATED")
        )
        if len(parts) < 2:
            await event.reply(
                f"⚠️ **Usage:** `/setmargintype <isolated|cross>`\n"
                f"Current margin mode: `{current}`\n\n"
                f"Example: `/setmargintype cross` → new positions use cross margin"
            )
            return
        arg = parts[1].strip().lower()
        if arg in ("isolated", "iso", "i"):
            new_mode = "ISOLATED"
            emoji = "🧱"
        elif arg in ("cross", "crossed", "c"):
            new_mode = "CROSSED"
            emoji = "🔗"
        else:
            await event.reply(
                f"❌ Invalid margin mode: `{arg}`.\n"
                f"Use `/setmargintype isolated` or `/setmargintype cross`."
            )
            return
        self.config.setdefault("risk", {})["margin_type"] = new_mode
        log.info("Margin type changed to %s", new_mode)
        # Persist to config.yaml so the setting survives a restart/deploy.
        persisted = self._persist_margin_type(new_mode)
        applied = "CROSS" if new_mode == "CROSSED" else "ISOLATED"
        note = "" if persisted else "\n⚠️ (config.yaml not written — change is in-memory only for this session)"
        await event.reply(
            f"{emoji} **Margin Mode Updated**\n\n"
            f"New positions will use `{applied}` margin.\n\n"
            f"⚠️ Existing open positions keep their original margin type until closed."
            f"{note}"
        )

    def _persist_margin_type(self, margin_type: str) -> bool:
        """Rewrite the `margin_type:` line in config.yaml in place.

        Preserves every other line (comments, indentation) and only swaps the
        value, so a restart/deploy reloads the same margin mode. Returns True
        if the file was updated, False if it couldn't be (non-fatal).
        """
        path = self.config_path
        try:
            if not path or not Path(path).exists():
                log.warning("config.yaml not found at %s — skipping persist", path)
                return False
            text = Path(path).read_text(encoding="utf-8")
            new_lines = []
            found = False
            for line in text.splitlines():
                # Match the top-level `margin_type:` under the risk: block.
                # Only touch an unindented (top-level) key to avoid any nested
                # key that might coincidentally be named margin_type.
                stripped = line.lstrip()
                if not found and stripped.startswith("margin_type:") and not line.startswith((" ", "\t")):
                    indent = line[: len(line) - len(stripped)]
                    new_lines.append(f"{indent}margin_type: {margin_type}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                log.warning("margin_type key not found in %s — skipping persist", path)
                return False
            Path(path).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            log.info("Persisted margin_type=%s to %s", margin_type, path)
            return True
        except Exception as e:  # noqa: BLE001 — persistence is best-effort
            log.warning("Failed to persist margin_type to %s: %s", path, e)
            return False


    def _normalize_margin_type(self, raw: str) -> str:
        """Normalize any accepted margin-type spelling to the Binance enum."""
        r = (raw or "").strip().upper()
        if r in ("CROSSED", "CROSS"):
            return "CROSSED"
        return "ISOLATED"

    async def _handle_getleverage(self, event):
        """Handle /leverage — show current default leverage ceiling."""
        lev = self.config.get("risk", {}).get("max_leverage", 20)
        await event.reply(
            f"⚙️ **Default Leverage Ceiling**\n\n"
            f"`{lev}x`\n\n"
            f"This is the bot's default leverage ceiling for new trades.\n"
            f"Each trade also respects the pair's own Binance max leverage — "
            f"whichever is lower wins.\n\n"
            f"Change it with `/setleverage <value>` (e.g. `/setleverage 10`)."
        )

    async def _handle_setleverage(self, event):
        """Handle /setleverage N — change the default leverage ceiling for new trades."""
        parts = (event.message.text or "").strip().split()
        current = self.config.get("risk", {}).get("max_leverage", 20)
        if len(parts) < 2:
            await event.reply(
                f"⚠️ **Usage:** `/setleverage <value>`\n"
                f"Current default leverage ceiling: `{current}x`\n\n"
                f"Example: `/setleverage 10` → new trades use up to 10x\n"
                f"(each trade is still clamped to the pair's Binance max)"
            )
            return
        try:
            new_lev = int(float(parts[1]))
        except ValueError:
            await event.reply(f"❌ Invalid number: `{parts[1]}`. Use e.g. `/setleverage 20`.")
            return
        if new_lev < 1:
            await event.reply("❌ Leverage must be ≥ 1.")
            return
        if new_lev > 125:
            await event.reply("❌ Leverage cannot exceed 125x (Binance hard limit).")
            return
        self.config.setdefault("risk", {})["max_leverage"] = new_lev
        log.info("Default leverage ceiling changed to %dx", new_lev)
        # Persist to config.yaml so the setting survives a restart/deploy.
        persisted = self._persist_leverage(new_lev)
        note = "" if persisted else "\n⚠️ (config.yaml not written — change is in-memory only for this session)"
        await event.reply(
            f"⚙️ **Default Leverage Updated**\n\n"
            f"New trades use up to `{new_lev}x` (clamped to each pair's Binance max).\n"
            f"Effective leverage per trade is still computed from risk sizing and "
            f"snapped to the nearest valid Binance step."
            f"{note}"
        )

    def _persist_leverage(self, leverage: int) -> bool:
        """Rewrite the `max_leverage:` line in config.yaml in place (best-effort)."""
        path = self.config_path
        try:
            if not path or not Path(path).exists():
                log.warning("config.yaml not found at %s — skipping leverage persist", path)
                return False
            text = Path(path).read_text(encoding="utf-8")
            new_lines = []
            found = False
            for line in text.splitlines():
                stripped = line.lstrip()
                if not found and stripped.startswith("max_leverage:") and not line.startswith((" ", "\t")):
                    indent = line[: len(line) - len(stripped)]
                    new_lines.append(f"{indent}max_leverage: {leverage}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                log.warning("max_leverage key not found in %s — skipping persist", path)
                return False
            Path(path).write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            log.info("Persisted max_leverage=%d to %s", leverage, path)
            return True
        except Exception as e:  # noqa: BLE001 — persistence is best-effort
            log.warning("Failed to persist max_leverage to %s: %s", path, e)
            return False

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

    async def _handle_db(self, event):
        """Handle /db ... — browse and edit the trade SQLite DB.

        Subcommands mirror a tiny SQL REPL but are safe by design: reads are
        free, writes (delete/update/insert) require a confirmation pass. The
        actual command logic lives in `src/state/db_admin.run_db_command` so it
        is unit-testable without Telegram.
        """
        raw = (event.message.text or "")[len("/db"):]
        log.info("Command: /db %s", raw.strip())
        try:
            conn = get_connection()
            reply = run_db_command(conn, raw)
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to run /db command")
            reply = f"❌ **Error:** `{e}`"
        await event.reply(reply)
