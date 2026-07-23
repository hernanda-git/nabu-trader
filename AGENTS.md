# AGENTS.md — nabu-trader

> **An AI agent's guide to this project.** Read this first before making any changes.

## 📌 Project Identity

**What:** Real-time Telegram signal listener → LLM-powered analysis → automated Binance Futures trading pipeline.  
**Owner:** Hernanda (YOUR_EMAIL@gmail.com)  
**Channel:** @YOUR_SIGNAL_CHANNEL (*UNKNOWN TRADERS ACADEMY*)  
**Exchange:** Binance USDⓈ-M Futures (mainnet)  
**LLM:** OpenCode Go / deepseek-v4-flash  
**Deployed:** Fly.io (Singapore, `sin`)  
**Status:** 🚀 YOLO auto-trade ($5 max pos, 10% risk, 30% daily loss limit)

---

## 🚀 Quick Start for AI Agents

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/nabu-trader.git
cd nabu-trader

# 2. Install deps
pip install -r requirements.txt

# 3. Copy env + configure
cp .env.example .env
# Edit .env with your Telegram API credentials, bot token, etc.

# 4. Run (paper mode for testing)
# Edit config.yaml: exchange.active: paper, agent.auto_trade: false
python src/main.py
```

---

## 🏗 Architecture

### Pipeline (one signal = one pass through all stages)

```
@YOUR_SIGNAL_CHANNEL ──► SignalListener ──► Orchestrator ──► Exchange
                      │                    │              │
                 Telethon          1. Regex parse       Binance
                 user session      2. Gate 1 (pre-LLM)  Futures API
                      │            3. Fetch balance     (fapi.binance.com)
                      │            4. LLM decide        httpx.AsyncClient
                      │            5. Gate 2 (clamp)
                      ▼            6. Execute order     ▼
                 Telegram Bot       7. Notify ─────► SQLite DB (14 tables)
                 (notifier)          8. LLM log          │
                                      9. Config snap   /data/data/trades.db
                                     10. Trade logs        │
                                     11. Position events   │
                                                            ▼
                                                   FastAPI (port 9090)
                                                   Hermes ←→ API Bridge
```

> **DB location (corrected 2026-07-12):** the live SQLite DB is
> **`/data/trades.db`** on the Fly volume (`DATA_ROOT=/data`). It used to be
> nested at `/data/data/trades.db` (a `get_data_dir()` bug, now fixed). If you
> ever see a second `/data/data/trades.db`, that is the OLD path — ignore it.

### Directory Layout

```
src/
├── main.py                      # Entry point — wires everything
├── orchestrator.py              # Pipeline coordinator + structured logging
├── listener.py                  # Telethon Telegram listener
├── api/
│   ├── __init__.py              # API package
│   ├── auth.py                  # API key + HMAC auth + rate limiter
│   ├── server.py                # FastAPI server (15 endpoints)
│   └── webhook.py               # Trade event webhook emitter
├── agent/
│   ├── agent.py                 # LLM brain (OpenCode Go)
│   ├── gate.py                  # Safety Gate 1 + Gate 2
│   └── parser.py                # Regex signal parser
├── exchange/
│   ├── base.py                  # ABC (abstract interface)
│   ├── binance.py               # Real Binance REST API
│   └── paper.py                 # Simulated paper trading
├── execution/
│   ├── order_service.py         # Translate decision → orders
│   └── position_manager.py      # Background position monitor
├── notifier/
│   └── telegram.py              # Telegram Bot API notifications
├── state/
│   ├── database.py              # SQLite setup + schema + migrations
│   └── repositories.py          # DAO layer per entity (11 repos)
├── domain/
│   └── models.py                # Frozen dataclasses (12 models)
├── events/
│   └── bus.py                   # In-process pub/sub
└── config/
    ├── loader.py                # YAML + .env merge
    └── validator.py             # Fail-fast startup validation
```

---

## 📐 Code Conventions (Agents MUST Follow)

### Async Everywhere
- **All** I/O is `async def` — never `httpx.Client` (sync), always `httpx.AsyncClient`
- `await` every HTTP call, DB call, and exchange method
- Never block the event loop with sync requests

### Domain Models
- All inter-module data uses frozen dataclasses (`@dataclass(frozen=True)`)
- `TradeSignal` = raw parsed signal (immutable)
- `TradeDecision` = LLM output (immutable, includes leverage)
- `Position` = mutable (state changes over time)
- `ExecutionResult`, `OrderInfo`, `BalanceInfo` = exchange responses

### Error Handling
- **Binance 401/403** → `BinanceErrorCategory.AUTH` — fail-fast, don't retry
- **Binance 429** → `RATE_LIMIT` — retryable with backoff
- **Binance 5xx** → `SERVER` — retryable
- **Network errors** → `NETWORK` — return `PENDING` status (not FAILED)
- **Order failures** → return `OrderInfo(status="FAILED", error="[category] message")`

### Datetime Standard
- **Always** `datetime.now(timezone.utc)` — NEVER `datetime.utcnow()` (deprecated)
- All model default factories use `lambda: datetime.now(timezone.utc)`
- SQLite stores UTC via `CURRENT_TIMESTAMP`

### Gate Safety Philosophy
- **Gate 1** (pre-LLM): cheap checks that save LLM token cost — idempotency, whitelist, cooldown
- **Gate 2** (post-LLM): hard clamps the LLM cannot override — position size, leverage, daily loss
- Gate 2 runs ONLY for `ENTER` decisions; SKIP/CLOSE exit before Gate 2
- When adding a new safety rule, ALWAYS put it in the correct gate

### LLM Interaction
- Single call per signal — no multi-turn conversations
- Prompt includes: raw text, regex pre-parse, open positions, account balance
- `response_format: {"type": "json_object"}` enforces structured output
- Response parsed by `_parse_decision()` with 3 fallback strategies (direct JSON → code-fence → regex)
- **Full interaction captured**: Every LLM call records prompt, response, token counts, latency to DB (`llm_interactions` table)
- Dry-run: `agent.auto_trade: false` logs decisions without executing

### Repository Pattern
- Business logic NEVER touches SQL directly
- Each entity has its own repository class (11 total)
- All repositories accept an optional `sqlite3.Connection` in constructor
- One connection per process (created in `main.py`, passed to all repos)
- **New repos**: `LLMInteractionRepository`, `TradeLogRepository`, `PositionEventRepository`, `ConfigSnapshotRepository`

### Correlation ID Tracing
- Every pipeline run generates a unique `correlation_id` (12-char hex)
- The ID flows through: signal → decision → order → position → trade logs → position events
- Query full pipeline trace: `GET /api/v1/logs/{correlation_id}` or `SELECT * FROM trade_logs WHERE correlation_id = ?`

---

## 🐛 Known Pitfalls

| Pitfall | Symptom | Fix |
|---------|---------|-----|
| **Cross-platform Fly auth** | `flyctl status` returns "Could not find App" | Use Windows `flyctl` — WSL token doesn't have access |
| **Edited messages** | Duplicate signals from same message_id | Fixed: `get_by_message_id()` in Gate1 blocks edits |
| **LLM returns text instead of JSON** | "LLM parse error, skipping" | Fixed: `response_format: json_object` in API call |
| **Position manager stale orders** | SL/TP not placed | Check exchange balance + open orders manually |
| **Binance 401 on order** | "code:-2015 Invalid API-key" | API key missing Futures permission — check Binance API mgmt |
| **1000x STOP orders blocked** | `-4120 Use Algo Order API` | Binance limitation — SL handled by position-manager monitoring (mark price + 1m klines polling). Use `get_mark_price()` for faster detection |
| **1000x LIMIT SL fills instantly** | Position closed immediately | LIMIT SELL below market is NOT a valid SL — use position manager monitoring or MARKET |
| **1000x filter cache miss** | `-1111 Precision over maximum` for low-price coins | Fixed: `_round_quantity()` now lazy-loads filters on miss, falls back to integer rounding |
| **Windows/WSL out of sync** | Deploy pushes old code | Always `cp` changed files to Windows path before `flyctl deploy` |
| **API_KEY not set on Fly.io** | API returns 401 for all requests | `flyctl secrets set API_KEY=<key> --app nabu-trader` then restart |
| **HMAC signing mismatch** | POST requests return 401 | Ensure client and server use same API_HMAC_SECRET |
| **Rate limited by API bridge** | 429 Too Many Requests | Wait 60s or reduce query frequency (30 req/min per IP) |
| **DB migration skipped columns** | New columns show as NULL | Safe — `_run_migrations()` uses try/except for ALTER TABLE |
| **Webhook URL not set** | Trade events not pushed | Set `WEBHOOK_URL` env var on Fly.io; push is fire-and-forget |
| **close notification not sent** | SL/TP close position but Telegram not notified | Fixed v103: `_finalize_close` now sends Telegram notification with PnL, direction, reason |
| **Health report markdown error** | Telegram "can't parse entities" on /health | Fixed v103: dynamic health check fields now escaped with `_md_escape()` |
| **uvicorn fails to start** | API bridge not available | Check port conflicts; bot continues without API, log shows "API bridge not started" |

### ⚠️ 1000x Contract Compatibility

Binance lists some low-price tokens (BONK, PEPE, SHIB, FLOKI) as "1000x" contracts on USDⓈ-M Futures. These have critical differences:

**Current mapped symbols:**
| User Symbol | Exchange Symbol | Base Asset |
|---|---|---|
| `BONKUSDT` | `1000BONKUSDT` | 1000BONK (price/tick in BONK scale) |
| `PEPEUSDT` | `1000PEPEUSDT` | 1000PEPE |
| `SHIBUSDT` | `1000SHIBUSDT` | 1000SHIB |
| `FLOKIUSDT` | `1000FLOKIUSDT` | 1000FLOKI |

**Semantics:**
- **Price:** Quoted in base asset price (e.g., 1 BONK = 0.0044 USDT). NOT multiplied by 1000.
- **Quantity:** In base asset tokens (7309 BONK). NOT divided by 1000.
- **LOT_SIZE:** `stepSize=1`, `minQty=1` (integer quantity only)
- **MIN_NOTIONAL:** `5 USDT` (enforced as `qty × price`)

**SL/TP Limitations (API):**
- `LIMIT SELL` ABOVE market → ✅ Take Profit (resting order, waits for price rise)
- `LIMIT SELL` BELOW market → ❌ NOT a valid Stop Loss (fills instantly as cheap ask)
- `STOP_MARKET`, `TAKE_PROFIT_MARKET` → ❌ blocked (-4120) on most contracts
- `STOP`, `TAKE_PROFIT` (stop-limit / tp-limit) → ✅ Conditional tab, fills as LIMIT (no slippage). Used by default for all contracts.
- **If `STOP`/`TAKE_PROFIT` blocked (-4120) on a contract**, the bot falls back: SL → position-manager monitoring (polls mark price, closes via LIMIT on breach); TP → resting Basic-tab LIMIT at the TP price.
- **SL/TP default strategy (v54+):** SL = `STOP` (Conditional), TP = `TAKE_PROFIT` (Conditional), both LIMIT fills. Fallbacks applied per-contract.

---

## 🔧 Agentic Workflows

### 1. Health Check
```bash
# From git-bash/MSYS (NOT PowerShell/cmd — the Hermes terminal runs bash):
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl status --app nabu-trader
flyctl logs --app nabu-trader --no-tail

# Key things to check:
# - Machine state == "started"
# - Latest logs show "Listening for new messages..."
# - No 401 errors from Binance
# - No "LLM returned invalid JSON" warnings
```

### 2. Deploy New Code

Deploy directly from git-bash/MSYS (the Hermes terminal runs bash). The Fly CLI
binary is at `/c/Users/it26/.fly/bin/flyctl.exe` — add it to PATH first.

```bash
export PATH="$PATH:/c/Users/it26/.fly/bin"
cd "/c/Working Folder/Research/nabu-trader"
source .venv/Scripts/activate
python -m pytest -q            # 1. verify: must be green before committing
git add src/... tests/...      # 2. stage specific paths (NOT -A — avoids .hermes/ + junk)
git commit -m "..."            # 3. commit
git push origin fix/code-review-fixes   # 4. push
flyctl deploy                  # 5. deploy (rolling update, ~1-2 min)
flyctl status                  # machine STATE=started + new deployment hash
curl -s -o /dev/null -w "HTTP %{http_code}\n" https://nabu-trader.fly.dev/health   # expect HTTP 200
flyctl logs --no-tail          # confirm boot: "Listening for new messages..." + sendMessage 200 OK
```

**Standing "verify → commit → push → deploy" loop** (full version in the
`crypto-auto-trader-reliability` skill reference `nabu-bot.md`; the
live end-to-end verification recipe is in the `fly-bot-deploy-verify` Hermes
skill). Reusable helper script: `scripts/deploy_and_verify.sh`.

**Staging gotcha:** prefer `git add <specific paths>` over `git add -A` — `-A`
staged agent-internal dirs (`.hermes/`) and stray untracked files. `.gitignore`
now excludes `.hermes/`.

### 3. Add a New Signal Format
1. Edit `src/agent/parser.py` — add regex pattern + test
2. The parser returns `TradeSignal(pair, direction, entry_price, sl_price, tp_prices)`

### 4. Add a New Exchange
1. Create `src/exchange/<name>.py` implementing `Exchange` ABC
2. Add config key in `config.yaml` → `exchange.active`
3. Add import + instantiation in `src/main.py`

### 5. Debug a Failed Trade
1. Check Fly logs for the error category (`[auth]`, `[rate]`, `[server]`, `[network]`)
2. Auth errors → regenerate Binance API key
3. Network errors → usually transient, check again
4. Gate rejections → check config limits vs account balance

### 6. Set Binance API Key
```bash
# From Windows PowerShell:
flyctl secrets set BINANCE_API_KEY=xxx BINANCE_API_SECRET=xxx

# Verify:
flyctl secrets list --app nabu-trader
```

### 7. Check Binance Futures Balance
```bash
# From git-bash/MSYS. Pipe a heredoc (NOT -C 'python -c ...' — nested quotes break):
export PATH="$PATH:/c/Users/it26/.fly/bin"
flyctl ssh console --app nabu-trader <<'PY'
python - <<'PY'
import os, asyncio
os.chdir("/app")
from src.exchange.binance import BinanceExchange
e=BinanceExchange(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"],
                  futures=True, testnet=False)
b=asyncio.run(e.get_balance())
print(f"Free: ${b.free_usdt}, Total: ${b.total_usdt}")
PY
exit
PY
```

### 8. Query Trades via API Bridge
The bot exposes a secure HTTP API on port 9090. Once deployed with `API_KEY` set:

```bash
# Stats dashboard
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/stats

# Full trade trace
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/trades/1

# Search LLM decisions
curl -s -H "X-API-Key: $API_KEY" \
  "https://nabu-trader.fly.dev/api/v1/llm/search?q=SKIP"
```

### 9. Pipeline Trace via Correlation ID
Every pipeline run generates a correlation ID. Use it to reconstruct the full execution:

```bash
curl -s -H "X-API-Key: $API_KEY" \
  https://nabu-trader.fly.dev/api/v1/logs/{correlation_id}
```

Returns: all structured logs + signal + decision + positions for that run.

### 10. Agentic Post-Mortem
When investigating a failed trade:
1. `GET /api/v1/trades/{id}` — full trade with LLM interaction + position events
2. Check `trade_logs[]` for ERROR entries
3. Examine `llm_interaction.user_prompt` to see what the LLM was told
4. Compare with `config_snapshot` to see what risk settings were active
5. Load `fly-trade-bridge` skill for structured query workflows

---

## 📋 Configuration Reference

See [`config.yaml`](config.yaml) for all settings.

| Key | Default | Description |
|-----|---------|-------------|
| `exchange.active` | `paper` | `paper`, `binance`, or `binance_testnet` |
| `exchange.binance.futures` | `true` | Use USDⓈ-M Futures |
| `risk.max_position_size_usdt` | `5` | Hard cap per position |
| `risk.risk_per_trade_percent` | `10` | % of balance to risk per trade |
| `risk.max_concurrent_positions` | `2` | Max open positions at once |
| `risk.daily_loss_limit_percent` | `30` | Stop trading if daily loss exceeds this |
| `risk.max_leverage` | `20` | Cap for dynamic leverage |
| `agent.auto_trade` | `true` | YOLO mode — enable real trading |
| `agent.llm.model` | `deepseek-v4-flash` | LLM model via OpenCode Go |
| `monitoring.check_interval_seconds` | `10` | Position manager loop interval |

---

## 📦 Deployment (Fly.io)

### Architecture
- **Region:** Singapore (`sin`)
- **Machine:** 1x shared-cpu-1x, 256MB RAM
- **Volume:** `/data` (persistent — survives restarts)
- **Port:** 9090 (health check only — app is Telegram-based, not HTTP)

### Secrets (set via `flyctl secrets set`)
```
TG_API_ID, TG_API_HASH, TELEGRAM_BOT_TOKEN, NOTIFY_CHAT_ID
BINANCE_API_KEY, BINANCE_API_SECRET, OPENCODE_GO_API_KEY
CHANNEL_USERNAME
```

### Persistent Data
| Path | Contents |
|------|----------|
| `/data/trades.db` | SQLite (14 tables: signals, decisions, orders, positions, llm_interactions, trade_logs, position_events, config_snapshots, etc.) — **DB path corrected 2026-07-12** from the old nested `/data/data/trades.db`. |
| `/data/sessions/nabu.session` | Telethon session (auth persistence) |
| `/data/logs/trading.log` | Debug logs |

### ⚠️ Cross-Platform Auth
The app is deployed via the **Windows Fly CLI** binary at
`YOUR_HOME\.fly\bin\flyctl.exe`. Add it to PATH in git-bash/MSYS:
`export PATH="$PATH:/c/Users/it26/.fly/bin"`. Run `flyctl` directly from
git-bash — do NOT shell out through `powershell.exe` (the nested quoting breaks
SSH-console heredocs). The `flyctl` binary (not the WSL `fly` token) owns this
app.

---

## 🧪 Testing

```bash
# Manual LLM test (dry-run)
cd /mnt/c/"Working Folder/Research/nabu-trader"
python -c "
import sys; sys.path.insert(0, '.')
from src.agent.agent import AgentBrain
from src.config.loader import load_config
from src.domain.models import TradeSignal

cfg = load_config()
brain = AgentBrain(cfg)
signal = TradeSignal(message_id=1, channel='test',
    raw_text='BUY BTCUSDT Entry: 65400 SL: 64800 TP: 66200')
decision = brain.decide(signal)
print(f'{decision.action} {decision.pair} conf={decision.confidence}')
"
```

---

## 🛡 Trade Execution Reliability Runbook (APE-1111 fixes)

Every order passes through ONE deterministic, fully-validated pipeline. No LLM
involvement in repair. After the 2026-07-09 reliability pass:

### Execution pipeline (single path)
1. Resolve the futures symbol (`BinanceExchange._resolve_futures_symbol`).
2. Load the symbol's **real** exchange filters by symbol
   (`_load_futures_filters`) — NOT `rules[0]`. Captures `tickSize`, `minPrice`,
   `maxPrice`, `stepSize`, `minQty`, `minNotional`.
3. Round price to the symbol's `tickSize`, qty to its `stepSize` + integer lots
   (`_round_price`, `_round_quantity`). `_round_quantity` loops until
   `qty × price ≥ minNotional` using the **real** filter.
4. `SafetyGate2.check` sizes qty/leverage from `config.risk`
   (`port_usdt`, `max_leverage`, `max_leverage_increase_pct`, exchange
   `minNotional`). No hardcoded 5/1/50 literals in the math.
5. **Pre-submission gate** `validate_order` (in `src/exchange/validation.py`)
   checks precision, min/max price, minQty, minNotional, integer lots — must
   return `None` or the order is **skipped** (`VALIDATION_SKIP`), never sent.
6. Entry is a **LIMIT** order. Before placing, `OrderRepository.get_active_for_decision`
   dedupes — a decision can only ever have one active entry (idempotent).
7. If the exchange rejects, `_repair_order` deterministically re-derives
   qty/price from filters, recomputes leverage, validates, and resubmits **once**.
   If it still can't validate → `VALIDATION_SKIP`. **The LLM fallback was removed.**

### Config knobs
| Key | Default | Purpose |
|-----|---------|---------|
| `risk.port_usdt` | 1.0 | margin per trade; `leverage = pos_value / port_usdt` |
| `risk.max_leverage` | 50 | hard ceiling for dynamic leverage |
| `risk.max_leverage_increase_pct` | 10 | cap dynamic lev at `baseline×(1+pct%)` |
| `risk.min_notional_usdt` | 5.0 | floor; overridden by the symbol's real `minNotional` |
| `risk.margin_usage_pct` | 80 | % of balance usable as margin per trade |

### How to verify before deploying
```bash
# from repo root, using the project venv
.venv/Scripts/python -m pytest tests/ -q      # WIN: use .venv interpreter
# or: uv run pytest tests/ -q
```
All pairs (BTC/ETH/APE/DOGE/1000PEPE) are covered by `tests/exchange/test_pair_matrix.py`.

### Deploy note
No DB schema change in this pass (tables already had `status DEFAULT 'PENDING'`
and `client_order_id UNIQUE`). Config-only + code changes are safe to deploy
with a rolling `fly deploy`. After deploy, confirm the order path by sending a
test signal in paper mode and checking the Telegram notification says either
`LIMIT order placed`, `Trade entered (repaired)`, or a `VALIDATION_SKIP`
(rejection) — never an unvalidated fill.

---

## 🔗 Quick Links

| File | Purpose |
|------|---------|
| [`README.md`](README.md) | Project overview, features, quick start |
| [`config.yaml`](config.yaml) | All configuration |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Full architecture + data flow + DB schema |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | Fly.io + local deployment + API key setup |
| [`docs/RISK_MANAGEMENT.md`](docs/RISK_MANAGEMENT.md) | Safety gates + risk rules |
| [`docs/API.md`](docs/API.md) | Exchange adapter API reference |
| [`docs/SIGNAL_PARSING.md`](docs/SIGNAL_PARSING.md) | Regex patterns for signal formats |
| [`docs/FLY_OPERATIONS.md`](docs/FLY_OPERATIONS.md) | Fly.io operations, health checks, logs, SSH |
| [`docs/SIGNAL_PARSING.md`](docs/SIGNAL_PARSING.md) | Regex patterns for signal formats |
| [`src/api/server.py`](src/api/server.py) | API bridge server (15 endpoints) |
| [`src/api/auth.py`](src/api/auth.py) | Auth middleware + rate limiter |
| [`CHANGELOG.md`](CHANGELOG.md) | Version history |
| [`PLAN.md`](PLAN.md) | Original design plan |
| [`PLAN_REVISE.md`](PLAN_REVISE.md) | Revised plan |
