import pandas as pd
import requests
import io
import config
from datetime import datetime

# Configuration
DB_URL = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/query"
DB_NAME = config.INFLUX_DB_NAME

def query_influx(query):
    params = {'db': DB_NAME, 'q': query, 'epoch': 's'}
    try:
        response = requests.get(DB_URL, params=params)
        data = response.json()
        if 'results' in data and 'series' in data['results'][0]:
            series = data['results'][0]['series'][0]
            columns = series['columns']
            values = series['values']
            df = pd.DataFrame(values, columns=columns)
            # Convert time to readable format
            df['time'] = pd.to_datetime(df['time'], unit='s')
            # Adjust timezone to US/Eastern (approximate for viewing)
            df['time'] = df['time'] - pd.Timedelta(hours=5) 
            return df
        return pd.DataFrame()
    except Exception as e:
        print(f"Error querying {query}: {e}")
        return pd.DataFrame()

print(f"--- 📊 EXPORTING DATA FROM {config.INFLUX_HOST} ---")

# 1. Get Trade History (All Bots)
print("1. Fetching Trade History...")
measurements = ["trades", "crypto_trades", "survivor_trades", "breakout_trades", "wheel_trades"]
all_trades = []

for m in measurements:
    df = query_influx(f"SELECT * FROM {m} WHERE time > now() - 7d")
    if not df.empty:
        df['bot_type'] = m
        all_trades.append(df)

if all_trades:
    final_trades = pd.concat(all_trades).sort_values(by='time', ascending=False)
    # Reorder columns for readability
    cols = ['time', 'symbol', 'action', 'price', 'qty', 'bot_type']
    # Add any extra columns that exist
    remaining = [c for c in final_trades.columns if c not in cols]
    final_trades = final_trades[cols + remaining]
    
    filename = f"trade_history_{datetime.now().strftime('%Y%m%d')}.csv"
    final_trades.to_csv(filename, index=False)
    print(f"✅ Saved {len(final_trades)} trades to: {filename}")
else:
    print("⚠️ No trades found in the last 7 days.")

# 2. Get Performance Snapshot (Last 24h)
print("2. Fetching Performance Stats...")
perf_df = query_influx("SELECT * FROM bot_performance WHERE time > now() - 1d")
if not perf_df.empty:
    filename_perf = f"bot_performance_{datetime.now().strftime('%Y%m%d')}.csv"
    perf_df.sort_values(by='time', ascending=False).to_csv(filename_perf, index=False)
    print(f"✅ Saved performance logs to: {filename_perf}")
else:
    print("⚠️ No performance data found.")

print("--- DONE ---")