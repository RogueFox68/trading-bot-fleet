# sector_scout.py
import config
import time
import json
import yfinance as yf
import pandas_ta as ta
import datetime

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"

# --- THE UNIVERSES ---
# 1. IRON CONDOR: Indices & Boring Giants (Low Vol, Sideways)
UNIVERSE_CONDOR = ["SPY", "QQQ", "IWM", "DIA", "TLT", "KO", "MCD", "JNJ", "PG", "WM"]

# 2. THE WHEEL: Stocks you want to own (Moderate Vol, Liquidity)
UNIVERSE_WHEEL = ["AAPL", "AMD", "F", "T", "PLTR", "SOFI", "BAC", "PFE", "DIS", "AMZN"]

# 3. TREND / SURVIVOR: High Beta Soldiers (Momentum)
UNIVERSE_TREND = ["NVDA", "TSLA", "MSTR", "COIN", "SMCI", "META", "NFLX"]

def get_technical_status(tickers):
    """Scans a list of tickers and returns metrics."""
    results = {}
    try:
        # Bulk download for speed
        data = yf.download(tickers, period="60d", interval="1d", group_by='ticker', progress=False)
        
        for ticker in tickers:
            try:
                df = data[ticker].dropna()
                if len(df) < 50: continue
                
                # Indicators
                adx = ta.adx(df['High'], df['Low'], df['Close'], length=14)['ADX_14'].iloc[-1]
                rsi = ta.rsi(df['Close'], length=14).iloc[-1]
                
                results[ticker] = {"adx": adx, "rsi": rsi}
            except: continue
            
    except Exception as e:
        print(f"Scan Error: {e}")
        
    return results

def run_scout():
    print("--- 🔭 SECTOR SCOUT V2 (Target Commander) STARTED ---")
    
    while True:
        try:
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning Universes...")
            
            final_targets = {
                "condor_targets": [],
                "wheel_targets": [],
                "trend_targets": [],
                "updated": str(datetime.datetime.now())
            }

            # 1. SCAN CONDOR TARGETS
            # Criteria: ADX < 25 (Sideways) AND RSI between 40-60 (Stable)
            stats = get_technical_status(UNIVERSE_CONDOR)
            for t, s in stats.items():
                if s['adx'] < 25 and 40 < s['rsi'] < 60:
                    final_targets["condor_targets"].append(t)

            # 2. SCAN WHEEL TARGETS
            # Criteria: RSI < 55 (Not overbought, safe to sell puts)
            stats = get_technical_status(UNIVERSE_WHEEL)
            for t, s in stats.items():
                if s['rsi'] < 55:
                    final_targets["wheel_targets"].append(t)

            # 3. SCAN TREND TARGETS
            # Criteria: ADX > 25 (Trending)
            stats = get_technical_status(UNIVERSE_TREND)
            for t, s in stats.items():
                if s['adx'] > 25:
                    final_targets["trend_targets"].append(t)

            # SAVE TO FILE
            with open(TARGET_FILE, 'w') as f:
                json.dump(final_targets, f, indent=4)
                
            print(f"  🦅 Condor Targets: {final_targets['condor_targets']}")
            print(f"  🚜 Wheel Targets:  {final_targets['wheel_targets']}")
            print(f"  🏹 Trend Targets:  {final_targets['trend_targets']}")

            time.sleep(3600) # Run every hour

        except Exception as e:
            print(f"Scout Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_scout()