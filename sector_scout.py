import config
import time
import json
import requests
import pandas as pd
import pandas_ta as ta
import datetime
import math
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
CHECK_INTERVAL = 3600  # Run hourly
TARGET_FILE = "active_targets.json"
MAX_TARGETS = 15       # Only send the Top 15 most volatile stocks to the fleet
MIN_PRICE = 20.0       # Avoid penny stocks
MIN_VOLUME = 1000000   # Liquidity filter

# --- CLIENT ---
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def get_sp500_tickers():
    """Scrapes Wikipedia for the current S&P 500 list."""
    try:
        url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        tables = pd.read_html(url)
        df = tables[0]
        tickers = df['Symbol'].tolist()
        # Clean tickers (e.g., BRK.B -> BRK-B for some APIs, but Alpaca usually likes standardized)
        return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"Error fetching S&P 500: {e}")
        # Fallback list if wiki fails
        return ["NVDA", "TSLA", "AMD", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NFLX", "COIN"]

def get_volatility_rank(tickers):
    """Fetches data and calculates volatility (NATR)."""
    print(f"  -> Analyzing {len(tickers)} tickers for volatility...")
    
    ranked_list = []
    
    # Chunking to respect API limits (Alpaca handles multi-symbol well, but let's be safe on the Pi)
    chunk_size = 50
    start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)

    for i in range(0, len(tickers), chunk_size):
        chunk = tickers[i:i+chunk_size]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=chunk,
                timeframe=TimeFrame.Day,
                start=start_time,
                limit=10,
                adjustment='all'
            )
            bars = data_client.get_stock_bars(req)
            
            for symbol in chunk:
                if symbol not in bars.df.index.get_level_values(0): continue
                
                df = bars.df.xs(symbol)
                if df.empty or len(df) < 10: continue
                
                # Check Liquidity (Volume of last bar)
                if df['volume'].iloc[-1] < MIN_VOLUME: continue
                if df['close'].iloc[-1] < MIN_PRICE: continue

                # Calculate NATR (Normalized Average True Range)
                # This gives us volatility as a % of price, allowing fair comparison between stocks
                df.ta.natr(length=7, append=True)
                
                # Get latest NATR
                natr_col = f"NATR_7"
                if natr_col in df.columns:
                    vol_score = df[natr_col].iloc[-1]
                    ranked_list.append((symbol, vol_score))
                    
        except Exception as e:
            print(f"    Error processing chunk {i}: {e}")
            continue
            
    # Sort by Volatility (Highest First)
    ranked_list.sort(key=lambda x: x[1], reverse=True)
    return ranked_list[:MAX_TARGETS]

def update_targets(active_tuples):
    """Writes the approved hit list."""
    try:
        # Extract just the symbols
        targets = [x[0] for x in active_tuples]
        
        # Always keep Inverse ETFs for hedging in a crash
        hedges = ["SQQQ", "SPXU", "SOXS"]
        final_list = list(set(targets + hedges))
        
        with open(TARGET_FILE, 'w') as f:
            json.dump({
                "targets": final_list, 
                "details": active_tuples, # Save scores for debugging
                "updated": str(datetime.datetime.now())
            }, f)
        print(f"  -> 🎯 Updated Targets: {final_list}")
    except Exception as e:
        print(f"Error writing targets: {e}")

def run_scout():
    print("--- 🔭 SECTOR SCOUT (S&P 500 Volatility Mode) STARTED ---")
    
    while True:
        try:
            now = datetime.datetime.now()
            # Market hours check (extended slightly)
            if now.hour < 7 or now.hour > 19:
                print("Sleeping until market hours...", end='\r')
                time.sleep(60)
                continue

            print(f"\n[{now.strftime('%H:%M')}] Scouring S&P 500...")
            
            tickers = get_sp500_tickers()
            top_volatile = get_volatility_rank(tickers)
            
            print("  🔥 Top Volatility Targets:")
            for sym, score in top_volatile:
                print(f"     {sym:<5} | Volatility: {score:.2f}%")

            update_targets(top_volatile)
            
            # Sleep 1 hour (Analysis is heavy)
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"Scout Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_scout()