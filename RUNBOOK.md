# Trading Fleet Runbook

## Overview
This runbook covers the Beelink/PM2/Alpaca automated trading fleet ("The Executor"). The brain (Corsair/LM Studio/Gemma 4/Task Scheduler) pushes `active_targets.json` to this fleet to execute trades.

## Key Boundaries
*   **Brain (Corsair):** Runs Sector Scout & Llama 3 via Task Scheduler. Generates `active_targets.json` and syncs it here.
*   **Executor (Beelink):** Runs fleet bots via PM2. Consumes `active_targets.json`, enforces safety gates, and executes on Alpaca.
*   **Generated Files:** Files like `active_targets.json`, `logs/`, `bot_config.json`, and `effective_budgets.json` are strictly runtime components and are ignored by git.

## Deployment & Startup
1.  **Clone / Pull Repo:** `git pull origin main`
2.  **Setup Environment:** Ensure `.env` is configured with `API_KEY` and `SECRET_KEY`.
3.  **Setup Config:** Copy `config.example.py` to `config.py` and populate the discord webhooks.
4.  **Setup Targets:** Create/Sync `active_targets.json` (or use `active_targets.example.json` to test).
5.  **Start Fleet:** Use PM2 to start bots:
    ```bash
    pm2 start commander.py --interpreter python3
    pm2 start survivor_bot.py --interpreter python3
    pm2 start wheel_bot.py --interpreter python3
    pm2 start trend_bot.py --interpreter python3
    # Condor bot is currently paused
    # pm2 start condor_bot.py --interpreter python3
    ```

## Safety Gates
Safety is centralized in `utils.py`.
*   **Fail-Closed Budget Checks:** Bots will pause if `bot_config.json` is missing or has no budget allocation.
*   **Target Freshness:** `active_targets.json` older than 24 hours will be rejected, forcing bots into a static-fallback/standby mode.
*   **Daily Loss Cap:** Trading stops for the day if PnL drops below the `MAX_DAILY_LOSS` (e.g. -$500).
*   **Exposure Caps:** Max order notional ($2000) and max symbol exposure ($500) limit excessive concentration.

## Incident Response
*   **Log Check:** `pm2 logs` or check `logs/fleet_error_registry.jsonl`.
*   **Fleet Stop:** `pm2 stop all`
*   **Clear Pending Orders:** Clear manually through the Alpaca Dashboard.
*   **Error Codes:** Check `error_registry.py` for Discord alert code meanings (e.g., `CFO-001`, `SAFE-001`).

## Target File Contract
Ensure your Brain/Corsair syncing script outputs `active_targets.json` using the v1.1 Dictionary schema format:
```json
{
  "version": "1.1",
  "status": "success",
  "updated": "2026-06-08T15:00:00+00:00",
  "survivor_targets": {
    "AAPL": {"confidence": 0.8},
    "MSFT": {"confidence": 0.6}
  },
  "trend_targets": {},
  "short_targets": {},
  "wheel_targets": {}
}
```
