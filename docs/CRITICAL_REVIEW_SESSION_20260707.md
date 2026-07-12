# Critical Review — nabu-trader (July 7, 2026)

## Session Summary

This session debugged a failed BONKUSDT LONG trade on the nabu-trader. The signal came through the @YOUR_SIGNAL_CHANNEL channel, the LLM decided ENTER, Gate2 sized the position correctly, but all Binance API calls returned `-1121 Invalid symbol`.

## Root Cause Analysis

### 1. 1000x Contract Symbol Mapping (BONKUSDT → 1000BONKUSDT)

**Problem:** The LLM/parser extracts "BONK" as the pair and builds "BONKUSDT" as the Binance symbol. On Binance USDⓈ-M Futures, BONK does NOT exist as `BONKUSDT`. It's listed as `1000BONKUSDT`.

**Why this is confusing:**
- The PRICE on 1000BONKUSDT is quoted in BONK spot price (0.0044 USDT), NOT multiplied by 1000 (4.4 USDT)
- The QUANTITY is in BONK tokens (7309), NOT in contract units (7.309 → integer 7)
- The "1000" only affects the contract multiplier (PnL calculation), not the order parameters
- Binance's own exchangeInfo returns `pricePrecision: 7` and `tickSize: 0.0000010` (consistent with BONK spot price)

**Key insight:** For all 1000x symbols on Binance Futures (1000BONKUSDT, 1000PEPEUSDT, 1000SHIBUSDT, 1000FLOKIUSDT), both price and quantity use the base asset (BONK) scale, not the contract scale.

**Fix:** Added `SYMBOL_MAP` in `binance.py` with `_resolve_futures_symbol()` that maps `BONKUSDT → 1000BONKUSDT` with `price_mult=1.0, qty_div=1.0`.

Other affected symbols: PEPEUSDT, SHIBUSDT, FLOKIUSDT

### 2. Quantity Precision (Error -1111)

**Problem:** Even with the correct symbol, orders failed with `-1111: Precision is over the maximum defined for this asset`. The LOT_SIZE filter for 1000BONKUSDT has `stepSize=1` (integer quantity), but the LLM produced qty=7309.091 (3 decimal places).

**Why this is subtle:** The `_round_quantity()` method loads filters from exchangeInfo and rounds to the correct step size. However, the filter caching (`self._filters`) was unreliable — orders were placed BEFORE the exchangeInfo response returned, so the filter cache was empty at rounding time.

**Fix:** Added a fallback rounding path in `_place_order()` that always rounds qty when filters indicate integer step size, and a hard safety check for any quantity under 10M that isn't already an integer.

### 3. STOP Orders Blocked for 1000x Contracts (Error -4120)

**Problem:** Binance returns `-4120: Order type not supported for this endpoint. Please use the Algo Order API endpoints instead.` for STOP_MARKET, STOP, TAKE_PROFIT_MARKET, and TAKE_PROFIT order types on 1000BONKUSDT.

**Confirmed blocked:**
- `STOP_MARKET` → -4120
- `STOP` (stop-limit) → -4120
- `TAKE_PROFIT_MARKET` → -4120
- `TAKE_PROFIT` → -4120

**What works:**
- `MARKET` (BUY/SELL) ✅
- `LIMIT` with `reduceOnly=true` for orders ABOVE market ✅ (TP works)
- `LIMIT` with `reduceOnly=true` for orders BELOW market ❌ (fills instantly — sells at discount)

**Algo Orders API:** Tried multiple endpoints:
- `fapi.binance.com/sapi/v1/algo/futures/newOrder` → 404
- `fapi.binance.com/fapi/v1/algo/order/new` → 404
- `api.binance.com/sapi/v1/algo/futures/newOrder` → 404
- All sapi/papi/fapi algo endpoints → 404

**Impact:** SL orders cannot be placed via API for 1000x contracts. The position is unprotected unless position-manager-based monitoring is used.

**Mitigation:** Added monitoring-based SL in `PositionManager._check_position()` — polls 1m klines every 10s, closes via MARKET SELL when price breaches the SL level.

### 4. Deploy Process: Windows/WSL Out of Sync

**Problem:** The Fly.io deploy runs from the Windows path (`C:\Working Folder\Research\nabu-trader`), but code was developed in the WSL path (`/home/it26/nabu-trader`). These were different git branches with different commit histories.

**Impact:** Three deploys (v24, v25, v26) deployed OLD code even though new commits showed in WSL. The error only manifested when SSH-examining the running container.

**Root cause:** The Windows Fly CLI uses `flyctl deploy` which reads from the current Windows working directory. Git operations in WSL don't sync to Windows automatically.

**Fix:** Always manually copy or sync files between paths:
```bash
cp /home/it26/nabu-trader/src/exchange/binance.py "/mnt/c/Working Folder/Research/nabu-trader/src/exchange/binance.py"
```

### 5. Quantity Rounding Cache Race

**Problem:** The `_round_quantity()` method relies on `self._filters` cache populated by `_preflight_symbol()`. The async flow is:
1. `_place_order()` calls `await _preflight_symbol(symbol_key)` — loads filters
2. Then calls `_round_quantity(symbol_key, qty)` — looks up filters

But the `_load_futures_filters()` method stores results under a re-resolved symbol name. If the resolution changes between the two calls (e.g., first time vs cached), the cache key might differ.

**Observation:** Despite correct code structure, `_round_quantity` sometimes returns `(qty, None)` indicating the cache was empty. This might be related to exchangeInfo returning empty symbol lists or non-TRADING status.

## Order Type Compatibility Matrix

| Order Type | Regular Futures | 1000x Contracts |
|---|---|---|
| MARKET BUY | ✅ | ✅ |
| MARKET SELL | ✅ | ✅ |
| LIMIT BUY above mkt | ✅ (TP Short) | ✅ |
| LIMIT SELL above mkt | ✅ (TP Long) | ✅ |
| LIMIT BUY below mkt | ✅ (TP Long) | ✅ |
| LIMIT SELL below mkt | ⚠️ fills if below best bid | ❌ fills instantly |
| STOP_MARKET | ✅ | ❌ (-4120) |
| STOP (stop-limit) | ✅ | ❌ (-4120) |
| TAKE_PROFIT_MARKET | ✅ | ❌ (-4120) |
| TAKE_PROFIT (take-profit-limit) | ✅ | ❌ (-4120) |
| TRAILING_STOP_MARKET | ✅ | ❌ (-4120) |

## Recommendations

1. **Auto-detect 1000x symbols** from exchangeInfo `baseAsset` prefix (e.g., "1000BONK" → check if starts with digit)
2. **Always round qty to integer** for symbols where price < 0.01 USDT (all 1000x tokens)
3. **Position manager SL:** Always enable price-based SL monitoring as supplement to exchange STOP orders
4. **TP/SL strategy for 1000x:** TP → LIMIT reduceOnly (works), SL → position-manager monitoring or manual Binance UI
5. **Deploy automation:** Write a sync script that rsyncs WSL→Windows before deploy
6. **Comprehensive testing:** Before deploying to prod, run a dry-run test against paper exchange

## Fixes Applied (v24 → v31)

| Version | Fix |
|---------|-----|
| v24 | Initial SYMBOL_MAP (WRONG: used 1000x price) |
| v25 | Deployed from correct Windows path |
| v26 | Integer qty for 1000x contracts |
| v27 | Corrected price multiplier (price in BONK scale) |
| v28 | qty_div=1.0 (qty in BONK tokens) |
| v29 | Fallback integer rounding |
| v30 | Hard safety integer rounding |
| v31 | stop_loss() simplified for 1000x + monitoring SL |
