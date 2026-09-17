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
  config_keys     keys this bot reads from bot_config.json bots{<name>},
                  as {key: (default, severity, why)} - see missing_config_keys
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
        config_keys={
            "force_close_symbols": ([], "warn",
                                    "the per-ticker force-close lever is unavailable"),
            "force_roll_symbols": ([], "warn",
                                   "the per-ticker force-roll lever is unavailable"),
        },
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

# --- bot_config.json CONTRACT --------------------------------------------
#
# Every read of bot_config.json in this fleet is a `.get(key, default)`, so a
# MISSING key is silent and the default applies. That is fine when the default
# is what you would have chosen, and dangerous when it is not:
#
#   global_settings.vix absent        -> 15.0, BELOW every gate. The VIX
#                                        kill-switch reads a calm market.
#   cfo_settings.unallocated_reserve  -> 0.0, so budgets compute on FULL
#     absent                             equity instead of equity minus reserve.
#
# bot_config.json is gitignored and lives on the host, so no config change ever
# arrives by deploy: anything added to bot_config.template.json is a manual step
# on the Beelink, every time. Nothing checked that the live file and the code
# agreed until `missing_config_keys` and fleet_doctor's section 8 check.
#
# Declared here rather than in fleet_doctor because the registry already owns
# the bot_config contract (see "Adding a new bot" in CLAUDE.md, steps 2-3) —
# so a new bot's config keys are checked the moment it is registered, and no
# per-bot list appears anywhere else.
#
# (key, default, severity, why). severity "critical" means the silent default
# is unsafe; "warn" means it is merely not what you intended.
GLOBAL_SETTINGS_KEYS = (
    ("vix", 15.0, "critical",
     "BELOW every gate - the VIX kill-switch would read a calm market"),
    ("market_condition", "SIDEWAYS", "critical",
     "a tradeable regime - this un-gates wheel_bot and crypto_grid"),
    ("CAPITAL_CRUNCH", False, "warn", "the capital-crunch brake reads as released"),
    ("emergency_stop", False, "warn", "a /panic has nowhere to persist"),
    ("macro_climate", "UNKNOWN", "warn", "the advisor's regime bucket degrades"),
    ("sector_rotation", "UNKNOWN", "warn", "the advisor's regime bucket degrades"),
)

CFO_SETTINGS_KEYS = (
    ("unallocated_reserve", 0.0, "critical",
     "NO reserve - every budget computes on full equity"),
    ("base_allocations", None, "critical",
     "budgets fall back to bots.<name>.allocation, or fail closed at 0.0"),
    ("minimum_reserves", None, "warn",
     "the reallocator can drain a bot to zero"),
    ("reallocation_enabled", None, "warn", "reallocation is silently off"),
    ("reallocation_cap_per_cycle", None, "warn", "no per-cycle shift cap"),
    ("gate_idle_threshold_cycles", None, "warn",
     "a gated bot's surplus is released immediately"),
)

# Keys every registered bot needs in its bots{} entry.
REQUIRED_BOT_KEYS = (
    ("allocation", 0.0, "warn",
     "only consulted for bots outside cfo_settings, but then it fails closed"),
    ("status", None, "warn",
     "commander /stop and the analyst's VIX pause have nowhere to persist"),
)


def missing_config_keys(config_data):
    """Keys the CODE reads that `config_data` does not define.

    Returns [(path, default, severity, why)], most severe first. This is NOT a
    diff against bot_config.template.json: the template is a bootstrap, and a
    live config legitimately accumulates runtime state the template never had
    (vix, vix_source, data_stale, regime_updated, CAPITAL_CRUNCH). Drift in
    that direction is expected. This checks the other direction only.
    """
    config_data = config_data or {}
    gs = config_data.get("global_settings") or {}
    cfo = config_data.get("cfo_settings") or {}
    bots = config_data.get("bots") or {}
    found = []

    for key, default, severity, why in GLOBAL_SETTINGS_KEYS:
        if key not in gs:
            found.append((f"global_settings.{key}", default, severity, why))
    for key, default, severity, why in CFO_SETTINGS_KEYS:
        if key not in cfo:
            found.append((f"cfo_settings.{key}", default, severity, why))

    for name, cfg in BOTS.items():
        entry = bots.get(name)
        if entry is None:
            found.append((f"bots.{name}", None, "critical",
                          "registered in fleet_registry but absent from bot_config"))
            continue
        for key, default, severity, why in REQUIRED_BOT_KEYS:
            if key not in entry:
                found.append((f"bots.{name}.{key}", default, severity, why))
        for key, (default, severity, why) in (cfg.get("config_keys") or {}).items():
            if key not in entry:
                found.append((f"bots.{name}.{key}", default, severity, why))

    return sorted(found, key=lambda f: 0 if f[2] == "critical" else 1)


def unregistered_config_bots(config_data):
    """bots{} entries with no registry entry. Harmless, but dead weight."""
    bots = (config_data or {}).get("bots") or {}
    # `accountant` is a real bots{} entry for an infra process, not a strategy.
    return sorted(set(bots) - set(BOTS) - {"accountant"})


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
