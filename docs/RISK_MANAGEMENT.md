# Risk Management

The system uses a **multi-layer safety architecture** — no single component can override the hard limits. Every trade passes through three independent layers of checks.

---

## Safety Gates Overview

```
Signal → Gate 1 (pre-LLM) → LLM → Gate 2 (post-LLM) → Exchange
           │                           │
      Fast rejects               Hard clamps
      (no LLM cost)              (no override)
```

---

## Gate 1 — Pre-LLM Checks (`agent/gate.py`)

These run before the LLM call — they cost nothing and protect against spam/duplicates.

### Idempotency Check

Every incoming message is checked against the `processed_messages` table:
- If `message_id` already exists → **SKIP** silently
- If same `signal_hash` (SHA-256 of raw text) processed within 1 hour → **SKIP**
- Prevents duplicate trades from network retries or channel re-posts

### Cooldown Timer

- If the **same pair** was traded less than `min_cooldown_minutes` (default: 5) ago → **SKIP**
- Prevents overtrading on rapid repeated signals
- Resets on any ENTER or CLOSE for that pair

### Pair Whitelist

- `allowed_pairs` in config (`"*"` = wildcard, allow any)
- If pair is explicitly listed but doesn't match → **SKIP**
- Only applies when whitelist is non-wildcard

### Media-Only Filter

- If message has media (image/video) but no parsable text → **SKIP**
- Prevents reacting to memes and charts without signals

---

## Gate 2 — Post-LLM Hard Limits (`agent/gate.py`)

These run after the LLM returns a decision. They **override** the LLM's numbers — the LLM cannot bypass these.

### Position Size Clamp

```
actual_quantity = min(llm_quantity, max_position_size / mark_price)
```

- `port_usdt` (default: **$1.00**) limits margin per trade
- Position value = `margin × leverage`
- `max_port_pct` (default: **10%**) = position value never exceeds 10% of balance
- `max_position_size_usdt` (default: **$5**) = fallback hard cap

**Example:**
- Balance = $10, port_usdt = $1, leverage = 20
- Max position notional = $1 × 20 = $20 (or 10% of $10 = $1 hard cap by max_port_pct)
- For 1000BONKUSDT @ $0.003: qty = $1 / 0.003 = 333 × leverage

### Max Concurrent Positions

- **Hard limit: 2** (`max_concurrent_positions`)
- If 2 positions already open → CLOSE signal required first
- Prevents over-concentration

### Daily Loss Limit

- **-30%** (`daily_loss_limit_percent`) of starting balance per day
- Calculated as: (current_balance - day_start_balance) / day_start_balance
- If limit hit → all ENTER decisions rejected until next day
- CLOSE/MODIFY still allowed (to exit existing positions)

### Minimum Notional

- **$5.00** (`min_notional_usdt`) — Binance Futures minimum
- If position value < $5 → reject
- Bot may auto-increase leverage to meet minimum if `max_leverage_increase_pct` allows

### SL Direction Validation

| Direction | SL must be |
|-----------|-----------|
| LONG | Below entry price |
| SHORT | Above entry price |

If SL is on the wrong side → **reject** (prevents LLM hallucination of inverted SL/TP)

### Margin Usage Cap

- **80%** (`margin_usage_pct`) of available balance
- Prevents using all balance as margin (leaves room for fees and draws)

### Portfolio Leverage Cap

- Total notional of all positions / total balance ≤ **10×** (`max_portfolio_leverage`)
- Prevents over-leveraging the entire portfolio

---

## Position Manager Runtime Safety (`execution/position_manager.py`)

Runs as a background loop every `check_interval_seconds` (default: 10s).

### SL Monitoring

**Two mechanisms — primary and backup:**

1. **Exchange conditional orders (primary)**
   - Bot places a `STOP_LOSS_LIMIT` order (reduceOnly) on entry
   - Binance handles the execution automatically
   - Covers most standard futures pairs

2. **Price-based monitoring (backup)**
   - For pairs where conditional orders fail (e.g. 1000× contracts with error `-4120`)
   - Position manager polls mark price every 10s
   - If `current_price <= sl_price` (LONG) or `current_price >= sl_price` (SHORT) → **market close**
   - Logged as: `SL HIT for 1000BONKUSDT (via mark): current=0.00301000 <= sl=0.00302200`

### TP Monitoring

- Primary: `TAKE_PROFIT_LIMIT` conditional order (reduceOnly)
- Fallback: Resting Basic-tab `LIMIT` order at TP price
- Multi-TP supported: e.g. TP1 close 50%, TP2 close remainder

### Self-Heal

If SL/TP orders are missing from the exchange but the position is still open:
- **Re-places** the missing orders (once per session, tracked in `_protection_placed`)
- Prevents positions from becoming unprotected after restart or network issues

### Orphan Detection

- If a position has **zero open orders** and age > **30 minutes**:
  1. Check exchange for live position (source of truth)
  2. If exchange says flat → mark as CLOSED automatically
  3. If exchange says still open → skip auto-close (self-heal will re-attach protection)

### Time-Based Exit

- If position held > `max_position_hold_hours` (default: **48h**) → **market close**
- Prevents trades from being held indefinitely
- Logged as: `Time-based exit for 1000BONKUSDT (age=24.5h > max=48h)`

### Telegram Notification on Close

**Added in v103** — every position close sends a Telegram message:

```
{🟢/🔴} **Position Closed** — `1000BONKUSDT`
   ├ Direction: `LONG`
   ├ Entry: `0.00310400`
   ├ Exit: `0.00301000`
   ├ PnL: `-0.1514` USDT
   ├ Reason: `SL hit (0.00301000)`
   └ Closed by: `Stop Loss`
```

This covers all close types: SL, TP, MANUAL, SYSTEM, TRIGGER.

---

## Exchange Layer Safety (`exchange/binance.py`)

### Reduce-Only Flag

- SL/TP orders are always placed with `reduceOnly=true`
- Prevents a close order from accidentally opening a reverse position (flip)
- Explicit `reduce=` flag per call (fix v81 — no type-based inference)

### Min Notional Enforcement

- Validates `quantity × price >= min_notional` before every order
- Auto-bumps quantity if below minimum (steps up by `stepSize`)

### Step Size Rounding

- Quantity is always rounded to Binance's `stepSize` (lot size filter)
- Prevents `-1111 Precision error` rejections

### Isolated Margin

- All positions open with isolated margin by default
- Prevents one losing trade from affecting other positions

### API Key Security

- Keys stored in Fly secrets, never in the codebase
- IP-restricted on Binance (recommended: whitelist Fly.io egress IPs)
- `recv_window: 5000` prevents replay attacks

---

## Risk Parameter Quick Reference

| Parameter | Default | Effect |
|-----------|---------|--------|
| `port_usdt` | $1.00 | Margin per trade |
| `max_concurrent_positions` | 2 | Maximum simultaneous trades |
| `daily_loss_limit_percent` | 30% | Stop trading after daily drawdown |
| `max_position_hold_hours` | 48h | Auto-close positions older than this |
| `max_leverage` | 20× | Ceiling on dynamic leverage |
| `margin_usage_pct` | 80% | Max margin as % of balance |
| `min_notional_usdt` | $5.00 | Minimum order size (Binance rule) |
| `min_cooldown_minutes` | 5 | Min gap between trades on same pair |
| `check_interval_seconds` | 10 | Position monitoring frequency |

---

## Failure Modes & Recovery

| Failure | Detection | Recovery |
|---------|-----------|----------|
| LLM hallucinates bad SL/TP | Gate 2 validation rejects | Trade skipped, Telegram notification sent |
| Conditional order rejected (-4120) | Price monitoring fallback | SL monitored every 10s via mark price |
| Exchange API timeout | httpx timeout (10s) | Retry on next monitoring tick |
| Position manager crash | asyncio exception handler | Logs error, continues loop |
| Bot restart mid-trade | Startup loads open positions from exchange | Resumes monitoring + self-heals protection |
| Duplicate signal re-processed | Idempotency check (message_id) | SKIP silently |
| 1000× pair not found | SymbolRegistry lookup fails | Signal rejected |
