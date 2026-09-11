"""Crypto Grid Bot — BTC/ETH/SOL zone-grid scalping, 24/7.

Runs on the fleet_bot runner (market_hours=False, no entry-time paging —
crypto ownership resolves statically).

  a +/-15% 8-level grid recenters after 4 consecutive out-of-band cycles;
  a zone drop buys a slice, a zone rise closes the OLDEST open lot, and only
  at a price that clears that lot's basis plus round-trip costs.

--- Why the sell side is entry-linked --------------------------------------

The original bot bought on any downward zone crossing and sold on any upward
one, each at the price prevailing when the crossing was noticed. The grid's
"spacing" therefore constrained the ZONE INDEX, not the transaction prices:
price ticking either side of one boundary produces a buy and a sell within
cents of each other, and the pair books a guaranteed loss of the round-trip
fee. Nothing compared a sell price to the price it was closing against, so
there was no level at which that was noticed — the bot could churn a boundary
indefinitely while the order count climbed and the dashboard showed activity.
365 filled orders since July 13 sit behind that logic.

--- Why the ledger is driven by FILLS, not submissions ----------------------

`bot.submit()` returns an order, not an outcome. It can be pending,
rejected, canceled, or partially filled. A first version of this ledger
recorded the REQUESTED quantity at the SUBMIT price whenever no fill was
present, and retired a whole lot whenever a sell returned any order object at
all. Both invent inventory: an unfilled $50 buy became a real 0.5-unit lot
with a fabricated cost basis, and a 1-unit sell that filled 0.25 erased the
remaining 0.75 from the books while the coins stayed in the account.

So nothing enters the ledger until the broker confirms it. Orders are tracked
in `pending` by id and reconciled against Alpaca every cycle:

  * a BUY's lot IS its order — reconciliation sets the lot to the order's
    cumulative (filled_qty, filled_avg_price), which is exact and idempotent
    no matter how many times it runs or how the fills arrive;
  * a SELL reduces its target lot by the NEWLY filled quantity only, tracked
    against an `applied_qty` watermark, so a partial keeps its remainder;
  * a terminal zero-fill leaves no lot behind;
  * pending orders live in the state file, so a restart mid-flight resumes
    reconciliation instead of losing or double-counting the fill.

--- FIFO is enforced here, not approximated --------------------------------

An earlier version sold the oldest *qualifying* lot, skipping underwater
ones. That is specific-lot selection, and it silently disagreed with the FIFO
the accountant and strategy_advisor use: buy 1 at $200 then 1 at $90, sell at
$120, and execution books +$30 against the $90 lot while the books book -$80
against the $200 lot. Strict FIFO means the oldest lot is the only candidate:
if it does not clear its cost, nothing is sold. The execution ledger and the
reports then describe the same trade.

--- Why the ledger is separate from the position ---------------------------

crypto_grid and moon_bot hold the same three coins in the same Alpaca
positions, and Alpaca positions are per-symbol, not per-bot. Reading the
account position as "the grid's inventory" let the grid sell moon_bot's coins
(an account-level FIFO reconstruction matched moon-origin ETH lots against
grid sells 113 times). The account position is only ever a ceiling — and only
when the read is KNOWN. A failed read is not a flat position: returning 0.0
for a timeout once let one bad request retire every lot in the file.

Budget: the CFO number comes from utils.get_budget_dollars, split PER SYMBOL,
with outstanding buy notional reserved. The old per-symbol check compared one
symbol's value against the bot's WHOLE budget, so three symbols could each
spend all of it — a 3x overrun that read as compliant on every check.
"""
import datetime
import json
import os
import uuid

from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest

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
#
# This is a LIMIT price, not a filter in front of a market order. A price
# check followed by a market order guarantees nothing: spread, slippage and
# fees all land after the check. The floor has to be the order's own limit or
# it is not a floor.
REQUIRED_SPREAD_PCT = ROUND_TRIP_COST_PCT + MIN_NET_PROFIT_PCT

# A resting sell that has not filled in this long is cancelled and re-decided
# next cycle. Explicit non-fill behavior: a GTC limit left alone forever is
# inventory silently out of the strategy's control.
PENDING_SELL_TTL_SECONDS = 30 * 60

# The grid's OWN inventory, separate from the shared Alpaca position.
# Gitignored (*.json); missing file = flat.
STATE_FILE = "crypto_grid_state.json"
STATE_VERSION = 2
DUST_QTY = 1e-8          # below this a lot is noise, not inventory

bot = FleetBot("crypto_grid", loop_seconds=30, market_hours=False,
               needs_entry_times=False)
logger = bot.logger

# --- STATE (per symbol, recalibrated at runtime) ---
grids = {sym: {"top": 0, "bottom": 0, "size": 0,
               "prev_zone": GRID_LEVELS // 2, "oob": 0} for sym in SYMBOLS}

# Set when the ledger or a broker read is untrustworthy. While true the bot
# manages what it can but opens nothing new: acting on inventory you cannot
# read is how a ledger and an account drift apart.
_entries_suspended = False
_suspend_reason = ""


def suspend_entries(reason):
    global _entries_suspended, _suspend_reason
    if not _entries_suspended or _suspend_reason != reason:
        logger.error(f"    [SUSPEND] New grid entries halted: {reason}")
        registry.log_error("crypto_grid", "entries_suspended", Exception(reason))
    _entries_suspended = True
    _suspend_reason = reason


def resume_entries():
    global _entries_suspended, _suspend_reason
    if _entries_suspended:
        logger.info("    [RESUME] Grid entries re-enabled — state is readable again.")
    _entries_suspended = False
    _suspend_reason = ""


# --- LEDGER ---------------------------------------------------------------
def _empty_state():
    return {"version": STATE_VERSION, "lots": {s: [] for s in SYMBOLS}, "pending": {}}


def load_state():
    """The grid's confirmed lots plus its in-flight orders.

    A ledger that cannot be read is NOT an empty one. Returning a clean slate
    for a truncated file would let the bot re-buy on top of inventory it still
    holds and re-sell lots it has already sold, so an unreadable file suspends
    entries instead.
    """
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        resume_entries()
        return _empty_state()
    except Exception as e:
        registry.log_error("crypto_grid", "load_state", e, context=STATE_FILE)
        suspend_entries(f"lot ledger unreadable ({e}) — repair or remove {STATE_FILE}")
        return _empty_state()

    if not isinstance(raw, dict):
        suspend_entries(f"lot ledger is not an object — repair {STATE_FILE}")
        return _empty_state()

    state = _empty_state()
    for sym in SYMBOLS:
        for e in (raw.get("lots") or {}).get(sym) or []:
            try:
                qty, price = float(e["qty"]), float(e["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if qty > DUST_QTY and price > 0:
                state["lots"][sym].append({
                    "lot_id": str(e.get("lot_id") or uuid.uuid4()),
                    "qty": qty, "price": price,
                    "opened_at": e.get("opened_at", ""),
                })
    for order_id, p in ((raw.get("pending") or {}) if isinstance(raw.get("pending"), dict) else {}).items():
        try:
            state["pending"][str(order_id)] = {
                "symbol": str(p["symbol"]),
                "side": str(p["side"]),
                "requested_qty": float(p.get("requested_qty", 0.0)),
                "applied_qty": float(p.get("applied_qty", 0.0)),
                "lot_id": str(p.get("lot_id") or ""),
                "limit_price": float(p.get("limit_price", 0.0) or 0.0),
                "submitted_at": float(p.get("submitted_at", 0.0) or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
    resume_entries()
    return state


def save_state(state):
    """Write atomically. A half-written ledger is a suspended bot next cycle."""
    tmp = f"{STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        registry.log_error("crypto_grid", "save_state", e, context=STATE_FILE)
        # Losing the write means the in-memory ledger and the file disagree —
        # a trading-state failure, not a logging inconvenience.
        suspend_entries(f"could not persist the lot ledger ({e})")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def lots_for(state, symbol):
    return state["lots"].setdefault(symbol, [])


def ledger_qty(state, symbol):
    return sum(lot["qty"] for lot in lots_for(state, symbol))


def ledger_value(state, symbol, price):
    """Mark-to-market value of the grid's OWN confirmed inventory."""
    return ledger_qty(state, symbol) * price


def find_lot(state, symbol, lot_id):
    for idx, lot in enumerate(lots_for(state, symbol)):
        if lot["lot_id"] == lot_id:
            return idx, lot
    return None, None


def outstanding_buy_notional(state, symbol):
    """Unfilled buy dollars already committed — reserved against the budget.

    Without this, several not-yet-filled entries can each see the same
    headroom and spend it.
    """
    total = 0.0
    for p in state["pending"].values():
        if p["symbol"] != symbol or p["side"] != "buy":
            continue
        unfilled = max(0.0, p["requested_qty"] - p["applied_qty"])
        total += unfilled * (p["limit_price"] or 0.0)
    return total


def lot_has_pending_sell(state, lot_id):
    return any(p["side"] == "sell" and p["lot_id"] == lot_id
               for p in state["pending"].values())


def sellable_lot(state, symbol, price):
    """The OLDEST open lot, if it clears its cost at `price`. Strict FIFO.

    Returns (index, lot) or (None, None). Deliberately does not look past the
    first lot: skipping an underwater lot to reach a profitable one is
    specific-lot selection, which the accountant's FIFO would book
    differently. One policy, both places.
    """
    for idx, lot in enumerate(lots_for(state, symbol)):
        if lot["qty"] <= DUST_QTY or lot_has_pending_sell(state, lot["lot_id"]):
            continue
        if price >= lot["price"] * (1.0 + REQUIRED_SPREAD_PCT):
            return idx, lot
        return None, None   # oldest lot does not clear: FIFO stops here
    return None, None


def sell_floor(lot):
    """The lowest price this lot may be sold at."""
    return lot["price"] * (1.0 + REQUIRED_SPREAD_PCT)


# --- BROKER READS ---------------------------------------------------------
def _http_status(exc):
    """HTTP status behind an exception, or None.

    Deliberately duck-typed rather than keyed on alpaca's APIError class: the
    SDK wraps and re-raises through several layers, and the only thing that
    matters here is whether the broker gave us a status at all. A 404 is an
    ANSWER ("no such position"); everything else is silence, and silence must
    never be read as a flat position.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def account_qty(symbol):
    """(qty, known) for the SHARED account position.

    `known=False` means the broker did not answer — not that the position is
    flat. Collapsing those two was destructive once the ledger existed: one
    timeout returned 0.0, reconciliation treated it as authoritative, and every
    lot in the file was retired and persisted.

    A 404 IS an answer: Alpaca says the position does not exist.
    """
    missing = 0
    for candidate in (symbol, symbol.replace("/", "")):
        try:
            return float(bot.trading_client.get_open_position(candidate).qty), True
        except Exception as e:
            if _http_status(e) == 404:
                missing += 1          # the broker ANSWERED: no such position
                continue
            registry.log_error("crypto_grid", "check_inventory", e, context=candidate)
            return 0.0, False         # no answer at all
    # Both symbol spellings confirmed absent.
    return (0.0, True) if missing else (0.0, False)


def reconcile_lots(state, symbol, held, known):
    """Trim the ledger when the account confirms it holds less than we claim.

    Only a KNOWN read may retire inventory. Returns True if anything changed.
    """
    if not known:
        return False
    have = ledger_qty(state, symbol)
    excess = have - held
    if excess <= DUST_QTY:
        return False

    logger.warning(f"    [{symbol}] Ledger {have:.8f} > account {held:.8f}; "
                   f"retiring {excess:.8f} from the oldest lots.")
    book = lots_for(state, symbol)
    while excess > DUST_QTY and book:
        lot = book[0]
        take = min(lot["qty"], excess)
        lot["qty"] -= take
        excess -= take
        if lot["qty"] <= DUST_QTY:
            book.pop(0)
    return True


def reconcile_pending(state):
    """Apply confirmed fills for every in-flight order. Idempotent.

    This is the ONLY path by which inventory enters or leaves the ledger.
    Returns True if the state changed.
    """
    changed = False
    for order_id in list(state["pending"].keys()):
        p = state["pending"][order_id]
        try:
            order = bot.trading_client.get_order_by_id(order_id)
        except Exception as e:
            # Unknown, not finished. Leave it pending and stop opening more.
            registry.log_error("crypto_grid", "reconcile_pending", e, context=order_id)
            suspend_entries(f"cannot read in-flight order {order_id} ({e})")
            continue

        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        filled_price = float(getattr(order, "filled_avg_price", 0) or 0)
        status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
        terminal = status in {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}
        symbol = p["symbol"]

        if p["side"] == "buy":
            # The lot IS the order: restate it from the broker's cumulative
            # numbers. Exact and idempotent however the fills arrive.
            idx, lot = find_lot(state, symbol, p["lot_id"])
            if filled_qty > DUST_QTY and filled_price > 0:
                if lot is None:
                    lots_for(state, symbol).append({
                        "lot_id": p["lot_id"], "qty": filled_qty, "price": filled_price,
                        "opened_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    })
                    logger.info(f"    [LOT+] {symbol} {filled_qty:.8f} @ ${filled_price:,.2f} "
                                f"(sells at/above ${filled_price * (1 + REQUIRED_SPREAD_PCT):,.2f})")
                elif abs(lot["qty"] - filled_qty) > DUST_QTY or lot["price"] != filled_price:
                    lot["qty"], lot["price"] = filled_qty, filled_price
                p["applied_qty"] = filled_qty
                changed = True
            if terminal:
                if filled_qty <= DUST_QTY:
                    # Rejected/canceled with nothing filled: no inventory, and
                    # no fabricated lot left behind.
                    if lot is not None:
                        lots_for(state, symbol).pop(idx)
                    logger.info(f"    [LOT-] {symbol} buy {order_id} ended {status} with no fill.")
                del state["pending"][order_id]
                changed = True

        else:  # sell
            new_qty = filled_qty - p["applied_qty"]
            if new_qty > DUST_QTY:
                idx, lot = find_lot(state, symbol, p["lot_id"])
                if lot is not None:
                    lot["qty"] = max(0.0, lot["qty"] - new_qty)
                    if lot["qty"] <= DUST_QTY:
                        lots_for(state, symbol).pop(idx)
                    logger.info(f"    [LOT-] {symbol} sold {new_qty:.8f} @ ${filled_price:,.2f}")
                p["applied_qty"] = filled_qty
                changed = True
            if terminal:
                # Whatever did not fill stays in the lot, by construction: we
                # only ever subtracted what the broker confirmed.
                unfilled = max(0.0, p["requested_qty"] - filled_qty)
                if unfilled > DUST_QTY:
                    logger.info(f"    [{symbol}] sell {order_id} ended {status}; "
                                f"{unfilled:.8f} stays in the ledger.")
                del state["pending"][order_id]
                changed = True
    return changed


def expire_stale_sells(state):
    """Cancel resting sells that have sat too long, so they get re-decided."""
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    changed = False
    for order_id, p in list(state["pending"].items()):
        if p["side"] != "sell" or not p["submitted_at"]:
            continue
        if (now - p["submitted_at"]) < PENDING_SELL_TTL_SECONDS:
            continue
        try:
            bot.trading_client.cancel_order_by_id(order_id)
            logger.info(f"    [CANCEL] {p['symbol']} resting sell {order_id} exceeded "
                        f"{PENDING_SELL_TTL_SECONDS / 60:.0f}m; re-deciding next cycle.")
            changed = True
        except Exception as e:
            # Already terminal, or unreachable. reconcile_pending settles it.
            logger.info(f"    [CANCEL] {p['symbol']} sell {order_id} not cancelled: {e}")
    return changed


# --- MARKET DATA ----------------------------------------------------------
def get_crypto_price(symbol):
    try:
        from alpaca.data.requests import CryptoLatestTradeRequest
        res = crypto_data_client.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbol))
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


def cancel_my_open_orders(symbol, side=None):
    """Cancel THIS BOT's open orders on a symbol. Never another bot's.

    The old helper cancelled every open order on the symbol. crypto_grid and
    moon_bot trade the same three coins, so it could cancel moon_bot's resting
    breakout orders as a side effect of a grid decision.
    """
    try:
        open_orders = bot.trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]))
    except Exception as e:
        logger.error(f"Error listing open orders for {symbol}: {e}")
        return
    for o in open_orders:
        if not str(getattr(o, "client_order_id", "") or "").startswith("crypto_grid-"):
            continue  # not ours
        if side is not None and o.side != side:
            continue
        try:
            logger.info(f"    [CANCEL] grid order {o.id} on {symbol} (Side: {o.side}).")
            bot.trading_client.cancel_order_by_id(o.id)
        except Exception as e:
            logger.error(f"Error canceling {o.id} on {symbol}: {e}")


def per_symbol_budget():
    """The bot's CFO budget, divided evenly across the symbols it trades.

    The old check compared ONE symbol's value against the WHOLE budget, so
    each of three symbols could independently spend all of it. Each check
    passed; the bot ran at 3x its allocation.
    """
    whole = utils.get_budget_dollars("crypto_grid", bot.trading_client, equity=bot.equity)
    return whole / len(SYMBOLS) if SYMBOLS else 0.0


# --- TRADING --------------------------------------------------------------
def grid_buy(symbol, price, current_zone, state):
    """Zone drop -> accumulate a slice, unless regime/crunch/budget says no."""
    if _entries_suspended:
        logger.warning(f"    [SKIP] {symbol} entries suspended: {_suspend_reason}")
        return
    if "BEAR" in bot.regime:
        logger.info(f"    [SKIP] Bear Trend Detected. Buying Paused in Zone {current_zone} for {symbol}.")
        return
    if bot.capital_crunch:
        logger.warning(f"    [SKIP] CAPITAL_CRUNCH active. Buy paused for {symbol}.")
        return

    my_budget = per_symbol_budget()
    # Confirmed inventory + dollars already committed to unfilled buys. Both,
    # or several in-flight orders each see the same headroom.
    committed = ledger_value(state, symbol, price) + outstanding_buy_notional(state, symbol)

    if committed >= my_budget:
        logger.warning(f"    [BUDGET STOP] {symbol} committed ${committed:.2f} >= "
                       f"per-symbol budget ${my_budget:.2f}. Skipping buy.")
        return  # still allowed to SELL if price rises

    buying_power = float(bot.account.buying_power)
    slice_dollars = min(BUDGET_PER_GRID, my_budget - committed)
    if not bot.budget_ok:
        logger.warning(f"    [SKIP] {symbol} Grid buy ${BUDGET_PER_GRID} > Available (budget limit)")
        return
    if slice_dollars <= 0:
        logger.warning(f"    [SKIP] {symbol} no per-symbol budget headroom left.")
        return
    if buying_power <= slice_dollars:
        logger.warning(f"    [SKIP] {symbol} Low Balance: ${buying_power:.2f} (Need ${slice_dollars:.2f})")
        return

    logger.info(f"    [BUY] {symbol} Dropped to Zone {current_zone}")
    cancel_my_open_orders(symbol, side=OrderSide.SELL)
    qty = slice_dollars / price
    lot_id = str(uuid.uuid4())

    order = bot.submit(
        bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.GTC),
        action="grid_buy",
        notify=f"🟢 **GRID BUY {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}"
    )
    if order is None:
        return  # refused before reaching the broker; nothing to track

    order_id = str(getattr(order, "id", "") or "")
    if not order_id:
        registry.log_error("crypto_grid", "grid_buy",
                           Exception("submitted order has no id; cannot track its fills"),
                           context=symbol)
        return
    # Tracked, NOT booked. The lot appears only when a fill confirms it.
    state["pending"][order_id] = {
        "symbol": symbol, "side": "buy", "requested_qty": qty,
        "applied_qty": 0.0, "lot_id": lot_id, "limit_price": price,
        "submitted_at": datetime.datetime.now(datetime.timezone.utc).timestamp(),
    }


def grid_sell(symbol, price, current_zone, state, held, known):
    """Zone rise -> close the oldest lot, at or above its own cost floor.

    A zone rise is not by itself a reason to sell. The order is a LIMIT at the
    floor, so spread and slippage cannot push the fill below cost: a price
    check in front of a market order checks a price the trade never uses.
    """
    idx, lot = sellable_lot(state, symbol, price)
    if lot is None:
        book = [l for l in lots_for(state, symbol) if l["qty"] > DUST_QTY]
        if not book:
            logger.info(f"    [SKIP] {symbol} Sell Signal but no open grid lots.")
        elif lot_has_pending_sell(state, book[0]["lot_id"]):
            logger.info(f"    [SKIP] {symbol} oldest lot already has a resting sell.")
        else:
            need = sell_floor(book[0])
            logger.info(f"    [SKIP] {symbol} Zone {current_zone} rise, but the oldest lot "
                        f"(basis ${book[0]['price']:,.2f}) needs ${need:,.2f}, price ${price:,.2f}.")
        return

    # Never offer more than the account holds — the shared position may have
    # been drawn down by moon_bot or by hand. An UNKNOWN read is not a zero,
    # but it is also not a licence to sell into the dark.
    if not known:
        logger.warning(f"    [SKIP] {symbol} position unreadable this cycle; not selling blind.")
        return
    sell_qty = min(lot["qty"], held)
    if sell_qty <= DUST_QTY:
        logger.warning(f"    [SKIP] {symbol} lot {lot['qty']:.8f} but account holds {held:.8f}.")
        return

    floor = sell_floor(lot)
    limit_price = max(floor, price)
    gain_pct = (limit_price - lot["price"]) / lot["price"]
    logger.info(f"    [SELL] {symbol} Zone {current_zone} — offering lot {sell_qty:.8f} "
                f"at limit ${limit_price:,.2f} (basis ${lot['price']:,.2f}, +{gain_pct:.2%} gross)")

    cancel_my_open_orders(symbol, side=OrderSide.BUY)
    order = bot.submit(
        LimitOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL,
                          time_in_force=TimeInForce.GTC,
                          limit_price=round(limit_price, 2),
                          client_order_id=bot.tag(symbol)),
        action="grid_sell",
        notify=(f"🔴 **GRID SELL {symbol}**\nLimit: ${limit_price:,.2f}\nZone: {current_zone}\n"
                f"Basis: ${lot['price']:,.2f} (+{gain_pct:.2%} gross, "
                f"{gain_pct - ROUND_TRIP_COST_PCT:+.2%} net of assumed costs)")
    )
    if order is None:
        return

    order_id = str(getattr(order, "id", "") or "")
    if not order_id:
        registry.log_error("crypto_grid", "grid_sell",
                           Exception("submitted order has no id; cannot track its fills"),
                           context=symbol)
        return
    # Nothing leaves the lot until the broker confirms a fill.
    state["pending"][order_id] = {
        "symbol": symbol, "side": "sell", "requested_qty": sell_qty,
        "applied_qty": 0.0, "lot_id": lot["lot_id"], "limit_price": limit_price,
        "submitted_at": datetime.datetime.now(datetime.timezone.utc).timestamp(),
    }


# One-time migration notice, per symbol.
_unledgered_reported = set()


def report_unledgered_inventory(symbol, held, known, state):
    """Say so, once, when the account holds coins the grid has no lot for.

    On the first run after the lot-ledger migration the ledger is empty while
    the account still holds crypto. The grid deliberately does NOT adopt that
    inventory: attribution between crypto_grid, moon_bot and untagged history
    is exactly what the 2026-09-11 audit could not establish, and inventing a
    cost basis here would put a made-up number straight into the sell floor
    this redesign exists to make trustworthy.

    The consequence is real: coins with no lot are inventory the grid will
    never sell. They are not at risk of being double-bought — `bot.budget_ok`
    counts the actual account positions — but winding them down is a human
    action (sell manually, or seed the ledger with the real basis).
    """
    if not known or symbol in _unledgered_reported:
        return
    unledgered = held - ledger_qty(state, symbol)
    if unledgered <= DUST_QTY:
        return
    _unledgered_reported.add(symbol)
    logger.warning(
        f"    [{symbol}] account holds {unledgered:.8f} with no grid lot. The grid will "
        f"not sell it (no cost basis to test against) and will not re-buy it (budget_ok "
        f"counts real positions). Seed {STATE_FILE} or wind it down by hand.")


def cycle(bot):
    state = load_state()

    # 1. Settle everything in flight BEFORE deciding anything new. Suspends
    #    entries by itself if an order cannot be read.
    dirty = reconcile_pending(state)
    if expire_stale_sells(state):
        dirty = True

    for symbol in SYMBOLS:
        price = get_crypto_price(symbol)
        if price is None:
            continue

        held, known = account_qty(symbol)
        if not known:
            logger.warning(f"    [{symbol}] position read failed; ledger preserved, "
                           f"entries held off this cycle.")
            suspend_entries(f"position read failed for {symbol}")
        if reconcile_lots(state, symbol, held, known):
            dirty = True
        report_unledgered_inventory(symbol, held, known, state)

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
            before = json.dumps(state, sort_keys=True)
            if current_zone < previous_zone:
                grid_buy(symbol, price, current_zone, state)
            elif current_zone > previous_zone:
                grid_sell(symbol, price, current_zone, state, held, known)
            if json.dumps(state, sort_keys=True) != before:
                dirty = True

        grid["prev_zone"] = current_zone

    if dirty:
        save_state(state)


# Crypto data needs its own client; the runner only carries stock data.
from alpaca.data.historical import CryptoHistoricalDataClient  # noqa: E402
crypto_data_client = CryptoHistoricalDataClient()


if __name__ == "__main__":
    logger.info("--- 🕸️ CRYPTO GRID BOT V5 (fill-driven ledger, FIFO floor) ---")
    logger.info(f"    Sells are LIMIT orders at basis +{REQUIRED_SPREAD_PCT:.2%} "
                f"(costs {ROUND_TRIP_COST_PCT:.2%} + net {MIN_NET_PROFIT_PCT:.2%})")
    bot.run(cycle)
