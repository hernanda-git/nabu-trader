"""Agent brain — single LLM call via OpenCode Go for trade decisions.

Takes raw signal text + regex pre-parse fields + open positions + balance context.
Returns a structured TradeDecision (ENTER / CLOSE / SKIP).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from src.domain.models import TradeDecision, TradeSignal

log = logging.getLogger("agent.brain")

# System prompt for the trading agent
SYSTEM_PROMPT = """You are a crypto trading signal analyst. After reasoning, output this JSON:

{"action":"ENTER|CLOSE|SKIP","pair":"BTCUSDT","direction":"LONG|SHORT","order_type":"MARKET|LIMIT","quantity":0.01,"entry_price":null,"sl_price":null,"tp_prices":[],"reason":"...","confidence":0.0}

Rules: ENTER=open, CLOSE=close existing, SKIP=skip. Be conservative. End with JSON."""


def _build_prompt(signal: TradeSignal, open_positions: list[dict],
                  balance: dict[str, Any] | None = None) -> str:
    """Build a concise prompt for the LLM with all relevant context."""
    lines = [
        "## Signal Text",
        signal.raw_text,
        "",
        "## Regex Pre-Parse",
        f"  Pair: {signal.pair}",
        f"  Direction: {signal.direction}",
        f"  Entry: {signal.entry_price}",
        f"  SL: {signal.sl_price}",
        f"  TP: {signal.tp_prices}",
        "",
    ]

    if open_positions:
        lines.append("## Open Positions")
        for pos in open_positions:
            lines.append(f"  {pos['direction']} {pos['pair']} @ {pos['entry_price']} qty={pos['quantity']}")
        lines.append("")

    if balance:
        lines.append("## Account")
        for asset, val in balance.items():
            if isinstance(val, (int, float)) and val > 0:
                lines.append(f"  {asset}: {val}")
        lines.append("")

    lines.append("## Decision")
    return "\n".join(lines)


def _parse_decision(raw: str) -> TradeDecision | None:
    """Parse LLM response into a TradeDecision.

    Tries: raw JSON → code-fenced JSON → regex JSON extraction from full text.
    """
    text = raw.strip()

    # Try 1: Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    # Try 2: Direct JSON parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try 3: Find JSON-like object anywhere in the text
        import re as _re
        match = _re.search(r'\{[\s\S]*?"action"\s*:\s*"(ENTER|CLOSE|SKIP)"[\s\S]*?\}', text)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                log.warning("Found JSON-like block but failed to parse: %s", match.group(0)[:100])
                return None
        else:
            log.warning("LLM returned invalid JSON: %s", raw[:200])
            return None

    action = data.get("action", "SKIP")
    if action not in ("ENTER", "CLOSE", "SKIP"):
        action = "SKIP"

    direction = data.get("direction", "LONG")
    if direction not in ("LONG", "SHORT"):
        direction = "LONG"

    order_type = data.get("order_type", "MARKET")
    if order_type not in ("MARKET", "LIMIT"):
        order_type = "MARKET"

    return TradeDecision(
        action=action,
        pair=data.get("pair", ""),
        direction=direction,
        order_type=order_type,
        quantity=float(data.get("quantity", 0)),
        entry_price=float(data["entry_price"]) if data.get("entry_price") else None,
        sl_price=float(data["sl_price"]) if data.get("sl_price") else None,
        tp_prices=[float(tp) for tp in data.get("tp_prices", []) if tp],
        reason=data.get("reason", ""),
        confidence=float(data.get("confidence", 0)),
    )


class AgentBrain:
    """LLM-powered trading agent using OpenCode Go (or any OpenAI-compatible API)."""

    def __init__(self, config: dict):
        agent_cfg = config.get("agent", {})
        llm_cfg = agent_cfg.get("llm", {})
        self.api_url = llm_cfg.get("api_url", "https://opencode.ai/zen/go/v1/chat/completions")
        self.model = llm_cfg.get("model", "deepseek-v4-flash")

        # API key: try api_key field first, then api_key_env env var, then env file
        self.api_key = llm_cfg.get("api_key", "")
        if not self.api_key:
            env_var = llm_cfg.get("api_key_env", "")
            if env_var:
                self.api_key = os.environ.get(env_var, "")
                if not self.api_key:
                    # Try loading from .env
                    from dotenv import load_dotenv
                    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
                    self.api_key = os.environ.get(env_var, "")

        self.timeout = llm_cfg.get("timeout", 30)
        self.auto_trade = agent_cfg.get("auto_trade", False)
        self.confidence_threshold = agent_cfg.get("confidence_threshold", 0.6)

    def decide(self, signal: TradeSignal,
               open_positions: list[dict] | None = None,
               balance: dict[str, Any] | None = None) -> TradeDecision:
        """Analyze a signal and return a trade decision."""
        prompt = _build_prompt(signal, open_positions or [], balance)

        try:
            response = self._call_llm(prompt)
            decision = _parse_decision(response)
        except Exception as e:
            log.error("LLM call failed: %s", e)
            return TradeDecision(
                action="SKIP", pair=signal.pair or "", direction="LONG",
                reason=f"LLM error: {e}", confidence=0.0,
            )

        if decision is None:
            return TradeDecision(
                action="SKIP", pair=signal.pair or "", direction="LONG",
                reason="Failed to parse LLM response", confidence=0.0,
            )

        # Apply confidence threshold
        if decision.confidence < self.confidence_threshold and decision.action == "ENTER":
            return TradeDecision(
                action="SKIP", pair=decision.pair, direction=decision.direction,
                reason=f"Confidence {decision.confidence:.2f} below threshold {self.confidence_threshold}",
                confidence=decision.confidence,
            )

        # In dry-run mode, log but don't enter
        if not self.auto_trade and decision.action == "ENTER":
            log.info("Dry-run: would ENTER %s %s qty=%.4f (reason: %s)",
                     decision.direction, decision.pair, decision.quantity, decision.reason)
            return TradeDecision(
                action="SKIP", pair=decision.pair, direction=decision.direction,
                reason=f"[DRY-RUN] Would enter: {decision.reason}",
                confidence=decision.confidence,
            )

        return decision

    def _call_llm(self, prompt: str) -> str:
        """Call the LLM via OpenAI-compatible chat completions endpoint."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }

        log.info("Calling LLM: %s with model %s", self.api_url, self.model)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            msg = data["choices"][0]["message"]
            # Reasoning models may put JSON in content or reasoning_content
            content = msg.get("content", "") or ""
            rc = msg.get("reasoning_content", "") or ""
            # Concatenate both and search for JSON in the full text
            return content + "\n" + rc
