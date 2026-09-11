# Trading Fleet Runbook

## Overview
The fleet ("The Executor") runs on the Beelink as the `trading-fleet` Docker container:
PM2 inside the container manages every bot and infra process. The brain
(Corsair / LM Studio / Gemma 4 21B MoE / Task Scheduler) pushes `active_targets.json`
to `~/bots/repo/` 3x daily; the live mount makes it visible in-container.

## Key Boundaries
*   **Brain (Corsair):** Runs Market Scanner + Sector Scout via Task Scheduler. Generates
    `active_targets.json` and SCPs it here.
*   **Executor (Beelink):** `trading-fleet` container consumes targets, enforces safety
    gates, and executes on Alpaca. Sibling containers: `influxdb`, `grafana`, `wg-easy`,
    `searxng` (see `~/homelab/docker-compose.yml`).
*   **Generated files** (`active_targets.json`, `bot_config.json`, `effective_budgets.json`,
    `moon_bot_state.json`, `logs/`) are runtime state in the repo dir on the host, gitignored.

## First Command in Any Incident

```bash
docker exec -w /app/code trading-fleet python3 fleet_doctor.py
```

`fleet_doctor.py` checks, in the container, against the code the fleet actually runs:
where it is running from, whether every `.py` parses and every PM2 process survives
**import**, whether `config.py` is complete (and `INFLUX_HOST` is not `localhost`),
Alpaca auth + SPY bars, **each VIX source independently**, an InfluxDB round-trip, and
the freshness of `bot_config.json` / `active_targets.json` plus live `pm2` state.
Read-only apart from one InfluxDB test point; it never places an order.
Exit code 0 = clean, 1 = something failed. `--skip-network` for an offline code check.

It exists because an import-time crash is the one failure a bot's own `try/except`
main loop cannot catch — the loop never starts — so it looks identical from Discord
to every other kind of outage. It also answers the "did my edit land in the right
place?" question directly: it prints the running file's hash, its mtime, the git
HEAD, and any uncommitted drift.

## Daily Operations

```bash
# What's running (ground truth)
docker exec trading-fleet pm2 ls

# Deploy new code (no dependency/deploy changes)
cd ~/bots/repo && git pull
docker exec trading-fleet pm2 restart all

# Deploy when requirements.txt or deploy/ changed
cd ~/homelab
docker compose build trading-fleet && docker compose up -d trading-fleet
docker exec -w /app/code trading-fleet python3 fleet_doctor.py   # verify the rebuild

# Logs
docker logs -f trading-fleet          # PM2 + process stdout
tail -f ~/bots/repo/logs/<bot>.log    # per-bot rotating logs (host-visible)
```

## First-Time Setup
1.  Clone repo to `/home/trader/bots/repo`.
2.  `cp config.example.py config.py`; fill Alpaca keys, Discord token/channel, webhooks.
    **`INFLUX_HOST` must be `"influxdb"`** (Docker DNS) — not localhost.
3.  `cp bot_config.template.json bot_config.json` (entrypoint does this too if missing).
4.  Add the compose service from `deploy/README.md` to `~/homelab/docker-compose.yml`,
    then `docker compose build trading-fleet && docker compose up -d trading-fleet`.

## Safety Gates
Safety is centralized in `utils.py`:
*   **Containment:** orders are refused unless the process is inside a container. A second
    fleet on this account (see the duplicate-fleet entry under Incident Response) can alert
    and crash-loop, but it cannot trade. Deliberate host-side work:
    `FLEET_ALLOW_UNCONTAINED_ORDERS=1`. `fleet_doctor` section 1 reports the verdict.
*   **Fail-Closed Budget Checks:** entries pause if `bot_config.json` is missing or the bot
    has no allocation. (Runtime API errors currently fail-open — paper-trading choice.)
*   **Target Freshness:** `active_targets.json` older than 24h is rejected → standby mode.
*   **Daily Loss Cap:** all orders blocked below `MAX_DAILY_LOSS` (-$5,000).
*   **Exposure Caps:** $20,000 max order notional, $5,000 max symbol exposure — enforced on
    any equity order that opens or increases exposure (longs AND shorts); closes are exempt.
*   **CAPITAL_CRUNCH:** accountant halts new entries fleet-wide above 90% capital utilization.

## Incident Response
*   **Discord:** `/status` for fleet state; `/panic` stops all bots and sets the emergency
    flag; `/resume` clears it (commander's watchdog revives bots).
*   **Manual stop:** `docker exec trading-fleet pm2 stop all` (or `docker stop trading-fleet`).
*   **Error pipeline:** every registered error lands in `logs/fleet_error_registry.jsonl`;
    `error_watchdog` ships them to InfluxDB (`bot_error_events`) → visible in Grafana.
*   **Orphan alerts:** the accountant flags held positions no bot is managing
    (`orphan_position` measurement + overseer webhook). Investigate before closing manually.
    The sweep stands down (no alerts, `orphan_sweep` skip metric instead) while the Alpaca
    order-history fetch is failing — a 2026-07-15 timeout storm once alert-flagged the whole
    book as orphaned off an empty fetch. A burst of orphan alerts during API trouble is
    suspect; a *sustained* `orphan_sweep skipped` streak means order history has been
    unfetchable for a while and deserves a look.
*   **Pending orders:** clear via the Alpaca dashboard.
*   **Discord says a bot is down but `pm2 ls` disagrees:** read the *source line* on the
    alert — every commander message now ends with `Reported by <host> pid <n> — container`
    or `— HOST (not the fleet container!)`. If that host is not the fleet container, a
    second fleet is running and alerting about its own PM2 daemon. Confirm and kill it
    **on the host**:
    ```bash
    docker exec -w /app/code trading-fleet python3 fleet_doctor.py   # section 7b
    pm2 ls                       # a host-level PM2 daemon (NOT via docker exec)?
    pm2 kill && pm2 unstartup    # stop it and remove its systemd unit
    docker ps                    # a second fleet container?
    systemctl list-units | grep -i pm2
    ```
    A host-level PM2 with a saved process list (`~/.pm2/dump.pm2`) is resurrected by its
    systemd unit on reboot — so a long-deferred OS update that finally reboots the box can
    bring a retired pre-container fleet back from the dead, running the same live-mounted
    code against its own daemon.
*   **A bot keeps "crashing":** the alert now says which kind. `CRASHED` means PM2 gave
    up after repeated exits (an import-time failure — bad dependency, syntax error in a
    shared module, missing `config.py` key); the alert carries the tail of the PM2 error
    log. `NOT RUNNING` means the process is merely stopped while `bot_config` says active.
    Down-alerts are throttled to one per process per 15 min, and a bot commander itself
    paused (analyst VIX > 28) resumes silently. Full reason:
    `docker exec trading-fleet pm2 logs <name> --err --lines 40 --nostream`.
*   **VIX / regime dark:** `/status` shows the live VIX **and which source produced it**
    (`vix_source`, also a tag on the InfluxDB `market_regime` row). `stale_failsafe` means
    no source answered for 45 min and the fleet is on CRITICAL_VOLATILITY + VIX 25 with
    `data_stale=true` — entries are gated, which is the safe posture, but it is not a
    market reading. `fleet_doctor.py` tests each source separately and tells you whether
    it is one provider or container egress.

*   **A bot is running but its trades make no sense:** suspect the *data*, not the logic.
    A truncated bar request returns a perfectly well-formed frame of stale prices — it
    cannot be seen from `pm2 ls`, error counts, or Grafana process panels, and three of the
    fleet's four fetch sites were in that state for months. Check the bar ages first:
    ```bash
    docker exec -w /app/code trading-fleet python3 fleet_doctor.py   # section 5b
    docker exec trading-fleet pm2 logs <name> --lines 60 --nostream | grep STALE
    ```
    A `[STALE]` line names the symbol and the age; the bot stands down on that symbol
    rather than trading on it. In Grafana, `market_regime.spy_bar_age_hours` is the same
    signal for the regime feed — the analyst's heartbeat proves the *process* is alive,
    that field proves the *data* is.
*   **crypto_grid holds coins it won't sell:** expected immediately after the lot-ledger
    migration. The grid only sells lots it recorded buying, and the pre-migration inventory
    has none — the log says so once per symbol at startup. It cannot over-buy on top of them
    (`budget_ok` counts real positions), but it will not wind them down either. Either close
    those coins by hand on the Alpaca dashboard, or seed `crypto_grid_state.json` with the
    real basis:
    ```json
    {"BTC/USD": [], "ETH/USD": [{"qty": 0.5, "price": 2800.0, "opened_at": "2026-08-01T00:00:00+00:00"}], "SOL/USD": []}
    ```
    Only put a basis in that file if you know it. A guessed number defeats the profit guard
    the ledger exists to enforce.
*   **crypto_grid never buys:** expected. Entries are fail-closed — set
    `bots.crypto_grid.entries_enabled` to `true` in `bot_config.json` (host repo dir, then
    `docker exec trading-fleet pm2 restart crypto_grid`) once you have verified the lot ledger
    against a live account and decided what to do with the pre-existing coins. Sells and
    reconciliation run either way, so held inventory is never stranded by this lever.
*   **crypto_grid logs `[SUSPEND] New grid entries halted`:** the bot is managing what it can
    but opening nothing new, deliberately. Three causes, all in the log line: the lot ledger
    could not be read or written (`crypto_grid_state.json` — repair or remove it), an
    in-flight order could not be read from Alpaca, or a position read failed. It resumes by
    itself once the underlying read succeeds. Do NOT delete the ledger to clear it without
    reading it first: an empty ledger is a grid that will never sell what it holds.
*   **The CFO logs `FAIL-CLOSED — N pending order(s) cannot be priced`:** a bot has an unfilled
    market buy with no limit price, no dollar notional, no partial fill and no existing
    position, so its capital cannot be reserved. New entries for that bot are blocked until the
    order fills, prices or terminates — which for a crypto market order is usually seconds.
    A *persistent* one means an order is stuck open: check it on the Alpaca dashboard and
    cancel it if it will never fill.
*   **The advisor says `period_return_unavailable` / `unranked_bots`:** working as intended,
    not a fault. A window's return can only be measured when the bot started that window flat;
    with inventory carried in, the honest figure needs a mark at the window's open that is not
    stored anywhere. Those bots are dropped from the ranking rather than scored on a proxy.
    `realized_pl` and `lifetime_unrealized_pl` are still reported for them.
*   **The advisor recommends moving capital into crypto:** check
    `recommended_allocations.json` → `assumptions.negative_basis_positions` before acting.
    A non-empty list means the broker is reporting a negative cost basis on those symbols,
    so their unrealized P&L — which feeds the scores — is not trustworthy. Also compare
    `source_comparison`: a large Alpaca-vs-Influx delta on one bot means the two ledgers
    disagree about that strategy and neither should pick a winner. The advisor never
    writes `effective_budgets.json`; promotion is always a human step.

## Target File Contract
The Corsair scout must emit the v1.1 dictionary schema:
```json
{
  "version": "1.1",
  "status": "success",
  "updated": "2026-06-08T15:00:00+00:00",
  "survivor_targets": { "AAPL": {"confidence": 0.8} },
  "trend_targets": {},
  "short_targets": {},
  "wheel_targets": {}
}
```
Stale (>24h), wrong-version, or non-success payloads push bots into standby/fallback.
