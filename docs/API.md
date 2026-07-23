# Exchange API Reference

## Abstract Interface (`src/exchange/base.py`)

Every exchange adapter must implement the `Exchange` ABC:

```python
class Exchange(ABC):
    @property
    def name(self) -> str: ...

    async def get_balance(self) -> BalanceInfo: ...
    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo: ...
    async def market_sell(self, symbol: str, quantity: float) -> OrderInfo: ...
    async def limit_buy(self, symbol: str, quantity: float, price: float, reduce: bool = False) -> OrderInfo: ...
    async def limit_sell(self, symbol: str, quantity: float, price: float, reduce: bool = False) -> OrderInfo: ...
    async def stop_loss(self, symbol: str, quantity: float, stop_price: float, limit_price: float, reduce: bool = True) -> OrderInfo: ...
    async def take_profit(self, symbol: str, quantity: float, stop_price: float, limit_price: float, reduce: bool = True) -> OrderInfo: ...
    async def market_close(self, symbol: str, quantity: float, side: str) -> OrderInfo: ...
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    async def cancel_all_orders(self, symbol: str) -> int: ...
    async def get_order(self, symbol: str, order_id: str) -> OrderInfo: ...
    async def get_open_orders(self, symbol: str) -> list[OrderInfo]: ...
    async def get_positions(self) -> list[PositionInfo]: ...
    async def get_mark_price(self, symbol: str) -> float: ...
    async def get_klines_close(self, symbol: str, interval: str) -> float: ...
    async def get_ticker_24hr(self, symbol: str) -> TickerInfo: ...
    async def set_leverage(self, symbol: str, leverage: int) -> bool: ...
    async def set_margin_mode(self, symbol: str, margin_type: str) -> bool: ...
```

## Return Types

```python
@dataclass(frozen=True)
class OrderInfo:
    symbol: str
    order_id: str
    client_order_id: str | None
    side: str                        # BUY / SELL
    type: str                        # LIMIT / MARKET / STOP / TAKE_PROFIT
    quantity: float
    price: float
    stop_price: float | None
    status: str                      # NEW / FILLED / PARTIALLY_FILLED / CANCELED / EXPIRED / REJECTED
    filled_quantity: float
    avg_price: float
    reduce_only: bool
    error: str | None

@dataclass(frozen=True)
class BalanceInfo:
    total_wallet_balance: float
    total_usdt: float
    available_balance: float

@dataclass(frozen=True)
class PositionInfo:
    symbol: str
    direction: str                   # LONG / SHORT
    size: float                      # positive = LONG, negative = SHORT
    entry_price: float
    mark_price: float
    liquidation_price: float
    unrealized_pnl: float
    leverage: int

@dataclass(frozen=True)
class TickerInfo:
    symbol: str
    last_price: float
    mark_price: float
    price_change_percent: float
    high_price: float
    low_price: float
    volume: float
    quote_volume: float
```

## Binance Exchange (`src/exchange/binance.py`)

The primary exchange adapter. Supports:
- Spot trading (`api.binance.com`)
- USDⓈ-M Futures (`fapi.binance.com`)
- Testnet for both (`testnet.binance.vision` / `testnet.binancefuture.com`)

### Key Features

| Feature | Implementation |
|---------|---------------|
| **Auth** | HMAC-SHA256 signature, timestamp + recvWindow |
| **Futures** | Uses `fapi.binance.com` endpoints |
| **Testnet** | Switch via `testnet=True` constructor parameter |
| **Reduce-Only** | Explicit `reduce` flag on LIMIT/STOP/TAKE_PROFIT orders (fix v81) |
| **Symbol Resolution** | Internal `_resolve_futures_symbol()` normalizes tickers |
| **Min Notional** | Validated pre-submission + bumped if below minimum |
| **Step Size** | Quantity rounded to exchange's `stepSize` |
| **Leverage** | `set_leverage()` calls Binance before placing orders |
| **Margin Mode** | `set_margin_mode()` switches between ISOLATED and CROSSED |
| **Error Handling** | Parses Binance error codes (`-1121`, `-2022`, `-4120`, etc.) |

### Rate Limits

- 1200 requests per minute (futures API)
- Order placement: 10 per 10 seconds per symbol (heavy users: 100 per 10s)
- The bot respects rate limits via `recv_window` and non-blocking httpx

### Error Codes

| Binance Code | Meaning | Bot Handling |
|-------------|---------|-------------|
| `-1121` | Invalid symbol | Logged → signal rejected |
| `-2022` | reduceOnly mismatch | Fixed in v81 with explicit `reduce=` flag |
| `-2019` | Margin insufficient | Logged → position skipped |
| `-4120` | Conditional order not supported | Fall back to price monitoring (SL) or resting LIMIT (TP) |
| `-1111` | Precision error | Step size rounding applied |
| `-2010` | Insufficient balance | Logged → position skipped |

## Paper Exchange (`src/exchange/paper.py`)

Simulated trading for testing. Features:
- Instant fills at requested price (no slippage)
- Configurable balance (default: $100 USDT)
- Simulated fee: 0.04% per trade
- No API keys needed
- All state in memory

## Validation (`src/exchange/validation.py`)

Pre-submission order validation:
- **Min Notional**: `quantity * price >= min_notional`
- **Step Size**: quantity must be multiple of `stepSize`
- **Lot Size**: quantity within `[minQty, maxQty]`
- **Price Filter**: price within `[minPrice, maxPrice]`
- **Tick Size**: price must be multiple of `tickSize`
- **Min Notional Bump**: auto-increases qty when below minimum

## Symbol Registry (`src/exchange/symbol_registry.py`)

Dynamic symbol cache:
- Fetches all USDⓈ-M Futures pairs from Binance at startup
- Refreshes every 15 minutes
- Exposes `resolve(text)` and `get_symbol_info(symbol)`
- Caches min notional, step size, tick size, contract type for each pair
- Handles `1000`-prefix resolution for small-cap coins
