"""Moon Bot — Donchian channel breakout on BTC/ETH/SOL, 24/7.

Ported onto the fleet_bot runner (market_hours=False, no entry-time paging).
Strategy semantics unchanged: buy a 20-day-high breakout with 10% of equity,
trail out on a 10-day-low break.

moon_bot shares its symbols with crypto_grid, and Alpaca positions are
per-symbol, not per-bot — so a ledger file records what moon_bot itself
bought. The trailing stop only sells the ledger quantity (never the grid's
inventory) and entries key off the ledger, not the shared position.
"""
import json

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
import datetime

from fleet_bot import FleetBot

# --- STRATEGY SETTINGS ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
LOOKBACK_ENTRY = 20  # Buy if we break the 20-day high
LOOKBACK_EXIT = 10   # Sell if we break the 10-day low
RISK_PCT = 0.10      # Allocate 10% of equity per trade (Aggressive)

# Ledger of moon_bot's own holdings. Gitignored (*.json); missing file = flat.
STATE_FILE = "moon_bot_state.json"

bot = FleetBot("moon_bot", loop_seconds=3600, market_hours=False,
               needs_entry_times=False, discord_username="Moon Bot 🚀")
logger = bot.logger

# Crypto data needs its own client; the runner only carries stock data.
crypto_data_client = CryptoHistoricalDataClient()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return {k: float(v) for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.error(f"[!] State file unreadable ({e}); assuming flat.")
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"[!] Could not save state file: {e}")


def get_donchian_levels(symbol):
    try:
        # 1. Fetch History for Levels
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
        req = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=30
        )
        bars = crypto_data_client.get_crypto_bars(req)
        df = bars.df.loc[symbol]

        # Exclude current incomplete bar for levels
        completed_candles = df.iloc[:-1]

        entry_high = completed_candles['high'].tail(LOOKBACK_ENTRY).max()
        exit_low = completed_candles['low'].tail(LOOKBACK_EXIT).min()

        # 2. Fetch REAL-TIME Price for Execution
        trade_req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        trade = crypto_data_client.get_crypto_latest_trade(trade_req)
        current_price = float(trade[symbol].price)

        return entry_high, exit_low, current_price
    except Exception as e:
        logger.error(f"Data Error {symbol}: {e}")
        return None, None, None


def cycle(bot):
    buying_power = float(bot.account.buying_power)

    # Shared account-wide positions vs moon_bot's own ledger
    pos_qty = {p.symbol: float(p.qty) for p in bot.positions}
    state = load_state()

    logger.info(f"Scanning Markets... Equity: ${bot.equity:,.2f}")

    for symbol in SYMBOLS:
        try:
            entry_high, exit_low, current_price = get_donchian_levels(symbol)
            if current_price is None: continue

            total_held = pos_qty.get(symbol, 0)
            my_qty = state.get(symbol, 0.0)

            # Ledger says we hold coins the account no longer has
            # (manual sale / grid sweep): reconcile down to reality.
            if my_qty > total_held:
                logger.warning(f"    [{symbol}] Ledger {my_qty:.6f} > account {total_held:.6f}; reconciling down.")
                my_qty = max(0.0, total_held)
                state[symbol] = my_qty
                save_state(state)

            logger.info(f"  {symbol:<8} | Price: ${current_price:,.2f} | Breakout: ${entry_high:,.2f} | Stop: ${exit_low:,.2f} | Mine: {my_qty:.6f}")

            # --- ENTRY LOGIC (gate on OUR ledger, not the shared position) ---
            if my_qty <= 0:
                if current_price > entry_high:
                    logger.info(f"    [SIGNAL] BREAKOUT! Price ${current_price} > ${entry_high}")

                    if not bot.budget_ok:
                        logger.warning(f"    [SKIP] Breakout buy blocked — CFO Budget limit reached.")
                        continue

                    # Calculate Size
                    target_val = bot.equity * RISK_PCT
                    qty_to_buy = round(target_val / current_price, 4)

                    if (qty_to_buy * current_price) > buying_power:
                        logger.info("    [!] Insufficient Buying Power")
                        continue

                    order = bot.submit(
                        bot.market_order(symbol, qty_to_buy, OrderSide.BUY, TimeInForce.GTC),
                        action="buy_breakout",
                        notify=f"🚀 **MOONSHOT ENTRY: {symbol}**\nBreakout Price: ${current_price}\nTargeting trends."
                    )
                    if order is not None:
                        filled = float(getattr(order, 'filled_qty', 0) or 0)
                        state[symbol] = filled if filled > 0 else qty_to_buy
                        save_state(state)

            # --- EXIT LOGIC (sell only OUR coins, never the grid's) ---
            elif my_qty > 0:
                if current_price < exit_low:
                    sell_qty = round(min(my_qty, total_held), 6)
                    if sell_qty <= 0:
                        continue
                    logger.info(f"    [SIGNAL] TRAILING STOP! Price ${current_price} < ${exit_low} (selling {sell_qty})")

                    order = bot.submit(
                        bot.market_order(symbol, sell_qty, OrderSide.SELL, TimeInForce.GTC),
                        action="sell_breakout",
                        notify=f"🛑 **STOP LOSS: {symbol}**\nPrice: ${current_price}\nTrend broken."
                    )
                    if order is not None:
                        state[symbol] = 0.0
                        save_state(state)
                else:
                    logger.info(f"    [HOLD] Riding the trend.")

        except Exception as e:
            logger.error(f"    [!] Error {symbol}: {e}")


if __name__ == "__main__":
    bot.notify("🚀 **Moon Bot Online**\nRunner: fleet_bot | Strategy: Donchian Breakout (20/10)")
    bot.run(cycle)
