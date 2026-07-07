#!/usr/bin/env python3
"""Auto-trade system entry point.

Wires the entire pipeline and starts the Telegram listener.

Usage:
    python -m src.main
    python src/main.py

Environment:
    TELEGRAM_BOT_TOKEN  — from .env or environment
    NOTIFY_CHAT_ID      — from .env or environment
    TG_API_ID           — from .env
    TG_API_HASH         — from .env
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# Used in shutdown for api_task cancellation

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from src.agent.agent import AgentBrain
from src.agent.gate import SafetyGate1, SafetyGate2
from src.config.loader import get_log_dir, load_config
from src.config.validator import validate_config
from src.events.bus import EventBus
from src.exchange.binance import BinanceExchange
from src.exchange.paper import PaperExchange
from src.exchange.symbol_registry import SymbolRegistry, set_registry
from src.execution.order_service import OrderService
from src.execution.position_manager import PositionManager
from src.listener import SignalListener
from src.notifier.telegram import TelegramNotifier
from src.orchestrator import TradeOrchestrator
from src.state.database import get_connection
from src.state.repositories import (
    ConfigSnapshotRepository,
    DecisionRepository,
    EventRepository,
    LLMInteractionRepository,
    OrderRepository,
    PendingSignalRepository,
    PositionEventRepository,
    PositionRepository,
    SignalRepository,
    TradeLogRepository,
)

log = logging.getLogger("main")


def setup_logging():
    """Configure logging to stdout + file."""
    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "trading.log"),
        ],
    )


async def main():
    """Main entry point."""
    setup_logging()

    # ── Load config ──────────────────────────────────────────────────────
    cfg = load_config()
    validate_config(cfg)
    log.info("Config loaded (exchange=%s, auto_trade=%s)",
             cfg["exchange"]["active"], cfg["agent"]["auto_trade"])

    # ── Load .env for Telegram secrets ───────────────────────────────────
    load_dotenv(ROOT / ".env")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    notify_chat_id = os.getenv("NOTIFY_CHAT_ID", "YOUR_CHAT_ID")

    # ── Database ─────────────────────────────────────────────────────────
    conn = get_connection()

    # ── Symbol Registry ─────────────────────────────────────────────────
    # Dynamic pair resolution from live exchangeInfo. Falls back to seed data.
    seed_path = str(ROOT / "data" / "symbols_seed.json")
    symbol_registry = SymbolRegistry(seed_path=seed_path)
    await symbol_registry.initialize()
    set_registry(symbol_registry)  # makes it available to parser and other modules
    symbol_registry.start_background_refresh(interval_minutes=15)
    log.info("Symbol registry ready: %d pairs cached", symbol_registry.symbol_count)

    # ── Repositories ─────────────────────────────────────────────────────
    signal_repo = SignalRepository(conn)
    decision_repo = DecisionRepository(conn)
    order_repo = OrderRepository(conn)
    position_repo = PositionRepository(conn)
    event_repo = EventRepository(conn)
    pending_signal_repo = PendingSignalRepository(conn)
    llm_repo = LLMInteractionRepository(conn)
    trade_log_repo = TradeLogRepository(conn)
    position_event_repo = PositionEventRepository(conn)
    config_snapshot_repo = ConfigSnapshotRepository(conn)

    # ── Event Bus ────────────────────────────────────────────────────────
    event_bus = EventBus()

    # ── Exchange ─────────────────────────────────────────────────────────
    exchange_mode = cfg["exchange"]["active"]
    if exchange_mode == "paper":
        exchange = PaperExchange(cfg.get("exchange", {}).get("paper", {}))
        log.info("Using PAPER exchange (simulated)")
    elif exchange_mode in ("binance", "binance_testnet"):
        binance_cfg = cfg["exchange"]["binance"]
        exchange = BinanceExchange(
                api_key=binance_cfg["api_key"],
                api_secret=binance_cfg["api_secret"],
                testnet=(exchange_mode == "binance_testnet"),
                futures=binance_cfg.get("futures", False),
                recv_window=binance_cfg.get("recv_window", 5000),
            )
        mode_label = "BINANCE"
        if exchange_mode == "binance_testnet":
            mode_label += " TESTNET"
        if binance_cfg.get("futures"):
            mode_label += " FUTURES"
        log.info("Using %s exchange", mode_label)
    else:
        log.error("Unknown exchange mode: %s", exchange_mode)
        sys.exit(1)

    # ── Agent ────────────────────────────────────────────────────────────
    agent = AgentBrain(cfg)

    # ── Safety Gates ─────────────────────────────────────────────────────
    gate1 = SafetyGate1(cfg, signal_repo, position_repo)
    gate2 = SafetyGate2(cfg, position_repo)

    # ── Order Service ────────────────────────────────────────────────────
    order_service = OrderService(exchange, cfg, signal_repo, decision_repo, order_repo, position_repo, agent=agent)

    # ── Notifier ─────────────────────────────────────────────────────────
    notifier = TelegramNotifier(bot_token=bot_token, chat_id=notify_chat_id)

    # Determine version: Fly.io release version, git commit hash, or package version
    app_version = os.environ.get("FLY_RELEASE_VERSION") or os.environ.get("SOURCE_VERSION", "")
    if not app_version:
        try:
            import subprocess
            result = subprocess.run(
                ["git", "-C", str(Path(__file__).resolve().parent.parent), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            app_version = result.stdout.strip()
        except Exception:
            app_version = ""
    if not app_version:
        try:
            from src.version import __version__  # type: ignore[import-untyped]
            app_version = __version__
        except Exception:
            app_version = ""
    version_str = f"v{app_version}" if app_version and not app_version.startswith("v") else app_version

    if bot_token:
        log.info("Telegram notifier ready (chat_id=%s)", notify_chat_id)
        await notifier.set_commands()
    else:
        log.warning("TELEGRAM_BOT_TOKEN not set — notifications disabled")

    # ── Position Manager ─────────────────────────────────────────────────
    position_manager = PositionManager(exchange, cfg, position_repo,
                                       pending_signal_repo=pending_signal_repo,
                                       position_event_repo=position_event_repo,
                                       notifier=notifier)

    # ── API Server (background task) ──────────────────────────────────────
    api_task = None
    if cfg.get("api", {}).get("enabled", True):
        try:
            import asyncio
            import uvicorn
            from src.api.server import app as api_app

            api_host = cfg.get("api", {}).get("host", "0.0.0.0")
            api_port = cfg.get("api", {}).get("port", 9090)

            async def start_api():
                config = uvicorn.Config(api_app, host=api_host, port=api_port, log_level="info")
                server = uvicorn.Server(config)
                await server.serve()

            api_task = asyncio.create_task(start_api())
            log.info("API bridge starting on %s:%d", api_host, api_port)
        except Exception as e:
            log.warning("API bridge not started: %s", e)

    # ── Orchestrator ─────────────────────────────────────────────────────
    orchestrator = TradeOrchestrator(
        config=cfg,
        exchange=exchange,
        agent=agent,
        gate1=gate1,
        gate2=gate2,
        order_service=order_service,
        position_manager=position_manager,
        notifier=notifier,
        signal_repo=signal_repo,
        decision_repo=decision_repo,
        order_repo=order_repo,
        position_repo=position_repo,
        event_repo=event_repo,
        llm_repo=llm_repo,
        trade_log_repo=trade_log_repo,
        position_event_repo=position_event_repo,
        config_snapshot_repo=config_snapshot_repo,
        event_bus=event_bus,
        pending_signal_repo=pending_signal_repo,
        app_version=version_str or "",
    )

    # Wire orchestrator to position manager for trigger auto-entry
    position_manager._orchestrator = orchestrator

    # ── Listener ─────────────────────────────────────────────────────────
    listener = SignalListener(orchestrator, cfg, exchange=exchange,
                              version=version_str or None, notifier=notifier)

    # ── Start ────────────────────────────────────────────────────────────
    try:
        await position_manager.start()
        await listener.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        await listener.stop()
        await position_manager.stop()
        if api_task:
            api_task.cancel()
            try:
                await api_task
            except (asyncio.CancelledError, Exception):
                pass
        await symbol_registry.stop()
        conn.close()
        log.info("Goodbye.")


if __name__ == "__main__":
    asyncio.run(main())
