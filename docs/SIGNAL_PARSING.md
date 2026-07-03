# Signal Parsing

## Regex Pre-Parse

The first step in the pipeline is a fast (~1ms) regex extraction of structured fields from the raw signal text. Located in `src/agent/parser.py`.

### Supported Signal Formats

The parser handles various signal formats commonly used in crypto Telegram channels:

**Standard format:**
```
BUY / LONG
Entry: 65000
SL: 64000
TP1: 66000
TP2: 67000
```

**Inline format:**
```
LONG BTCUSDT Entry 65000 SL 64000 TP 66000-67000
```

**Short format:**
```
#BTC/USDT
LONG
Entry: 65000
Targets: 66000, 67000
Stop: 64000
```

### Extracted Fields

| Field | Source | Example |
|-------|--------|---------|
| `pair` | #BTC, BTCUSDT, BTC/USDT | `BTCUSDT` |
| `direction` | LONG, BUY, SHORT, SELL | `LONG` |
| `entry_price` | Entry, Enter, Price | `65400.00` |
| `sl_price` | SL, Stop, Stop Loss | `64000.00` |
| `tp_prices` | TP, Target, Take Profit | `[66000.00, 67000.00]` |

### Signal Quality Heuristics

| Criteria | Decision |
|----------|----------|
| Valid pair + direction + entry + SL | Likely ENTER |
| Missing pair or direction | SKIP |
| Entry price too far from current market | LLM decides |
| SL too wide (>50% from entry) | LLM decides |
| Media without text | SKIP (can't parse) |
| Edited message | Re-processed (signals often update) |

## LLM Signal Analysis

After the regex pre-parse, the **Agent Brain** (LLM) performs deep analysis:

### What the LLM sees

```yaml
## Signal Text
#BTC/USDT 🟢 BUY
Entry: 65400
SL: 64800
TP1: 66200
TP2: 67000

## Regex Pre-Parse
  Pair: BTCUSDT
  Direction: LONG
  Entry: 65400.0
  SL: 64800.0
  TP: [66200.0, 67000.0]

## Open Positions
  LONG BTCUSDT @ 65100 qty=0.0005

## Account
  USDT: 10.58
  Total: 10.58
```

### What the LLM outputs

```json
{
  "action": "ENTER",
  "pair": "BTCUSDT",
  "direction": "LONG",
  "order_type": "MARKET",
  "quantity": 0.00035,
  "entry_price": null,
  "sl_price": 64800.0,
  "tp_prices": [66200.0, 67000.0],
  "reason": "Clear entry with tight SL, good R:R",
  "confidence": 0.85
}
```

### Quantity Calculation

The LLM is instructed to calculate:

```
quantity = (balance × risk_per_trade_percent / 100) / abs(entry_price - sl_price)
```

Example with $10.58 balance, 10% risk, BTC entry $65400, SL $64800:
```
max_loss = $10.58 × 0.10 = $1.058
SL_distance = |65400 - 64800| = $600
quantity = $1.058 / $600 = 0.00176 BTC
position_value = 0.00176 × 65400 = $115
```

Gate 2 then clamps: $115 > $5 max_size → clamped to $5 → qty = 0.000076 BTC

### Action Types

| Action | Meaning | Next Step |
|--------|---------|-----------|
| `ENTER` | Open new position | Gate 2 → Order Service |
| `CLOSE` | Close existing position | Order Service (market sell/buy) |
| `SKIP` | Insufficient quality | Log and skip |

### When the LLM says CLOSE

The LLM may decide to CLOSE an existing position when:
- Signal contradicts the open direction
- Better opportunity elsewhere (max concurrent reached)
- Signal appears to be an exit instruction
