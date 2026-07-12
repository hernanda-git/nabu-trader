# Risk Management

The system uses a **multi-layer safety architecture** — no single component can override the hard limits.

## Safety Gates Overview

```
Signal → Gate 1 (pre-LLM) → LLM → Gate 2 (post-LLM) → Exchange
              │                        │
         Fast rejects              Hard clamps
         (no LLM cost)             (no override)
```

## Gate 1 — Pre-LLM Checks

These run before the LLM call, saving token cost:

| Check | Why | Config |
|-------|-----|--------|
| **Idempotency** | Prevents duplicate processing after crashes/restarts. Signal hash stored in `processed_signals` table. | Always on |
| **Pair Whitelist** | Only trades explicitly allowed pairs. `["*"]` = allow all. | `agent.allowed_pairs` |
| **Cooldown** | Prevents re-entering a pair you just exited (avoiding whipsaw). | `risk.min_cooldown_minutes` |

## Gate 2 — Post-LLM Hard Limits

These enforce non-negotiable limits that the LLM cannot override:

### 1. Position Value Clamp

Clamps the position value (quantity × entry_price) to the smaller of:
- `risk.max_position_size_usdt` (hard cap)
- `balance × (risk_per_trade_percent / 100)` (dynamic cap)

```
max_size = min(config.max_position_size_usdt, balance × risk_pct / 100)

Example: balance=$10.58, max_size=$5
  LLM wants: $52.90 position → clamped to $5.00
  Loss at SL: $0.10–$1.00 (0.9–9.5% of balance)
```

### 2. Minimum Notional Scaling

If the clamped position is below the exchange minimum (e.g., $1), the position is scaled up to meet it. This may exceed the risk loss %, but the user accepts this ("it's worth if I win").

```
Example: balance=$10.58, sl_pct=20%
  LLM calculates: $1.058 / 0.20 = $5.29 position
  Below min_notional ($10)? → scale to $10
  Actual loss: $10 × 20% = $2.00 (18.9% of balance) — user accepts
```

### 3. Max Concurrent Positions

Limits how many positions can be open simultaneously. Configurable via `risk.max_concurrent_positions` (default: 2).

### 4. Daily Loss Limit

If cumulative daily P&L (across all closed positions) exceeds `risk.daily_loss_limit_percent` of balance, all trades are rejected for the rest of the day.

```
Example: balance=$10.58, limit=30%
  Dynamic limit: $10.58 × 0.30 = $3.17 max daily loss
  After losing trades totaling -$3.17 → no more trades until UTC midnight
```

### 5. SL Sanity Check

Rejects trades where the stop-loss is on the wrong side of the entry:
- LONG: SL must be < entry price
- SHORT: SL must be > entry price

If violated, the trade is **rejected entirely** (not clamped).

### 6. Dynamic Leverage

Leverage is calculated per trade to keep margin usage at ~50% of balance:

```
margin_target = balance × (margin_usage_pct / 100)
leverage = max(1, min(ceil(position_value / margin_target), max_leverage))
```

This ensures:
- You don't lock 100% of your balance as margin
- Higher leverage kicks in for larger positions
- The cap (`max_leverage: 20`) prevents excessive risk

## Position Manager

A background loop runs every `monitoring.check_interval_seconds` (default: 10s):

| Feature | Description |
|---------|-------------|
| **SL/TP monitoring** | Checks if stop-loss or take-profit orders were filled on Binance |
| **Time-based auto-close** | Closes positions older than `max_position_hold_hours` (48h) |
| **Stale cleanup** | Auto-closes positions with no remaining orders after 30 min |
| **Reconciliation** | Syncs position state with actual Binance fills |

## Exchange-Level Safety

| Exchange | Risk Level | Description |
|----------|------------|-------------|
| **Paper** | ⚪ None | Simulated — no real money. All fills 100% at desired price. |
| **Binance Testnet** | 🟡 Low | Real API, fake money ($100k test USDT). Tests API integration. |
| **Binance Mainnet** | 🔴 Real | Real money. All safety gates active. |

## Recommended Progressive Testing

```
Phase 1: Paper + Dry-Run
  config.yaml:
    exchange.active: paper
    agent.auto_trade: false
  → Verify: signals parsed, LLM decisions, notifications

Phase 2: Paper + Live
  config.yaml:
    exchange.active: paper
    agent.auto_trade: true
  → Verify: order execution, position tracking, SL/TP logic

Phase 3: Binance Testnet
  config.yaml:
    exchange.active: binance_testnet
    agent.auto_trade: true
  → Verify: Binance API works, leverage set, orders filled

Phase 4: Binance Mainnet
  config.yaml:
    exchange.active: binance
    agent.auto_trade: true
  → REAL MONEY TRADING
```

## LLM Prompt Guardrails

The system prompt instructs the LLM to:
1. Calculate quantity = (balance × risk%) / |entry - sl|
2. Be conservative — if unsure, SKIP
3. Output ONLY valid JSON

The LLM *cannot* override Gate 2 limits — even if it returns an unsafe value, the gate clamps/rejects it.
