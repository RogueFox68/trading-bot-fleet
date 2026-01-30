import config
import time
import datetime
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass

# --- CONFIGURATION ---
# We verify these against the specific bot scripts to ensure correct attribution
BOT_MAPPING = {
    "survivor": ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"],
    "wheel": ["DIS", "F", "PLTR"], 
    "condor": ["COIN", "MSTR", "TSLA", "NVDA", "NFLX"],
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
        
        if not all_trades: return pd.DataFrame()
        return pd.concat(all_trades)
    except Exception as e:
        print(f"[!] History Fetch Error: {e}")
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
        print(f"[!] Influx Write Error: {e}")

def get_bot_owner(symbol, asset_class):
    """
    Decides which bot owns a specific live position.
    """
    # 1. Crypto is easy
    if asset_class == AssetClass.CRYPTO: return "crypto_grid"
    
    # 2. Options: Distinguish between Wheel and Condor
    if asset_class == AssetClass.US_OPTION:
        # Check the underlying symbol (e.g. "TSLA230120..." -> "TSLA")
        # Alpaca symbols for options are standard, but the 'symbol' arg passed here is usually the contract name
        # We need to extract the root. However, for simple mapping:
        
        root = symbol
        # Basic attempt to find root if it's a contract string (Alpaca sometimes gives the root in a separate field, 
        # but here we rely on the input). If 'symbol' is "TSLA", it works. 
        # If 'symbol' is "TSLA240119P00200000", we check if it starts with the ticker.
        
        for ticker in BOT_MAPPING["wheel"]:
            if symbol.startswith(ticker): return "wheel_bot"
            
        for ticker in BOT_MAPPING["condor"]:
            if symbol.startswith(ticker): return "condor_bot"
            
        return "condor_bot" # Default aggressive options to Condor if unknown
    
    # 3. Stocks (Equity)
    if symbol in BOT_MAPPING["survivor"]: return "survivor_bot"
    
    # Wheel bot sometimes holds stock (assignment), but usually sells covered calls.
    # If we hold stock in DIS/F/PLTR, it's likely Wheel Bot (unless Trend Bot went rogue).
    # Since Trend Bot doesn't trade DIS/F, this is safe.
    if symbol in BOT_MAPPING["wheel"]: return "wheel_bot"
    
    # Trend Bot takes the rest (NVDA, TSLA shares, etc.)
    return "trend_bot"

def run_accountant():
    print("--- 🧾 SMART ACCOUNTANT (Condor Aware) STARTED ---")

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
                owner = get_bot_owner(p.symbol, p.asset_class)
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
            print(f"[!] Accountant Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_accountant()