"""Crypto Grid Bot — BTC/ETH/SOL zone-grid scalping, 24/7.

Ported onto the fleet_bot runner (market_hours=False, no entry-time paging —
crypto ownership resolves statically). Strategy semantics unchanged from V2:

  a +/-15% 8-level grid recenters after 4 consecutive out-of-band cycles;
  a zone drop buys a $50 slice (paused in bear regimes / CAPITAL_CRUNCH /
  over budget), a zone rise sells the slice (or sweeps remaining dust).

Budget: the CFO number now comes from utils.get_budget_dollars (effective
budget -> cfo base allocation -> fail-closed 0.0) instead of a private
effective_budgets.json read with a magic $5k fallback. Fail-closed means
buys pause; sells always continue.
"""
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest

import utils
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
GRID_WIDTH_PCT = 0.15    # Grid covers +/- 15% of current price
GRID_LEVELS = 8          # More levels = Finer scalping
BUDGET_PER_GRID = 50     # $50 per slice
RECALIBRATE_DELAY = 4    # Cycles out of zone before resetting (Prevent jitter)

bot = FleetBot("crypto_grid", loop_seconds=30, market_hours=False,
               needs_entry_times=False)
logger = bot.logger

# Crypto data needs its own client; the runner only carries stock data.
crypto_data_client = CryptoHistoricalDataClient()

# --- STATE (per symbol, recalibrated at runtime) ---
grids = {sym: {"top": 0, "bottom": 0, "size": 0,
               "prev_zone": GRID_LEVELS // 2, "oob": 0} for sym in SYMBOLS}


def get_crypto_price(symbol):
    try:
        req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        res = crypto_data_client.get_crypto_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        logger.error(f"  [!] Price Error {symbol}: {e}")
        return None


def recalibrate_grid(symbol, current_price):
    """Centers the grid around the NEW price."""
    grid_top = current_price * (1 + GRID_WIDTH_PCT)
    grid_bottom = current_price * (1 - GRID_WIDTH_PCT)
    zone_size = (grid_top - grid_bottom) / GRID_LEVELS

    grids[symbol]["top"] = grid_top
    grids[symbol]["bottom"] = grid_bottom
    grids[symbol]["size"] = zone_size
    grids[symbol]["prev_zone"] = GRID_LEVELS // 2
    grids[symbol]["oob"] = 0

    logger.info(f"    [RECALIBRATE] {symbol} Center: {current_price:.0f} | Range: {grid_bottom:.0f}-{grid_top:.0f}")
    bot.notify(f"♻️ **Grid Recalibrated ({symbol})**\n"
               f"Center: ${current_price:,.0f}\n"
               f"Range: ${grid_bottom:,.0f} - ${grid_top:,.0f}")


def cancel_open_orders_for_symbol(symbol, opposite_side_only=None):
    """Cancels any open orders for a specific symbol, optionally filtering by side."""
    try:
        req_filter = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        open_orders = bot.trading_client.get_orders(filter=req_filter)
        for o in open_orders:
            if opposite_side_only is None or o.side == opposite_side_only:
                logger.info(f"    [CANCEL] Canceling open order {o.id} on {symbol} (Side: {o.side}) before placing new order.")
                bot.trading_client.cancel_order_by_id(o.id)
    except Exception as e:
        logger.error(f"Error canceling open orders for {symbol}: {e}")


def held_qty(symbol):
    """Current position qty for a crypto symbol (0.0 if flat). Handles the
    BTCUSD vs BTC/USD symbol-format mismatch."""
    try:
        return float(bot.trading_client.get_open_position(symbol).qty)
    except Exception:
        try:
            alt = symbol.replace("/", "")
            return float(bot.trading_client.get_open_position(alt).qty)
        except Exception as e2:
            registry.log_error("crypto_grid", "check_inventory", e2, context=symbol)
            return 0.0


def grid_buy(symbol, price, current_zone):
    """Zone drop -> accumulate a slice, unless regime/crunch/budget says no."""
    if "BEAR" in bot.regime:
        logger.info(f"    [SKIP] Bear Trend Detected. Buying Paused in Zone {current_zone} for {symbol}.")
        return
    if bot.capital_crunch:
        logger.warning(f"    [SKIP] CAPITAL_CRUNCH active. Buy paused for {symbol}.")
        return

    # Per-symbol cap at the bot's whole CFO budget (0.0 = fail-closed: no buys)
    my_budget = utils.get_budget_dollars("crypto_grid", bot.trading_client, equity=bot.equity)
    try:
        current_val = float(bot.trading_client.get_open_position(symbol).market_value)
    except Exception:
        current_val = 0.0

    if current_val >= my_budget:
        logger.warning(f"    [BUDGET STOP] {symbol} Current value ${current_val:.2f} >= Budget ${my_budget:.2f}. Skipping buy.")
        return  # still allowed to SELL if price rises

    logger.info(f"    [BUY] {symbol} Dropped to Zone {current_zone}")

    buying_power = float(bot.account.buying_power)
    if not bot.budget_ok:
        logger.warning(f"    [SKIP] {symbol} Grid buy ${BUDGET_PER_GRID} > Available (budget limit)")
    elif buying_power > BUDGET_PER_GRID:
        cancel_open_orders_for_symbol(symbol, opposite_side_only=OrderSide.SELL)
        qty = BUDGET_PER_GRID / price
        bot.submit(
            bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.GTC),
            action="grid_buy",
            notify=f"🟢 **GRID BUY {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}"
        )
    else:
        logger.warning(f"    [SKIP] {symbol} Low Balance: ${buying_power:.2f} (Need ${BUDGET_PER_GRID})")


def grid_sell(symbol, price, current_zone):
    """Zone rise -> take profit on a slice, or sweep remaining dust."""
    logger.info(f"    [SELL] {symbol} Rose to Zone {current_zone}")

    qty_to_sell = BUDGET_PER_GRID / price
    current_qty_held = held_qty(symbol)

    if current_qty_held >= qty_to_sell:
        cancel_open_orders_for_symbol(symbol, opposite_side_only=OrderSide.BUY)
        bot.submit(
            bot.market_order(symbol, qty_to_sell, OrderSide.SELL, TimeInForce.GTC),
            action="grid_sell",
            notify=f"🔴 **GRID SELL {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}"
        )
    elif current_qty_held > (qty_to_sell * 0.1):
        # Partial Sell (Sweep Dust)
        logger.info(f"    [SWEEP] {symbol} Selling remaining {current_qty_held:.6f} (Target: {qty_to_sell:.6f})")
        cancel_open_orders_for_symbol(symbol, opposite_side_only=OrderSide.BUY)
        bot.submit(
            bot.market_order(symbol, current_qty_held, OrderSide.SELL, TimeInForce.GTC),
            action="grid_sweep",
            notify=f"🧹 **GRID SWEEP {symbol}**\nSold remaining {current_qty_held:.4f}\nPrice: ${price:,.2f}"
        )
    else:
        logger.info(f"    [SKIP] {symbol} Sell Signal but Zero Inventory (Ghost Signal handled).")


def cycle(bot):
    for symbol in SYMBOLS:
        price = get_crypto_price(symbol)
        if price is None:
            continue

        grid = grids[symbol]

        # First sight of a price (or fresh start): center the grid here.
        if grid["size"] == 0:
            recalibrate_grid(symbol, price)
            continue

        # Determine Zone
        if price < grid["bottom"]:
            current_zone = -1
            grid["oob"] += 1
        elif price > grid["top"]:
            current_zone = GRID_LEVELS + 1
            grid["oob"] += 1
        else:
            current_zone = int((price - grid["bottom"]) / grid["size"]) if grid["size"] > 0 else GRID_LEVELS // 2
            grid["oob"] = 0  # back in range

        # Lost out-of-band too long -> move the base
        if grid["oob"] >= RECALIBRATE_DELAY:
            recalibrate_grid(symbol, price)
            continue

        # Trade on zone change
        previous_zone = grid["prev_zone"]
        if current_zone != previous_zone and 0 <= current_zone <= GRID_LEVELS:
            logger.info(f"[{symbol}] Zone Change: {previous_zone} -> {current_zone} | Price: ${price:.0f}")
            if current_zone < previous_zone:
                grid_buy(symbol, price, current_zone)
            elif current_zone > previous_zone:
                grid_sell(symbol, price, current_zone)

        grid["prev_zone"] = current_zone


if __name__ == "__main__":
    logger.info("--- 🕸️ CRYPTO GRID BOT V3 (fleet_bot runner) ---")
    bot.run(cycle)
