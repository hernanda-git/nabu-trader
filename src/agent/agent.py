"""Agent brain — single LLM call via OpenCode Go for trade decisions.

Takes raw signal text + regex pre-parse fields + open positions + balance context.
Returns a structured TradeDecision (ENTER / CLOSE / SKIP).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from src.domain.models import TradeDecision, TradeSignal

log = logging.getLogger("agent.brain")

# System prompt for the trading agent
SYSTEM_PROMPT = """You are a crypto trading signal analyst. Your job is to analyze Telegram signals and output a structured trade decision.

RESPOND ONLY WITH VALID JSON. No markdown, no explanation, no code blocks.

Available actions:
- "ENTER": Open a new position
- "CLOSE": Close an existing position on this pair
- "SKIP": Do nothing (signal is not actionable)

Rules:
1. Be conservative — if you're unsure, output SKIP
2. For ENTER: provide pair (e.g. BTCUSDT), direction (LONG/SHORT), order_type (MARKET/LIMIT), quantity, optional sl_price and tp_prices
3. For CLOSE: provide pair and direction only
4. Quantity: suggest a reasonable amount in the base asset. The safety gate will clamp it.
5. Confidence: 0.0-1.0. Below 0.5 → SKIP recommended
6. Reason: brief explanation of your decision

Output format:
```json
{
  "action": "ENTER|CLOSE|SKIP",
  "pair": "BTCUSDT",
  "direction": "LONG|SHORT",
  "order_type": "MARKET|LIMIT",
  "quantity": 0.01,
  "entry_price": null,
  "sl_price": null,
  "tp_prices": [],
  "reason": "Clear signal with defined entry, SL at 1% risk",
  "confidence": 0.85
}
```
"""


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
    """Parse LLM response into a TradeDecision."""
    # Strip markdown code fences if present
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
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
        self.api_url = llm_cfg.get(
            "api_url",
            agent_cfg.get("llm_api_url", ""),
        ) or "http://localhost:8080/v1/chat/completions"
        self.model = llm_cfg.get("model", agent_cfg.get("llm_model", "deepseek-v4-flash"))
        self.api_key = llm_cfg.get("api_key", agent_cfg.get("llm_api_key", ""))
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
            "max_tokens": 500,
        }

        log.info("Calling LLM: %s with model %s", self.api_url, self.model)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return content
