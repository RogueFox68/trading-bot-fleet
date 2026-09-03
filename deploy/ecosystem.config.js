// =============================================================================
// PM2 Ecosystem Config - Trading Bot Fleet
// The canonical process list for the fleet container. Adding a bot to the
// fleet means: script + fleet_registry.py entry + bot_config allocation +
// an app block here (see CLAUDE.md "Adding a new bot").
// =============================================================================

const APP_DIR = process.env.APP_DIR || '/app/code';

// Memory backstop. The Beelink has 16GB shared with InfluxDB/Grafana/wg-easy,
// and a bot that ratchets its RSS has nothing else to catch it. PM2 restarts
// the process (and commander's watchdog reports the restart to Grafana) rather
// than letting the box swap. The accountant gets more headroom: it holds a 30d
// InfluxDB trade DataFrame plus a paged order window every cycle, so its
// working set is legitimately the largest in the fleet.
const MAX_MEM = '600M';
const MAX_MEM_ACCOUNTANT = '1G';

const infra = (name, opts = {}) => ({
  name,
  script: `${APP_DIR}/${name}.py`,
  interpreter: 'python3',
  cwd: APP_DIR,
  autorestart: true,
  max_restarts: 10,
  restart_delay: 5000,
  max_memory_restart: MAX_MEM,
  env: { PYTHONUNBUFFERED: '1' },
  ...opts,
});

const bot = (name, script) => ({
  name,
  script: `${APP_DIR}/${script || name + '.py'}`,
  interpreter: 'python3',
  cwd: APP_DIR,
  autorestart: true,
  max_restarts: 10,
  restart_delay: 10000,
  max_memory_restart: MAX_MEM,
  env: { PYTHONUNBUFFERED: '1' },
});

module.exports = {
  apps: [
    // ---- INFRASTRUCTURE (start first) ----
    infra('market_analyst'),
    infra('accountant', { max_memory_restart: MAX_MEM_ACCOUNTANT }),
    infra('commander'),
    // Ships logs/fleet_error_registry.jsonl into InfluxDB (bot_error_events)
    infra('error_watchdog'),

    // ---- TRADING BOTS ----
    bot('wheel_bot'),
    bot('trend_bot'),
    bot('survivor_bot'),
    bot('crypto_grid'),
    // Canonical name is moon_bot (matches order tags, breakout_trades
    // measurement, CFO keys, and bot_config). The old PM2 name moon_bag
    // disappears when the container is recreated.
    bot('moon_bot', 'crypto_breakout.py'),

    // condor_bot retired 2026-07: Alpaca multi-leg handling was never
    // reliable. Code removed from the repo; history in git if ever needed.
  ],
};
