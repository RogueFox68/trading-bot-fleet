import config
import re
import time
import datetime
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from logger import logger
import utils
import fleet_registry
import strategy_advisor


# --- CONFIGURATION ---
# Ownership/attribution is resolved via utils.get_bot_owner; the bot roster,
# measurements, and gating rules all come from fleet_registry.

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
DB_QUERY_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/query"

# --- REGIME STALENESS WATCHDOG ---
# market_analyst writes a market_regime row only on a fully-successful SPY+VIX
# fetch, so the age of the newest row is the fleet's "regime is live" signal.
# If it goes stale the VIX>28 kill-switch is running blind (the 2026-06-24
# outage ran ~12 days unseen). This backstop is cross-process: it fires even if
# the analyst is wedged/dead and its own in-process fail-safe never runs.
REGIME_STALE_SECONDS = 30 * 60
REGIME_STALE_ALERT_INTERVAL = 3600  # throttle overseer pings to once/hour
_last_regime_stale_alert = 0

# Paper-only allocation recommendation cadence. This writes
# recommended_allocations.json for review; it never mutates effective_budgets.json.
STRATEGY_ADVISOR_INTERVAL = 3600
_last_strategy_advisor_run = 0
# The advisor's Alpaca ledger spans max(WINDOW_DAYS) days; the Influx side of
# its source comparison must cover the same span or the deltas are meaningless.
# The CFO's own realized_scores stays a 30d read and is unaffected.
ADVISOR_COMPARE_DAYS = max(strategy_advisor.WINDOW_DAYS)

# Options measurements log per-share premium and contract qty, so their realized
# P&L must be scaled by the 100-share contract multiplier to express dollars.
# (Retired measurements like condor_trades stay in InfluxDB but are no longer
# queried or reported.)
OPTION_MEASUREMENTS = fleet_registry.OPTION_MEASUREMENTS
OPTION_CONTRACT_SIZE = 100

# measurement -> bot, for attributing realized P&L rows
MEASUREMENT_TO_BOT = {
    cfg["measurement"]: name for name, cfg in fleet_registry.BOTS.items()
}

# --- CLIENT ---
# Bounded read timeout: a hung Alpaca socket in reconcile_fills stalled the
# whole 2026-07-05 accountant cycle (orphan sweep + CFO realloc sit behind it).
trading_client = utils.bound_session_timeout(TradingClient(API_KEY, SECRET_KEY, paper=PAPER))

# Reporting window for realized P&L.
PL_WINDOW_DAYS = 30
# Extra history fetched PURELY to reconstruct what was already held when the
# reporting window opened. Without it, a sell inside the window has no basis
# to close against and its whole proceeds look like profit. 180d covers every
# holding period the fleet actually runs (the wheel's longest expiries, the
# grid's oldest un-recycled lot) without unbounded queries.
INVENTORY_LOOKBACK_DAYS = 180


def query_influx_trades(days=30):
    """Fetches trade history from InfluxDB to calculate Realized P&L.

    `days` must cover the reporting window PLUS the inventory lookback — see
    calculate_realized_pl. Use realized_pl_for_window() rather than pairing
    these two by hand.
    """
    try:
        # Every active bot's measurement, straight from the registry.
        measurements = ", ".join(sorted(MEASUREMENT_TO_BOT))
        query = f"SELECT * FROM {measurements} WHERE time > now() - {days}d"
        params = {'db': INFLUX_DB_NAME, 'q': query, 'epoch': 's'}
        response = requests.get(DB_QUERY_URL, params=params, timeout=5)
        data = response.json()
        
        all_trades = []
        if 'results' in data and 'series' in data['results'][0]:
            for series in data['results'][0]['series']:
                name = series['name'] # measurement name
                cols = series['columns']
                vals = series['values']
                df = pd.DataFrame(vals, columns=cols)
                df['bot_type'] = name
                all_trades.append(df)
        
# FILTER: Exclude empty DFs AND Drop columns that are all NaN
        valid_dfs = []
        for df in all_trades:
            if not df.empty:
                # Drop columns that have NO data (all NaNs)
                clean_df = df.dropna(axis=1, how='all')
                if not clean_df.empty:
                    valid_dfs.append(clean_df)
        
        if not valid_dfs: 
            return pd.DataFrame()
            
        # CONCAT: Ignore index to prevent alignment warnings
        return pd.concat(valid_dfs, ignore_index=True, sort=False)
    
    except Exception as e:
        logger.error(f"History Fetch Error: {e}")
        return pd.DataFrame()

# Actions that are position mutations with no counterparty price of their own
# (see utils.reconcile_option_events). They must never be paired as trades:
# an assignment is a state change, not a buy or a sell.
LIFECYCLE_ACTIONS = {"assigned", "exercised", "expired"}
# `grid_sweep` is a retired crypto_grid action, kept here because historical
# rows still carry it. It was a SELL, and the old substring test matched
# neither 'buy' nor 'sell' — so every sweep silently vanished from realized
# P&L while its proceeds stayed in the equity curve.
SELL_HINTS = ("sell", "sweep")
BUY_HINTS = ("buy",)
# OCC option symbol, e.g. PAAS260918P00015000. Anything else is an equity or
# crypto row, whatever measurement it landed in.
_OCC_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[PC]\d{8}$")


def _row_side(action):
    """'buy' / 'sell' / None for a trade-row action string."""
    act = str(action or "").strip().lower()
    if not act or act in LIFECYCLE_ACTIONS:
        return None
    if any(h in act for h in BUY_HINTS):
        return "buy"
    if any(h in act for h in SELL_HINTS):
        return "sell"
    return None


def calculate_realized_pl(df, window_days=PL_WINDOW_DAYS, now=None):
    """FIFO realized P&L per bot over the trailing `window_days`.

    Replaces an average-cost approximation that could not be right on any
    book that was not flat at both ends of the window. The old version took
    every buy in the window, averaged them into one cost, and multiplied that
    average by the total quantity sold — so:

      * a sell whose matching buy predated the window was costed at the
        average of UNRELATED later buys (or, with no buys in the window,
        contributed nothing at all);
      * inventory still held at the window's end was costed as though it had
        been sold, because the average absorbed it;
      * shorts were mispriced outright — the formula assumes buy-then-sell.

    crypto_grid, which turns inventory over hundreds of times a month and is
    never flat, is exactly the book that breaks worst. It is also the book the
    dashboard was reporting several thousand dollars of "profit" on.

    So: rows are replayed in time order through a per-(measurement, symbol)
    FIFO. Rows OLDER than the window build the opening inventory and book no
    profit; only closes that occur inside the window are counted, and each is
    matched against the basis it actually closed. Callers must therefore pass
    a frame covering `window_days + INVENTORY_LOOKBACK_DAYS` — a window-only
    frame silently reintroduces the missing-basis half of the bug.
    """
    scores = {}
    if df is None or df.empty:
        return scores
    if not {"action", "price", "qty", "time"}.issubset(df.columns):
        logger.error(f"[CFO] trade frame missing columns for FIFO P&L: {list(df.columns)}")
        return scores

    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=window_days)).timestamp()

    for measurement, group in df.groupby("bot_type"):
        bot_name = MEASUREMENT_TO_BOT.get(measurement)
        if not bot_name:
            continue  # Skip any measurements we don't recognize

        books = {}       # symbol -> [ {qty (signed), price} ], oldest first
        realized = 0.0
        for row in group.sort_values("time").itertuples(index=False):
            side = _row_side(getattr(row, "action", None))
            if side is None:
                continue
            try:
                price = float(row.price)
                qty = abs(float(row.qty))
                ts = float(row.time)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue

            in_window = ts >= cutoff
            symbol = str(getattr(row, "symbol", "") or "")
            # The multiplier comes from the INSTRUMENT, not the measurement.
            # wheel_trades holds both option premium (per share, against a
            # contract count) and the wheel's own STOCK — assigned shares, and
            # the covered-call underlying. Scaling by measurement multiplied
            # those share trades by 100: a 100-share PAAS buy at $48 sold at
            # $40 reported -$80,000 instead of -$800.
            multiplier = OPTION_CONTRACT_SIZE if _OCC_SYMBOL.match(symbol) else 1
            book = books.setdefault(symbol, [])
            remaining = qty if side == "buy" else -qty

            # Close against opposing lots first, then keep the remainder open.
            while abs(remaining) > 1e-12 and book and (book[0]["qty"] * remaining) < 0:
                lot = book[0]
                closed = min(abs(remaining), abs(lot["qty"]))
                if lot["qty"] > 0:                      # closing a long
                    pnl = (price - lot["price"]) * closed * multiplier
                    lot["qty"] -= closed
                    remaining += closed
                else:                                   # covering a short
                    pnl = (lot["price"] - price) * closed * multiplier
                    lot["qty"] += closed
                    remaining -= closed
                if in_window:
                    realized += pnl
                if abs(lot["qty"]) <= 1e-12:
                    book.pop(0)

            if abs(remaining) > 1e-12:
                book.append({"qty": remaining, "price": price})

        scores[bot_name] = realized

    return scores

def realized_pl_for_window(window_days=PL_WINDOW_DAYS):
    """Realized P&L per bot over `window_days`, with opening inventory.

    The single place the reporting window and the inventory lookback are
    paired. Both callers used to fetch exactly their window and get a book
    that started mid-position.
    """
    df = query_influx_trades(days=window_days + INVENTORY_LOOKBACK_DAYS)
    return calculate_realized_pl(df, window_days=window_days)


def log_metric(measurement, tags, fields):
    try:
        tag_str = ",".join([f"{k}={v}" for k, v in tags.items()])
        field_parts = []
        for k, v in fields.items():
            if isinstance(v, str): field_parts.append(f'{k}="{v}"')
            else: field_parts.append(f'{k}={v}')
        field_str = ",".join(field_parts)
        data_str = f"{measurement},{tag_str} {field_str}"
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=5)
    except Exception as e:
        logger.error(f"Influx Write Error: {e}")

def send_overseer(msg):
    """Fleet-level alert to the overseer Discord webhook (skipped if unconfigured)."""
    hook = getattr(config, "WEBHOOK_OVERSEER", "")
    if not hook or "YOUR" in hook:
        return
    try:
        requests.post(hook, json={"content": msg}, timeout=5)
    except Exception as e:
        logger.error(f"Overseer webhook failed: {e}")

def check_regime_freshness():
    """Alert if market_analyst hasn't written a fresh market_regime row lately.

    Cross-process backstop for the analyst's own in-process fail-safe: catches a
    wedged/dead analyst (or a sustained data-feed outage) that leaves the fleet
    trading on a frozen regime with the VIX>28 kill-switch running blind. Writes
    a regime_freshness metric every cycle (Grafana) and pings the overseer
    (throttled) once the newest row is older than REGIME_STALE_SECONDS.
    """
    global _last_regime_stale_alert
    try:
        params = {'db': INFLUX_DB_NAME,
                  'q': 'SELECT last(regime_score) FROM market_regime',
                  'epoch': 's'}
        resp = requests.get(DB_QUERY_URL, params=params, timeout=5)
        series = resp.json().get('results', [{}])[0].get('series')
        last_ts = series[0]['values'][0][0] if series else None

        now = time.time()
        age = None if last_ts is None else now - float(last_ts)
        is_stale = (age is None) or (age > REGIME_STALE_SECONDS)

        log_metric("regime_freshness", {"source": "market_regime"},
                   {"age_seconds": float(age) if age is not None else -1.0,
                    "stale": 1 if is_stale else 0})

        if is_stale and (now - _last_regime_stale_alert) > REGIME_STALE_ALERT_INTERVAL:
            _last_regime_stale_alert = now
            age_str = "no rows on record" if age is None else f"{age / 60:.0f} min old"
            logger.warning(f"[RegimeWatch] market_regime STALE: {age_str}")
            send_overseer(
                f"⚠️ **MARKET REGIME STALE**\n"
                f"Newest market_regime row is {age_str} "
                f"(threshold {REGIME_STALE_SECONDS // 60} min).\n"
                f"market_analyst may be wedged or its data feed is down — the "
                f"VIX>28 kill-switch is running blind. Check the analyst process.")
    except Exception as e:
        logger.error(f"[RegimeWatch] freshness check failed: {e}")

# Throttle orphan alerts so a persistently-orphaned position pings once, not
# every 5-minute accountant cycle.
_orphan_alert_times = {}
ORPHAN_ALERT_INTERVAL = 6 * 3600  # 6 hours

def _skip_orphan_sweep(reason):
    """Stand the sweep down for a cycle, visibly but without paging anyone.

    2026-07-15: three ReadTimeouts on the order fetch emptied the ownership
    map and the sweep alert-flagged the entire 9-position book as orphaned in
    one shot. An Alpaca outage must not masquerade as a fleet-wide orphan
    event — when order history is unfetchable, "not in the map" means
    "unknown", not "untagged". The skip is logged and metered (Grafana:
    orphan_sweep) so suppression during a storm is itself observable.
    """
    logger.warning(f"[Orphan] sweep SKIPPED this cycle — {reason}")
    log_metric("orphan_sweep", {"status": "skipped"},
               {"skipped": 1, "reason": reason.replace('"', "'")})

def detect_orphans(positions, trading_client):
    """Flag held positions that no bot is durably managing.

    Shares the resolver's ownership definition: a position is an orphan when
    the owner map has no claim on it (no tag, no option-root inference) —
    exactly when get_bot_owner returns None and no bot will trade it. Owned
    positions with an unresolvable entry time (e.g. assignment-created stock,
    which has no opening order) are NOT orphans — the owner manages them —
    but time-based backstops like max-hold are blind for them, so they emit
    an 'entry_time_missing' metric instead of an alert. Orphans alert
    (throttled) to the overseer webhook and log an 'orphan_position' metric.

    Fail-safe: the sweep only runs against a freshly-fetched map. If the
    order-history fetch failed (or the map was served from the stale
    fallback cache), the whole sweep — alerts AND metrics — stands down
    until order history is healthy again (see _skip_orphan_sweep).
    """
    from alpaca.trading.enums import AssetClass
    managed = [p for p in positions
               if p.asset_class in (AssetClass.US_EQUITY, AssetClass.US_OPTION)]
    if not managed:
        return
    syms = [p.symbol for p in managed]
    try:
        owner_map = utils._build_order_based_map(trading_client, held_symbols=syms)
        if utils.ownership_map_degraded() or not utils.order_history_healthy():
            # Don't launch the entry-times fetch into a known storm — it
            # would just burn another retries-x-timeout round to no purpose.
            _skip_orphan_sweep("order-history fetch failed; ownership served "
                               "from last-known-good cache")
            return
        entry_times = utils.get_position_entry_times(trading_client, held_symbols=syms)
        if not utils.order_history_healthy():
            # The map fetched clean but the entry-times read failed right
            # after it — same storm, partial data. Missing entries would read
            # as 'entry_time_missing' for the whole book; stand down instead.
            _skip_orphan_sweep("entry-time fetch failed after a clean "
                               "ownership fetch")
            return
    except utils.OrderFetchError as e:
        _skip_orphan_sweep(f"order-history fetch failed with no cached map ({e})")
        return
    except Exception as e:
        logger.error(f"[Orphan] detection failed: {e}")
        return

    now = time.time()
    # Throttle timestamps are only meaningful for positions we still hold;
    # without this the map keeps one entry per symbol ever orphaned, forever.
    held = {p.symbol for p in managed}
    for sym in list(_orphan_alert_times):
        if sym not in held:
            del _orphan_alert_times[sym]

    for p in managed:
        sym = p.symbol
        # Options may be tagged by full contract or by root symbol.
        root = sym
        if p.asset_class == AssetClass.US_OPTION:
            for i, ch in enumerate(sym):
                if ch.isdigit():
                    root = sym[:i]
                    break
        tagged = sym in owner_map or root in owner_map
        has_entry = sym in entry_times
        if tagged:
            if not has_entry:
                # Owned but no opening order to date it (assigned stock).
                # Metric only: ownership gates trading now, but max-hold /
                # stop timing can't see this position.
                log_metric("entry_time_missing", {"symbol": sym}, {"detected": 1})
            continue

        if now - _orphan_alert_times.get(sym, 0) < ORPHAN_ALERT_INTERVAL:
            continue
        _orphan_alert_times[sym] = now

        detail = "no bot tag in order history"
        logger.warning(f"[Orphan] {sym} side={p.side} qty={p.qty} — {detail}")
        log_metric("orphan_position", {"symbol": sym}, {"detected": 1, "reason": detail})
        try:
            upl = float(p.unrealized_pl)
        except Exception:
            upl = 0.0
        send_overseer(f"⚠️ **ORPHANED POSITION: {sym}**\n"
                      f"Side: {p.side} | Qty: {p.qty} | Unrealized: ${upl:.2f}\n"
                      f"Issue: {detail}\n"
                      f"Unowned — no bot will trade it. Review and close manually, "
                      f"or place a tagged order to claim it.")

bot_idle_cycles = {name: 0 for name in fleet_registry.BOTS}

def is_bot_gated(bot, regime, vix):
    """Determine if a bot is currently prohibited from entering new positions."""
    return fleet_registry.is_gated(bot, regime, vix)

def calculate_dynamic_allocations(equity, allocation_stats, regime, vix, config_data):
    """
    Executes the dynamic capital reallocation algorithm based on bot gating status
    and minimum reserve floors. Reallocates surplus capital to free bots.
    """
    import json
    
    cfo_settings = config_data.get("cfo_settings")
    if not cfo_settings or not cfo_settings.get("reallocation_enabled"):
        return None # Graceful fallback to static bot_config
        
    base = cfo_settings["base_allocations"]
    mins = cfo_settings["minimum_reserves"]
    priority = cfo_settings["reallocation_priority"]
    reserve = cfo_settings["unallocated_reserve"] * equity  
    allocatable_equity = equity - reserve
    
    bot_status = {}
    for bot in base.keys():
        gated = is_bot_gated(bot, regime, vix)
        if gated:
            bot_idle_cycles[bot] += 1
        else:
            bot_idle_cycles[bot] = 0
            
        bot_status[bot] = {
            "gated": gated,
            "idle_cycles": bot_idle_cycles[bot],
            "positions_held": allocation_stats.get(bot, 0.0), # Current locked capital
            "can_accept_capital": not gated
        }
    
    surplus = 0.0
    effective_alloc = {}
    
    # 1. Harvest Surplus from Gated Bots
    # Cycles a bot must sit gated before its capital is released (5-min
    # accountant cycles: 3 = 15 minutes). Read from cfo_settings so tuning
    # doesn't require a code change.
    threshold = cfo_settings.get("gate_idle_threshold_cycles", 3)
    for bot in base.keys():
        if bot_status[bot]["gated"] and bot_status[bot]["idle_cycles"] >= threshold:
            locked_capital = bot_status[bot]["positions_held"]
            # Floor is strictly the higher of its absolute minimum percentage, or its actual existing positions
            floor = max(mins.get(bot, 0) * allocatable_equity, locked_capital)
            released = (base.get(bot, 0) * allocatable_equity) - floor
            surplus += max(0, released)
            effective_alloc[bot] = floor
        else:
            effective_alloc[bot] = base.get(bot, 0) * allocatable_equity
            
    # 2. Distribute Surplus to Active Bots capped by velocity
    max_move = cfo_settings.get("reallocation_cap_per_cycle", 0.02) * equity
    distributable = min(surplus, max_move)
    remaining = distributable
    
    active_priority = [b for b in priority if bot_status.get(b, {}).get("can_accept_capital")]
    
    if active_priority and remaining > 0:
        for i, bot in enumerate(active_priority):
            if remaining <= 0: break
            
            if i == 0:
                share = remaining * 0.50
            elif i == 1:
                share = remaining * 0.30
            else:
                share = remaining * 0.20 / max(1, len(active_priority) - 2)
                
            effective_alloc[bot] = effective_alloc.get(bot, 0) + share
            remaining -= share

    # 3. Export Data for Fleet Consumption
    try:
        with open("effective_budgets.json", "w") as f:
            json.dump(effective_alloc, f, indent=4)
            
        budgets_log = " | ".join(f"{b}=${int(v)}" for b, v in effective_alloc.items())
        logger.info(f"[CFO] Effective budgets: {budgets_log}")
        logger.info(f"[CFO] Reserve: ${int(reserve)} | Surplus pool: ${int(surplus)} (Distributing: ${int(distributable)})")
    except Exception as e:
        logger.error(f"[CFO] Could not write effective_budgets.json: {e}")
        
    return effective_alloc


# Removed duplicated get_bot_owner. Accountant now uses utils.get_bot_owner directly.
def _has_impossible_basis(position):
    """True if a LONG position reports a negative cost basis.

    Shorts legitimately carry one (selling to open is a credit), so side is
    the whole question. Alpaca signs `qty` — negative for a short — and also
    exposes `side`; either is enough, and both are checked because option
    positions have been seen with one missing.
    """
    try:
        basis = float(position.cost_basis)
    except (TypeError, ValueError, AttributeError):
        return False
    if basis >= 0:
        return False

    side = str(getattr(getattr(position, "side", ""), "value",
                       getattr(position, "side", ""))).lower()
    if side == "short":
        return False
    try:
        if float(position.qty) < 0:
            return False
    except (TypeError, ValueError, AttributeError):
        pass
    return True


def build_bot_performance_point(bot, realized_pl, unrealized_pl, allocation, suspect):
    """The bot_performance tags+fields for one bot. Pure, so it can be tested.

    An unrealized figure derived from an impossible cost basis is not
    published, because it is not a measurement (rule 24).

    Flagging it was not enough: the accounting_anomaly row said something was
    wrong while bot_performance went on reporting crypto_grid at +$5,027.81
    unrealized / +$5,038.94 total off a -$1,495 cost basis on 1.4156 ETH.
    Whoever reads the dashboard reads the number, not the flag beside it.

    The whole bot is withheld, not just the bad position's share: netting the
    suspect leg out and publishing the rest under the same field name is the
    proxy rule 24 forbids — a partial sum that still reads as this bot's P&L.

    realized_pl and allocation SURVIVE and are always written. Realized is
    FIFO over confirmed fills (calculate_realized_pl); allocation is
    |market_value|. Neither reads avg_entry_price, so the bad basis cannot
    reach them.

    pl_suspect is written on EVERY row, true and false alike, so a panel can
    filter on it and so "clean" is visible rather than merely implied by
    absence — the same reason accounting_anomaly is written with a zero count.
    """
    fields = {"allocation": allocation, "realized_pl": realized_pl}
    if not suspect:
        fields["unrealized_pl"] = unrealized_pl
        fields["total_pl"] = realized_pl + unrealized_pl
    tags = {"bot": bot, "pl_suspect": "true" if suspect else "false"}
    return tags, fields


def run_accountant():
    import json
    global _last_strategy_advisor_run
    logger.info("--- 🧾 SMART ACCOUNTANT (Condor Aware) STARTED ---")

    while True:
        try:
            # 0. RECONCILE FILLS — wheel's LIMIT options fill asynchronously, and
            #    equity MARKET orders can finish filling after submit_and_log_order's
            #    poll window; pull the authoritative fills from Alpaca so the trade
            #    measurements reflect real price/qty/time, not submit-time phantoms.
            try:
                utils.reconcile_fills(trading_client, logger)
            except Exception as e:
                logger.error(f"[Reconcile] fill reconciliation failed: {e}")

            # 0b. OPTION LIFECYCLE EVENTS — assignments/exercises/expirations
            #     are position mutations with no order behind them; pull them
            #     from account activities so the wheel's book reflects them
            #     (the 2026-07-02 PAAS assignment arrived invisible).
            try:
                utils.reconcile_option_events(logger, alert=send_overseer)
            except Exception as e:
                logger.error(f"[OptionEvents] activity reconciliation failed: {e}")

            # 0c. REGIME FRESHNESS WATCHDOG — alert if market_analyst's regime
            #     feed has gone stale (VIX kill-switch running blind).
            #     Best-effort: never let it stall the accountant cycle.
            try:
                check_regime_freshness()
            except Exception as e:
                logger.error(f"[RegimeWatch] watchdog error: {e}")

            # 1. FETCH REALIZED P&L (HISTORY)
            realized_scores = realized_pl_for_window(PL_WINDOW_DAYS)
            
            # 2. FETCH UNREALIZED P&L (LIVE)
            positions = trading_client.get_all_positions()
            account = trading_client.get_account()

            # 2b. ORPHAN SWEEP — alert on any held position no bot is durably
            #     managing (untagged / no resolvable entry time). Best-effort:
            #     never let a detection error stall the accountant cycle.
            try:
                detect_orphans(positions, trading_client)
            except Exception as e:
                logger.error(f"[Orphan] sweep error: {e}")

            # One P&L bucket per registry bot. (moon_bot's realized breakout
            # P&L reports here even though crypto positions resolve to
            # crypto_grid in ownership.)
            unrealized_stats = {name: 0.0 for name in fleet_registry.BOTS}
            allocation_stats = unrealized_stats.copy()
            # Positions whose cost basis is impossible for their SIDE.
            #
            # A negative cost basis is perfectly normal on a SHORT: selling to
            # open is a credit, so every wheel_bot short put and every
            # trend_bot short carries one. Flagging on sign alone therefore
            # called routine premium selling an anomaly, which is worse than
            # not checking — a detector that fires on normal operation trains
            # you to ignore it (see rule 10).
            #
            # The real anomaly is a LONG position with a negative basis: you
            # cannot pay a negative price for something you own. On 2026-09-11
            # long ETH and SOL carried roughly -$1,196 and -$65, yielding
            # ~$4,914 of "unrealized crypto profit" on ~$3,652 of market value,
            # which is most of why the advisor wanted to move capital into the
            # grid. The upstream cause is still unresolved — so the affected
            # STRATEGIES are dropped from scoring, not merely annotated.
            negative_basis_positions = []
            anomalous_bots = set()

            for p in positions:
                from utils import get_bot_owner
                owner = get_bot_owner(p.symbol, p.asset_class, trading_client)
                if _has_impossible_basis(p):
                    negative_basis_positions.append(str(p.symbol))
                    if owner:
                        anomalous_bots.add(owner)
                if owner in unrealized_stats:
                    unrealized_stats[owner] += float(p.unrealized_pl)
                    
                    if p.asset_class == AssetClass.US_OPTION and float(p.qty) < 0:
                        import re
                        match = re.match(r"^[A-Z]{1,6}\d{6}(P|C)(\d{8})$", p.symbol)
                        if match and match.group(1) == 'P':
                            strike = float(match.group(2)) / 1000
                            allocation_stats[owner] += strike * abs(float(p.qty)) * 100
                        else:
                            allocation_stats[owner] += abs(float(p.market_value))
                    else:
                        allocation_stats[owner] += abs(float(p.market_value))

            # 3. COMBINE & REPORT
            # print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] TRUE P&L UPDATE:")
            
            for bot in unrealized_stats.keys():
                r_pl = realized_scores.get(bot, 0.0)
                u_pl = unrealized_stats[bot]
                suspect = bot in anomalous_bots
                if suspect:
                    logger.warning(f"[CFO] {bot}: unrealized/total P&L WITHHELD from "
                                   f"bot_performance — it owns a long position with a "
                                   f"negative cost basis. realized_pl and allocation "
                                   f"are unaffected.")
                tags, fields = build_bot_performance_point(
                    bot, r_pl, u_pl, allocation_stats[bot], suspect)
                log_metric(measurement="bot_performance", tags=tags, fields=fields)

            # --- PHASE 23C: CFO DYNAMIC REALLOCATION ---
            try:
                with open("bot_config.json", "r") as f:
                    config_data = json.load(f)
                    
                if "global_settings" not in config_data:
                    config_data["global_settings"] = {}
                    
                regime = config_data["global_settings"].get("market_condition", "SIDEWAYS")
                vix = config_data["global_settings"].get("vix", 15.0)
                current_crunch = config_data["global_settings"].get("CAPITAL_CRUNCH", False)
                
                total_equity = float(account.equity)
                total_committed = sum(allocation_stats.values())
                utilization = total_committed / total_equity if total_equity > 0 else 0
                
                if utilization > 0.90:
                    if not current_crunch:
                        logger.warning(f"[CFO] CAPITAL_CRUNCH ACTIVATED! Utilization: {utilization*100:.1f}% > 90%")
                        config_data["global_settings"]["CAPITAL_CRUNCH"] = True
                        with open("bot_config.json", "w") as f:
                            json.dump(config_data, f, indent=4)
                elif utilization < 0.80:
                    if current_crunch:
                        logger.info(f"[CFO] CAPITAL_CRUNCH LIFTED! Utilization: {utilization*100:.1f}% < 80%")
                        config_data["global_settings"]["CAPITAL_CRUNCH"] = False
                        with open("bot_config.json", "w") as f:
                            json.dump(config_data, f, indent=4)
                
                calculate_dynamic_allocations(total_equity, allocation_stats, regime, vix, config_data)

                if time.time() - _last_strategy_advisor_run > STRATEGY_ADVISOR_INTERVAL:
                    try:
                        try:
                            advisor_influx_realized = realized_pl_for_window(
                                ADVISOR_COMPARE_DAYS)
                        except Exception as influx_err:
                            logger.warning(f"[StrategyAdvisor] {ADVISOR_COMPARE_DAYS}d "
                                           f"influx compare unavailable: {influx_err}")
                            advisor_influx_realized = {}
                        strategy_advisor.generate_and_write_report(
                            trading_client=trading_client,
                            unrealized_by_bot=unrealized_stats,
                            allocation_by_bot=allocation_stats,
                            equity=total_equity,
                            config_data=config_data,
                            logger=logger,
                            influx_realized_by_bot=advisor_influx_realized,
                            influx_window_days=ADVISOR_COMPARE_DAYS,
                            negative_basis_positions=negative_basis_positions,
                            excluded_bots=sorted(anomalous_bots),
                        )
                        _last_strategy_advisor_run = time.time()
                    except Exception as advisor_err:
                        logger.error(f"[StrategyAdvisor] recommendation failed: {advisor_err}")
            except Exception as e:
                logger.error(f"[CFO] Reallocation and Utilization process failed: {e}")
            
            if negative_basis_positions:
                logger.warning(f"[CFO] {len(negative_basis_positions)} LONG position(s) with a "
                               f"NEGATIVE cost basis: {', '.join(negative_basis_positions)} — "
                               f"their unrealized P&L is not trustworthy. Excluded from "
                               f"scoring: {', '.join(sorted(anomalous_bots)) or 'none'}.")
            # Written every cycle, zero included. A metric that only appears
            # while something is wrong can never show that it CLEARED — the
            # series just stops, which is indistinguishable from the writer
            # dying.
            log_metric("accounting_anomaly", {"kind": "negative_long_cost_basis"},
                       {"count": len(negative_basis_positions),
                        "affected_bots": len(anomalous_bots)})

            # Log Global Stats
            log_metric("account_stats", {"type": "global"}, {
                "equity": float(account.equity),
                "cash": float(account.cash),
                "buying_power": float(account.buying_power)
            })

            time.sleep(300) # 5 minutes

        except Exception as e:
            logger.error(f"Accountant Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_accountant()
