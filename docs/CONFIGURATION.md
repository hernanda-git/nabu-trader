# Configuration Reference

All configuration is in `config.yaml` at the project root. Secrets (API keys) are loaded from environment variables or `.env` file.

---

## Exchange Settings

```yaml
exchange:
  active: binance              # paper | binance | binance_testnet
  binance:
    api_key_env: BINANCE_API_KEY
    api_secret_env: BINANCE_API_SECRET
    testnet: false
    futures: true              # false = spot, true = USDⓈ-M futures
    recv_window: 5000
```

| Key | Default | Description |
|-----|---------|-------------|
| `active` | `binance` | Which exchange backend to use. `paper` = simulated trading (no real API calls), `binance_testnet` = Binance testnet, `binance` = real Binance mainnet |
| `binance.api_key_env` | `BINANCE_API_KEY` | Environment variable name holding the API key |
| `binance.api_secret_env` | `BINANCE_API_SECRET` | Environment variable name holding the API secret |
| `binance.testnet` | `false` | Use testnet.binance.vision instead of mainnet API |
| `binance.futures` | `true` | USDⓈ-M Futures API (false = Spot trading API) |
| `binance.recv_window` | `5000` | Timestamp recv window in ms (anti-replay) |

### Gateway Proxy (Optional)

```yaml
exchange:
  binance:
    proxy:
      enabled: false
      url: "https://binance-gateway.fly.dev"   # overridden by GATEWAY_URL env
      hmac_secret: ""                            # overridden by GATEWAY_HMAC_SECRET env
```

When enabled, ALL Binance REST calls route through a signed gateway relay. The bot does NOT need the Binance API key directly — only `GATEWAY_URL` + `GATEWAY_HMAC_SECRET`.

---

## Risk Settings

```yaml
risk:
  port_usdt: 1.0               # $ margin per trade (leverage scales position)
  max_port_pct: 10             # hard ceiling: position never >10% of balance
  max_position_size_usdt: 5    # fallback max size when balance unavailable
  max_concurrent_positions: 2
  risk_per_trade_percent: 10.0
  daily_loss_limit_percent: 30.0
  min_cooldown_minutes: 5
  min_notional_usdt: 5.0       # Binance minimum, scaled up to avoid rejection
  max_position_hold_hours: 48
  max_leverage: 20             # bot default leverage ceiling
  max_leverage_increase_pct: 10
  margin_usage_pct: 80         # use up to 80% of balance as margin per trade
  margin_type: ISOLATED        # ISOLATED or CROSSED
  max_portfolio_leverage: 10   # total notional / balance cap
```

| Key | Default | Description |
|-----|---------|-------------|
| `port_usdt` | `1.0` | Margin budget per trade in USDT. Leverage = position_value / port_usdt |
| `max_port_pct` | `10` | Hard ceiling: position value never exceeds 10% of total balance |
| `max_position_size_usdt` | `5` | Fallback max position value when balance info is unavailable |
| `max_concurrent_positions` | `2` | Maximum simultaneously open positions |
| `risk_per_trade_percent` | `10` | Risk X% of balance per trade (Gate 2 clamp) |
| `daily_loss_limit_percent` | `30` | Stop trading for the day if drawdown exceeds this |
| `min_cooldown_minutes` | `5` | Minimum minutes between trades on the same pair |
| `min_notional_usdt` | `5` | Minimum order notional (Binance requirement) |
| `max_position_hold_hours` | `48` | Auto-close positions held longer than this |
| `max_leverage` | `20` | Default leverage ceiling; per-pair Binance max clamps further |
| `max_leverage_increase_pct` | `10` | Allow leverage to increase up to 10% beyond baseline to meet min notional |
| `margin_usage_pct` | `80` | Use at most 80% of available balance as margin per trade |
| `margin_type` | `ISOLATED` | `ISOLATED` = isolated margin per position, `CROSSED` = cross margin |
| `max_portfolio_leverage` | `10` | Total portfolio notional / balance ≤ 10x |

---

## Agent Settings

```yaml
agent:
  confidence_threshold: 0.0
  auto_trade: true           # YOLO mode activated! 🚀
  allowed_pairs:
    - "*"                     # wildcard: allow any pair
  llm:
    api_url: "https://opencode.ai/zen/go/v1/chat/completions"
    model: "deepseek-v4-flash"
    api_key_env: OPENCODE_GO_API_KEY
    timeout: 30
```

| Key | Default | Description |
|-----|---------|-------------|
| `confidence_threshold` | `0.0` | Minimum LLM confidence to execute (0.0 = trust all) |
| `auto_trade` | `true` | `true` = live trading, `false` = dry-run (analyze only, no orders) |
| `allowed_pairs` | `["*"]` | Pair whitelist. `"*"` = any pair. Can list specific: `["BTCUSDT", "ETHUSDT"]` |
| `llm.api_url` | OpenCode Go | OpenAI-compatible chat completions endpoint |
| `llm.model` | `deepseek-v4-flash` | Model name |
| `llm.api_key_env` | `OPENCODE_GO_API_KEY` | Env var for API key |
| `llm.timeout` | `30` | LLM request timeout in seconds |

---

## Monitoring Settings

```yaml
monitoring:
  check_interval_seconds: 10
  health_check_port: 9090
  health_report_hours: 6      # periodic /health to Telegram every N hours
```

| Key | Default | Description |
|-----|---------|-------------|
| `check_interval_seconds` | `10` | Position manager monitoring loop interval |
| `health_check_port` | `9090` | HTTP health check port (used by Fly.io) |
| `health_report_hours` | `6` | Auto-post /health report to Telegram every N hours |

---

## API Bridge

```yaml
api:
  enabled: true
  host: "0.0.0.0"
  port: 9090
```

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable the REST API bridge |
| `host` | `0.0.0.0` | Bind address |
| `port` | `9090` | Listen port |

API key is loaded from `API_KEY` environment variable. Rate limit: 30 req/min/IP.

---

## Webhook

```yaml
webhook:
  url: ""                       # Set via env WEBHOOK_URL or config
  hmac_secret: ""               # Set via env WEBHOOK_HMAC_SECRET or config
```

When configured, trade events are POSTed as signed JSON to the webhook URL.

---

## Environment Variables (.env)

| Variable | Required | Description |
|----------|----------|-------------|
| `TG_API_ID` | ✅ | Telegram API ID (from my.telegram.org) |
| `TG_API_HASH` | ✅ | Telegram API hash |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `NOTIFY_CHAT_ID` | ✅ | Your Telegram chat ID for notifications |
| `CHANNEL_USERNAME` | ✅ | Signal channel username (e.g. `YOUR_SIGNAL_CHANNEL`) |
| `SESSION_STRING` | ✅ | Telethon string session (generated by auth.py) |
| `BINANCE_API_KEY` | ✅* | Binance API key (Futures-enabled) |
| `BINANCE_API_SECRET` | ✅* | Binance API secret |
| `OPENCODE_GO_API_KEY` | ✅ | OpenCode Go API key for LLM |
| `API_KEY` | ⚠️ | Required for HTTP API bridge |
| `WEBHOOK_URL` | Optional | Trade event webhook URL |
| `WEBHOOK_HMAC_SECRET` | Optional | Webhook HMAC signing secret |
| `GATEWAY_URL` | Optional | Binance gateway proxy URL |
| `GATEWAY_HMAC_SECRET` | Optional | Gateway HMAC secret |

*Not required when using paper trading or gateway proxy mode.

---

## Trading Pairs

The bot uses a **dynamic symbol registry** (`src/exchange/symbol_registry.py`) that fetches all tradable USDⓈ-M Futures pairs from Binance at startup (via `exchangeInfo`). No hardcoded pair list needed.

**Pair resolution order:**
1. Strip `$`, `#`, whitespace → uppercase
2. Check SymbolRegistry (cached exchangeInfo)
3. If ends with USDT/USD/BUSD/USDC → use as-is
4. If not found, append `USDT` and try again
5. If still not found, try `1000` prefix (for small-cap coins)
6. If still not found → signal rejected

**Spread syntax:** The channel may post `BONK` (bare base) → resolved to `1000BONKUSDT` via the registry.
