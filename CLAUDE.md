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
│   │                              # imports, config, Alpaca, each VIX source, Influx,
│   │                              # duplicate-fleet detection, regime heartbeat, pm2)
│   ├── test_commander.py          # Regression: watchdog alert throttling, crash-vs-stop wording
│   ├── test_containment.py        # Regression: orders refused outside the fleet container
│   ├── test_orphan_resolution.py  # Regression: ownership/entry-time resolution + root inference
│   ├── test_fill_logging.py       # Regression: ms-floored fill stamps, wheel close ladder
│   ├── test_strategy_advisor.py   # Regression: advisor attribution, P&L, drawdown, allocation recs
│   ├── test_bar_freshness.py      # Regression: newest-bar selection + staleness guards
│   ├── test_risk_exits.py         # Regression: stops/targets run AHEAD of the EOD hold branch
│   ├── test_crypto_grid.py        # Regression: entry-linked grid spacing, lot ledger, budget split
│   ├── test_pl_accounting.py      # Regression: FIFO period P&L, opening inventory, window scoping
│   ├── test_safety_gates.py       # Regression: loss cap exempts closes, pending-order reservation
│   ├── test_config_audit.py       # Regression: bot_config completeness, fail-safe bootstrap
│   ├── dedupe_trades.py           # One-off: delete pre-fix duplicate trade rows (dry-run default)
│   ├── export_data.py             # Export InfluxDB trade data to CSV
│   └── fetch_trade_history.py     # Pull raw FILL activities from Alpaca
│
└── (gitignored, host repo dir)  config.py, bot_config.json, active_targets.json,
                                 effective_budgets.json, moon_bot_state.json,
                                 crypto_grid_state.json, logs/
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
  static symbols, reconciliation flag, gating rule, manual-state flag, and the `bot_config.json`
  keys that bot reads (`config_keys`). Ownership tags, accountant queries/reporting, analyst
  pause behavior, fill reconciliation and the **bot_config completeness check** ALL derive from
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
  **crypto_grid now keeps the same kind of ledger** (`crypto_grid_state.json`, one lot per buy
  with its fill price). It previously read the shared account position as "its" inventory,
  which is how it reached moon_bot's coins: an account-level FIFO reconstruction matched
  moon-origin ETH lots against grid sells **113 times**. The account position is now only ever
  a ceiling — `grid_sell` caps its quantity at what the account actually holds, and
  `reconcile_lots` trims the ledger (oldest first) when it over-states reality.
- **Both crypto ledgers move only on CONFIRMED FILLS.** `bot.submit()` returns an *order*, not
  an outcome: pending, rejected, canceled and partially filled are all normal. A first version
  recorded the *requested* quantity at the *submit* price whenever no fill was present, and
  retired a whole lot whenever a sell returned any order object — so an unfilled $50 buy became
  a real lot with a fabricated basis, and a 1-unit sell that filled 0.25 erased the other 0.75
  from the books while the coins stayed in the account, where the other bot could reach them.
  Orders now live in `pending` keyed by broker id and are reconciled every cycle: a **buy's lot
  IS its order** (restated from the order's cumulative `filled_qty`/`filled_avg_price`, exact
  and idempotent however the fills arrive), a **sell** subtracts only the newly-filled quantity
  against an `applied_qty` watermark, a terminal zero-fill leaves nothing behind, and pending
  orders persist across a restart. moon_bot carries the same discipline on its per-coin
  quantity. Both save atomically (temp + `os.replace`); a failed write suspends entries rather
  than trading on an unpersisted ledger.
- **A lot stores cumulative acquisition and disposal, never a net quantity.** With a single
  `qty`, a BUY update could overwrite a SELL's effect: a buy that filled 0.5 of 1 and stayed
  pending could have that 0.5 sold and the lot emptied, and when the buy was later CANCELED
  still reporting cumulative `filled_qty=0.5`, restating `qty` from it **resurrected** a
  0.5-coin lot for coins already gone — on a shared symbol, a later sell would then reach
  moon_bot's inventory to cover it. `acquired_qty` is the buy's to restate, `disposed_qty` is
  the sells' to add to, and `lot_qty()` is the difference. An exhausted lot is kept — by
  `prune_empty_lots` **and by `load_state`** — while its buy is still pending, because it is
  the record of that disposal; dropping it at either point just moves the resurrection to the
  other side of a restart. `sellable_lot` additionally refuses to sell out of an unfinished
  acquisition.
- **One sale is never subtracted twice.** Two reconcilers can see the same fill: the ORDER
  read (`reconcile_pending`, attributable) and the POSITION read (`reconcile_lots`, which
  exists to catch *external* disposals). They are separate network calls, and a fill landing
  between them is ordinary — the position read books an "external" disposal, then the next
  cycle's order read reports the same quantity as newly filled and books it again. A 1-coin
  lot with a pending sell, 0.4 filling between the reads, ended up reading **0.2** in the
  ledger while the account held **0.6**, and the untracked 0.4 invites a replacement purchase.
  Reordering the reads does not help; they are not atomic either way. `reconcile_lots` now
  stands down entirely while that symbol has an unsettled order (`symbol_has_pending`), so
  attributable fills settle first and external adjustment waits for quiet. moon_bot's
  equivalent reconcile carries the same guard.
- **A confirmed fill outranks any position read that might predate it.** "No longer pending"
  is not the same as "the broker's position endpoint knows". `reconcile_pending` returns the
  set of symbols whose ledger moved on a confirmed fill **this cycle**, and external
  adjustment skips those entirely — a just-settled buy compared against a lagging position
  read looks exactly like an external disposal. moon_bot had the worse version of this: it
  measured against `bot.positions`, the snapshot `FleetBot.refresh()` caches **before**
  `cycle()` runs, so the comparison was against a list captured while the buy was still
  unfilled. A just-confirmed 1 ETH buy left `qty=0`, no replacement-purchase guard, and — with
  a triggered trailing stop — **zero exit submissions**. Both bots now read positions fresh,
  per symbol, after settlement, through `utils.account_position_qty`.
- **A triggered stop on an unreadable position is loud, not silent.** moon_bot will not sell
  into a position it cannot see, but the old code reached that state by computing
  `min(mine, total_held)` = 0 and simply continuing. It now routes through
  `registry.log_error` and retries next cycle. Risk management must stay *reachable*; when it
  cannot act, that has to be visible.
- **Crypto fills reach InfluxDB from the bots' own reconcilers** (`utils.log_confirmed_fill`).
  Both crypto bots are `reconciled=False` in the registry, so `reconcile_fills` never visits
  them — survivable while every crypto order was a MARKET order, since `submit_and_log_order`
  polls those to completion and logs the fill inline. The moment the grid's exits became LIMIT
  orders it stopped being survivable: a resting limit order returns unlogged, so completed grid
  **sells** had no path into `crypto_trades` while its **buys** still did, and the accountant
  would have seen a book that only ever bought. `log_confirmed_fill` fires on TERMINAL orders
  only (an open partial may yet fill completely and be logged at its broker `filled_at`), and
  routes a broker-stamped fill to `_log_fill_to_influx` and a terminal partial to
  `log_terminal_partial_fill` — both idempotent, so repeated reconciliation overwrites one point.
- **Every terminal-partial write goes through ONE identity function.** The market branch of
  `submit_and_log_order` used to call `_log_fill_to_influx` directly, which stamps a partial
  with no broker `filled_at` using `time.time_ns()`, while the crypto reconciler stamped the
  identical fill with `log_terminal_partial_fill`'s deterministic synthetic time. Two
  timestamps means **two rows for one trade**, not an idempotent overwrite — it doubled the
  quantity for grid market buys and moon market buys/sells. The submission poll now calls
  `log_confirmed_fill` like everyone else. Repeatability of each helper individually was never
  the property that mattered; agreement between them is.
- **A refused write is a queued delivery, not a lost trade.** `_log_fill_to_influx` swallowed
  HTTP failures, `log_confirmed_fill` reported success anyway, and the reconcilers dropped the
  pending order regardless — so one 503 lost a crypto trade permanently, with no backfill path
  because crypto is excluded from `reconcile_fills`. Broker **settlement** and InfluxDB
  **delivery** are now separate: settlement applies once, delivery retries. A line that fails
  to land is stashed in the state file's `outbox` (bounded at `FILL_OUTBOX_MAX`, loud on
  overflow) and retried by `utils.flush_fill_outbox` at the top of every cycle until InfluxDB
  answers 204. Every queued line carries its own deterministic timestamp, so a replay
  overwrites the same point. The outbox persists across restarts. **It is not a lossless
  guarantee**: at `FILL_OUTBOX_MAX` (500) the oldest queued row is dropped with a
  `registry.log_error`. It recovers ordinary transient failures; it does not survive an
  arbitrarily long InfluxDB outage, and crypto has no broker-side backfill (see Known Issues).
- **Crypto symbols are compared canonically.** Alpaca reports positions as `BTCUSD` while the
  bots' `SYMBOLS` use `BTC/USD`. moon_bot keyed its position dict on the raw broker symbol and
  looked it up with the slash form, so `total_held` came back 0, the reconcile decided the
  ledger over-stated reality and zeroed the coin, and the trailing-stop branch was never
  reached — a stop that silently became a ledger wipe. `utils._same_symbol` and the canonical
  keys in both bots settle it.
- **A failed position read is not a flat position.** `account_qty` returns `(qty, known)`: a
  404 is an *answer* ("no such position"), a timeout or a 500 is silence. Only a **known** read
  may retire inventory. Before this distinction existed one timeout returned `0.0`,
  reconciliation treated it as authoritative, and every lot in the file was retired and
  persisted — a read failure that used to merely skip a sell became destructive the moment
  durable state existed. An unreadable *ledger* likewise suspends entries instead of returning
  a tradable empty book.
- **Grid entries are fail-closed.** `bots.crypto_grid.entries_enabled` in `bot_config.json`
  must be explicitly `true` before the grid opens anything; absent or false means no new
  buys. Sells, reconciliation and the ledger all run regardless, so existing inventory can
  always wind down. Default-off is deliberate: the PR #22 review asked that entries stay off
  until the fill-driven ledger and the migration of the pre-existing coins are verified
  against a live account, and a lever defaulting to on makes "nobody got to it" look
  identical to "we checked".
- **The grid sells strict FIFO, at an executable floor.** Selling the oldest *qualifying* lot
  (skipping underwater ones) is specific-lot selection, and it disagreed with the accountant:
  buy 1 at $200 then 1 at $90, sell at $120, and execution books +$30 against the $90 lot while
  the books book −$80 against the $200 lot. The oldest lot is now the only candidate — if it
  does not clear its cost, nothing is sold. And the sell is a **LIMIT at the floor**, not a
  price check in front of a market order: spread, slippage and fees all land *after* such a
  check, so it guarantees nothing. Resting sells are cancelled after `PENDING_SELL_TTL_SECONDS`
  and re-decided. `cancel_my_open_orders` filters on the `crypto_grid-` tag; the old helper
  cancelled every open order on the symbol, including moon_bot's.
  **Migration note:** on the first run the ledger is empty while the account still holds
  crypto. The grid deliberately does not adopt that inventory — attribution between
  crypto_grid, moon_bot and untagged history is precisely what the audit could not establish,
  and a made-up cost basis would go straight into the new sell guard. So those coins are
  inventory the grid **will never sell** (logged once per symbol at startup). They are not at
  risk of being double-bought: `bot.budget_ok` still counts the real account positions, so the
  bot's total allocation is enforced. Winding them down is a human action — sell manually, or
  seed `crypto_grid_state.json` with lots carrying the real basis.

### Market Data Correctness (post 2026-09-11 performance follow-up)

**Alpaca returns bars ascending from `start` and truncates at `limit`** — so
`StockBarsRequest(start=<wide>, limit=<small N>)` hands back the **oldest** N bars in the
window, not the newest. Nothing raises: the frame has the right columns, the right dtypes
and a plausible price, it is simply weeks out of date. Three of the fleet's four fetch sites
were in that state for months while every process reported healthy:

| Site | Request | Newest bar on 2026-09-11 |
|---|---|---|
| survivor 15m | `start=-20d, limit=200` | Aug 27 (~2wk stale) |
| survivor SMA200 | `start=-460d, limit=250` | Jun 5 (~14wk stale) |
| moon donchian | `start=-60d, limit=30` | Aug 12 (~4wk stale) |
| trend 15m | `start=-10d, limit=500` | **current** |

trend was correct only by accident — a 10-day 15m window holds ~280 bars, so its
`limit=500` never truncated. That is the whole reason this survived: the bug is invisible
unless `limit` < bars-in-window, and one of four sites happened to sit on the right side of
that line. moon compounded it with `df.iloc[:-1]` ("drop today's forming bar"), which on an
already-truncated frame discards a **completed** bar and ages the Donchian levels by one
more day.

The fix is structural, not a re-tuning. **Never pass `limit` alongside `start` on a bar
request** — size `start` to the history the indicator needs, let alpaca-py page the window,
and take the newest rows with `utils.newest_bars`. `test_bar_freshness` enforces this by
parsing the source: a reintroduced `limit` fails the suite, which is the only guard that
survives a future window change.

Belt and braces, `utils.bars_are_fresh` refuses to let a stale frame drive a decision and
routes the rejection through `registry.log_error` (a `[STALE]` line + a Grafana-visible
error) rather than skipping quietly. Daily-bar callers pass a wider `stale_factor` so a
normal weekend or holiday close is not mistaken for an outage. `utils.drop_forming_bar`
decides "is the last bar still forming?" from its timestamp, so it is correct on a fresh
frame and on a stale one.

`market_analyst`'s SPY fetch never carried a `limit` and was not affected, but a *successful
fetch of stale bars* is invisible to its existing fail-safe (which only keys off fetch
**failure**). It now publishes `spy_bar_age_hours` on the `market_regime` row and logs loudly
past `SPY_BAR_WARN_HOURS`. It deliberately does **not** gate on that: the analyst runs 24/7,
a Friday close plus a Monday holiday legitimately ages the newest bar past three days, and
refusing the frame would force `CRITICAL_VOLATILITY` over a normal long weekend — a
self-inflicted halt strictly worse than the condition it reports.

`fleet_doctor` section 5b issues each bot's **own** fetcher and prints the timestamp that
comes back, because this failure class cannot be seen from process health or error counts.

**The probe speaks the fetchers' contract, and takes their verdict rather than recomputing
it.** Its first version did neither, and the first live run on the Beelink (2026-09-11) said:

```
[ FAIL ] survivor_bot 15m (SPY): 2 bars, but no usable timestamp.
[ FAIL ] trend_bot 15m (SPY): 2 bars, but no usable timestamp.
```

on a completely healthy feed. `get_data_alpaca` returns `(df, indicators_ok)`; the probe used
the return value as a DataFrame, so `len()` was the **tuple's arity** — "2 bars" — and a tuple
has no index, hence "no usable timestamp". Both bots unpack correctly, so nothing was wrong
with the fleet: the only broken thing was the check built to catch a frame that is plausible
and silently wrong, reporting its own type error in exactly that shape — a specific, credible
number produced by code that never looked at the data. The contract change and its only
out-of-bot caller shipped in the same PR, and no test drove the caller, so neither review nor
the suite saw it.

It now also **primes `bot.session_elapsed`** before fetching, from
`fleet_bot.session_elapsed_seconds` — the same function `FleetBot.refresh()` uses. The bots
widen their freshness bound by how far into the session it is; `refresh()` never runs under
fleet_doctor, so the attribute stayed `None`, the flat 45-minute bound applied, and the probe
would have called every weekday morning a data outage while the fleet was correctly trading.
Taking `indicators_ok` rather than recomputing a second bound is the point: a diagnostic that
derives the same fact its own way will eventually disagree with the thing it is diagnosing.

**A stale frame stops entries, never exits.** The first version of this guard returned `None`
for stale bars, the caller did `continue`, and `manage_position` was never reached — so a
stale history feed silently suppressed stop losses, take profits, max-hold and EOD
liquidation on every held position. That is the same defect the tiered-hold reordering
removed, reintroduced one layer up in the caller. The fetchers now return
`(df, indicators_ok)`: a held symbol **always** reaches `manage_position`, which runs the
price-based exits off `utils.live_equity_price` and suppresses only the bar-derived ones (the
RSI exit, the crossover exits, the ADX/EMA/RSI inputs to the hold score — a missing indicator
scores *lower*, biasing toward CLOSE_EOD, which is the safe direction when blind).
`live_equity_price` has **no fallback**: the bots used to fall back to the latest bar close,
which is safe only while the bars are fresh, and a stop loss priced off a two-week-old close
is worse than none because it looks like risk management. No validated price at all ⇒ the bot
says so loudly and manages nothing that cycle.

**Freshness is session-aware, and the allowance widens — it never skips.** At Monday 09:30
the newest 15m bar is Friday's close, ~65h old, which any honest intraday bound rejects, so
without `session_elapsed` (`FleetBot.session_elapsed`, from a hardcoded 09:30 ET open
matching the existing EOD windows) the guard manufactures a data outage at every open. But
the first attempt at that *exempted* the frame — returning True before inspecting the
timestamp at all — which accepted a **two-week-old** bar during the opening 45 minutes and
skipped the undateable-frame check with it, so both equity bots could compute entry signals
from stale data every morning. The bound is now *widened* instead: before enough session has
elapsed to have produced a fresh bar, the newest bar may be as old as the session is plus
`PRIOR_SESSION_GAP_SECONDS` (4.5 days — Friday 16:00 ET to Tuesday 09:30 ET across a Monday
holiday is ~89.5h), and no older. The timestamp is always checked.

### The bot_config.json Contract

`bot_config.json` is gitignored and lives on the host, so **no config change ever arrives by
deploy**. Anything a release adds to `bot_config.template.json` is a manual step on the
Beelink, every time. The live file also accumulates runtime state the template never had —
`vix`, `vix_source`, `data_stale`, `regime_updated`, `CAPITAL_CRUNCH`, commander's paused
`status` values, the wheel's per-ticker levers. **Drift in that direction is expected and
healthy; never `cp bot_config.template.json bot_config.json` on a running fleet** — it resets
`market_condition` to a tradeable regime (un-gating wheel_bot and crypto_grid), clears a
latched `emergency_stop`, and discards every allocation you tuned.

Drift in the *other* direction was invisible until 2026-09-11: every read is a
`.get(key, default)`, so a missing key is silent, and two of those defaults are not
conservative.

| Missing key | Silent default | Consequence |
|---|---|---|
| `global_settings.vix` | `15.0` | **Below every gate** — the VIX kill-switch reads a calm market |
| `cfo_settings.unallocated_reserve` | `0.0` | No reserve; every budget computes on full equity |
| `global_settings.market_condition` | `"SIDEWAYS"` | Tradeable — un-gates wheel_bot and crypto_grid |
| `bots.crypto_grid.entries_enabled` | `False` | Grid opens nothing (safe, but silent) |

`fleet_registry.missing_config_keys()` declares the whole expected shape —
`GLOBAL_SETTINGS_KEYS`, `CFO_SETTINGS_KEYS`, `REQUIRED_BOT_KEYS`, and each bot's own
`config_keys` — and `fleet_doctor` section 8 reports what the live file lacks, failing on the
unsafe defaults and warning on the rest. It lives in the registry rather than in fleet_doctor
because the registry already owns this contract (see "Adding a new bot", steps 2–3), so a
newly registered bot is covered the moment it is registered. `unregistered_config_bots()`
separately reports `bots{}` entries with no registry entry — harmless, but dead.

**The template itself was missing `vix`**, which the check found on its first run: a fresh
install traded as though the market were calm until market_analyst's first successful fetch,
up to 15 minutes of a disabled kill-switch that looked exactly like a working one. The
template now seeds the analyst's **own blind-state values** — `CRITICAL_VOLATILITY`, VIX 25,
`data_stale: true` — because a config that has never had a successful fetch *is* the stale
case, and `STALE_VIX_SENTINEL` is what the analyst writes when it is blind. A fresh install
therefore boots gated and un-gates itself once the regime is real.

### CFO / Budget Enforcement

`utils.check_budget(bot_name, client)` before every entry:

- Budget = dynamic `effective_budgets.json` (written by the accountant's reallocation) with
  fallback to `cfo_settings.base_allocations × (1 − unallocated_reserve) × equity` — the same
  formula the reallocator uses, so the two paths can't drift. `bots{}.allocation` only covers
  bots outside `cfo_settings`.
- **Fail-closed** when `bot_config.json` is missing or the bot has no allocation.
  **Fail-open** on runtime/API errors (deliberate for paper trading — revisit before live).
- Counts held positions (options at collateral) + pending tagged orders.
- **A boolean budget check is not a size.** `budget_ok` says there is room, never how much.
  moon_bot sized every breakout at a flat 10% of equity against a 4% allocation — a ~2.5x
  overshoot on each entry — until it was clipped to `utils.get_available_budget`.
  crypto_grid compared **one symbol's** value against the bot's **whole** budget, so all
  three symbols could each spend it: a 3x overrun in which every individual check read as
  compliant. It now uses `crypto_grid.per_symbol_budget()` and clips each slice to the
  remaining per-symbol headroom.
- **Unfilled buys reserve their capital.** The pending loop counted only equity *limit* buys
  and short options, so equity **market** buys (no `limit_price`) and **all crypto** reserved
  nothing — several in-flight entries each saw the same headroom and spent it.
  `utils._pending_unit_price` prices a pending order through limit → dollar notional →
  partial-fill average → the current mark on a held position → a live quote (equities only).
  When **none** of those answer — a fresh quantity-based crypto MARKET buy has no limit, no
  notional, no fill and no position, which is precisely the new-entry case — `check_budget`
  now **fails closed** for that bot rather than warning and reserving zero. Warning and
  continuing meant the next symbol saw the same headroom and spent it again. Entries resume
  as soon as the order fills, prices, or terminates. crypto_grid also reserves its own
  outstanding buy notional in its ledger (`outstanding_buy_notional`), which is the tighter
  of the two.
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
- **Window P&L needs opening inventory** (2026-09-11). `build_window_metrics` used to filter
  fills to the window and FIFO-pair only those, so a sell whose opening buy predated the
  cutoff had no lot to close: `realized_metrics` appended it as a fresh **short** lot, the
  sale booked **zero** realized P&L, and any later in-window buy was scored as covering a
  short that never existed. Each window now seeds its FIFO books from
  `opening_inventory(fills, cutoff)` — and the production fetch spans
  `LEDGER_LOOKBACK_DAYS` (longest window + 180d), because defaulting to `max(WINDOW_DAYS)`
  left the **60d** window with no prior fills at all: the fix worked on the short windows and
  was silently absent on the one that matters most. `opening_inventory` also reports which
  bots it could **not** reconstruct — a residual *short* lot means a close whose open predates
  the fetch, so the history is incomplete and nothing derived from that bot's basis is
  trustworthy. **Hitting the fetch cap is a coverage fact, not a log line**:
  `fetch_alpaca_fills` returns `(fills, complete)`, and an incomplete fetch marks every bot
  unreconstructable — including the ones that happen to look flat, which is exactly the case
  that would otherwise be ranked on an unverifiable starting inventory.
- **Period return is reported only when it can be measured** — no proxy. A window's return is
  `realized-in-window + (unrealized_end − unrealized_start)`, and `unrealized_start` needs a
  mark at the window's open that this fleet stores nowhere. So `total_pl` is populated **only**
  when the bot started the window flat (then everything it holds now was opened inside the
  window and the sum is exact); otherwise it is `null`, `period_return_available` is false,
  and the bot leaves the ranking with a stated reason. The rejected approximation —
  apportioning the lifetime unrealized figure by the open notional each window opened — can
  **reverse the sign**: an old position up $100 and an equally sized new one down $50 net to
  +$50 lifetime, and an equal split credits the new window +$25 when its actual change was
  −$50. Disclosing that in a notes field did not stop it being called `total_pl` and ranking
  the bots for reallocation. `lifetime_unrealized_pl` carries the raw figure, labeled.
- **An impossible cost basis excludes the strategy from scoring.** A negative cost basis is
  *normal* on a short — selling to open is a credit, so every wheel_bot short put carries one.
  Checking the sign alone called routine premium selling an anomaly, which is worse than not
  checking (rule 10: a detector that fires on normal operation trains you to ignore it). The
  real anomaly is a **long** with a negative basis: `accountant._has_impossible_basis` keys on
  side (and on signed `qty`, since some option positions arrive without a usable `side`).
  On 2026-09-11 long ETH and SOL carried roughly -$1,196 and -$65, yielding ~$4,914 of
  "unrealized crypto profit" on ~$3,652 of market value — most of why the advisor wanted to
  move another 2% out of trend_bot and into crypto_grid. Flagging alone did not stop that:
  the affected **owning bots** are now passed to the advisor as `excluded_bots`, which voids
  their period return and drops them from the ranking until the basis is reconciled. The
  `accounting_anomaly` metric is written **every cycle, zero included**, because a series that
  only exists while something is wrong can never show that it cleared.

### Safety Gates (in `utils.submit_and_log_order`)

- **Containment (gate 0, before any broker call):** orders are refused outright unless the
  process is running inside a container (`utils.assert_order_allowed_here`). The fleet runs
  only in `trading-fleet`; a second copy on the same `config.py` trades the same Alpaca
  account and submits orders tagged identically to the real ones, which the ownership model
  cannot tell apart. Detection is deliberately generous — `/.dockerenv`, `/run/.containerenv`,
  the code living at `/app/code`, or a container cgroup, any one is enough — because a false
  negative would stop the real fleet trading. Blocks route to `registry.log_error`
  (`containment_guard`) so a rogue fleet is visible in Grafana rather than silently idle.
  Deliberate host-side work sets `FLEET_ALLOW_UNCONTAINED_ORDERS=1`, which warns once
  (not per order) and proceeds. `fleet_doctor` reports the guard's verdict in section 1,
  so a misfire is visible before it bites.
- Daily loss cap: `MAX_DAILY_LOSS` (-$5,000) blocks orders that **open or increase**
  exposure. It used to block *every* order, before looking at what the order did — so a fleet
  down $5,000 could no longer sell a losing long, cover a short, or buy back a short option.
  A circuit breaker that traps you inside the position is the opposite of a risk control, and
  no amount of exit reordering in the bots could fix it, because the block was below them in
  the shared submit path. `utils.exposure_increasing_qty` is the shared classifier (signed
  position quantity, crypto's BTCUSD/BTC-slash-USD spelling normalised); the loss cap and the
  notional/exposure caps both use it, so the two cannot disagree about what "closing" means.
  A partial close is fully exempt; an over-sell is blocked only on the part that flips the
  position into a new short.
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
CBOE delayed-quote JSON → stooq CSV → yfinance, first sane reading wins. That order
is measured, not assumed: from the fleet container on 2026-09-06 CBOE answered in 0.2s,
stooq returned HTTP 404, and yfinance timed out after 130s (so yfinance carries its own
short `VIX_YF_TIMEOUT`, since three chain attempts at 130s would stall the loop for
minutes). Every source returns
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

**A hold decision governs the EOD sweep, never risk management** (fixed 2026-09-11). Both
equity bots evaluated the tiered-hold branch *first* and returned early on a
HOLD_OVERNIGHT/HOLD_SWING tier — so from 15:30 ET a position scoring "hold" had **no stop
loss, no take profit and no signal exit**, for the rest of the session and straight through
the overnight gap, which is the one window where a gap can actually happen. The tier meaning
"worth carrying overnight" was the tier that removed its protection. Stops/targets/crossovers
now run ahead of the hold branch in both `survivor_bot.manage_position` and
`trend_bot.manage_position`; the hold override itself is unchanged and still suppresses the
EOD sweep when no risk exit fires. `test_risk_exits` pins the ordering (it fails 7 ways
against the pre-fix code).

The same failure then reappeared one layer up, in the callers: a stale-bar guard returned
`None` and `cycle()` skipped the held symbol entirely, so `manage_position` was never reached.
See **Market Data Correctness** — indicator eligibility and risk management are now separate
questions, and `test_risk_exits` drives `cycle()` rather than `manage_position`, because the
bug lived in the caller both times.

*Still partially wired:* `OVERNIGHT_STOPS` stop/trailing percentages and `premarket_check()`
are defined but not enforced anywhere — the bots' own stop percentages are what close the
gap above. `max_hold_days` remains the only tiered_hold backstop that is live.

## Key Conventions

- **One name per bot, everywhere.** The registry key (e.g. `moon_bot`) is the PM2 process name,
  bot_config key, order-tag prefix, and budget key. (The old `moon_bag` PM2 name is retired.)
- InfluxDB measurements use the `_trades` suffix; webhook keys use the `WEBHOOK_` prefix
  (moon_bot's is `WEBHOOK_MOONBAG`, historical).
- Webhooks are skipped when unset or containing `"YOUR"` (unconfigured sentinel).
- Crypto bots run 24/7; equity bots gate on market hours.
- **Commit style:** lowercase, concise, no conventional-commit prefixes.

## Testing

`python -m unittest test_orphan_resolution test_fill_logging test_market_analyst test_strategy_advisor test_commander test_containment test_bar_freshness test_risk_exits test_crypto_grid test_pl_accounting test_safety_gates test_config_audit -v` —
regression suites for the ownership/entry-time paging fix (+ option-root inference and the
no-default-owner rule), fill-row stamping / wheel close-ladder pricing, and the market-regime
pipeline (SPY-df normalization, VIX>28 kill-switch, loud-failure + stale fail-safe), plus the
paper-only allocation advisor. Run the
first two after touching `utils.py` ownership/order/logging code or wheel close logic, and
`test_market_analyst` after touching `market_analyst.py` (it covers each VIX source's parsing
and the chain's fall-through/rejection rules), and `test_commander` after touching the watchdog's
alerting. Run `test_containment` after touching anything in the order-submission path —
its load-bearing assertion is that an uncontained fleet never reaches the broker at all.

Run **`test_bar_freshness` after touching ANY bar request.** Its load-bearing assertion is
source-level: it parses every fetch site and fails if a `StockBarsRequest`/`CryptoBarsRequest`
pairs `start` with `limit`. That is the only check that survives someone widening a window
later, because the bug it catches produces a plausible frame rather than an error.
It also drives `fleet_doctor`'s own section 5b and section 6 (`BarProbeContractTest`,
`VixSourceClassificationTest`), because both shipped broken: **run it after touching
`fleet_doctor` too**, not just after touching a bar request.
Run `test_risk_exits` after touching either equity bot's `manage_position` — it asserts a
stop loss fires inside the 15:30+ hold window, the case that was silently disabled.
Run `test_crypto_grid` after touching grid entry/exit or either crypto ledger — it covers the
whole order lifecycle (delayed fill, zero-fill rejection, partial-then-canceled, repeated
reconciliation, restart while pending) because every one of those was a way to invent or
destroy inventory. Run `test_config_audit` after adding any `bot_config.json` key or registering a bot — it
asserts the shipped template defines everything the code reads, and that a fresh config boots
into the fail-safe rather than a calm market. Run `test_pl_accounting` after touching
`accountant.calculate_realized_pl`
or the advisor's window metrics, and `test_safety_gates` after touching the order-submission
path or `check_budget_details` — its load-bearing assertion is that a breached daily loss cap
still lets a long exit, a short cover and an option buy-to-close through.

Strategy/advisor changes are still validated through paper trading; there is no
backtest harness. Two dependencies are not installable everywhere: `ta` is sdist-only and
fails to build on some toolchains (`test_risk_exits` stubs it when absent), and the suites
need a `config.py` — copy `config.example.py` for a local run.

**`fleet_doctor.py`** is the diagnostic entry point — run it *in the container* against the code
the fleet actually runs:
`docker exec -w /app/code trading-fleet python3 fleet_doctor.py`. It verifies location, syntax +
uncommitted drift, config completeness, that **every PM2 process survives import** (the one
failure class a bot's own main-loop `try/except` cannot catch), Alpaca, **the age of the bars
each bot actually trades on** (section 5b — it calls the bots' own fetchers, since a stale
frame is a *successful* fetch and shows up nowhere else), each VIX source separately, an
InfluxDB round-trip, **how many commanders are writing telemetry**, the `market_regime`
heartbeat, and pm2 state. Read-only; never orders.

Two of its checks are deliberately *not* file-mtime based, because mtime lies here:
`bot_config.json` is only rewritten when a published value moves (a closed weekend with a
frozen VIX legitimately ages it), so the live-regime signal is the newest `market_regime`
InfluxDB row; and `active_targets.json` is written by a weekday-only scout, so its age is
judged against that schedule rather than a flat 24h.

## Known Issues / Tech Debt

- **tiered_hold overnight stops unwired** (see above) — `max_hold_days` is the only enforced
  tiered_hold backstop. The bots enforce their own stop/target percentages around the clock
  now, so a held-overnight position is no longer unprotected, but `OVERNIGHT_STOPS`'
  per-tier trailing stops and `premarket_check()` still do nothing.
- **Negative cost bases on crypto positions are unexplained.** The broker reported long ETH
  and SOL at roughly -$1,196 and -$65 of cost basis on 2026-09-11, producing ~$4,914 of
  "unrealized profit" on ~$3,652 of market value. Those positions' owning bots are now
  excluded from advisor scoring (`accounting_anomaly` metric written every cycle,
  `assumptions.negative_basis_positions` + `excluded_from_scoring` in the report) rather than
  silently ranked, but **the upstream cause is not established** — it needs a
  reconciliation against actual fills and coin fees. An independent reconstruction of all
  BTC/ETH/SOL cash flows from inception put combined crypto P&L near **+$123**, against a
  dashboard reading several thousand; that reconstruction itself leaves a $5.49 cash
  discrepancy and small BTC/SOL quantity discrepancies, so it is provisional too.
- **Per-bot historical attribution depends on an attribution policy.** A complete-history
  FIFO reconstruction finds cross-owner lot matches among the equity bots and untagged
  orders, not only in crypto. Opening-owner allocation is a reasonable default but is not a
  fact about which strategy earned what — treat pre-2026-09 per-bot dollar totals as
  indicative.
- **Config keys are checked, but config *values* are not.** `missing_config_keys` reports an
  absent key; it does not know that `allocation: 0.9` is wrong or that `base_allocations` no
  longer sums sensibly (rule 4 is still a human check). It also cannot see a key that is
  present but stale — a hand-edited `vix` nobody updated reads as live.
- **The scout's last run lands after entries stop.** The documented schedule starts its final
  run at 15:00 CT (16:00 ET) while trend_bot and survivor_bot stop new entries at 14:00 ET
  (`FleetBot.is_eod_skip_entry`), so that run's targets cannot be acted on until the next
  session. Sequential shadow-advisor analysis delays publication further. No historical
  target archive or per-candidate timing log exists yet, so how much return this costs is
  unquantified — it is a reason to instrument the timing, not yet a measured loss.
- **Coin-denominated fees are not modelled.** `ROUND_TRIP_COST_PCT` (0.5%) is an assumption,
  deliberately an over-estimate, not a reading of the fees Alpaca actually charged. Crypto
  fees paid in coin reduce the position quantity rather than cash, so the grid's reconcile
  absorbs them as a ledger shortfall instead of attributing them to the trade that incurred
  them. Realized crypto P&L is therefore approximate by a small, unmeasured amount.
- **The fill outbox is bounded, so a long InfluxDB outage still loses rows.** Failed crypto
  fill writes queue in the bot state files and retry until a 204, but at `FILL_OUTBOX_MAX`
  (500) the oldest is dropped with a loud `registry.log_error`. That covers a restart or a
  transient 503; it does not cover a multi-day outage. Closing it properly needs either
  durable spill storage or a broker-side backfill for crypto — `reconcile_fills` deliberately
  skips both crypto bots because their `grid_buy`/`grid_sell` action vocabulary cannot be
  rebuilt from an order alone. Until then, treat an `[Outbox] FULL` alert as data loss that
  has already happened, not a warning that it might.
- **`max_orders` truncation is reported, not recovered.** If the advisor's history fetch hits
  its cap, `fetch_alpaca_fills` returns `complete=False`, every bot's period return reports
  `truncated_history_fetch` and no allocation is recommended. Correct, but blunt: a long
  enough order history costs you the ranking rather than triggering a deeper page walk.
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
  source's health individually — and a dead source is a **warning** while any source is still
  live, a failure only when the last one dies. stooq has 404'd since 2026-09-06; reporting a
  condition the chain is designed to absorb as `[ FAIL ]` on every run put a permanent red line
  in the summary and, through the exit code, made a healthy fleet look broken forever (rule 10,
  in the one section whose header already said "a red line here is not an outage by itself").
- **`ta` is an sdist-only, effectively unmaintained dependency** (trend_bot's EMA/ADX,
  survivor_bot's RSI). It builds and computes correctly under the pinned pandas 3.x / numpy 2.x
  set, but it is the one package here that can fail a container rebuild outright on a toolchain
  change — and on 2026-09-11 it did exactly that on a clean Python 3.11 box (`pip install ta`
  failed to build a wheel), which is the failure mode described below arriving in practice. `market_analyst.TechnicalMath` and `market_scanner.TechnicalMath` already implement the
  same indicators natively, so replacing it is a contained job if it breaks — the reason it hasn't
  been done pre-emptively is that it would change live indicator math with no backtest to validate
  against.
- **Two fleets can run at once.** Confirmed on 2026-09-06: `pm2 ls` *on the Beelink host*
  returned a full nine-process fleet with restart counts in the thousands (and a `moon_bag`
  fossil from before the containerisation), resurrected by a `pm2-trader.service` systemd
  unit when a long-deferred OS update rebooted the box — while
  `docker exec trading-fleet pm2 ls` showed nine healthy processes at zero restarts. Both
  were true, of different PM2 daemons. Orders are now blocked off-container (see Safety
  Gates), and both halves of the diagnosis are automated: The fleet is supposed to
  run only inside `trading-fleet`. A commander started anywhere else (a host-level PM2
  daemon resurrected by a reboot, a second container) watches a *different* PM2 daemon
  and alerts Discord about *its* processes — which is why it produced "bot down" pings the
  container flatly contradicted. Every commander alert carries
  `commander._source_tag()` (hostname + pid + in-container-or-not), and `fleet_doctor`
  reads the `host` tag on `bot_monitor` to report every commander currently writing
  telemetry. More than one live host = a second fleet.
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
9. **An alert must name its own source.** `bot_monitor` is tagged with the writing
   host, and every commander Discord message carries host + pid + whether it is in the
   container. An alert that cannot say where it came from is not diagnosable when two
   copies of the fleet are running — which has happened.
10. **Alerts must survive being right.** A watchdog that fires every cycle for every bot is
   indistinguishable from noise, and the 2026-09 storm buried a real failure under ~9 identical
   pings per cycle. Anything that alerts on a *persistent* condition needs a throttle, and its
   wording must distinguish the conditions it covers (see `commander._alert_down`).
11. **Run the orphan regression suite** after touching ownership/order-history code.
12. **Never pass `limit` alongside `start` on a bar request.** It returns the OLDEST bars in
   the window. Size the window to the history the indicator needs and slice the newest rows
   with `utils.newest_bars`. `test_bar_freshness` fails the build if this returns.
13. **Risk exits come before hold/EOD policy.** A tier, a regime, or a time-of-day branch may
   decide whether to *sweep* a position; none of them may decide whether its stop loss
   applies. If a new exit path is added, put it ahead of the tiered-hold block.
14. **A boolean budget check is not a size.** `budget_ok` says there is room, not how much.
   Size entries against `utils.get_available_budget` (or a per-symbol share of it), and clip
   the order rather than trusting a percentage-of-equity target.
15. **Don't promote an allocation on an unexplained number.** The advisor is paper-only and
   advisory for exactly this reason; check `assumptions.negative_basis_positions` and the
   `source_comparison` deltas before acting on a recommendation.
16. **A submitted order is not a filled one.** `bot.submit()` returns an order object for a
   pending, rejected, canceled or partially-filled order alike. Never write inventory,
   quantity or cost basis from a submission — reconcile it from the broker's cumulative
   `filled_qty`/`filled_avg_price` against an applied watermark, so the update is idempotent
   and a partial keeps its remainder.
17. **A failed read is not a zero.** Distinguish "the broker said none" (a 404) from "the
   broker did not answer" (timeout, 500). Only an answer may retire durable state. This is
   the same rule as `OrderFetchError` in the ownership map, one layer down.
17a. **Compare symbols canonically.** Alpaca spells crypto positions `BTCUSD` and orders
   `BTC/USD`. A raw-string comparison between the two silently reads as "no position", which
   downstream becomes a ledger wipe or a phantom short. Use `utils._same_symbol`.
17b. **A widened bound is not a skipped check.** When a guard must be relaxed for a known
   legitimate case, raise the threshold — never return early before validating. The early
   return also skips every *other* check on that path, which is how a session-open allowance
   came to accept two-week-old bars and undateable frames alike.
18. **A risk control must not trap you inside the position.** Anything that blocks orders
   has to ask what the order *does* first: exposure-increasing orders can be refused,
   risk-reducing ones must have a path through. Classify with
   `utils.exposure_increasing_qty` so every gate agrees on what "closing" means.
19. **Idempotence is a property of the SYSTEM, not of a helper.** Two functions that each
   overwrite their own row still produce two rows if they stamp the same event differently.
   When more than one path can write the same fact, they must share one identity function —
   and the test must assert the resulting quantity, not that each helper repeats itself.
20. **Settlement and delivery are different jobs.** Applying a fill to trading state must
   happen exactly once; getting it into the reporting database must retry until it lands.
   Never let a failed write silently finish a state transition — queue the delivery.
21. **A log line is not a control.** If a condition should change behavior, it has to be
   returned and acted on, not warned about. "Hitting max_orders will make returns
   unavailable" was logged while the same plain fill list went downstream and got ranked
   anyway.
22. **A key the code reads belongs in `fleet_registry`'s config contract.** Adding a
   `bot_config.json` read means adding it to `GLOBAL_SETTINGS_KEYS`, `CFO_SETTINGS_KEYS` or
   the bot's own `config_keys`, with its silent default and why that default matters. The
   live file is gitignored, so a new key never arrives by deploy — if it is not declared,
   nothing will ever tell the operator it is absent.
23. **A bootstrap default must fail safe.** A fresh `bot_config.json` has had no successful
   market fetch, so it must seed the same values the analyst publishes when blind, not a
   calm market it never measured.
24. **Report an unmeasurable number as unmeasurable.** If a metric needs data the fleet does
   not store, publish it as unavailable and withhold whatever depends on it. Do not
   substitute a proxy and disclose the substitution in a notes field — the number still gets
   used, and a proxy that can invert the sign of a return is worse than a gap.
25. **The diagnostic is production code.** `fleet_doctor` is what you read when you cannot
   trust anything else, so a broken check is worse than no check — it answers with the same
   confident formatting whether or not it looked. Section 5b reported "2 bars" for a tuple's
   arity on a healthy feed; the unsafe-default audit found the shipped template was missing
   `vix`. Both were caught by running them, not by reading them. When a check cannot reach
   the broker from the session, **stub the dependency and drive the check anyway**
   (`BarProbeContractTest`) — "it needs a live account" is why these ship untested, and it
   is not a reason, because the interesting half is the check's own logic.
26. **A diagnostic reports the fleet's verdict; it does not form its own.** If the code being
   checked already decides something — is this frame fresh, is this order closing — take that
   answer and print it. A second implementation in the checker will drift, and then the two
   disagree in exactly the incident where you needed one of them to be authoritative. Share
   the function instead (`fleet_bot.session_elapsed_seconds`). This is rule 19 pointed at the
   tooling.
27. **A tolerated failure is a warning, not a failure.** If a design exists specifically to
   survive something — a fallback chain, a spare, a retry — then that thing happening is not
   a failure of the system, and reporting it as one every run burns the summary and the exit
   code that a real failure needs. Judge it after all the alternatives are known: dead **and
   uncovered** is the failure.
