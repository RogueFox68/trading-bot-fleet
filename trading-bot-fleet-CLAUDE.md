# CLAUDE.md - Trading Bot Fleet

> **System Context:** This repo runs on the **Beelink S12 Mini** (Ubuntu), the
> execution node in a two-machine trading system. The companion repo
> `TradingAgent` runs on the **Corsair AI Workstation** (AMD Strix Halo) and
> handles all market scanning and AI-powered target generation. See
> `CORSAIR_ARCHITECTURE.md` in that repo for details.

## Project Overview

Autonomous multi-strategy trading bot fleet for US equities, options, and cryptocurrency via Alpaca Markets. The fleet consists of 5 active trading strategies and 3 infrastructure services, orchestrated through PM2 and controlled via Discord.

**Language:** Python 3 (100%)  
**Broker API:** Alpaca Markets (paper trading — live trading capable)  
**Process Manager:** PM2  
**Monitoring:** InfluxDB 1.8 + Grafana (both Dockerized on the Beelink)  
**Notifications:** Discord webhooks + Discord bot commands

## Repository Structure

This is a flat repository with no subdirectories. All Python modules live at the root level.

```
trading-bot-fleet/
├── CLAUDE.md                    # This file
├── requirements.txt             # Python dependencies
├── bot_config.template.json     # Runtime config template (copy to bot_config.json)
├── .gitignore                   # Ignores secrets, logs, live config
│
├── Trading Bots (Active)
│   ├── wheel_bot.py             # Options premium selling (40% base allocation, VIX/regime gated)
│   ├── trend_bot.py             # EMA momentum, long/short capable (30% base allocation)
│   ├── survivor_bot.py          # RSI mean-reversion dip buying (20% base allocation)
│   ├── crypto_grid.py           # BTC/ETH/SOL grid trading on Alpaca (5% base allocation)
│   └── crypto_breakout.py       # Donchian breakout for crypto (moon_bot, 5% allocation)
│
├── Trading Bots (Sidelined)
│   └── condor_bot.py            # Iron condor options selling — explicitly excluded from all
│                                # allocations. Alpaca can't handle multi-leg complexity reliably.
│
├── Infrastructure
│   ├── commander.py             # Discord bot for fleet management + watchdog
│   ├── accountant.py            # CFO: P&L attribution, budget enforcement, InfluxDB metrics
│   └── market_analyst.py        # Market regime detection (SPY-based: BULL/SIDEWAYS/BEAR_TREND/
│                                # CRITICAL_VOLATILITY) + VIX monitoring
│
└── Utilities
    ├── utils.py                 # Shared: BOT_MAPPING, budget checks, ownership resolution,
    │                            # active_targets loader, parse_target()
    ├── logger.py                # Centralized logging config
    ├── config.py                # (gitignored) API keys, tokens, webhook URLs, InfluxDB config
    ├── sector_scout_legacy.py   # Mothballed — scanning now handled by Corsair workstation
    └── export_data.py           # Export InfluxDB trade data to CSV
```

## Two-Machine Architecture

```
┌─────────────────────────────────┐       SCP Transfer        ┌──────────────────────────────────┐
│   CORSAIR AI WORKSTATION        │    active_targets.json     │   BEELINK S12 MINI               │
│   ("The Brain")                 │ ─────────────────────────► │   ("Execution Node")             │
│                                 │    3x daily (08:30,        │                                  │
│   AMD Strix Halo (96GB)        │    12:00, 15:00 CT)        │   Intel N100 (16GB)              │
│   Ollama → Llama 3.3 70B      │                            │   Ubuntu Linux                   │
│   Docker: ollama_backend +     │                            │                                  │
│           sector_scout_bot     │                            │   Native: Trading bot fleet      │
│   systemd timers for scheduling│                            │   Docker: InfluxDB 1.8 + Grafana │
│                                 │                            │           + WireGuard/wg-easy    │
│   Repo: TradingAgent           │                            │   Repo: trading-bot-fleet        │
└─────────────────────────────────┘                            └──────────────────────────────────┘
```

**Data flow:** Corsair scans ~4,800 Alpaca assets → filters to ~1,100 liquid candidates → runs
Llama 3.3 70B sentiment analysis on top 50 → writes `active_targets.json` → SCP transfers to
Beelink → bots read targets and trade against them.

**Key change (April 2026):** Sector scout functions have been **fully migrated from the Windows
desktop (MSI Aegis / RTX 5080) to the Corsair AI workstation**. The scout previously ran on
Windows via Task Scheduler with Llama 3.1 8B; it now runs on Corsair via systemd timers with
Llama 3.3 70B, giving dramatically better analysis quality. The Windows machine is freed for
ComfyUI/creative AI work without GPU contention.

## Architecture

### Bot Fleet Design

Each bot is a standalone Python script with an infinite loop (`while True` + `time.sleep()`).
PM2 manages process lifecycle. The commander bot monitors and auto-restarts bots based on
`bot_config.json`. Bots are launched with a 10-second staggered delay between starts to avoid
API rate limiting at boot.

### Position Ownership Model

Central to the system is preventing "bot fratricide" — multiple bots fighting over the same
positions. This is enforced through:

- **`BOT_MAPPING`** in `utils.py` — Static baseline: crypto tickers assigned to `crypto_grid`
  and `moon_bag`. Stock/option assignments are empty lists (dynamic resolution handles them).
- **`_build_order_based_map()`** — Queries the last 500 Alpaca orders and builds a
  `symbol → bot_name` lookup from the `client_order_id` prefix (format:
  `{bot_name}-{symbol}-{timestamp}`). Cached for 60 seconds.
- **`get_bot_owner(symbol, asset_class, trading_client)`** — Resolution order:
  1. Crypto → `crypto_grid`
  2. Options → extract root symbol, check order history, default to `condor_bot`
  3. Stocks → check order history, default to `trend_bot`

### CFO / Budget Enforcement

`utils.check_budget(bot_name, trading_client)` enforces per-bot equity allocation limits from
`bot_config.json`. Every bot calls this before placing orders. The CFO:

- Calculates budget as `equity × allocation_pct`
- Sums current position market values owned by the bot
- Sums pending (unfilled) order values from orders tagged with the bot's `client_order_id`
- For options, parses strike price from OCC symbol to calculate collateral at risk
- Returns `True` (under budget) or `False` (over budget)
- **Defaults to allowing trades on error** (fail-open — deliberate design choice for paper trading)

**Designed but not yet fully implemented:**
- **Dynamic capital allocation** — CFO can reallocate idle capital from gated bots (primarily
  wheel_bot's surplus) to active bots, with a gradual per-cycle movement cap and 5% cash
  reserve buffer. Spec: `fleet_upgrade_spec.md`.
- **Tiered hold system** — Replaces binary EOD liquidation with scored
  CLOSE_EOD / HOLD_OVERNIGHT / HOLD_SWING decisions based on P&L direction, signal validity,
  entry confidence, hold duration, and regime/VIX penalties. Includes a pre-market gap check
  at 8:00 AM ET. Spec: `fleet_upgrade_spec.md`.

### wheel_bot Gating

wheel_bot is gated from new put entries when VIX > 22 or regime is BEAR_TREND /
CRITICAL_VOLATILITY. It continues monitoring existing positions for take-profit opportunities
while gated. This is an architectural response — the wheel strategy is structurally wrong in
bear conditions, not a code failure.

### crypto_grid as Capital Consumer

crypto_grid trades on **Alpaca** (not Coinbase), drawing from the shared USD pool. It has a 5%
base allocation and explicit CFO budget-check integration. This was a structural blind spot
that required deliberate attention — without a budget line, it was an invisible capital consumer.

### Configuration System

- **`config.py`** (gitignored) — API keys, tokens, webhook URLs, InfluxDB connection params.
  Required at runtime.
  - `INFLUX_HOST = "localhost"` (bots run natively on same box as Dockerized InfluxDB)
  - `INFLUX_PORT = 8086`
  - `INFLUX_DB_NAME = "trading_bots"` (must match docker-compose `INFLUXDB_DB` exactly)
- **`bot_config.json`** (gitignored, template provided) — Runtime state: bot status
  (active/paused), allocation percentages, market condition, emergency stop flag.
- **`active_targets.json`** (gitignored) — Dynamic watchlists SCP'd from Corsair. Keys:
  `wheel_targets`, `condor_targets`, `trend_targets`, `survivor_targets`, `short_targets`,
  `updated`.

### Monitoring Stack (Docker)

All three services share a Docker network on the Beelink (`docker-compose.yml` in `~/homelab/`):

| Container | Image | Port | Purpose |
|-----------|-------|------|---------|
| `influxdb` | `influxdb:1.8` | 8086 | Time-series storage (v1 write API, no auth) |
| `grafana` | `grafana/grafana-oss:latest` | 3000 | Dashboard visualization |
| `wg-easy` | `ghcr.io/wg-easy/wg-easy` | 51820/udp, 51821/tcp | WireGuard VPN |

**Important networking notes:**
- Bots write to `localhost:8086` (host port mapped into the InfluxDB container)
- Grafana references InfluxDB as `http://influxdb:8086` (container hostname, same Docker network)
- InfluxDB 1.8 was chosen deliberately over 2.x for compatibility with the bots' v1 write API
  (`/write?db=trading_bots`, no auth tokens)

### Data Flow

```
Corsair (Llama 70B analysis) ──SCP──► active_targets.json on Beelink
                                              │
Market Data (Alpaca/yfinance) ──► Bot Analysis ──► Trade Execution (Alpaca)
                                                          │
                                                          ▼
                                                InfluxDB (trade logs)
                                                          │
                                                          ▼
                                      Accountant (P&L calc) ──► Grafana (dashboards)
                                                          │
                                                          ▼
                                                Discord (notifications)
```

## Key Conventions

### Code Patterns

- **Discord notifications:** Each bot has a `send_discord(msg)` function using its own webhook.
  Webhooks are skipped if the URL contains `"YOUR"` (unconfigured sentinel).
- **InfluxDB logging:** Each bot has a `log_to_influx()` function writing to bot-specific
  measurements (`trades`, `wheel_trades`, `condor_trades`, `survivor_trades`, `crypto_trades`,
  `breakout_trades`).
- **Market hours check:** Bots check if markets are open before trading. Crypto bots run 24/7.
- **Error handling:** Try/except blocks with fallback defaults. Budget check defaults to allowing
  trades on error. Bots sleep and retry on exceptions.
- **Config imports:** All bots import `config` (the gitignored secrets file) and most import
  `utils` for shared functions.
- **Order tagging:** All orders use `client_order_id` format `{bot_name}-{symbol}-{timestamp}`
  for ownership resolution.

### Known Issues / Tech Debt

- **Silent InfluxDB failures:** `log_to_influx()` functions across the fleet use bare
  `except: pass`, causing write failures to fail silently. This was the root cause of the
  initial empty-dashboard issue after the Grafana migration. Needs replacement with proper
  error logging.
- **accountant.py BOT_MAPPING divergence:** The accountant historically had its own copy of
  `BOT_MAPPING` with slightly different keys (`"survivor"` vs `"survivor_bot"`). It now
  imports `get_bot_owner` from utils directly, but watch for regressions.
- **Iron condor bot:** Sidelined and excluded from allocations. Do not reactivate without
  solving Alpaca's multi-leg position tracking limitations.

### Naming Conventions

- Bot scripts are named descriptively: `wheel_bot.py`, `trend_bot.py`, etc.
- InfluxDB measurements use `_trades` suffix: `wheel_trades`, `condor_trades`, etc.
- Discord webhook config keys use `WEBHOOK_` prefix: `WEBHOOK_WHEEL`, `WEBHOOK_TREND`, etc.
- Bot names in `bot_config.json` match script names without `.py` extension.

### Commit Style

Commits use lowercase, concise descriptions focused on what changed:
- `fixed condor from opening half condors`
- `fixed wheel bot to account for open orders`
- `bot fratricide fixes`

No conventional commit prefixes (feat:, fix:, etc.) are used. Keep messages short and direct.

## Dependencies

```
alpaca-py          # Alpaca Markets API client (trading + data)
pandas             # DataFrames for trade analysis
pandas_ta          # Technical analysis indicators (RSI, EMA, ADX)
yfinance           # Yahoo Finance market data
requests           # HTTP for webhooks and InfluxDB writes
pytz               # Timezone handling (US/Eastern)
discord.py         # Discord bot framework (commander.py)
```

Install: `pip install -r requirements.txt`

## External Services Required

| Service | Purpose | Config Location |
|---------|---------|-----------------|
| Alpaca Markets | Trading API (paper/live) | `config.API_KEY`, `config.SECRET_KEY`, `config.PAPER` |
| InfluxDB 1.8 | Time-series trade logging | `config.INFLUX_HOST` (localhost), `config.INFLUX_PORT` (8086), `config.INFLUX_DB_NAME` (trading_bots) |
| Discord | Notifications + fleet control | `config.DISCORD_TOKEN`, `config.DISCORD_CHANNEL_ID`, `WEBHOOK_*` |
| PM2 | Process management | External, not in repo |
| Grafana | Dashboard visualization | Reads from InfluxDB, configured at `http://<beelink-ip>:3000` |

## Running the Fleet

Each bot is started as a PM2 process (staggered 10s apart):

```bash
pm2 start wheel_bot.py --name wheel_bot
pm2 start trend_bot.py --name trend_bot
pm2 start survivor_bot.py --name survivor_bot
pm2 start crypto_grid.py --name crypto_grid
pm2 start crypto_breakout.py --name moon_bag
pm2 start accountant.py --name accountant
pm2 start commander.py --name commander
```

The commander bot's watchdog task auto-restarts bots based on `bot_config.json` status.

## Important Rules for Code Changes

1. **Never commit secrets.** `config.py`, `bot_config.json`, and `keys.json` are gitignored.
2. **Position ownership is sacred.** Adding new tickers requires updating `BOT_MAPPING` in
   `utils.py` to prevent bot fratricide.
3. **Budget allocations must sum reasonably.** Check `bot_config.template.json` for current
   splits. Crypto bots share some allocation overlap.
4. **Crypto bots share tickers.** `crypto_grid` and `moon_bag` both trade BTC/USD. The
   ownership model defaults crypto to `crypto_grid`.
5. **No formal tests exist.** Changes should be validated through paper trading. Be extra
   careful with order logic and position sizing.
6. **InfluxDB measurements are bot-specific.** If adding a new bot, create a corresponding
   measurement name and update `accountant.py` queries.
7. **Silent failures are fleet killers.** The CFO import error cascade (GetOrdersRequest) and
   bare `except: pass` in InfluxDB writes both caused multi-day silent failures. Always add
   meaningful error logging.
8. **Iron condor bot is excluded from all allocations.** Do not add it to CFO budget lines.
9. **`config.py` InfluxDB settings must match docker-compose exactly.** The database name
   `trading_bots` must be identical in both places (a past typo of `tradingbots` vs
   `trading_bots` caused silent write failures).
