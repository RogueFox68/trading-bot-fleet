"""Crypto Grid Bot — BTC/ETH/SOL zone-grid scalping, 24/7.

Runs on the fleet_bot runner (market_hours=False, no entry-time paging —
crypto ownership resolves statically).

  a +/-15% 8-level grid recenters after 4 consecutive out-of-band cycles;
  a zone drop buys a slice, a zone rise closes the oldest OPEN LOT that
  clears its own cost basis plus round-trip costs plus a minimum spread.

--- Why the sell side is entry-linked (2026-09 redesign) -------------------

The original bot bought on any downward zone crossing and sold on any upward
one, each at the price prevailing when the crossing was noticed. The grid's
"spacing" therefore constrained the ZONE INDEX, not the transaction prices:
price ticking either side of one boundary produces a buy and a sell within
cents of each other, and the pair books a guaranteed loss of the round-trip
fee. Nothing in the old code compared a sell price to the price it was
closing against, so there was no level at which that was noticed — the bot
could churn a boundary indefinitely while `grid_buy`/`grid_sell` counts rose
and the dashboard showed activity. 365 filled orders since July 13 sit
behind that logic.

So sells are now lot-linked. Every buy records a lot (qty + fill price); a
zone rise closes the oldest lot whose basis clears REQUIRED_SPREAD_PCT, and
closes nothing if none does. The profit per closed lot is bounded below by
construction rather than by hope. FIFO is deliberate: it matches the FIFO
the accountant and strategy_advisor use, so the bot's own idea of what it
closed agrees with the books.

--- Why the ledger is separate from the position --------------------------

crypto_grid and moon_bot hold the same three coins in the same Alpaca
positions, and Alpaca positions are per-symbol, not per-bot. Reading the
account position as "the grid's inventory" let the grid sell moon_bot's
coins (an account-level FIFO reconstruction matched moon-origin ETH lots
against grid sells 113 times). The ledger here is the grid's own inventory,
mirroring `moon_bot_state.json`; the account position is only ever used as
an upper bound when reconciling down.

Budget: the CFO number comes from utils.get_budget_dollars, split PER SYMBOL.
The old per-symbol check compared one symbol's value against the bot's WHOLE
budget, so three symbols could each spend all of it — a 3x overrun that read
as compliant on every individual check. Fail-closed 0.0 means buys pause;
sells always continue.
"""
import datetime
import json

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

# Round-trip taker cost assumption for Alpaca crypto, both sides combined.
# Deliberately an over-estimate: under-stating it is what makes a churn pair
# look profitable on paper while losing money in fact.
ROUND_TRIP_COST_PCT = 0.005   # 0.25% per side
# Net profit required on top of costs before a lot may be closed.
MIN_NET_PROFIT_PCT = 0.005
# A sell must clear the lot's basis by at least this much. One zone is
# (2 * 15%) / 8 = 3.75%, so an honest zone-to-zone round trip clears this
# comfortably; only the boundary-hugging pairs are excluded.
REQUIRED_SPREAD_PCT = ROUND_TRIP_COST_PCT + MIN_NET_PROFIT_PCT

# The grid's OWN inventory, separate from the shared Alpaca position.
# Gitignored (*.json); missing file = flat.
STATE_FILE = "crypto_grid_state.json"
DUST_QTY = 1e-8          # below this a lot is noise, not inventory

bot = FleetBot("crypto_grid", loop_seconds=30, market_hours=False,
               needs_entry_times=False)
logger = bot.logger

# Crypto data needs its own client; the runner only carries stock data.
crypto_data_client = CryptoHistoricalDataClient()

# --- STATE (per symbol, recalibrated at runtime) ---
grids = {sym: {"top": 0, "bottom": 0, "size": 0,
               "prev_zone": GRID_LEVELS // 2, "oob": 0} for sym in SYMBOLS}


# --- LOT LEDGER -----------------------------------------------------------
def load_lots():
    """{symbol: [ {qty, price, opened_at}, ... ]}, oldest first."""
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {sym: [] for sym in SYMBOLS}
    except Exception as e:
        # NOT a silent reset: an unreadable ledger means the grid has lost
        # track of what it owns, and treating that as "flat" would let it
        # re-buy on top of inventory it still holds.
        registry.log_error("crypto_grid", "load_lots", e, context=STATE_FILE)
        logger.error(f"[!] Lot ledger unreadable ({e}); treating all symbols as flat "
                     f"— inventory may be double-counted until it is repaired.")
        return {sym: [] for sym in SYMBOLS}

    lots = {}
    for sym in SYMBOLS:
        entries = raw.get(sym) or []
        clean = []
        for e in entries:
            try:
                qty, price = float(e["qty"]), float(e["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if qty > DUST_QTY and price > 0:
                clean.append({"qty": qty, "price": price,
                              "opened_at": e.get("opened_at", "")})
        lots[sym] = clean
    return lots


def save_lots(lots):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({sym: lots.get(sym, []) for sym in SYMBOLS}, f, indent=2)
    except Exception as e:
        registry.log_error("crypto_grid", "save_lots", e, context=STATE_FILE)
        logger.error(f"[!] Could not save lot ledger: {e}")


def ledger_qty(lots, symbol):
    return sum(lot["qty"] for lot in lots.get(symbol, []))


def ledger_value(lots, symbol, price):
    """Mark-to-market value of the grid's OWN inventory in this symbol."""
    return ledger_qty(lots, symbol) * price


def reconcile_lots(lots, symbol, account_qty):
    """Trim the ledger when the account holds less than it claims.

    Happens when a human sells manually, or when a shared coin leaves by
    another route. Oldest lots go first, matching the FIFO close order.
    Returns True if anything changed.
    """
    have = ledger_qty(lots, symbol)
    excess = have - account_qty
    if excess <= DUST_QTY:
        return False

    logger.warning(f"    [{symbol}] Ledger {have:.8f} > account {account_qty:.8f}; "
                   f"retiring {excess:.8f} from the oldest lots.")
    book = lots.setdefault(symbol, [])
    while excess > DUST_QTY and book:
        lot = book[0]
        take = min(lot["qty"], excess)
        lot["qty"] -= take
        excess -= take
        if lot["qty"] <= DUST_QTY:
            book.pop(0)
    return True


def sellable_lot(lots, symbol, price):
    """The oldest lot whose basis clears REQUIRED_SPREAD_PCT at `price`.

    Returns (index, lot) or (None, None). This is the guard that makes a
    grid sell an actual round trip rather than a boundary tick.
    """
    for idx, lot in enumerate(lots.get(symbol, [])):
        if price >= lot["price"] * (1.0 + REQUIRED_SPREAD_PCT):
            return idx, lot
    return None, None


# --- MARKET DATA ----------------------------------------------------------
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


def account_qty(symbol):
    """Account-wide position qty for a crypto symbol (0.0 if flat).

    This is the SHARED position — grid and moon_bot both sit in it. Use it
    only as a ceiling on what can be sold, never as the grid's inventory.
    Handles the BTCUSD vs BTC/USD symbol-format mismatch.
    """
    try:
        return float(bot.trading_client.get_open_position(symbol).qty)
    except Exception:
        try:
            alt = symbol.replace("/", "")
            return float(bot.trading_client.get_open_position(alt).qty)
        except Exception as e2:
            registry.log_error("crypto_grid", "check_inventory", e2, context=symbol)
            return 0.0


def per_symbol_budget():
    """The bot's CFO budget, divided evenly across the symbols it trades.

    The old check compared ONE symbol's value against the WHOLE budget, so
    each of three symbols could independently spend all of it. Each check
    passed; the bot ran at 3x its allocation.
    """
    whole = utils.get_budget_dollars("crypto_grid", bot.trading_client, equity=bot.equity)
    return whole / len(SYMBOLS) if SYMBOLS else 0.0


# --- TRADING --------------------------------------------------------------
def grid_buy(symbol, price, current_zone, lots):
    """Zone drop -> accumulate a slice, unless regime/crunch/budget says no."""
    if "BEAR" in bot.regime:
        logger.info(f"    [SKIP] Bear Trend Detected. Buying Paused in Zone {current_zone} for {symbol}.")
        return
    if bot.capital_crunch:
        logger.warning(f"    [SKIP] CAPITAL_CRUNCH active. Buy paused for {symbol}.")
        return

    my_budget = per_symbol_budget()
    # The grid's OWN inventory, not the shared position: moon_bot's coins
    # sitting in the same symbol are not the grid's capital at risk.
    current_val = ledger_value(lots, symbol, price)

    if current_val >= my_budget:
        logger.warning(f"    [BUDGET STOP] {symbol} grid inventory ${current_val:.2f} >= "
                       f"per-symbol budget ${my_budget:.2f}. Skipping buy.")
        return  # still allowed to SELL if price rises

    logger.info(f"    [BUY] {symbol} Dropped to Zone {current_zone}")

    buying_power = float(bot.account.buying_power)
    # Never let one slice take the symbol past its share of the budget.
    slice_dollars = min(BUDGET_PER_GRID, my_budget - current_val)
    if not bot.budget_ok:
        logger.warning(f"    [SKIP] {symbol} Grid buy ${BUDGET_PER_GRID} > Available (budget limit)")
    elif slice_dollars <= 0:
        logger.warning(f"    [SKIP] {symbol} no per-symbol budget headroom left.")
    elif buying_power > slice_dollars:
        cancel_open_orders_for_symbol(symbol, opposite_side_only=OrderSide.SELL)
        qty = slice_dollars / price
        order = bot.submit(
            bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.GTC),
            action="grid_buy",
            notify=f"🟢 **GRID BUY {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}"
        )
        if order is not None:
            # Record the lot at the BROKER's fill price when it has one; the
            # basis is what every later sell is tested against, so a submit-time
            # estimate is only a fallback for a not-yet-filled order.
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            filled_price = float(getattr(order, "filled_avg_price", 0) or 0)
            lot_qty = filled_qty if filled_qty > 0 else qty
            lot_price = filled_price if filled_price > 0 else price
            lots.setdefault(symbol, []).append({
                "qty": lot_qty,
                "price": lot_price,
                "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            })
            save_lots(lots)
            logger.info(f"    [LOT+] {symbol} {lot_qty:.8f} @ ${lot_price:,.2f} "
                        f"(sells above ${lot_price * (1 + REQUIRED_SPREAD_PCT):,.2f})")
    else:
        logger.warning(f"    [SKIP] {symbol} Low Balance: ${buying_power:.2f} (Need ${slice_dollars:.2f})")


def grid_sell(symbol, price, current_zone, lots, held):
    """Zone rise -> close the oldest lot that clears its own cost basis.

    A zone rise is NOT by itself a reason to sell. The old bot sold on any
    upward crossing at whatever price it noticed, so a buy just under a
    boundary and a sell just over it round-tripped for a guaranteed fee loss.
    """
    idx, lot = sellable_lot(lots, symbol, price)
    if lot is None:
        open_lots = lots.get(symbol, [])
        if not open_lots:
            logger.info(f"    [SKIP] {symbol} Sell Signal but no open grid lots.")
        else:
            cheapest = min(l["price"] for l in open_lots)
            need = cheapest * (1.0 + REQUIRED_SPREAD_PCT)
            logger.info(f"    [SKIP] {symbol} Zone {current_zone} rise, but no lot clears cost: "
                        f"best basis ${cheapest:,.2f} needs ${need:,.2f}, price ${price:,.2f}.")
        return

    # Never sell more than the account actually holds — the shared position
    # may already have been drawn down by moon_bot or by hand.
    sell_qty = min(lot["qty"], held)
    if sell_qty <= DUST_QTY:
        logger.warning(f"    [SKIP] {symbol} lot {lot['qty']:.8f} but account holds {held:.8f}.")
        return

    gain_pct = (price - lot["price"]) / lot["price"]
    logger.info(f"    [SELL] {symbol} Rose to Zone {current_zone} — closing lot "
                f"{sell_qty:.8f} @ basis ${lot['price']:,.2f} (+{gain_pct:.2%} gross)")

    cancel_open_orders_for_symbol(symbol, opposite_side_only=OrderSide.BUY)
    order = bot.submit(
        bot.market_order(symbol, sell_qty, OrderSide.SELL, TimeInForce.GTC),
        action="grid_sell",
        notify=(f"🔴 **GRID SELL {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}\n"
                f"Basis: ${lot['price']:,.2f} (+{gain_pct:.2%} gross, "
                f"{gain_pct - ROUND_TRIP_COST_PCT:+.2%} net of costs)")
    )
    if order is not None:
        lot["qty"] -= sell_qty
        if lot["qty"] <= DUST_QTY:
            lots[symbol].pop(idx)
        save_lots(lots)


# One-time migration notice, per symbol.
_unledgered_reported = set()


def report_unledgered_inventory(symbol, held, lots):
    """Say so, once, when the account holds coins the grid has no lot for.

    On the first run after the lot-ledger migration the ledger is empty while
    the account still holds crypto. The grid deliberately does NOT adopt that
    inventory: attribution between crypto_grid, moon_bot and untagged history
    is exactly what the 2026-09-11 audit could not establish, and inventing a
    cost basis here would put a made-up number straight into the sell guard
    this redesign exists to make trustworthy.

    The consequence is real and worth stating plainly: coins with no lot are
    inventory the grid will never sell. They are not at risk of being
    double-bought — `bot.budget_ok` still counts the actual account positions,
    so the bot's total allocation is enforced — but winding them down is a
    human action (sell manually, or seed crypto_grid_state.json with a lot
    carrying the real basis).
    """
    if symbol in _unledgered_reported:
        return
    unledgered = held - ledger_qty(lots, symbol)
    if unledgered <= DUST_QTY:
        return
    _unledgered_reported.add(symbol)
    logger.warning(
        f"    [{symbol}] account holds {unledgered:.8f} with no grid lot. The grid will "
        f"not sell it (no cost basis to test against) and will not re-buy it (budget_ok "
        f"counts real positions). Seed {STATE_FILE} or wind it down by hand.")


def cycle(bot):
    lots = load_lots()
    dirty = False

    for symbol in SYMBOLS:
        price = get_crypto_price(symbol)
        if price is None:
            continue

        # Keep the ledger honest against the shared position before deciding
        # anything: a stale ledger over-states inventory and would re-buy on
        # top of coins the account no longer has.
        held = account_qty(symbol)
        if reconcile_lots(lots, symbol, held):
            dirty = True
        report_unledgered_inventory(symbol, held, lots)

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
                grid_buy(symbol, price, current_zone, lots)
            elif current_zone > previous_zone:
                grid_sell(symbol, price, current_zone, lots, held)

        grid["prev_zone"] = current_zone

    if dirty:
        save_lots(lots)


if __name__ == "__main__":
    logger.info("--- 🕸️ CRYPTO GRID BOT V4 (entry-linked lot spacing) ---")
    logger.info(f"    Sells require basis +{REQUIRED_SPREAD_PCT:.2%} "
                f"(costs {ROUND_TRIP_COST_PCT:.2%} + net {MIN_NET_PROFIT_PCT:.2%})")
    bot.run(cycle)
