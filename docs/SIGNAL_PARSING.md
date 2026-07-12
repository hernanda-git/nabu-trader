# Signal Parsing

## Dynamic Symbol Registry — No Hardcoded Symbols

The **first step** in the pipeline is pair resolution via the **SymbolRegistry** (`src/exchange/symbol_registry.py`), which dynamically caches all tradable USDⓈ-M Futures pairs from Binance at startup.

**Old approach (removed):** ~40 hardcoded coin symbols in `PAIR_PATTERNS` regex.  
**New approach:** Live `exchangeInfo` fetch → inverted index → multi-strategy matching.

### Architecture

```
Signal Text [#BTC/USDT LONG 🟢]
         │
         ▼
┌────────────────────────────────────┐
│  extract_candidates()              │  Generic regex: any #tag, $ticker,
│  (agnostic — no hardcoded coins)   │  PAIR/USDT format, or PAIRUSDT
└──────────────┬─────────────────────┘
               │ candidates: ["BTC", "BTCUSDT", "BTC/USDT"]
               ▼
┌────────────────────────────────────┐
│  SymbolRegistry._lookup()          │  Multi-strategy match against cache
│                                    │
│  Strategy 1: Exact match           │  ⚡ O(1) — BTCUSDT → BTCUSDT
│  Strategy 2: Base asset            │  "BTC" → matches baseAsset="BTC"
│  Strategy 3: USDT suffix           │  "BONK" → adds USDT → 1000BONKUSDT
│  Strategy 4: 1000x prefix          │  "BONK" → baseAsset="1000BONK" → auto
│  Strategy 5: Fuzzy (last resort)   │  Levenshtein ≤ 2 for typos
│                                    │
│  All strategies are sub-millisecond │
└──────────────┬─────────────────────┘
               │ resolved pair + metadata
               ▼
┌────────────────────────────────────┐
│  Regex Extraction (existing)       │  Direction, Entry, SL, TP
│  parser.py                         │  via generic patterns (unchanged)
└──────────────┬─────────────────────┘
               │ TradeSignal
               ▼
            Gate 1 → LLM → ...
```

### What Changed

| Aspect | Before | After |
|--------|--------|-------|
| Pair matching | 40 hardcoded symbols in `PAIR_PATTERNS` | Dynamic from Binance `exchangeInfo` (all ~300 USDⓈ-M pairs) |
| New token support | Manual code change | Auto-picked up on next 15-min refresh |
| 1000x handling | Hardcoded `SYMBOL_MAP` in `binance.py` | Auto-detected: baseAsset starting with "1000" + letters |
| Cache lifetime | Permanent (code level) | 15 minutes (background refresh) |
| Startup fallback | Old hardcoded list | Built-in seed data + optional seed file |
| Symbol metadata | None (precision fetched separately) | Cached: tickSize, stepSize, minNotional, minQty |

### Startup Flow

1. `main.py` creates `SymbolRegistry(seed_path="data/symbols_seed.json")`
2. Calls `await registry.initialize()` → fetches `https://fapi.binance.com/fapi/v1/exchangeInfo`
3. Builds inverted index: `symbols["BTCUSDT"]`, `symbols["btcusdt"]`, `by_base["BTC"]`
4. Sets global singleton via `set_registry(registry)` — parser auto-picks it up
5. Starts background refresh loop (every 15 min)
6. On shutdown: `await registry.stop()`

If the exchangeInfo fetch fails (API down, no network), it falls back to:
1. Seed JSON file at `data/symbols_seed.json` (persisted from previous successful fetch)
2. Built-in seed data in `symbol_registry.py` (major USDⓈ-M pairs)

### Cache Population (`symbol_registry.py`)

```python
# Filter: only TRADING symbols with quoteAsset="USDT"
for s in exchange_info["symbols"]:
    if s["status"] != "TRADING": continue
    if s["quoteAsset"] != "USDT": continue
    self._add_symbol(s)

# For each symbol, index:
self._symbols[name] = info           # "BTCUSDT" → SymbolInfo
self._symbols[name.lower()] = info    # "btcusdt" → SymbolInfo
self._by_base[base_asset].append(name)  # "BTC" → ["BTCUSDT"]

# 1000x auto-detection:
if base starts with "1000" + letters:
    clean = strip_numeric_prefix
    self._by_base[clean].append(name)  # "BONK" → ["1000BONKUSDT"]
```

### Signal Quality Heuristics

| Criteria | Decision |
|----------|----------|
| Valid pair + direction + entry + SL | Likely ENTER |
| Missing pair or direction | SKIP (saves LLM cost) |
| Entry price too far from current market | LLM decides |
| SL too wide (>50% from entry) | LLM decides |
| Media without text | SKIP (can't parse) |
| Edited message | Re-processed (signals often update) |

## LLM Signal Analysis

After the regex pre-parse, the **Agent Brain** (LLM) performs deep analysis.

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
  Symbol Info:
    Base: BTC
    Precision: price=2, qty=5
    Tick: 0.01 / Step: 0.001
    Min Notional: 5.0

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

### Action Types

| Action | Meaning | Next Step |
|--------|---------|-----------|
| `ENTER` | Open new position | Gate 2 → Order Service |
| `CLOSE` | Close existing position | Order Service (market sell/buy) |
| `SKIP` | Insufficient quality | Log and skip |
| `MODIFY` | Adjust SL/TP on open position | Order Service (cancel + replace) |
| `CONDITIONAL` | Setup signal — monitor price | PendingSignal → PositionManager loop |

### When the LLM says CLOSE

The LLM may decide to CLOSE an existing position when:
- Signal contradicts the open direction
- Better opportunity elsewhere (max concurrent reached)
- Signal appears to be an exit instruction
