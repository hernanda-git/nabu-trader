"""Configuration loader — merges config.yaml + .env + defaults."""

import os
import yaml
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"

# ─── Fly.io path helpers ─────────────────────────────────────
# On Fly.io, /data is a persistent volume; use env var to detect.

def _resolve_data_root() -> Path:
    """Return the root for persistent data (DB, sessions, logs).

    On Fly.io (FLY_MODE=1 or DATA_ROOT set) the volume is at /data.
    Locally it defaults to the project root, preserving existing behaviour.
    """
    override = os.environ.get("DATA_ROOT")
    if override:
        return Path(override)
    if os.environ.get("FLY_MODE"):
        return Path("/data")
    return ROOT_DIR


def get_data_dir() -> Path:
    """Return the directory for persistent data files.

    On Fly.io (DATA_ROOT/FLY_MODE set) the volume mounted at /data IS the data
    dir, so the DB lives at /data/trades.db — NOT /data/data/trades.db. Locally
    (no volume) it stays ROOT_DIR/data to preserve existing dev behaviour.
    """
    root = _resolve_data_root()
    if os.environ.get("DATA_ROOT") or os.environ.get("FLY_MODE"):
        return root
    return root / "data"


def get_session_dir() -> Path:
    """Return the directory for Telegram session files."""
    return _resolve_data_root() / "sessions"


def get_log_dir() -> Path:
    """Return the directory for log files."""
    return _resolve_data_root() / "logs"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config.yaml and overlay .env secrets. Returns a merged dict."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH

    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")

    # Load .env into os.environ once (python-dotenv handles all edge cases)
    env_path = path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    with open(path) as f:
        cfg: dict[str, Any] = yaml.safe_load(f) or {}

    # Overlay Binance keys from os.environ (populated by load_dotenv above)
    binance_cfg = cfg.get("exchange", {}).get("binance", {})
    api_key_env = binance_cfg.get("api_key_env", "BINANCE_API_KEY")
    api_secret_env = binance_cfg.get("api_secret_env", "BINANCE_API_SECRET")
    binance_cfg["api_key"] = os.environ.get(api_key_env, "")
    binance_cfg["api_secret"] = os.environ.get(api_secret_env, "")

    # ── Gateway proxy (optional) ──────────────────────────────────────────
    # When `exchange.binance.proxy.enabled` is true, the listener routes ALL
    # Binance REST calls through a signed relay (e.g. the binance-gateway Fly
    # app) and does NOT need the Binance API key locally. The key lives only
    # on the gateway. Default: OFF (direct calls, key on the listener).
    proxy_cfg = binance_cfg.get("proxy", {}) or {}
    proxy_enabled = bool(proxy_cfg.get("enabled", False))
    proxy_cfg["enabled"] = proxy_enabled
    if proxy_enabled:
        proxy_cfg.setdefault(
            "url", os.environ.get("GATEWAY_URL", proxy_cfg.get("url", ""))
        )
        proxy_cfg.setdefault(
            "hmac_secret",
            os.environ.get("GATEWAY_HMAC_SECRET", proxy_cfg.get("hmac_secret", "")),
        )
    binance_cfg["proxy"] = proxy_cfg
    cfg.setdefault("exchange", {})["binance"] = binance_cfg

    # Overlay LLM API key from os.environ
    llm_cfg = cfg.get("agent", {}).get("llm", {})
    llm_key_env = llm_cfg.get("api_key_env", "OPENCODE_GO_API_KEY")
    if not llm_cfg.get("api_key"):
        llm_cfg["api_key"] = os.environ.get(llm_key_env, "")
    cfg.setdefault("agent", {})["llm"] = llm_cfg

    return cfg
