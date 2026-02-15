# CLAUDE.md - Trading Bot Fleet

## Project Overview

Autonomous multi-strategy trading bot fleet for US equities, options, and cryptocurrency via Alpaca Markets. The fleet consists of 6 trading strategies and 3 infrastructure services, orchestrated through PM2 and controlled via Discord.

**Language:** Python 3 (100%)
**Broker API:** Alpaca Markets (paper and live trading)
**Process Manager:** PM2
**Monitoring:** InfluxDB + Grafana
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
├── Trading Bots
│   ├── wheel_bot.py             # Covered call strategy (conservative, 40% allocation)
│   ├── trend_bot.py             # EMA trend following (aggressive, 25% allocation)
│   ├── survivor_bot.py          # RSI mean reversion on leveraged ETFs (15% allocation)
│   ├── condor_bot.py            # Iron condor options selling (neutral, 10% allocation)
│   ├── crypto_grid.py           # BTC grid trading (neutral, 10% allocation)
│   └── crypto_breakout.py       # Donchian breakout for crypto (10% allocation)
│
├── Infrastructure
│   ├── commander.py             # Discord bot for fleet management
│   ├── accountant.py            # Performance tracking via InfluxDB
│   └── market_analyst.py        # Market regime detection (SPY-based)
│
└── Utilities
    ├── utils.py                 # Shared functions: BOT_MAPPING, budget checks, ownership
    ├── sector_scout_legacy.py   # Watchlist scanner (mothballed)
    └── export_data.py           # Export InfluxDB trade data to CSV
```

## Architecture

### Bot Fleet Design

Each bot is a standalone Python script with an infinite loop (`while True` + `time.sleep()`). PM2 manages process lifecycle. The commander bot monitors and auto-restarts bots based on `bot_config.json`.

### Position Ownership Model

Central to the system is preventing "bot fratricide" - multiple bots fighting over the same positions. This is enforced through:

- **`BOT_MAPPING`** in `utils.py` - Canonical source of truth for which bot owns which tickers
- **`get_bot_owner(symbol, asset_class)`** - Resolves position ownership by asset class:
  - Crypto -> `crypto_grid`
  - Options -> Check underlying against `wheel_bot` tickers, else `condor_bot`
  - Stocks -> Check against `survivor_bot`, then `wheel_bot`, else `trend_bot`

**Important:** `accountant.py` has its own copy of `BOT_MAPPING` with slightly different keys (e.g., `"survivor"` vs `"survivor_bot"`). Keep both in sync when changing ticker assignments.

### Budget Enforcement

`utils.check_budget(bot_name, trading_client)` enforces per-bot equity allocation limits defined in `bot_config.json`. Every bot should call this before placing orders.

### Configuration System

- **`config.py`** (gitignored) - API keys, tokens, webhook URLs. Required at runtime.
- **`bot_config.json`** (gitignored, template provided) - Runtime state: bot status (active/paused), allocation percentages, market condition, emergency stop flag.
- **`active_targets.json`** (gitignored) - Dynamic watchlists loaded by bots at runtime. Keys: `wheel_targets`, `condor_targets`, `trend_targets`, `survivor_targets`.

### Data Flow

```
Market Data (Alpaca/yfinance) -> Bot Analysis -> Trade Execution (Alpaca)
                                                        |
                                                        v
                                              InfluxDB (trade logs)
                                                        |
                                                        v
                                    Accountant (P&L calc) -> Grafana (dashboards)
                                                        |
                                                        v
                                              Discord (notifications)
```

## Key Conventions

### Code Patterns

- **Discord notifications:** Each bot has a `send_discord(msg)` function using its own webhook. Webhooks are skipped if the URL contains `"YOUR"` (unconfigured sentinel).
- **InfluxDB logging:** Each bot has a `log_to_influx()` function writing to bot-specific measurements (`trades`, `wheel_trades`, `condor_trades`, `survivor_trades`, `crypto_trades`, `breakout_trades`).
- **Market hours check:** Bots check if markets are open before trading. Crypto bots run 24/7.
- **Error handling:** Try/except blocks with fallback defaults. Budget check defaults to allowing trades on error. Bots sleep and retry on exceptions.
- **Config imports:** All bots import `config` (the gitignored secrets file) and most import `utils` for shared functions.

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
- `improvements in wheel bot cost protection`

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
| InfluxDB | Time-series trade logging | `config.INFLUX_HOST`, `config.INFLUX_PORT`, `config.INFLUX_DB_NAME` |
| Discord | Notifications + fleet control | `config.DISCORD_TOKEN`, `config.DISCORD_CHANNEL_ID`, `WEBHOOK_*` |
| PM2 | Process management | External, not in repo |
| Grafana | Dashboard visualization | Reads from InfluxDB, configured externally |

## Running the Fleet

Each bot is started as a PM2 process:

```bash
pm2 start wheel_bot.py --name wheel_bot
pm2 start trend_bot.py --name trend_bot
pm2 start survivor_bot.py --name survivor_bot
pm2 start condor_bot.py --name condor_bot
pm2 start crypto_grid.py --name crypto_grid
pm2 start crypto_breakout.py --name moon_bag
pm2 start accountant.py --name accountant
pm2 start commander.py --name commander
```

The commander bot's watchdog task auto-restarts bots based on `bot_config.json` status.

### Discord Commands (via Commander)

- `/status` - Fleet status report (all PM2 processes)
- `/start <bot_name>` - Start/restart a specific bot
- `/stop <bot_name>` - Stop a specific bot
- `/panic` - Emergency stop: halts all bots, sets `emergency_stop: true` in config
- `/resume` - Clears emergency stop, watchdog revives bots

### Manual Data Export

```bash
python export_data.py   # Exports last 7 days of trades from InfluxDB to CSV
```

## Setup for New Environment

1. Clone the repo
2. `pip install -r requirements.txt`
3. Create `config.py` with required credentials (see `config.py` section below)
4. Copy `bot_config.template.json` to `bot_config.json`
5. Create `active_targets.json` with watchlists per strategy
6. Ensure InfluxDB is running and database exists
7. Start bots via PM2

### Required `config.py` structure

```python
API_KEY = "..."          # Alpaca API key
SECRET_KEY = "..."       # Alpaca secret key
PAPER = True             # True for paper trading, False for live

DISCORD_TOKEN = "..."    # Discord bot token
DISCORD_CHANNEL_ID = "..." # Discord channel ID (as string)

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB_NAME = "trading_bots"

WEBHOOK_WHEEL = "https://discord.com/api/webhooks/..."
WEBHOOK_TREND = "https://discord.com/api/webhooks/..."
WEBHOOK_SURVIVOR = "https://discord.com/api/webhooks/..."
WEBHOOK_CONDOR = "https://discord.com/api/webhooks/..."
WEBHOOK_CRYPTO = "https://discord.com/api/webhooks/..."
WEBHOOK_MOONBAG = "https://discord.com/api/webhooks/..."
WEBHOOK_OVERSEER = "https://discord.com/api/webhooks/..."
```

## Testing

There is no automated test suite. Validation is done through:
- Paper trading mode (`config.PAPER = True`)
- InfluxDB trade logs reviewed via Grafana
- Discord notifications for real-time monitoring
- Budget checks and position ownership guards in code

## CI/CD

No CI/CD pipeline exists. Deployment is manual: push to repo, pull on server, PM2 restarts processes.

## Important Warnings for AI Assistants

1. **Never commit secrets.** `config.py`, `.env`, `bot_config.json`, and `active_targets.json` are gitignored for good reason. Do not create or modify these with real credentials.
2. **Keep BOT_MAPPING in sync.** Both `utils.py` and `accountant.py` maintain bot-to-ticker mappings. Changes to one must be reflected in the other.
3. **Respect the ownership model.** Adding new tickers to a bot requires updating `BOT_MAPPING` in `utils.py` to prevent position conflicts.
4. **Budget allocations must sum reasonably.** The allocations in `bot_config.template.json` intentionally exceed 100% (crypto bots share allocation). Understand the overlap before changing.
5. **Crypto bots share tickers.** `crypto_grid` and `crypto_breakout` (moon_bag) both trade BTC/USD. The ownership model defaults crypto to `crypto_grid`.
6. **No formal tests exist.** Any changes should be validated through paper trading. Be extra careful with order logic and position sizing.
7. **InfluxDB measurements are bot-specific.** If adding a new bot, create a corresponding InfluxDB measurement name and update `accountant.py` queries.
