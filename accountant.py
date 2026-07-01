import config
import time
import datetime
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from logger import logger
import utils


# --- CONFIGURATION ---
# Ownership/attribution is resolved via utils.get_bot_owner (imported below).
# The old local BOT_MAPPING copy was removed to prevent key-divergence regressions.

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
DB_QUERY_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/query"

# Options measurements log per-share premium and contract qty, so their realized
# P&L must be scaled by the 100-share contract multiplier to express dollars.
OPTION_MEASUREMENTS = {"wheel_trades", "condor_trades"}
OPTION_CONTRACT_SIZE = 100

# --- CLIENT ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

def query_influx_trades(days=30):
    """Fetches trade history from InfluxDB to calculate Realized P&L."""
    try:
        # Includes breakout_trades (moon_bot) so its realized P&L is attributed.
        query = f"SELECT * FROM trades, crypto_trades, survivor_trades, wheel_trades, condor_trades, breakout_trades WHERE time > now() - {days}d"
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

        # --- MAPPING: Measurement Name -> Bot Name ---
        mapping = {
            "trades": "trend_bot",
            "crypto_trades": "crypto_grid",
            "survivor_trades": "survivor_bot",
            "wheel_trades": "wheel_bot",
            "condor_trades": "condor_bot",
            "breakout_trades": "moon_bot"
        }
        bot_name = mapping.get(bot)
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

bot_idle_cycles = {
    "trend_bot": 0, "survivor_bot": 0, "wheel_bot": 0, 
    "crypto_grid": 0, "moon_bot": 0, "condor_bot": 0
}

def is_bot_gated(bot, regime, vix):
    """Determine if a bot is currently prohibited from entering new positions."""
    if bot == "wheel_bot":
        return regime in ["BEAR_TREND", "CRITICAL_VOLATILITY"] or vix > 22
    elif bot == "crypto_grid":
        return regime in ["BEAR_TREND", "CRITICAL_VOLATILITY"]
    return False

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
    for bot in base.keys():
        threshold = 3 # Release capital much faster (from 50 mins down to 15 mins)
        
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

            # 1. FETCH REALIZED P&L (HISTORY)
            history_df = query_influx_trades()
            realized_scores = calculate_realized_pl(history_df)
            
            # 2. FETCH UNREALIZED P&L (LIVE)
            positions = trading_client.get_all_positions()
            account = trading_client.get_account()
            
            unrealized_stats = {
                "survivor_bot": 0.0, "trend_bot": 0.0,
                "wheel_bot": 0.0, "crypto_grid": 0.0,
                "condor_bot": 0.0,
                "moon_bot": 0.0  # realized breakout P&L reported here (crypto positions still owned by crypto_grid)
            }
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