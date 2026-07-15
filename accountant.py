import config
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

def query_influx_trades(days=30):
    """Fetches trade history from InfluxDB to calculate Realized P&L."""
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

def calculate_realized_pl(df):
    """
    Calculates Closed Trade P&L.
    """
    scores = {}
    if df.empty: return scores

    # Group by Bot Measurement Name
    for bot, group in df.groupby('bot_type'):
        # Filter for entry/exit actions (flexible for various bot log formats)
        buys = group[group['action'].str.contains('buy', case=False)]
        sells = group[group['action'].str.contains('sell', case=False)]
        
        buy_val = (buys['price'] * buys['qty']).sum() if 'qty' in buys else 0
        sell_val = (sells['price'] * sells['qty']).sum() if 'qty' in sells else 0
        
        # Simple approximation for Realized P&L
        # (Total Sold Value - Cost Basis of Sold Units)
        total_sold = sells['qty'].sum() if 'qty' in sells else 0
        total_bought = buys['qty'].sum() if 'qty' in buys else 0
        
        realized = 0.0
        if total_bought > 0 and total_sold > 0:
            avg_cost = buy_val / total_bought
            cost_of_sold = avg_cost * total_sold
            realized = sell_val - cost_of_sold

        # Options are 100 shares/contract; scale per-share premium P&L to dollars.
        if bot in OPTION_MEASUREMENTS:
            realized *= OPTION_CONTRACT_SIZE

        # --- MAPPING: Measurement Name -> Bot Name (from fleet_registry) ---
        bot_name = MEASUREMENT_TO_BOT.get(bot)
        if not bot_name:
            continue # Skip any measurements we don't recognize
            
        scores[bot_name] = realized

    return scores

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
            history_df = query_influx_trades()
            realized_scores = calculate_realized_pl(history_df)
            
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

            for p in positions:
                from utils import get_bot_owner
                owner = get_bot_owner(p.symbol, p.asset_class, trading_client)
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
                total_pl = r_pl + u_pl
                
                # print(f"  {bot:<15} | Real: ${r_pl:>7.2f} | Paper: ${u_pl:>7.2f} | TOTAL: ${total_pl:>7.2f}")
                
                log_metric(
                    measurement="bot_performance",
                    tags={"bot": bot},
                    fields={
                        "allocation": allocation_stats[bot],
                        "unrealized_pl": u_pl,
                        "realized_pl": r_pl,
                        "total_pl": total_pl
                    }
                )
            
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
                            advisor_influx_realized = calculate_realized_pl(
                                query_influx_trades(days=ADVISOR_COMPARE_DAYS))
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
                        )
                        _last_strategy_advisor_run = time.time()
                    except Exception as advisor_err:
                        logger.error(f"[StrategyAdvisor] recommendation failed: {advisor_err}")
            except Exception as e:
                logger.error(f"[CFO] Reallocation and Utilization process failed: {e}")
            
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
