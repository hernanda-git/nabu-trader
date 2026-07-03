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
    async def limit_buy(self, symbol: str, quantity: float, price: float) -> OrderInfo: ...
    async def limit_sell(self, symbol: str, quantity: float, price: float) -> OrderInfo: ...
    async def stop_loss(self, symbol: str, quantity: float, stop_price: float, side: str = "SELL") -> OrderInfo: ...
    async def cancel_order(self, symbol: str, order_id: str) -> bool: ...
    async def get_order(self, symbol: str, order_id: str) -> OrderInfo: ...
    async def get_open_orders(self, symbol: str | None = None) -> list[OrderInfo]: ...

    # Optional (no-op by default):
    async def set_symbol_leverage(self, symbol: str, leverage: int): ...
    async def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED"): ...
```

### Data Types

```python
@dataclass
class BalanceInfo:
    total_usdt: float = 0.0      # Total equity
    free_usdt: float = 0.0       # Available for trading
    assets: dict[str, dict] | None = None

@dataclass
class OrderInfo:
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    type: str = ""
    quantity: float = 0.0
    price: float = 0.0
    status: str = ""
    filled_quantity: float = 0.0
    avg_price: float = 0.0
    error: str | None = None
```

## Paper Exchange (`src/exchange/paper.py`)

Simulated trading for testing:

| Feature | Behavior |
|---------|----------|
| Balance | Configurable starting balance (default: $10,000) |
| Fill price | 100% at requested price (no slippage) |
| Fill time | Instant (no latency) |
| Balance tracking | Deducts from balance on buy, adds on sell |
| Leverage | Not supported (1x always) |

**Use for:** Testing the pipeline logic without real money or API keys.

## Binance Exchange (`src/exchange/binance.py`)

Real Binance REST API adapter. Supports both Spot and USDⓈ-M Futures.

### API Endpoints Used

| Function | Endpoint | Auth |
|----------|----------|------|
| Account balance | `GET /fapi/v2/account` | Signed |
| Set leverage | `POST /fapi/v1/leverage` | Signed |
| Set margin type | `POST /fapi/v1/marginType` | Signed |
| Place order | `POST /fapi/v1/order` | Signed |
| Cancel order | `DELETE /fapi/v1/order` | Signed |
| Get order | `GET /fapi/v1/order` | Signed |
| Open orders | `GET /fapi/v1/openOrders` | Signed |

For Spot mode, uses `/api/v3/` equivalents.

### Order Type Mapping (Futures)

| Input Type | Futures Equivalent |
|------------|-------------------|
| `MARKET` | `MARKET` |
| `LIMIT` | `LIMIT` |
| `STOP_LOSS` | `STOP_MARKET` |
| `TAKE_PROFIT` | `TAKE_PROFIT_MARKET` |

### Futures-Specific Features

**Dynamic Leverage** — Set per-symbol before each trade:
```python
await exchange.set_symbol_leverage("ETHUSDT", 5)  # 5x leverage
```

**Isolated Margin** — Each position has separate margin:
```python
await exchange.set_margin_type("ETHUSDT", "ISOLATED")
```

**Balance Reading** — Reads from futures wallet (not spot):
```python
# Returns:
#   total_usdt: marginBalance (equity incl. unrealized PnL)
#   free_usdt: crossWalletBalance (available for new positions)
```

### Configuration

```yaml
exchange:
  active: binance
  binance:
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
    testnet: false       # true = testnet.binancefuture.com
    futures: true        # true = fapi, false = api/v3
    recv_window: 5000
```

### API Key Requirements

On Binance → API Management → Edit Restrictions:
- ✅ Enable **Enable Futures** (required for futures mode)
- ✅ Enable **Enable Spot & Margin** (for balance lookup)
- ❌ Disable **Enable Withdrawals** (security)
- ✅ Optionally: **Restrict access to trusted IPs**

## Adding a New Exchange

1. Create `src/exchange/your_exchange.py`
2. Extend `Exchange` ABC from `base.py`
3. Implement all abstract methods
4. Wire it in `src/main.py` (add to exchange mode switch)
5. Add config section to `config.yaml`

```python
from src.exchange.base import Exchange, BalanceInfo, OrderInfo

class YourExchange(Exchange):
    @property
    def name(self) -> str:
        return "your_exchange"

    async def get_balance(self) -> BalanceInfo:
        # Implement balance fetch
        ...

    async def market_buy(self, symbol: str, quantity: float) -> OrderInfo:
        # Implement market buy
        ...
    # ... implement remaining methods
```
