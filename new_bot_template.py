"""Template for a new fleet bot. Copy, rename, and fill in the strategy.

Onboarding checklist (the full story is in CLAUDE.md "Adding a new bot"):

  1. Copy this file to <your_bot>.py and write your strategy in cycle().
  2. Register the bot in fleet_registry.py BOTS (name, measurement,
     webhook key, gating rule...). FleetBot refuses to start unregistered
     bots, so this template will raise ValueError until you do.
  3. Add the bot to bot_config.json "bots" with a status and allocation
     (and to cfo_settings if it should join CFO reallocation).
  4. Add WEBHOOK_<YOURBOT> to config.py (optional - unset skips Discord).
  5. Add an app block to deploy/ecosystem.config.js and rebuild/restart
     the trading-fleet container.

The runner gives you per cycle (see fleet_bot.py for the full API):
  bot.pos_dict / bot.positions      current account positions
  bot.my_symbols()                  equities THIS bot owns (order-tag based)
  bot.targets("trend_targets")      validated scout targets w/ confidence
  bot.regime / bot.vix              market weather from the analyst
  bot.budget_ok                     CFO budget gate (checked once per cycle)
  bot.is_eod_skip_entry / ..._eval / ..._close   EOD windows (ET)
  bot.size_position(...)            confidence-scaled, budget-capped sizing
  bot.market_order(...) + bot.submit(...)        tagged, safety-gated orders
  bot.hours_held(symbol)            hold duration from order history
  bot.in_cooldown(symbol)           skip symbols that recently errored
"""
from alpaca.trading.enums import OrderSide, TimeInForce

from fleet_bot import FleetBot

# --- STRATEGY SETTINGS ---
RISK_PER_TRADE = 0.02
MAX_POSITION_PCT = 0.10
TAKE_PROFIT = 0.08
STOP_LOSS = -0.05

# The registry key is the bot's identity everywhere: PM2 name, order-tag
# prefix, budget key, InfluxDB measurement mapping.
bot = FleetBot("example_bot", loop_seconds=60, market_hours=True)
logger = bot.logger


def cycle(bot):
    # 1. Load your strategy bucket from the scout (or build your own list)
    targets = bot.targets("trend_targets")
    target_map = {sym: d.get("confidence", 0.5) for sym, d in targets.items()}

    scan_list = list(set(list(target_map) + bot.my_symbols()))
    logger.info(f"Scanning {len(scan_list)} symbols | Regime: {bot.regime}")

    for symbol in scan_list:
        if bot.in_cooldown(symbol) or "/" in symbol:
            continue

        # 2. Fetch data + compute your signal here
        # df = ...; signal = ...
        price = 0.0  # TODO: real price from your data fetch

        # 3. EXIT: manage positions you own
        if symbol in bot.pos_dict:
            pos = bot.pos_dict[symbol]
            entry = float(pos.avg_entry_price)
            pnl = (price - entry) / entry if entry else 0.0
            if pnl >= TAKE_PROFIT or pnl <= STOP_LOSS:
                reason = "Take Profit" if pnl >= TAKE_PROFIT else "Stop Loss"
                bot.submit(
                    bot.market_order(symbol, int(abs(float(pos.qty))), OrderSide.SELL, TimeInForce.GTC),
                    reason=reason,
                    notify=f"**{reason} {symbol}** PnL: {pnl:.2%}"
                )
            continue

        # 4. ENTRY: gates first, then size, then submit
        if symbol in bot.pending_symbols:   # never stack orders on a symbol
            continue
        if bot.is_eod_skip_entry or not bot.budget_ok or bot.capital_crunch:
            continue
        # if not signal: continue          # TODO: your entry condition

        qty, cost = bot.size_position(price, target_map.get(symbol, 0.5),
                                      RISK_PER_TRADE, max_position_pct=MAX_POSITION_PCT)
        if qty > 0:
            bot.submit(
                bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.DAY),
                reason="Entry",
                notify=f"**LONG {symbol}** ({qty} shares, ${cost:.0f})"
            )


if __name__ == "__main__":
    bot.run(cycle)
