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
