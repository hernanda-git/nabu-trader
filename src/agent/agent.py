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
SYSTEM_PROMPT = """You are a crypto trading signal analyst on Binance USDⓈ-M Futures. Your goal is to maximize profit while strictly limiting risk.

Rules:
- If the signal looks valid, output ENTER with entry_price, sl_price, and tp_prices. DO NOT calculate quantity — the safety gate computes it automatically from your SL distance to cap risk at 10% of balance.
- You MUST provide sl_price for every ENTER decision — the system uses SL distance to size the position. No SL = no trade.
- Set order_type to MARKET for immediate entry, or LIMIT if a specific entry price is critical.
- If already in a position for that pair, output CLOSE.
- If signal is unclear / low quality, output SKIP.
- If the message is position management advice (e.g. "move SL to entry", "trail SL", "take partial profit") for an existing open position, output MODIFY with the pair, the new sl_price, and/or new tp_prices as specified. The MODIFY action cancels existing SL/TP orders and places new ones.
- If the signal is a conditional/setup signal (e.g. "look for long after 4h close above X", "wait for breakout above Y"), output CONDITIONAL with pair, direction, entry_price as the trigger price, and reason describing the condition and timeframe (e.g. "4h"). The system will monitor the price and enter automatically when the condition is met.

Output *only* a valid JSON object — no text before, no text after, no markdown fences.

**CRITICAL**: Your entire response must be ONLY the JSON object. No explanations, no reasoning, no markdown formatting — just the raw JSON starting with `{` and ending with `}`.

Example:
{"action":"ENTER","pair":"BTCUSDT","direction":"LONG","order_type":"MARKET","quantity":0,"entry_price":65000,"sl_price":64000,"tp_prices":[67000,68000],"reason":"Clear support bounce with good R/R","confidence":0.8}"""


def _build_prompt(signal: TradeSignal, open_positions: list[dict],
                  balance: dict[str, Any] | None = None,
                  ta_context: str | None = None) -> str:
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

    # Inject real market structure when the signal has no SL/TP
    if ta_context:
        lines.append(ta_context)
        lines.append("")

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

    Tries: raw JSON -> code-fenced JSON -> regex JSON extraction from full text.
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
        match = _re.search(r'\{[\s\S]*?"action"\s*:\s*"(ENTER|CLOSE|SKIP|MODIFY|CONDITIONAL)"[\s\S]*?\}', text)
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
    if action not in ("ENTER", "CLOSE", "SKIP", "MODIFY", "CONDITIONAL"):
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

    def _build_reinforce_prompt(self, original_prompt: str,
                                 bad_response: str) -> str:
        """Build a reinforced prompt telling the LLM its last response was invalid."""
        return (
            "Your previous response was NOT valid JSON. "
            "You MUST return ONLY a valid JSON object this time.\n\n"
            "## CRITICAL — READ CAREFULLY\n"
            "- Your ENTIRE response must be a single JSON object.\n"
            "- Start with `{` and end with `}`.\n"
            "- DO NOT include any text before or after the JSON.\n"
            "- DO NOT use markdown code fences (```).\n"
            "- DO NOT include explanations, reasoning, or analysis.\n"
            "- ONLY the raw JSON object.\n\n"
            "## Your invalid response (DO NOT repeat this)\n"
            f"{bad_response[:500]}\n\n"
            "## The original request\n"
            f"{original_prompt}\n\n"
            "## Decision\n"
            "Output ONLY the JSON object now:"
        )

    def decide(self, signal: TradeSignal,
               open_positions: list[dict] | None = None,
               balance: dict[str, Any] | None = None,
               ta_context: str | None = None) -> TradeDecision:
        """Analyze a signal and return a trade decision."""
        prompt = _build_prompt(signal, open_positions or [], balance, ta_context)

        # Try 1: normal call
        response = ""
        self._last_interaction: dict | None = None
        try:
            response, tokens_in, tokens_out, latency = self._call_llm(prompt)
            decision = _parse_decision(response)
            self._last_interaction = {
                "model": self.model,
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt": prompt,
                "raw_response": response,
                "parsed_decision_json": json.dumps({
                    "action": decision.action if decision else "SKIP",
                    "pair": decision.pair if decision else "",
                    "direction": decision.direction if decision else "LONG",
                }, default=str) if decision else "{}",
                "prompt_tokens": tokens_in,
                "completion_tokens": tokens_out,
                "latency_ms": latency,
                "success": decision is not None,
                "error": None,
            }
        except Exception as e:
            log.error("LLM call failed: %s", e)
            decision = None
            self._last_interaction = {
                "model": self.model,
                "system_prompt": SYSTEM_PROMPT,
                "user_prompt": prompt,
                "raw_response": response or str(e),
                "parsed_decision_json": "{}",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "latency_ms": 0,
                "success": False,
                "error": str(e),
            }

        # Try 2: retry with reinforced prompt if first attempt failed to parse
        if decision is None:
            log.warning("LLM response invalid, retrying with reinforced prompt...")
            reinforce = self._build_reinforce_prompt(prompt, response or "(no response)")
            try:
                response2, tokens_in2, tokens_out2, latency2 = self._call_llm(reinforce)
                decision = _parse_decision(response2)
                if decision is not None:
                    log.info("Retry succeeded — LLM returned valid JSON on second attempt")
                    self._last_interaction = {
                        "model": self.model,
                        "system_prompt": SYSTEM_PROMPT,
                        "user_prompt": reinforce,
                        "raw_response": response2,
                        "parsed_decision_json": json.dumps({
                            "action": decision.action,
                            "pair": decision.pair,
                            "direction": decision.direction,
                        }, default=str),
                        "prompt_tokens": tokens_in2,
                        "completion_tokens": tokens_out2,
                        "latency_ms": latency2,
                        "success": True,
                        "error": None,
                    }
            except Exception as e:
                log.error("LLM retry also failed: %s", e)
                decision = None

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

    def _call_llm(self, prompt: str) -> tuple[str, int, int, int]:
        """Call the LLM via OpenAI-compatible chat completions endpoint.

        Returns (response_text, prompt_tokens, completion_tokens, latency_ms).
        """
        import time
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }

        log.info("Calling LLM: %s with model %s", self.api_url, self.model)
        t0 = time.monotonic()
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(self.api_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            latency = int((time.monotonic() - t0) * 1000)
            usage = data.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            msg = data["choices"][0]["message"]
            # Reasoning models may put JSON in content or reasoning_content
            content = msg.get("content", "") or ""
            rc = msg.get("reasoning_content", "") or ""
            # Try content first (it's the actual response), fall back to full text
            if content.strip():
                return content, prompt_tokens, completion_tokens, latency
            return content + "\n" + rc, prompt_tokens, completion_tokens, latency
