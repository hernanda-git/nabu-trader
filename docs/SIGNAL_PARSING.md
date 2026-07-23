# Signal Parsing

## How Raw Telegram Messages Become Trade Signals

The pipeline uses a two-stage parsing approach: **fast regex pre-parse** (~1ms) followed by **LLM-based decision**. The regex pass extracts structured fields; the LLM validates + decides.

---

## Stage 1: Dynamic Symbol Registry

### No Hardcoded Symbols

The **first step** is pair resolution via the `SymbolRegistry` (`src/exchange/symbol_registry.py`), which dynamically caches all tradable USDⓈ-M Futures pairs from Binance at startup.

**How it works:**
1. On startup, fetches `exchangeInfo` from Binance Futures
2. Caches ~300+ tradable pairs (symbol, base asset, min notional, tick size, step size, contract type)
3. Exposes `resolve(text)` → returns `(symbol, SymbolInfo)` or `(None, None)`
4. Refreshes cache every 15 minutes

**Resolution logic:**
```
1. Strip $, #, whitespace → uppercase
2. Try exact match in registry (e.g. "BTCUSDT" → BTCUSDT, "1000BONKUSDT" → 1000BONKUSDT)
3. Try appending "USDT" (e.g. "BONK" → try "BONKUSDT")
4. If not found, try "1000" prefix (e.g. "BONK" → "1000BONKUSDT" — Binance lists small-cap coins as 1000× contracts)
5. If still not found → signal rejected with "Unknown pair"
```

## Stage 2: Regex Pre-Parse (`agent/parser.py`)

A fast regex pass extracts structured data from the raw message text.

### Pattern Detection

| Field | Patterns | Example Match |
|-------|----------|---------------|
| **Symbol/Ticker** | `$SYMBOL`, `#SYMBOL`, `SYMBOL/USDT` | `$BONK`, `#BTC`, `SOL/USDT` |
| **Direction** | LONG, SHORT, BUY, SELL, long, short, bullish, bearish | `LONG`, `BUY`, `short` |
| **Entry** | entry, enter, @, zone, entry zone | `Entry: 0.003105`, `@ 0.0031` |
| **Stop Loss** | sl, stop loss, stoploss, invalidation, invalidate | `SL: 0.003022`, `stoploss 0.0030` |
| **Take Profit** | tp, tp1, tp2, target, tgt, take profit | `TP: 0.00333`, `Target 0.0035` |
| **Volume/Quantity** | qty, volume, size, q | `Qty: 1000` |

### Regex Architecture

```python
# Simplified patterns from parser.py
SYMBOL_PATTERN = r'(?:\$|#)?([A-Z0-9]{2,10})(?:USDT)?(?:\s|$|/)'
DIRECTION_PATTERN = r'(LONG|SHORT|BUY|SELL|long|short|buy|sell|bullish|bearish)'
ENTRY_PATTERN = r'(?:entry|enter|@|zone)\s*:?\s*(\d+\.?\d*)'
SL_PATTERN = r'(?:sl|stop\s*loss|stoploss|invalidation)\s*:?\s*(\d+\.?\d*)'
TP_PATTERN = r'(?:tp\d?|target|tgt|take\s*profit)\s*:?\s*(\d+\.?\d*)'
```

The parser extracts the **first** valid match for each field. Multi-TP is supported (TP1, TP2, etc.) — each TP price is added to the `tp_prices` list.

### Management Command Detection

Some messages are **management commands** rather than trade signals. These are detected by regex patterns and handled directly without LLM overhead:

| Pattern | Handled As |
|---------|-----------|
| `tp1 booked`, `tp1 done`, `partial`, `1r up` | TP1 partial close (50% → breakeven → TP on rest) |
| `sl to entry`, `breakeven` | Move stop loss to entry |
| `closed at entry`, `cutting it here`, `full` | Full market close |
| `tp1 <price>`, `tp2 <price>` | Modify TP level |

Pair mention (`$ENA tp1 booked`) restricts the command to that specific pair. If the named pair has no open position, the command is **rejected** with a warning (fix v101).

---

## Stage 3: LLM Agent Decision (`agent/agent.py`)

If Gate 1 passes, the signal goes to the LLM for analysis.

### LLM Input Context

The LLM receives:
1. **Raw signal text** — the full Telegram message
2. **Regex pre-parse fields** — pair, direction, entry, SL, TP (as extracted above)
3. **Open positions** — current positions with entry price, quantity, PnL
4. **Account balance** — available USDT balance
5. **Recent decisions** — last 5 decisions (prevents contradiction)

### LLM Output

The LLM returns a structured JSON response:

```json
{
  "action": "ENTER",
  "pair": "1000BONKUSDT",
  "direction": "LONG",
  "order_type": "LIMIT",
  "entry_price": 0.003105,
  "sl_price": 0.003022,
  "tp_prices": [0.00333],
  "reason": "Clear support level with 1:2 risk/reward ratio",
  "confidence": 0.85
}
```

### LLM Prompt Design

The agent prompt includes:
- System role definition ("You are a crypto futures trading analyst...")
- Current market context (open positions, balance)
- Examples of good vs bad signal parsing
- Strict JSON output format requirement
- Risk warnings (SL must be below entry for LONG, etc.)

---

## Signal Flow Example

### Input: `#BONK $BONK LONG TRADE\n\nENTRY: 0.003105\n\nTARGET: 0.00333\n\nSTOPLOSS: 0.003022`

```
1. SymbolRegistry.resolve("BONK")
   → looks up "BONKUSDT" → not found
   → tries "1000BONKUSDT" → FOUND ✓
   → returns ("1000BONKUSDT", SymbolInfo)

2. Regex Pre-Parse
   → Direction: "LONG" (from "LONG TRADE")
   → Entry: 0.003105 (from "ENTRY: 0.003105")
   → SL: 0.003022 (from "STOPLOSS: 0.003022")
   → TP: [0.00333] (from "TARGET: 0.00333")

3. Gate 1 Check
   → Idempotency: message_id=139 not processed → pass
   → Cooldown: BONK not traded recently → pass
   → Wildcard whitelist → pass

4. LLM Agent
   → Input: raw_text + parsed fields + empty positions + balance
   → Output: ENTER, pair=1000BONKUSDT, qty=1610.3, SL=0.003022, TP=[0.00333]

5. Gate 2 Clamp
   → qty=1610 passes (port_usdt=$1 / price)
   → max_concurrent=2 → pass (0 open)
   → SL below entry for LONG → pass

6. Order Execution
   → LIMIT BUY 1610.3 @ 0.003104 → FILLED
   → SL: STOP_LOSS_LIMIT @ 0.003022 (reduceOnly)
   → TP: TAKE_PROFIT_LIMIT @ 0.00333 (reduceOnly)
   → Telegram: ✅ Order filled
```

---

## Signal Format Support

The parser handles multiple message formats from the channel:

### Format 1: Structured Trade
```
#BONK $BONK LONG TRADE

ENTRY: 0.003105

TARGET: 0.00333

STOPLOSS: 0.003022
```

### Format 2: Inline
```
$BTC LONG at 65400, SL 64800, TP 66200
```

### Format 3: Management Command
```
$ENA tp1 booked
$ENA sl to entry
$WLD closed at entry
```

### Format 4: Analysis (will be SKIP'd by LLM)
```
$BTC the move we expected happened. Daily closed above the midrange...
```
(No entry/SL/TP → LLM returns SKIP)

### Format 5: Non-Signal (SKIP'd by Gate 1 or LLM)
```
Goodmorning let's kill it today🔥
```
(No pair, no direction → SKIP)

---

## Performance

| Stage | Typical Time |
|-------|-------------|
| Symbol Registry lookup | <1ms |
| Regex Pre-Parse | ~1ms |
| Gate 1 Check | <1ms |
| LLM Agent Call | 500-2000ms (network dependent) |
| Gate 2 Clamp | <1ms |
| Order Execution | 200-500ms (network dependent) |
| **Total Pipeline** | **~700-2500ms** |
