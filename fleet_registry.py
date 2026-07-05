"""Single source of truth for the trading bots in the fleet.

Adding a bot:
  1. Write the bot script (start from new_bot_template.py).
  2. Add one entry to BOTS below.
  3. Add the bot to bot_config.json "bots" (status/allocation) and, if it
     should participate in CFO reallocation, to cfo_settings.
  4. Add a WEBHOOK_<NAME> to config.py (optional - unset skips Discord).
  5. Add an app block to deploy/ecosystem.config.js.

Everything else — ownership tags, InfluxDB measurements, accountant queries
and P&L reporting, analyst pause/resume, fill reconciliation — derives from
this file. Do not re-introduce per-bot lists elsewhere.

Entry fields:
  script          file the bot runs as (PM2 name == registry key)
  measurement     InfluxDB measurement for its trade rows
  webhook         config.py attribute holding its Discord webhook
  static_symbols  baseline ownership claims (crypto only; equities resolve
                  dynamically from client_order_id tags)
  reconciled      accountant rewrites its fills from Alpaca's order feed
                  (False for crypto bots: their action vocabulary like
                  grid_buy/grid_sweep can't be rebuilt from an order)
  manual_state    market_analyst never overwrites its bot_config status
  gated_when      regime/VIX rule blocking NEW entries (None = never gated)
  options         measurement logs per-share premium (P&L scales by 100)
"""

BOTS = {
    "trend_bot": dict(
        script="trend_bot.py",
        measurement="trades",
        webhook="WEBHOOK_TREND",
        static_symbols=[],
        reconciled=True,
        manual_state=False,
        gated_when=None,
    ),
    "survivor_bot": dict(
        script="survivor_bot.py",
        measurement="survivor_trades",
        webhook="WEBHOOK_SURVIVOR",
        static_symbols=[],
        reconciled=True,
        manual_state=False,
        gated_when=None,
    ),
    "wheel_bot": dict(
        script="wheel_bot.py",
        measurement="wheel_trades",
        webhook="WEBHOOK_WHEEL",
        static_symbols=[],
        reconciled=True,
        manual_state=False,
        gated_when=dict(regimes=("BEAR_TREND", "CRITICAL_VOLATILITY"), vix_above=22),
        options=True,
    ),
    "crypto_grid": dict(
        script="crypto_grid.py",
        measurement="crypto_trades",
        webhook="WEBHOOK_CRYPTO",
        static_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],
        reconciled=False,
        manual_state=False,
        gated_when=dict(regimes=("BEAR_TREND", "CRITICAL_VOLATILITY")),
    ),
    "moon_bot": dict(
        script="crypto_breakout.py",
        measurement="breakout_trades",
        webhook="WEBHOOK_MOONBAG",
        static_symbols=["BTC/USD", "ETH/USD", "SOL/USD"],
        reconciled=False,
        manual_state=True,
        gated_when=None,
    ),
}

# Retired bots: their tags still exist on historical Alpaca orders and their
# measurements still hold rows in InfluxDB. Kept for attribution only -
# never queried, reported, or launched.
RETIRED_BOT_MEASUREMENTS = {
    "condor_bot": "condor_trades",
}

# Options measurements log per-share premium; realized P&L scales by 100.
OPTION_MEASUREMENTS = {cfg["measurement"] for cfg in BOTS.values() if cfg.get("options")}


def is_gated(bot_name, regime, vix):
    """True if the bot is prohibited from NEW entries in this regime/VIX."""
    rule = BOTS.get(bot_name, {}).get("gated_when")
    if not rule:
        return False
    if regime in rule.get("regimes", ()):
        return True
    vix_above = rule.get("vix_above")
    return vix_above is not None and vix > vix_above
