# CLAUDE.md - Trading Bot Fleet

> **System Context:** This repo runs on the **Beelink S12 Mini** (Ubuntu), the
> execution node in a two-machine trading system. The companion repo
> `TradingAgent` runs on the **Corsair AI Workstation** (AMD Strix Halo) and
> handles all market scanning and AI-powered target generation. See
> `CORSAIR_ARCHITECTURE.md` in that repo for details.

## Project Overview

Autonomous multi-strategy trading bot fleet for US equities, options, and cryptocurrency via
Alpaca Markets. Five active trading strategies and four infrastructure services run as PM2
processes **inside the `trading-fleet` Docker container** on the Beelink, controlled via Discord.

**Language:** Python 3.11  
**Broker API:** Alpaca Markets (paper trading — live trading capable)  
**Deployment:** Docker container (`trading-fleet`) with PM2 inside; repo live-mounted at `/app/code`  
**Monitoring:** InfluxDB 1.8 + Grafana (sibling containers on the same compose network)  
**Notifications:** Discord webhooks + Discord bot commands

## Deployment Reality (read this before assuming anything)

The bots do **not** run natively on the host. The Beelink runs a homelab Docker stack
(`~/homelab/docker-compose.yml`) with these services:

| Container | Purpose |
|-----------|---------|
| `trading-fleet` | This repo. PM2 inside the container runs every bot + infra process |
| `influxdb` (1.8) | Time-series storage; reached as `http://influxdb:8086` via Docker DNS |
| `grafana` | Dashboards, reads InfluxDB |
| `wg-easy` | WireGuard VPN (unrelated to trading) |
| `searxng` | Search engine (unrelated to trading) |

Key consequences:

- **The repo is live-mounted** (`/home/trader/bots/repo` → `/app/code`). `git pull` on the host
  changes code inside the running container instantly; running processes pick it up on
  `pm2 restart` (or container restart).
- **The PM2 process list is `deploy/ecosystem.config.js`** — version-controlled here, baked into
  the image at build. Changing it requires an image rebuild (`docker compose build trading-fleet`).
- **Config truth is `config.py`** (gitignored, lives in the repo dir on the host). There is no
  env-var config layer. `INFLUX_HOST` must be `"influxdb"` (Docker DNS), **not** `localhost` —
  inside the container, localhost is the container itself.
- **`docker exec trading-fleet pm2 ls`** is the ground truth for what's running. Never trust
  docs (including this one) over that output.
- Build files live in `deploy/` (Dockerfile, entrypoint.sh, ecosystem.config.js, README with the
  compose block). The entrypoint exits hard if the code mount is missing — it never falls back
  to a stale code snapshot.

Day-to-day deploy: `git pull` on the host, then `docker exec trading-fleet pm2 restart all`.
Rebuild only when `requirements.txt` or `deploy/` changes.

## Repository Structure

```
trading-bot-fleet/
├── CLAUDE.md                    # This file
├── RUNBOOK.md                   # Ops: deploy, incident response, error pipeline
├── requirements.txt             # Single dependency manifest, PINNED (image installs THIS file)
├── bot_config.template.json     # Runtime config template (copy to bot_config.json)
│
├── deploy/                      # Container build: Dockerfile, entrypoint.sh,
│   │                            # ecosystem.config.js (canonical PM2 process list)
│   └── README.md                # Compose service block + rebuild instructions
│
├── Core architecture
│   ├── fleet_registry.py        # THE bot registry — single source of truth per bot
│   ├── fleet_bot.py             # Shared runner: scaffolding every bot used to copy-paste
│   ├── new_bot_template.py      # Copy-fill-register template for a new strategy
│   ├── utils.py                 # Ownership resolution, budget checks, order submission
│   │                            # safety gates, targets loader, fill reconciliation
│   ├── strategy_advisor.py      # Paper-only performance ledger + allocation recommendations
│   ├── tiered_hold.py           # CLOSE_EOD / HOLD_OVERNIGHT / HOLD_SWING scoring
│   └── logger.py                # Per-bot rotating logs + JSONL error registry
│
├── Trading Bots (Active — all five run on the fleet_bot runner)
│   ├── wheel_bot.py             # Options premium selling (38% base, VIX/regime gated)
│   ├── trend_bot.py             # EMA momentum, long/short (28% base)
│   ├── survivor_bot.py          # RSI dip buying (20% base) — was the runner pilot
│   ├── crypto_grid.py           # BTC/ETH/SOL grid trading (5% base)
│   └── crypto_breakout.py       # Donchian breakout, runs as moon_bot (4% base)
│
├── Infrastructure (PM2 processes in the container)
│   ├── commander.py             # Discord bot: /status /stop /start /panic /resume + watchdog
│   ├── accountant.py            # CFO: P&L attribution, dynamic reallocation, CAPITAL_CRUNCH,
│   │                            # orphan sweep, fill + option-event (assignment) reconciliation
│   ├── market_analyst.py        # SPY regime (BULL_TREND/SIDEWAYS/BEAR_TREND/CRITICAL_VOLATILITY)
│   │                            # + VIX → writes global_settings in bot_config.json
│   └── error_watchdog.py        # Tails logs/fleet_error_registry.jsonl → InfluxDB
│                                # bot_error_events (errors visible in Grafana)
│
├── Tests & tools
│   ├── fleet_doctor.py            # RUN THIS FIRST in any incident (in-container preflight:
│   │                              # imports, config, Alpaca, each VIX source, Influx, pm2)
│   ├── test_commander.py          # Regression: watchdog alert throttling, crash-vs-stop wording
│   ├── test_orphan_resolution.py  # Regression: ownership/entry-time resolution + root inference
│   ├── test_fill_logging.py       # Regression: ms-floored fill stamps, wheel close ladder
│   ├── test_strategy_advisor.py   # Regression: advisor attribution, P&L, drawdown, allocation recs
│   ├── dedupe_trades.py           # One-off: delete pre-fix duplicate trade rows (dry-run default)
│   ├── export_data.py             # Export InfluxDB trade data to CSV
│   └── fetch_trade_history.py     # Pull raw FILL activities from Alpaca
│
└── (gitignored, host repo dir)  config.py, bot_config.json, active_targets.json,
                                 effective_budgets.json, moon_bot_state.json, logs/
                                 recommended_allocations.json
```

Retired: `condor_bot.py` (2026-07, Alpaca multi-leg unreliability — in git history if ever
needed), `sector_scout_legacy.py`, `config_docker.py` + the container env-var config layer.

## Two-Machine Architecture

```
┌─────────────────────────────────┐       SCP Transfer        ┌──────────────────────────────────┐
│   CORSAIR AI WORKSTATION        │    active_targets.json     │   BEELINK S12 MINI               │
│   ("The Brain")                 │ ─────────────────────────► │   ("Execution Node")             │
│                                 │    3x daily (08:30,        │                                  │
│   AMD Strix Halo (96GB)        │    12:00, 15:00 CT)        │   Intel N100 (16GB), Ubuntu      │
│   LM Studio → Gemma 4 21B MoE  │                            │   Docker: trading-fleet (bots    │
│   Native Windows               │    lands in                │     under PM2), InfluxDB 1.8,    │
│   Task Scheduler (3x daily)    │    ~/bots/repo/            │     Grafana, wg-easy, searxng    │
│   Repo: TradingAgent           │                            │   Repo: trading-bot-fleet        │
└─────────────────────────────────┘                            └──────────────────────────────────┘
```

**Data flow:** Corsair scans the Alpaca universe → filters to liquid candidates → Gemma 4 21B MoE
sentiment analysis → writes `active_targets.json` (schema v1.1) → SCP to `~/bots/repo/` on the
Beelink → the live mount makes it visible in-container → bots trade against it.

## The Plug-and-Play Architecture

Two files make a new strategy cheap to add:

- **`fleet_registry.py`** — one entry per bot: script, InfluxDB measurement, webhook config key,
  static symbols, reconciliation flag, gating rule, manual-state flag. Ownership tags,
  accountant queries/reporting, analyst pause behavior, and fill reconciliation ALL derive from
  it. **Never re-introduce a per-bot list anywhere else.**
- **`fleet_bot.py`** — the shared runner. Provides clients, Discord, market-hours gate,
  regime/VIX, targets loading, ownership priming, pending-order tracking, budget gate, EOD
  windows, failed-symbol cooldowns, tagged+safety-gated order submission, sizing, and the main
  loop. A bot script is just its strategy (see `survivor_bot.py`, the pilot port).

### Adding a new bot

1. Copy `new_bot_template.py` → `<your_bot>.py`, write the strategy in `cycle()`.
2. Add one entry to `fleet_registry.BOTS` (the registry key becomes the PM2 name, order-tag
   prefix, budget key, and measurement mapping).
3. Add the bot to `bot_config.json` `bots{}` (status + allocation) — and to `cfo_settings`
   (base_allocations, minimum_reserves, reallocation_priority) if it joins CFO reallocation.
4. Add `WEBHOOK_<NAME>` to `config.py` (optional; unset skips Discord).
5. Add an app block to `deploy/ecosystem.config.js`, then
   `docker compose build trading-fleet && docker compose up -d trading-fleet`.

Migration status: **complete.** survivor_bot piloted the runner; trend_bot, wheel_bot,
crypto_grid, and moon_bot were ported 2026-07-08 with strategy semantics preserved
(the wheel kept its close ladder / expiry backstop / covered-call ownership rules
verbatim). Every bot's budget number now flows through `utils.get_budget_dollars` —
no bot reads `effective_budgets.json` directly anymore.

## Architecture Notes

### Position Ownership Model

Prevents "bot fratricide" — multiple bots fighting over one position:

- Orders are tagged `client_order_id = {bot_name}-{symbol}-{timestamp}`.
- `utils._build_order_based_map()` pages Alpaca order history (`_fetch_orders_covering`) until
  every held symbol's opening order is found — never a single bounded window (that caused the
  GEN/APTV orphaning bug). 60s cache; bots prime it each cycle via `utils.prime_ownership`.
- `utils.get_bot_owner()` resolution: crypto → `crypto_grid`; options → order-history tag
  (contract or root), else `wheel_bot`; stocks → order-history tag, else the root inferred from
  a bot's own **option** orders (assigned/exercised stock → wheel_bot), else **`None` = unowned**.
  A direct symbol tag always outranks root inference. There is deliberately no default owner:
  the old `else trend_bot` fallback let trend_bot adopt and liquidate wheel-assigned PAAS stock
  (2026-07-06). Unowned positions are quarantined — every bot filters holdings by its own name —
  and metered via `ownership_fallback`.
- The accountant runs an orphan sweep each cycle: orphan = **unowned** (the resolver's own
  definition) → `orphan_position` metric + overseer alert. Owned positions with no resolvable
  entry time (assignment-created stock has no opening order) emit `entry_time_missing` instead —
  the owner manages them, but time-based backstops (max-hold) are blind for them.
- **A failed order fetch is not an empty one** (2026-07-15 storm postmortem: three ReadTimeouts
  emptied the fetch and the sweep alert-flagged all 9 held positions as orphans at once).
  `_fetch_orders_covering` retries each page with backoff, then **raises `utils.OrderFetchError`**
  (partial pages attached) instead of returning a short list. The map build fails safe: it serves
  the last-known-good cache (never builds/caches a map from a failed fetch; raises if there's no
  cache at all), flags it via `utils.ownership_map_degraded()`, and cools down 120s between fetch
  attempts. While degraded, `get_bot_owner` quarantines unknowns **without** the
  `ownership_fallback` metric, and the accountant stands the orphan sweep down entirely
  (`orphan_sweep` skip metric, no alerts) until `utils.order_history_healthy()`.
  `reconcile_fills` and entry times salvage the partial pages (idempotent / newest-first-correct).
  The Alpaca session read timeout is 30s (`bound_session_timeout`), raised from 15s.
- **A symbol with no covering order is proven uncoverable once, not every cycle.** Some held
  positions never get a covering order by design — assignment/exercise makes bare stock with no
  opening order, and unowned positions are quarantined until a human acts. For those the paged
  fetch's coverage target could never be met, so every call walked the full `max_orders` window
  (24 pages / 12,000 orders instead of 2 / 1,000), twice per `FleetBot.refresh()`, every 60s —
  and it defeated the 60s ownership cache outright, since the coverage check can never be
  satisfied by a symbol that isn't in the map. That churn was the fleet's memory high-water
  mark. `_fetch_orders_covering` now memos such symbols (`_uncoverable_symbols`,
  `UNCOVERABLE_RECHECK_SECONDS` = 1h, keyed by `require_fill`) and stops letting them drive
  paging; `_build_order_based_map` treats a memoed symbol as covered ("unowned" IS the map's
  answer, represented by absence). The first walk still pages exhaustively — the GEN/APTV
  guarantee is untouched — and a tagged order placed later to claim an orphan still resolves on
  the next cycle, because it lands on page 1 of newest-first history.
- **Per-symbol throttle/dedupe state is bounded.** `_fallback_log_times`, `_alerted_event_ids`,
  `wheel_bot._close_failures`, `survivor_bot._daily_sma_cache`, `accountant._orphan_alert_times`
  and `FleetBot.failed_symbols` all evict (by TTL, by cap, or against currently-held symbols).
  Symbols churn constantly, and a month-long PM2 process kept one entry per symbol ever seen.
- The accountant also logs option lifecycle events (OPASN/OPEXC/OPEXP) from Alpaca account
  activities into `wheel_trades` (actions `assigned`/`exercised`/`expired`, ignored by realized
  P&L pairing) and pings the overseer on assignment/exercise.
- **Crypto special case:** crypto_grid and moon_bot share BTC/ETH/SOL. Ownership resolves the
  *positions* to crypto_grid; moon_bot tracks its own coins in `moon_bot_state.json` so its
  trailing stop only sells what it bought, and its entries don't depend on the shared position.

### CFO / Budget Enforcement

`utils.check_budget(bot_name, client)` before every entry:

- Budget = dynamic `effective_budgets.json` (written by the accountant's reallocation) with
  fallback to `cfo_settings.base_allocations × (1 − unallocated_reserve) × equity` — the same
  formula the reallocator uses, so the two paths can't drift. `bots{}.allocation` only covers
  bots outside `cfo_settings`.
- **Fail-closed** when `bot_config.json` is missing or the bot has no allocation.
  **Fail-open** on runtime/API errors (deliberate for paper trading — revisit before live).
- Counts held positions (options at collateral) + pending tagged orders.
- The accountant also flips `CAPITAL_CRUNCH` in `bot_config.json` at >90% utilization
  (released <80%), and reallocates gated bots' surplus to active bots
  (`cfo_settings.reallocation_*`, `gate_idle_threshold_cycles` honored).
- `strategy_advisor.py` is a separate, paper-only decision layer. The accountant runs it
  hourly and writes `recommended_allocations.json` using Alpaca fills plus live positions,
  rolling 5d/20d/60d risk-adjusted bot scores, and the current regime/VIX/macro/sector bucket.
  Option lifecycle activities (OPASN/OPEXC/OPEXP) are synthesized into the ledger as $0
  option closes plus strike-priced stock legs — without them an orders-only ledger never
  realizes expired-worthless premium and scores the wheel low. Its source comparison is
  window-matched (60d Alpaca ledger vs a dedicated 60d Influx read, not the CFO's 30d one).
  It never writes `effective_budgets.json`; recommendations are for review until promoted.

### Safety Gates (in `utils.submit_and_log_order`)

- Daily loss cap: `MAX_DAILY_LOSS` (-$5,000) blocks all orders.
- Notional cap ($20k) + symbol exposure cap ($5k) on any **equity order that opens or increases
  exposure** — long entries AND short entries. Risk-reducing orders (closes, covers) are exempt.
  Options rely on the bots' own collateral/BP checks; crypto is exempt. The gate's price lookup
  reuses one lazily-built, `bound_session_timeout`-wrapped data client (`_safety_price_client`)
  instead of constructing a fresh, unbounded-timeout client per order.
- Trade rows are written to InfluxDB **from confirmed fills** (broker price/qty/time), with the
  accountant reconciling late fills from Alpaca (`utils.reconcile_fills`, `RECONCILED_BOTS`).
  Reconcile **pages the whole lookback window** (`_fetch_orders_covering`, capped at
  `RECONCILE_MAX_ORDERS`), never a single `limit=500` read — a wheel option that fills days after
  submission sorts by its old `submitted_at`, so a crypto-order flood (or a catch-up after
  downtime) would bury it past a bounded window, the same trap that orphaned aged positions.
  Rows are stamped at the fill time **floored to the millisecond** so the submit-time and
  reconciled writes land on one point (Alpaca's two order endpoints serialize `filled_at` with
  sub-µs differences; raw-ns stamps duplicated an unpredictable subset of fills).

### Wheel Expiry Safety (post PAAS assignment postmortem)

- Buy-to-closes use an escalating price ladder — midpoint → half-cross → ask
  (`close_option_position`), ~10s per rung — never a bare midpoint limit that can't fill, and
  a wide spread never blocks a risk-reducing close. Each rung re-submits only the **unfilled
  remainder** (it subtracts a rung's partial fill before escalating), so a partial-then-escalate
  can't re-buy the original quantity and over-close a multi-contract short into a net long.
  A rung's partial fill has no stable broker `filled_at`, so it's logged as a `terminal_partial`
  row (`utils.log_terminal_partial_fill`) — idempotent (deterministic synthetic stamp), gated to
  TERMINAL orders with no `filled_at` so it can't double-count the full-fill path, and also
  recovered by `reconcile_fills` — so a close spread across partial rungs isn't under-counted in
  realized P&L.
- ITM contracts at DTE ≤ `FORCE_CLOSE_DTE` (5) are closed outright, with no roll target
  required (deep ITM often has none). Rolls trigger at DTE ≤ `STALE_ROLL_DTE` (10). A close
  that survives the full ladder `CLOSE_FAIL_ALERT_AFTER` (3) cycles in a row alerts Discord +
  the error registry.
- `bot_config.json` `bots.wheel_bot.force_close_symbols` / `force_roll_symbols` are per-ticker
  runtime levers (ladder-priced, so they work on wide spreads too).
- Covered calls only count **wheel-owned** shares (never another bot's stock) and are written
  on owned stock even when the ticker is off the scout list — including under VIX/regime/crunch
  gates, since a call on held shares adds no collateral and reduces risk.

### Market Regime & Gating

`market_analyst` (15-min cycle) writes `market_condition`, `vix`, `macro_climate` into
`bot_config.json`. VIX > 28 pauses all non-manual bots. Per-bot entry gating lives in the
registry (`gated_when`): wheel_bot gates on BEAR_TREND/CRITICAL_VOLATILITY or VIX > 22
(covered calls on owned stock exempt); crypto_grid on bear regimes. Gated bots keep managing
existing positions.

**Data sources & resilience (post 2026-06-24 silent-outage and 2026-09 yfinance-death
postmortems):** SPY (which drives the regime) comes from **Alpaca `get_stock_bars`** — reliable,
already authenticated. VIX cannot: alpaca-py exposes no index feed and this account 403s on the
index endpoints. So VIX runs a **multi-source fallback chain** (`market_analyst.VIX_SOURCES`):
stooq CSV → CBOE delayed-quote JSON → yfinance, first sane reading wins. Every source returns
**true index points**, so the 22/28 gates need no recalibration whichever answers; a dollar-priced
proxy (VIXY/VXX) is deliberately excluded, since a mis-scaled number feeding a kill-switch is
worse than no number (the stale fail-safe already covers "no number"). Readings outside
`[VIX_MIN, VIX_MAX]` = [5, 150] are rejected as garbage rather than published. yfinance is **last
and imported lazily** — it broke the fleet twice (2026-06 rate-limiting, 2026-09 outright), and a
broken install of an optional fallback must not kill the regime process at import. The winning
source is published to `global_settings.vix_source`, tagged on the InfluxDB `market_regime` row,
and shown by `/status` — so which provider carries the kill-switch is never a mystery.
Failures are **loud**: an empty/failed fetch logs `registry.log_error` + a throttled Discord
ping and **never silently skips** (the original bug: a two-ticker `yf.download` returned an
empty frame, the publish block skipped with no `else`, and VIX froze — disabling the kill-switch
for ~12 days). A fresh `market_regime` InfluxDB row is written **only on a fully-successful
fetch**, so its recency is the fleet's "regime is live" heartbeat. If no good fetch lands for
`STALE_REGIME_SECONDS` (45 min), the analyst **fails safe**: it degrades to `CRITICAL_VOLATILITY`
+ an elevated sentinel VIX (25, above the wheel/crypto gates but below the 28 full-kill so a data
outage can't self-inflict a total halt) flagged `global_settings.data_stale=true`, rather than
trusting the frozen low VIX. The **accountant** runs a cross-process backstop
(`check_regime_freshness`) that alerts the overseer if the `market_regime` measurement hasn't
been written in > 30 min — catching a wedged/dead analyst its own in-process fail-safe can't.

### Tiered Hold System

`tiered_hold.py` scores positions (P&L, signal validity, confidence, duration, regime/VIX
penalties) into CLOSE_EOD / HOLD_OVERNIGHT / HOLD_SWING at the 15:30 ET window; 15:45+ ET
liquidates CLOSE_EOD. `max_hold_days_for_tier` is the hard backstop exit (3d/7d).
*Partially wired:* `OVERNIGHT_STOPS` stop/trailing percentages and `premarket_check()` are
defined but not yet enforced anywhere — only `max_hold_days` is live.

## Key Conventions

- **One name per bot, everywhere.** The registry key (e.g. `moon_bot`) is the PM2 process name,
  bot_config key, order-tag prefix, and budget key. (The old `moon_bag` PM2 name is retired.)
- InfluxDB measurements use the `_trades` suffix; webhook keys use the `WEBHOOK_` prefix
  (moon_bot's is `WEBHOOK_MOONBAG`, historical).
- Webhooks are skipped when unset or containing `"YOUR"` (unconfigured sentinel).
- Crypto bots run 24/7; equity bots gate on market hours.
- **Commit style:** lowercase, concise, no conventional-commit prefixes.

## Testing

`python -m unittest test_orphan_resolution test_fill_logging test_market_analyst test_strategy_advisor test_commander -v` —
regression suites for the ownership/entry-time paging fix (+ option-root inference and the
no-default-owner rule), fill-row stamping / wheel close-ladder pricing, and the market-regime
pipeline (SPY-df normalization, VIX>28 kill-switch, loud-failure + stale fail-safe), plus the
paper-only allocation advisor. Run the
first two after touching `utils.py` ownership/order/logging code or wheel close logic, and
`test_market_analyst` after touching `market_analyst.py` (it covers each VIX source's parsing
and the chain's fall-through/rejection rules), and `test_commander` after touching the watchdog's
alerting. Strategy/advisor changes are still validated through paper trading; there is no
backtest harness.

**`fleet_doctor.py`** is the diagnostic entry point — run it *in the container* against the code
the fleet actually runs:
`docker exec -w /app/code trading-fleet python3 fleet_doctor.py`. It verifies location, syntax +
uncommitted drift, config completeness, that **every PM2 process survives import** (the one
failure class a bot's own main-loop `try/except` cannot catch), Alpaca, each VIX source
separately, an InfluxDB round-trip, and runtime freshness/pm2 state. Read-only; never orders.

## Known Issues / Tech Debt

- **tiered_hold overnight stops unwired** (see above) — `max_hold_days` is the only enforced
  backstop.
- **commander bare `except: pass`** remains on best-effort Discord sends.
- **`bot_monitor.memory` / `.cpu` in Grafana were flat 0 until 2026-09.** `pm2 jlist` reports
  live resource usage under `monit`, not `pm2_env`; commander read the wrong key, so the fleet's
  only per-process memory series never carried data. Fixed — but every `bot_monitor` point
  written before that fix has memory=0 and cpu=0, so historical panels are empty by construction.
- **VIX depends on free public providers.** alpaca-py has no index feed, so VIX can't move to
  Alpaca like SPY did. The 2026-09 chain (stooq → CBOE → yfinance) removes the *single*-provider
  SPOF, and the stale fail-safe + accountant freshness watchdog still bound the blast radius, but
  all three are unauthenticated endpoints that can rate-limit or change shape without notice. A
  paid index feed remains the only way to actually own this input. `fleet_doctor.py` reports each
  source's health individually.
- **`ta` is an sdist-only, effectively unmaintained dependency** (trend_bot's EMA/ADX,
  survivor_bot's RSI). It builds and computes correctly under the pinned pandas 3.x / numpy 2.x
  set, but it is the one package here that can fail a container rebuild outright on a toolchain
  change. `market_analyst.TechnicalMath` and `market_scanner.TechnicalMath` already implement the
  same indicators natively, so replacing it is a contained job if it breaks — the reason it hasn't
  been done pre-emptively is that it would change live indicator math with no backtest to validate
  against.
- **commander has no `__main__` guard on its Discord commands' side effects** beyond the
  `bot.run(TOKEN)` guard added in 2026-09; importing it constructs the Discord and Alpaca clients
  (harmless, no connection).
- Historical postmortems (GEN/APTV orphaning root cause, InfluxDB silent-failure eras, the
  2026-06-24 market-regime silent yfinance outage) live in git history and the
  `test_orphan_resolution.py` / `test_market_analyst.py` docstrings.

## Important Rules for Code Changes

1. **Never commit secrets.** `config.py`, `bot_config.json`, `keys.json` are gitignored.
2. **The registry is the only bot list.** New bots register in `fleet_registry.py`; do not add
   per-bot name lists to any other module.
3. **Position ownership is sacred.** Every order carries the bot's tag via
   `client_order_id` — use `FleetBot.tag()`/`market_order()` or match the format exactly.
4. **Budget allocations must sum sensibly** across `bot_config` + `cfo_settings`.
5. **Silent failures are fleet killers.** No bare `except: pass` on anything that matters;
   errors go through `registry.log_error` (they land in Grafana via error_watchdog).
6. **`config.py` must say `INFLUX_HOST = "influxdb"`** and `INFLUX_DB_NAME = "trading_bots"`
   (must match compose `INFLUXDB_DB`; a past `tradingbots` typo caused silent write failures).
7. **Deploy changes ship with code.** Anything touching `requirements.txt` or `deploy/` needs a
   container rebuild on the Beelink — say so in the PR/commit.
8. **`requirements.txt` stays pinned.** It was unpinned until 2026-09, which meant a rebuild
   resolved whatever PyPI served that day — a container nobody had tested, decided by the
   calendar. Bump a pin deliberately, run the suites and `fleet_doctor.py`, then rebuild.
9. **Alerts must survive being right.** A watchdog that fires every cycle for every bot is
   indistinguishable from noise, and the 2026-09 storm buried a real failure under ~9 identical
   pings per cycle. Anything that alerts on a *persistent* condition needs a throttle, and its
   wording must distinguish the conditions it covers (see `commander._alert_down`).
10. **Run the orphan regression suite** after touching ownership/order-history code.
