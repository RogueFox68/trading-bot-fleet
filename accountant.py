import config
import time
import datetime
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass
from logger import logger


# --- CONFIGURATION ---
# We verify these against the specific bot scripts to ensure correct attribution
BOT_MAPPING = {
    "survivor": [],
    "wheel": [], 
    "condor": [],
    "crypto": ["BTC/USD", "ETH/USD", "SOL/USD"]
}

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
DB_QUERY_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/query"

# --- CLIENT ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

def query_influx_trades(days=30):
    """Fetches trade history from InfluxDB to calculate Realized P&L."""
    try:
        # UPDATED: Added 'condor_trades' to the query list
        query = f"SELECT * FROM trades, crypto_trades, survivor_trades, wheel_trades, condor_trades WHERE time > now() - {days}d"
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
            
        # --- MAPPING: Measurement Name -> Bot Name ---
        bot_name = "trend_bot" # default
        if bot == "crypto_trades": bot_name = "crypto_grid"
        elif bot == "survivor_trades": bot_name = "survivor_bot"
        elif bot == "wheel_trades": bot_name = "wheel_bot"
        elif bot == "condor_trades": bot_name = "condor_bot" # <--- NEW
            
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

# Removed duplicated get_bot_owner. Accountant now uses utils.get_bot_owner directly.
def run_accountant():
    logger.info("--- 🧾 SMART ACCOUNTANT (Condor Aware) STARTED ---")

    while True:
        try:
            # 1. FETCH REALIZED P&L (HISTORY)
            history_df = query_influx_trades()
            realized_scores = calculate_realized_pl(history_df)
            
            # 2. FETCH UNREALIZED P&L (LIVE)
            positions = trading_client.get_all_positions()
            account = trading_client.get_account()
            
            unrealized_stats = {
                "survivor_bot": 0.0, "trend_bot": 0.0, 
                "wheel_bot": 0.0, "crypto_grid": 0.0,
                "condor_bot": 0.0 # <--- NEW
            }
            allocation_stats = unrealized_stats.copy()

            for p in positions:
                from utils import get_bot_owner
                owner = get_bot_owner(p.symbol, p.asset_class, trading_client)
                if owner in unrealized_stats:
                    unrealized_stats[owner] += float(p.unrealized_pl)
                    allocation_stats[owner] += float(p.market_value)

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