"""Configuration validator — fail-fast on invalid settings."""

from typing import Any


class ConfigError(Exception):
    """Raised when config validation fails."""


def validate_config(cfg: dict[str, Any]) -> None:
    """Validate all configuration values. Raises ConfigError on first failure."""
    _validate_exchange(cfg.get("exchange", {}))
    _validate_risk(cfg.get("risk", {}))
    _validate_agent(cfg.get("agent", {}))
    _validate_monitoring(cfg.get("monitoring", {}))


def _validate_exchange(exchange: dict[str, Any]) -> None:
    active = exchange.get("active", "paper")
    valid_modes = {"paper", "binance", "binance_testnet"}
    if active not in valid_modes:
        raise ConfigError(f"exchange.active must be one of {valid_modes}, got: {active}")

    if active in ("binance", "binance_testnet"):
        binance = exchange.get("binance", {})
        if not binance.get("api_key"):
            raise ConfigError(f"Binance API key is required when exchange.active={active}")
        if not binance.get("api_secret"):
            raise ConfigError(f"Binance API secret is required when exchange.active={active}")


def _validate_risk(risk: dict[str, Any]) -> None:
    max_pos = risk.get("max_position_size_usdt", 0)
    if not isinstance(max_pos, (int, float)) or max_pos <= 0:
        raise ConfigError(f"risk.max_position_size_usdt must be > 0, got: {max_pos}")

    risk_pct = risk.get("risk_per_trade_percent", 0)
    if not isinstance(risk_pct, (int, float)) or risk_pct <= 0 or risk_pct > 100:
        raise ConfigError(f"risk.risk_per_trade_percent must be 0-100, got: {risk_pct}")

    max_concurrent = risk.get("max_concurrent_positions", 0)
    if not isinstance(max_concurrent, int) or max_concurrent < 1:
        raise ConfigError(f"risk.max_concurrent_positions must be >= 1, got: {max_concurrent}")

    daily_loss = risk.get("daily_loss_limit_percent", 0)
    if not isinstance(daily_loss, (int, float)) or daily_loss <= 0 or daily_loss > 100:
        raise ConfigError(f"risk.daily_loss_limit_percent must be 0-100, got: {daily_loss}")

    cooldown = risk.get("min_cooldown_minutes", 0)
    if not isinstance(cooldown, (int, float)) or cooldown < 0:
        raise ConfigError(f"risk.min_cooldown_minutes must be >= 0, got: {cooldown}")


def _validate_agent(agent: dict[str, Any]) -> None:
    confidence = agent.get("confidence_threshold", 0)
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ConfigError(f"agent.confidence_threshold must be 0.0-1.0, got: {confidence}")

    allowed = agent.get("allowed_pairs", [])
    if not isinstance(allowed, list):
        raise ConfigError("agent.allowed_pairs must be a list")


def _validate_monitoring(monitoring: dict[str, Any]) -> None:
    interval = monitoring.get("check_interval_seconds", 10)
    if not isinstance(interval, (int, float)) or interval < 1:
        raise ConfigError(f"monitoring.check_interval_seconds must be >= 1, got: {interval}")
