import config
import time
import datetime
import requests
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass

# --- CONFIGURATION ---
BOT_MAPPING = {
    "survivor": ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"],
    "wheel": ["DIS", "F", "PLTR"], 
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
        query = f"SELECT * FROM trades, crypto_trades, survivor_trades, wheel_trades WHERE time > now() - {days}d"
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
    Simple approximation: Sum(Sell_Val) - Sum(Buy_Val) per bot.
    Note: This assumes we eventually close positions. For a running bot,
    FIFO is better, but this is efficient for the Pi.
    """
    scores = {}
    
    if df.empty: return scores

    # Group by Bot
    for bot, group in df.groupby('bot_type'):
        # Filter for entry/exit actions
        buys = group[group['action'].str.contains('buy')]
        sells = group[group['action'].str.contains('sell')]
        
        buy_val = (buys['price'] * buys['qty']).sum()
        sell_val = (sells['price'] * sells['qty']).sum()
        
        # This raw metric needs to be adjusted by net quantity change 
        # to avoid counting "Open Position Cost" as a "Realized Loss"
        # Adjusted Realized P&L = Total Sell Val - (Avg Cost * Qty Sold)
        
        total_sold = sells['qty'].sum()
        total_bought = buys['qty'].sum()
        
        if total_bought > 0:
            avg_cost = buy_val / total_bought
            # Cost of the specific units we sold
            cost_of_sold = avg_cost * total_sold
            realized = sell_val - cost_of_sold
        else:
            realized = 0.0
            
        # Map measurement name to bot name
        bot_name = "trend_bot" # default
        if bot == "crypto_trades": bot_name = "crypto_grid"
        elif bot == "survivor_trades": bot_name = "survivor_bot"
        elif bot == "wheel_trades": bot_name = "wheel_bot"
            
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
    if asset_class == AssetClass.CRYPTO: return "crypto_grid"
    if asset_class == AssetClass.US_OPTION: return "wheel_bot"
    if symbol in BOT_MAPPING["survivor"]: return "survivor_bot"
    if symbol in BOT_MAPPING["wheel"]: return "wheel_bot"
    return "trend_bot"

def run_accountant():
    print("--- 🧾 SMART ACCOUNTANT (Realized + Unrealized) STARTED ---")

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
                "wheel_bot": 0.0, "crypto_grid": 0.0
            }
            allocation_stats = unrealized_stats.copy()

            for p in positions:
                owner = get_bot_owner(p.symbol, p.asset_class)
                if owner in unrealized_stats:
                    unrealized_stats[owner] += float(p.unrealized_pl)
                    allocation_stats[owner] += float(p.market_value)

            # 3. COMBINE & REPORT
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] TRUE P&L UPDATE:")
            
            for bot in unrealized_stats.keys():
                r_pl = realized_scores.get(bot, 0.0)
                u_pl = unrealized_stats[bot]
                total_pl = r_pl + u_pl
                
                print(f"  {bot:<15} | Real: ${r_pl:>7.2f} | Paper: ${u_pl:>7.2f} | TOTAL: ${total_pl:>7.2f}")
                
                log_metric(
                    measurement="bot_performance",
                    tags={"bot": bot},
                    fields={
                        "allocation": allocation_stats[bot],
                        "unrealized_pl": u_pl,
                        "realized_pl": r_pl,
                        "total_pl": total_pl  # <--- NEW METRIC
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