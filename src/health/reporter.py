"""Health reporter — periodic full-system health check posted to Telegram.

Reuses the exact same checks as the ``/health`` slash command so the
scheduled report and the on-demand command always agree. Runs a background
loop (default every 6 hours) and posts a status summary to the notify chat.

Also exposes :func:`build_health_report` so the listener's ``/health``
command can render an identical report.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("health.reporter")


async def build_health_report(listener: Any) -> tuple[list[str], int, int]:
    """Run all subsystem health checks and return ``(lines, n_ok, n_fail)``.

    ``listener`` must expose: ``notifier``, ``client``, ``channel``,
    ``orchestrator`` (with ``.agent``), and ``exchange`` — the same object
    that owns the ``/health`` command handler.
    """
    t0 = time.monotonic()
    checks: list[tuple[str, str, str]] = []  # (label, status, detail)

    # ── 1. Telegram Bot API (token) ───────────────────────────────
    if listener.notifier is not None:
        bot = await listener.notifier.check_connection()
        if bot["status"] == "ok":
            checks.append(("🤖 Telegram Bot", "OK", f"@{bot.get('bot', '')}"))
        else:
            checks.append(("🤖 Telegram Bot", "FAIL", bot.get("error", "")))
    else:
        checks.append(("🤖 Telegram Bot", "FAIL", "notifier not configured"))

    # ── 2. Telethon user session + channel access ─────────────────
    try:
        authorized = await listener.client.is_user_authorized()
        if not authorized:
            checks.append(("👤 Telethon Session", "FAIL", "user not authorized"))
        else:
            me = await listener.client.get_me()
            ch = await listener.client.get_entity(listener.channel)
            checks.append((
                "👤 Telethon Session", "OK",
                f"{getattr(me, 'first_name', '?')} → @{getattr(ch, 'username', '?')}",
            ))
    except Exception as e:  # noqa: BLE001
        checks.append(("👤 Telethon Session", "FAIL", f"{type(e).__name__}: {e}"))

    # ── 3. LLM provider (OpenCode Go) ────────────────────────────
    agent = getattr(listener.orchestrator, "agent", None)
    if agent is not None and hasattr(agent, "ping"):
        llm = await agent.ping()
        if llm["status"] == "ok":
            checks.append((
                "🧠 LLM Provider", "OK",
                f"{llm.get('model')} ({llm.get('latency_ms', 0)}ms)",
            ))
        else:
            checks.append((
                "🧠 LLM Provider", "FAIL",
                f"{llm.get('error', '')} [{llm.get('url', '')}]",
            ))
    else:
        checks.append(("🧠 LLM Provider", "FAIL", "agent not available"))

    # ── 4. Exchange connection ────────────────────────────────────
    if listener.exchange is not None:
        try:
            bal = await listener.exchange.get_balance()
            checks.append((
                "💱 Exchange", "OK",
                f"bal ${getattr(bal, 'total_usdt', 0):.2f} USDT",
            ))
        except Exception as e:  # noqa: BLE001
            checks.append(("💱 Exchange", "FAIL", f"{type(e).__name__}: {e}"))
    else:
        checks.append(("💱 Exchange", "FAIL", "no exchange configured"))

    # ── 4b. Portfolio — open positions + margin-per-trade (port) ─────
    cfg = getattr(getattr(listener, "orchestrator", None), "config", None) \
        or getattr(listener, "config", None) or {}
    port_usdt = cfg.get("risk", {}).get("port_usdt", None)
    port_detail = f"margin/trade ${port_usdt:.2f}" if isinstance(port_usdt, (int, float)) else "margin/trade unset"
    n_open = 0
    try:
        if listener.exchange is not None:
            positions = await listener.exchange.get_positions()
            n_open = len(positions) if positions else 0
    except Exception:  # noqa: BLE001 — portfolio count is informational
        pass
    checks.append(("💼 Portfolio", "OK", f"{n_open} open · {port_detail}"))

    # ── 5. Symbol registry ────────────────────────────────────────
    try:
        from src.exchange.symbol_registry import get_registry
        reg = get_registry()
        if reg is not None and reg.is_ready:
            checks.append(("🔎 Symbol Registry", "OK", f"{reg.symbol_count} pairs"))
        else:
            checks.append(("🔎 Symbol Registry", "FAIL", "not ready"))
    except Exception as e:  # noqa: BLE001
        checks.append(("🔎 Symbol Registry", "FAIL", f"{type(e).__name__}: {e}"))

    # ── 6. Database ───────────────────────────────────────────────
    try:
        from src.state.database import get_connection
        c = get_connection()
        c.execute("SELECT 1")
        c.close()
        checks.append(("🗄️ Database", "OK", "trades.db reachable"))
    except Exception as e:  # noqa: BLE001
        checks.append(("🗄️ Database", "FAIL", f"{type(e).__name__}: {e}"))

    elapsed = int((time.monotonic() - t0) * 1000)
    n_ok = sum(1 for _, s, _ in checks if s == "OK")
    n_fail = len(checks) - n_ok
    overall = "✅ ALL SYSTEMS OK" if n_fail == 0 else f"⚠️ {n_fail} ISSUE(S)"

    lines = [f"🩺 **Health Check** — {overall}", ""]
    for label, status, detail in checks:
        icon = "✅" if status == "OK" else "❌"
        lines.append(f"{icon} {label}: {status} — {detail}")
    lines.append("")
    lines.append(f"⏱️ Check took {elapsed}ms · {n_ok}/{len(checks)} ok")
    return lines, n_ok, n_fail


class HealthReporter:
    """Background loop that posts a health report every ``interval_hours``."""

    def __init__(self, listener: Any, interval_hours: float = 6.0):
        self.listener = listener
        self.interval = int(interval_hours * 3600)
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("Health reporter started (every %dh)", self.interval / 3600)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("Health reporter stopped")

    async def _loop(self):
        while self._running:
            try:
                await self._run_once()
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("Health reporter cycle failed")
            # Sleep in small steps so stop() is responsive
            for _ in range(self.interval // 5):
                if not self._running:
                    return
                await asyncio.sleep(5)

    async def _run_once(self):
        notifier = self.listener.notifier
        if notifier is None:
            log.warning("Health reporter: no notifier configured, skipping")
            return
        try:
            lines, n_ok, n_fail = await build_health_report(self.listener)
        except Exception as e:  # noqa: BLE001
            log.exception("Health report build failed")
            await notifier.send_message(
                f"🩺 **Health Check** — ⚠️ ERROR\n\n`{type(e).__name__}: {e}`"
            )
            return
        await notifier.send_message("\n".join(lines))
