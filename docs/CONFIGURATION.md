# Configuration Reference

## `config.yaml`

The main configuration file controls exchange selection, risk parameters, agent behavior, and monitoring.

### Exchange Settings

```yaml
exchange:
  active: paper              # paper | binance_testnet | binance
  binance:
    api_key_env: BINANCE_API_KEY        # env var name for API key
    api_secret_env: BINANCE_API_SECRET  # env var name for secret
    testnet: false                      # true = use testnet endpoints
    futures: true                       # true = USDⓈ-M Futures, false = Spot
    recv_window: 5000                   # request validity window (ms)
```

| Key | Values | Default | Description |
|-----|--------|---------|-------------|
| `active` | `paper`, `binance_testnet`, `binance` | `paper` | Exchange backend to use |
| `binance.api_key_env` | string | `BINANCE_API_KEY` | Resolved from `.env` or environment |
| `binance.api_secret_env` | string | `BINANCE_API_SECRET` | Resolved from `.env` or environment |
| `binance.testnet` | `true`, `false` | `true` | Testnet uses fake money |
| `binance.futures` | `true`, `false` | `true` | Futures API not Spot |
| `binance.recv_window` | integer (ms) | `5000` | Binance request validity window |

### Risk Settings

```yaml
risk:
  max_position_size_usdt: 5       # Hard cap on position value
  max_concurrent_positions: 2     # Max open positions at once
  risk_per_trade_percent: 10.0    # % of balance to risk per trade
  daily_loss_limit_percent: 30.0  # Stop trading if daily loss exceeds
  min_cooldown_minutes: 5         # Min time between same-pair trades
  min_notional_usdt: 1.0          # Min position value (exchange req)
  max_position_hold_hours: 48     # Auto-close after this time
  max_leverage: 20                # Max futures leverage allowed
  margin_usage_pct: 50            # Max % of balance used as margin per trade
```

| Key | Range | Default | Description |
|-----|-------|---------|-------------|
| `max_position_size_usdt` | > 0 | `5` | Hard cap on position value in USDT. Combined with `risk_per_trade_percent` (the smaller is used). |
| `max_concurrent_positions` | 1–10 | `2` | Max simultaneously open positions. |
| `risk_per_trade_percent` | 0–100 | `10.0` | % of balance at risk if SL is hit. LLM sizes the position so loss = balance × this% |
| `daily_loss_limit_percent` | 0–100 | `30.0` | Stops all trading for the day if cumulative loss exceeds this % of balance. |
| `min_cooldown_minutes` | 0–1440 | `5` | Prevents re-entering the same pair within this window. |
| `min_notional_usdt` | > 0 | `1.0` | Exchange minimum position value. If below, Gate 2 scales up. |
| `max_position_hold_hours` | 1–720 | `48` | Auto-closes positions older than this. |
| `max_leverage` | 1–125 | `20` | Ceiling for dynamic leverage calculation. Higher = less margin needed. |
| `margin_usage_pct` | 1–100 | `50` | Target % of balance to use as margin per trade. Leverage is calculated to achieve this. |

### Dynamic Leverage Formula

```
margin_target = balance × (margin_usage_pct / 100)
leverage = ceil(position_value / margin_target)
leverage = clamp(leverage, 1, max_leverage)
```

Example with $10.58 balance, $5 position:
```
margin_target = $10.58 × 0.50 = $5.29
leverage = ceil($5.00 / $5.29) = 1x
```

Example with $10.58 balance, $50 position:
```
margin_target = $10.58 × 0.50 = $5.29
leverage = ceil($50.00 / $5.29) = 10x
```

### Agent Settings

```yaml
agent:
  confidence_threshold: 0.0    # Minimum LLM confidence (0.0 = all signals)
  auto_trade: true             # Master switch for live trading
  allowed_pairs:
    - "*"                      # Wildcard: allow any pair
  llm:
    api_url: "https://opencode.ai/zen/go/v1/chat/completions"
    model: "deepseek-v4-flash"
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 30
```

| Key | Values | Default | Description |
|-----|--------|---------|-------------|
| `confidence_threshold` | 0.0–1.0 | `0.0` | LLM's confidence value must be ≥ this for ENTER. 0.0 = trust all. |
| `auto_trade` | `true`, `false` | `true` | If false: LLM analyzes but no orders placed (dry-run). |
| `allowed_pairs` | list of strings, `["*"]` | `["*"]` | Filter which pairs to trade. E.g. `["BTCUSDT", "ETHUSDT"]`. |
| `llm.api_url` | URL | OpenCode Go | OpenAI-compatible chat completions endpoint. |
| `llm.model` | string | `deepseek-v4-flash` | Model name sent to API. |
| `llm.api_key_env` | string | `OPENCODE_GO_API_KEY` | Env var for API key. |
| `llm.timeout` | seconds | `30` | LLM request timeout. |

### Monitoring Settings

```yaml
monitoring:
  check_interval_seconds: 10   # Position manager poll interval
  health_check_port: 9090      # HTTP health check (for Fly.io)
```

## `.env` File Reference

```ini
TG_API_ID=12345678              # From my.telegram.org
TG_API_HASH=abc123...           # From my.telegram.org
CHANNEL_USERNAME=YOUR_SIGNAL_CHANNEL   # Channel to listen to
NOTIFY_CHAT_ID=YOUR_CHAT_ID       # Your Telegram chat ID
TELEGRAM_BOT_TOKEN=123:ABC...   # From @BotFather

BINANCE_API_KEY=xxx             # From Binance API Management
BINANCE_API_SECRET=xxx          # From Binance API Management
OPENCODE_GO_API_KEY=sk-...      # From OpenCode Go

# Fly.io only:
FLY_MODE=1                      # Enables persistent /data/ paths
DATA_ROOT=/data                  # Override data directory
```

## Environment Variable Precedence

1. `.env` file in project root (highest)
2. OS environment variables
3. Defaults in `config.yaml` (lowest)

## Pair Name Convention

- All pairs are normalized to `XXXUSDT` format
- The LLM may output `BTC` or `BTCUSDT` — the system appends `USDT` if missing
- Hash/currency symbols (`#BTC`, `$BTC`) are stripped
