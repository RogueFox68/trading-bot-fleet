import config
import time
import json
import requests
import pandas as pd
import datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
CHECK_INTERVAL = 3600  # Run hourly
TARGET_FILE = "active_targets.json"

# --- THE MAP: Generals -> Soldiers ---
# If the ETF (Key) moves, we activate the Stocks (Values)
SECTOR_MAP = {
    "XLK": ["NVDA", "AMD", "MSFT", "PLTR"],    # Tech -> Semis/Software
    "XLE": ["XOM", "CVX", "OXY"],              # Energy -> Oil Majors
    "XLF": ["JPM", "BAC", "GS"],               # Financials -> Banks
    "XLV": ["LLY", "UNH", "PFE"],              # Healthcare -> Pharma
    "GLD": ["GLD", "NEM", "GOLD"],             # Gold -> Miners
    "SLV": ["SLV", "AG", "PAAS"],              # Silver -> Miners
    "BITO": ["BITI", "MSTR", "COIN", "MARA", "CLSK"],         # Crypto -> Proxies
    "XBI": ["LABU", "XBI"],                    # Biotech (High Vol)
    "SMH": ["SOXL", "NVDA", "TSM"]             # Semis (High Vol)
}

# --- THRESHOLDS ---
MOMENTUM_THRESHOLD = 0.02  # 2% Daily Move triggers activation
VOLATILITY_THRESHOLD = 0.03 # 3% Intra-day range triggers activation

# --- CLIENT ---
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def log_scout_activity(sector, move_pct, status):
    try:
        data_str = f'sector_scout,sector={sector} move_pct={move_pct},status="{status}"'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=2)
    except: pass

def update_targets(active_list):
    """Writes the approved hit list to a file."""
    try:
        # Always keep a "Base List" of high-quality tickers that are always active
        base_list = ["SPY", "QQQ", "IWM"] 
        final_list = list(set(base_list + active_list))
        
        with open(TARGET_FILE, 'w') as f:
            json.dump({"targets": final_list, "updated": str(datetime.datetime.now())}, f)
        print(f"  -> 🎯 Updated Target List: {len(final_list)} symbols")
    except Exception as e:
        print(f"Error writing targets: {e}")

def run_scout():
    print("--- 🔭 SECTOR SCOUT (Reconnaissance) STARTED ---")
    
    while True:
        try:
            now = datetime.datetime.now()
            if now.hour < 8 or now.hour > 17:
                print("Sleeping until market hours...")
                time.sleep(3600)
                continue

            print(f"\n[{now.strftime('%H:%M')}] Scanning Sectors...")
            active_symbols = []

            # Bulk fetch data for all ETFs
            etfs = list(SECTOR_MAP.keys())
            start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
            
            req = StockBarsRequest(
                symbol_or_symbols=etfs,
                timeframe=TimeFrame.Day,
                start=start_time,
                limit=5
            )
            bars = data_client.get_stock_bars(req)
            
            for etf in etfs:
                if etf not in bars.df.index.get_level_values(0): continue
                
                df = bars.df.xs(etf)
                if df.empty: continue
                
                # Calculate Daily Move (Today vs Yesterday Close)
                last_close = df['close'].iloc[-1]
                prev_close = df['close'].iloc[-2]
                move_pct = (last_close - prev_close) / prev_close
                
                # Logic: Is this sector 'In Play'?
                is_active = False
                reason = ""
                
                if abs(move_pct) >= MOMENTUM_THRESHOLD:
                    is_active = True
                    reason = "Big Move"
                
                print(f"  {etf:<4} | Move: {move_pct*100:>5.2f}% | {'🔥 HOT' if is_active else 'zzz'}")
                
                log_scout_activity(etf, move_pct, "Active" if is_active else "Inactive")

                if is_active:
                    soldiers = SECTOR_MAP.get(etf, [])
                    print(f"    -> Activating: {soldiers}")
                    active_symbols.extend(soldiers)

            # Update the shared file
            update_targets(active_symbols)
            
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"Scout Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_scout()