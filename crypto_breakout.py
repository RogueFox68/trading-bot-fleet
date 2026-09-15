"""Moon Bot — Donchian channel breakout on BTC/ETH/SOL, 24/7.

Ported onto the fleet_bot runner (market_hours=False, no entry-time paging).
Strategy: buy a 20-day-high breakout, trail out on a 10-day-low break. Size is
10% of equity CLIPPED to the CFO budget remaining — the uncapped 10% overshot
moon_bot's 4% allocation by ~2.5x on every entry.

moon_bot shares its symbols with crypto_grid, and Alpaca positions are
per-symbol, not per-bot — so a ledger file records what moon_bot itself
bought. The trailing stop only sells the ledger quantity (never the grid's
inventory) and entries key off the ledger, not the shared position.
"""
import json
import os

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
import datetime

import utils
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
LOOKBACK_ENTRY = 20  # Buy if we break the 20-day high
LOOKBACK_EXIT = 10   # Sell if we break the 10-day low
RISK_PCT = 0.10      # Target size per trade, before the CFO budget clip
HISTORY_DAYS = 60    # Daily bars fetched; must comfortably exceed LOOKBACK_ENTRY
DAILY_BAR_SECONDS = 24 * 3600
# Crypto trades every calendar day, so a daily bar older than ~2 days is a
# feed failure, not a weekend.
DAILY_STALE_FACTOR = 2.0

# Ledger of moon_bot's own holdings. Gitignored (*.json); missing file = flat.
STATE_FILE = "moon_bot_state.json"

bot = FleetBot("moon_bot", loop_seconds=3600, market_hours=False,
               needs_entry_times=False, discord_username="Moon Bot 🚀")
logger = bot.logger

# Crypto data needs its own client; the runner only carries stock data.
crypto_data_client = CryptoHistoricalDataClient()


# --- LEDGER ---------------------------------------------------------------
# moon_bot's ledger is a running quantity per coin, not a lot book — but it
# needs the same fill discipline as crypto_grid's. `bot.submit()` returns an
# order, not an outcome: it can be pending, rejected, canceled or partially
# filled. The old code wrote the REQUESTED quantity when no fill was present
# ("state[symbol] = filled if filled > 0 else qty_to_buy") and zeroed the coin
# on a sell SUBMISSION. Both invent inventory: an unfilled buy became real
# holdings, and a sell that filled a third erased the rest from the ledger
# while the coins stayed in the account — where crypto_grid, which shares
# these symbols, could then reach them.
#
# So quantity moves only when the broker confirms a fill, tracked against an
# `applied_qty` watermark so reconciliation is idempotent and survives a
# restart mid-flight.
STATE_VERSION = 2
DUST_QTY = 1e-8


# In-memory only: flags load_state sets for cycle(), never written to disk.
_TRANSIENT_KEYS = frozenset({"unreadable", "migrated"})


def _empty_state():
    return {"version": STATE_VERSION, "qty": {}, "pending": {}, "outbox": []}


def load_state():
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except Exception as e:
        registry.log_error("moon_bot", "load_state", e, context=STATE_FILE)
        logger.error(f"[!] State file unreadable ({e}); holding off on entries this cycle.")
        state = _empty_state()
        state["unreadable"] = True
        return state

    if not isinstance(raw, dict):
        state = _empty_state()
        state["unreadable"] = True
        return state

    # v1 was a flat {symbol: qty} map; carry it forward.
    #
    # Build it from _empty_state() rather than a dict literal. The literal
    # listed the keys v2 had ON THE DAY IT WAS WRITTEN, so when the outbox
    # arrived every OTHER path gained the key and this one silently did not:
    # `state["outbox"]` at the top of cycle() raised KeyError before any work
    # ran, the runner caught it, and moon_bot logged one error a minute for
    # four days while PM2 showed it online and the doctor's import check
    # passed. Nothing was saved either — save_state only runs at the END of a
    # cycle that never got there — so the file stayed v1 and the crash
    # repeated forever. An empty `{}` file reached the same branch and broke
    # the same way. Seeding from _empty_state() means a key added to v2 is
    # carried by every loader by construction, not by remembering to.
    if "qty" not in raw and "pending" not in raw:
        try:
            migrated = {str(k): float(v) for k, v in raw.items()}
        except (TypeError, ValueError):
            state = _empty_state()
            state["unreadable"] = True
            return state
        state = _empty_state()
        state["qty"] = migrated
        # Convert the file on disk once, so the migration is not re-run (and
        # not re-risked) on every restart. cycle() acts on this.
        state["migrated"] = True
        return state

    state = _empty_state()
    for k, v in (raw.get("qty") or {}).items():
        try:
            state["qty"][str(k)] = float(v)
        except (TypeError, ValueError):
            continue
    state["outbox"] = [str(l) for l in (raw.get("outbox") or []) if l]
    for order_id, p in (raw.get("pending") or {}).items():
        try:
            state["pending"][str(order_id)] = {
                "symbol": str(p["symbol"]), "side": str(p["side"]),
                "requested_qty": float(p.get("requested_qty", 0.0)),
                "applied_qty": float(p.get("applied_qty", 0.0)),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return state


def save_state(state):
    """Write atomically — a half-written ledger is lost inventory tracking."""
    tmp = f"{STATE_FILE}.tmp"
    try:
        payload = {k: v for k, v in state.items() if k not in _TRANSIENT_KEYS}
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
        return True
    except Exception as e:
        registry.log_error("moon_bot", "save_state", e, context=STATE_FILE)
        logger.error(f"[!] Could not save state file: {e}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def my_qty(state, symbol):
    return float(state["qty"].get(symbol, 0.0))


def has_pending(state, symbol):
    return any(p["symbol"] == symbol for p in state["pending"].values())


def reconcile_pending(state):
    """Apply confirmed fills for in-flight orders. Idempotent.

    The ONLY path by which moon_bot's tracked quantity moves. Returns
    (changed, settled_symbols) — the symbols whose ledger moved on a confirmed
    fill during THIS cycle. External adjustment must leave those alone.
    """
    changed = False
    settled = set()
    for order_id in list(state["pending"].keys()):
        p = state["pending"][order_id]
        try:
            order = bot.trading_client.get_order_by_id(order_id)
        except Exception as e:
            registry.log_error("moon_bot", "reconcile_pending", e, context=order_id)
            logger.error(f"[!] Cannot read in-flight order {order_id} ({e}); leaving it pending.")
            continue


        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        status = str(getattr(getattr(order, "status", ""), "value",
                             getattr(order, "status", ""))).lower()
        terminal = status in {"filled", "canceled", "cancelled", "expired",
                              "rejected", "done_for_day"}
        new_qty = filled_qty - p["applied_qty"]
        if new_qty > DUST_QTY:
            sign = 1.0 if p["side"] == "buy" else -1.0
            current = my_qty(state, p["symbol"])
            state["qty"][p["symbol"]] = max(0.0, current + sign * new_qty)
            p["applied_qty"] = filled_qty
            settled.add(p["symbol"])
            logger.info(f"    [LEDGER] {p['symbol']} {p['side']} filled {new_qty:.6f} "
                        f"-> tracked {state['qty'][p['symbol']]:.6f}")
            changed = True
        if terminal:
            # moon_bot is reconciled=False in the registry, so reconcile_fills
            # never visits it; its rows reach InfluxDB only from here and from
            # submit_and_log_order's market-order poll. Idempotent either way.
            utils.log_confirmed_fill(
                order, logger,
                action="buy_breakout" if p["side"] == "buy" else "sell_breakout",
                outbox=state["outbox"])
            if filled_qty <= DUST_QTY:
                logger.info(f"    [LEDGER] {p['symbol']} {p['side']} {order_id} ended "
                            f"{status} with no fill; ledger unchanged.")
            del state["pending"][order_id]
            changed = True
    return changed, settled


def track_order(state, order, symbol, side, requested_qty):
    """Record an in-flight order. Records NO quantity — only a fill does that."""
    order_id = str(getattr(order, "id", "") or "")
    if not order_id:
        registry.log_error("moon_bot", "track_order",
                           Exception("submitted order has no id; cannot track its fills"),
                           context=symbol)
        return False
    state["pending"][order_id] = {
        "symbol": symbol, "side": side,
        "requested_qty": float(requested_qty), "applied_qty": 0.0,
    }
    return True


def get_donchian_levels(symbol):
    try:
        # 1. Fetch History for Levels
        #
        # The old request paired start=-60d with limit=30, and crypto trades
        # every calendar day - so it returned days 1-30 of the window and the
        # "20-day high" was a month old. Compounding it, `df.iloc[:-1]` then
        # dropped what it assumed was today's forming bar but was in fact a
        # completed one. Both are structural now: no `limit`, and the forming
        # bar is identified by its timestamp (utils.drop_forming_bar).
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=HISTORY_DAYS)
        req = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
        )
        bars = crypto_data_client.get_crypto_bars(req)
        df = bars.df.loc[symbol]

        if not utils.bars_are_fresh(df, DAILY_BAR_SECONDS, "moon_bot", symbol, "donchian",
                                    stale_factor=DAILY_STALE_FACTOR):
            return None, None, None

        # Exclude the current incomplete bar for levels
        completed_candles = utils.drop_forming_bar(df, DAILY_BAR_SECONDS)

        needed = max(LOOKBACK_ENTRY, LOOKBACK_EXIT)
        if len(completed_candles) < needed:
            logger.error(f"    [!] {symbol}: only {len(completed_candles)} completed daily bars, need {needed}")
            return None, None, None

        entry_high = utils.newest_bars(completed_candles, LOOKBACK_ENTRY)['high'].max()
        exit_low = utils.newest_bars(completed_candles, LOOKBACK_EXIT)['low'].min()

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

    state = load_state()
    # A migrated v1 ledger is written back immediately, not left to the
    # end-of-cycle `if dirty` save: anything that raises in between would
    # leave the file in the old shape and migrate it again next cycle.
    if state.pop("migrated", False):
        logger.info(f"    [LEDGER] migrated v1 ledger to v{STATE_VERSION}; "
                    f"rewriting {STATE_FILE}.")
        save_state(state)
    # Retry any fill rows InfluxDB refused earlier, then settle everything in
    # flight. Settlement applies once; delivery retries until it lands.
    dirty = bool(utils.flush_fill_outbox(state["outbox"], logger))
    settled_changed, settled = reconcile_pending(state)
    if settled_changed:
        dirty = True
    entries_blocked = bool(state.get("unreadable"))
    if entries_blocked:
        logger.error("    [SKIP] Ledger unreadable — managing nothing new this cycle.")

    # Positions are read FRESH, per symbol, AFTER reconciliation — never from
    # `bot.positions`.
    #
    # That snapshot is taken by FleetBot.refresh() before cycle() runs, so it
    # predates this cycle's own fill settlement. A buy that filled between the
    # snapshot and the order read was therefore compared against a position
    # list captured while it was still unfilled: the ledger "over-stated"
    # reality, the coin was zeroed, and with a triggered trailing stop no exit
    # was submitted at all. Reproduced: a just-confirmed 1 ETH buy against a
    # prior flat snapshot left qty=0 and zero stop submissions.
    #
    # utils.account_position_qty also distinguishes a 404 (an answer) from a
    # timeout (silence); only an answer may retire inventory.

    logger.info(f"Scanning Markets... Equity: ${bot.equity:,.2f}")

    for symbol in SYMBOLS:
        try:
            entry_high, exit_low, current_price = get_donchian_levels(symbol)
            if current_price is None: continue

            total_held, held_known = utils.account_position_qty(
                bot.trading_client, symbol, "moon_bot")
            mine = my_qty(state, symbol)

            # Ledger says we hold coins the account no longer has (manual
            # sale, grid sweep): reconcile down to reality — but ONLY on
            # evidence that outranks the ledger. Not while one of our own
            # orders is unsettled (the order reconciler owns that quantity),
            # not when the read failed, and not when a fill settled for this
            # symbol during this cycle, because the position endpoint can lag
            # the order endpoint and a just-confirmed buy would look like an
            # external disposal.
            if mine > total_held + DUST_QTY:
                if not held_known:
                    logger.warning(f"    [{symbol}] position read failed; preserving the "
                                   f"ledger ({mine:.6f}) rather than retiring it blind.")
                elif symbol in settled:
                    logger.info(f"    [{symbol}] a fill settled this cycle; deferring external "
                                f"adjustment until the next read.")
                elif has_pending(state, symbol):
                    logger.info(f"    [{symbol}] an order is still in flight; leaving the "
                                f"shortfall to the order reconciler.")
                else:
                    logger.warning(f"    [{symbol}] Ledger {mine:.6f} > account {total_held:.6f}; reconciling down.")
                    mine = max(0.0, total_held)
                    state["qty"][symbol] = mine
                    dirty = True

            logger.info(f"  {symbol:<8} | Price: ${current_price:,.2f} | Breakout: ${entry_high:,.2f} | Stop: ${exit_low:,.2f} | Mine: {mine:.6f}")

            if has_pending(state, symbol):
                logger.info(f"    [SKIP] {symbol} has an order in flight; waiting for its fill.")
                continue

            # --- ENTRY LOGIC (gate on OUR ledger, not the shared position) ---
            if mine <= DUST_QTY:
                if current_price > entry_high:
                    logger.info(f"    [SIGNAL] BREAKOUT! Price ${current_price} > ${entry_high}")

                    if entries_blocked:
                        continue
                    if not bot.budget_ok:
                        logger.warning(f"    [SKIP] Breakout buy blocked — CFO Budget limit reached.")
                        continue

                    # Calculate Size.
                    #
                    # RISK_PCT is a TARGET, not an entitlement: 10% of equity
                    # against a 4% base allocation overshoots the CFO budget by
                    # ~2.5x on every breakout. `budget_ok` above is only a
                    # boolean — it says there is room, never how much — so the
                    # order has to be clipped to the dollars actually left.
                    available = utils.get_available_budget("moon_bot", bot.trading_client)
                    target_val = min(bot.equity * RISK_PCT, available)
                    if target_val <= 0:
                        logger.warning(f"    [SKIP] {symbol} no budget left (available ${available:,.2f}).")
                        continue
                    if target_val < bot.equity * RISK_PCT:
                        logger.info(f"    [CLIP] {symbol} target ${bot.equity * RISK_PCT:,.2f} "
                                    f"-> ${target_val:,.2f} (CFO budget remaining).")
                    qty_to_buy = round(target_val / current_price, 4)
                    if qty_to_buy <= 0:
                        logger.warning(f"    [SKIP] {symbol} budget ${target_val:,.2f} rounds to zero qty.")
                        continue

                    if (qty_to_buy * current_price) > buying_power:
                        logger.info("    [!] Insufficient Buying Power")
                        continue

                    order = bot.submit(
                        bot.market_order(symbol, qty_to_buy, OrderSide.BUY, TimeInForce.GTC),
                        action="buy_breakout",
                        notify=f"🚀 **MOONSHOT ENTRY: {symbol}**\nBreakout Price: ${current_price}\nTargeting trends."
                    )
                    # Tracked, NOT booked: quantity moves on a confirmed fill.
                    if order is not None and track_order(state, order, symbol, "buy", qty_to_buy):
                        dirty = True

            # --- EXIT LOGIC (sell only OUR coins, never the grid's) ---
            else:
                if current_price < exit_low:
                    if not held_known:
                        # Risk management must stay REACHABLE, and it must not
                        # sell into a position it cannot see. Loud, and retried
                        # next cycle — never a silent no-op on a live stop.
                        registry.log_error(
                            "moon_bot", "stop_unverifiable",
                            Exception("trailing stop triggered but the position read failed"),
                            context=symbol)
                        logger.error(f"    [!] {symbol} TRAILING STOP triggered but the position "
                                     f"is unreadable — not selling blind. Retrying next cycle.")
                        continue
                    sell_qty = round(min(mine, total_held), 6)
                    if sell_qty <= DUST_QTY:
                        logger.warning(f"    [{symbol}] stop triggered but nothing sellable "
                                       f"(ledger {mine:.6f}, account {total_held:.6f}).")
                        continue
                    logger.info(f"    [SIGNAL] TRAILING STOP! Price ${current_price} < ${exit_low} (selling {sell_qty})")

                    order = bot.submit(
                        bot.market_order(symbol, sell_qty, OrderSide.SELL, TimeInForce.GTC),
                        action="sell_breakout",
                        notify=f"🛑 **STOP LOSS: {symbol}**\nPrice: ${current_price}\nTrend broken."
                    )
                    # The old code zeroed the coin here, on SUBMISSION. A
                    # partial fill then erased unsold coins from the ledger.
                    if order is not None and track_order(state, order, symbol, "sell", sell_qty):
                        dirty = True
                else:
                    logger.info(f"    [HOLD] Riding the trend.")

        except Exception as e:
            logger.error(f"    [!] Error {symbol}: {e}")

    if dirty:
        save_state(state)


if __name__ == "__main__":
    bot.notify("🚀 **Moon Bot Online**\nRunner: fleet_bot | Strategy: Donchian Breakout (20/10)")
    bot.run(cycle)
