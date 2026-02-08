import config
import time
import json
import yfinance as yf
import pandas as pd
import pandas_ta as ta
import requests
import datetime
import random

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
SCAN_INTERVAL = 3600  # Run hourly

# --- CORE WATCHLISTS (Always check these) ---
# Even if the scanner fails, these remain in rotation.
CORE_CONDOR = ["SPY", "IWM", "QQQ", "TLT", "KO", "WM"]
CORE_WHEEL  = ["F", "PLTR", "SOFI", "AMD", "BAC", "T"]
CORE_TREND  = ["NVDA", "TSLA", "MSTR", "COIN", "SMCI", "NFLX", "META"]

def get_sp500_tickers():
    """Scrapes Wikipedia for the S&P 500 list."""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        # CRITICAL: This header prevents Wikipedia from blocking the bot
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)'}
        tables = pd.read_html(requests.get(url, headers=headers).text)
        df = tables[0]
        tickers = df['Symbol'].tolist()
        # Clean up symbols (e.g., BRK.B -> BRK-B for yfinance)
        return [t.replace('.', '-') for t in tickers]
    except Exception as e:
        print(f"  [!] S&P 500 Scrape Failed: {e}")
        return []

def get_technical_metrics(tickers):
    """
    Downloads data for a list of tickers and returns a dict of metrics.
    Batch downloads to save time.
    """
    metrics = {}
    if not tickers: return metrics

    print(f"  [Scan] Fetching data for {len(tickers)} tickers...")
    
    try:
        # Download 6 months of data to calculate SMA200 and Volatility
        data = yf.download(tickers, period="6mo", interval="1d", group_by='ticker', progress=False, threads=True)
        
        # Iterate through the multi-index dataframe
        for ticker in tickers:
            try:
                # Handle single vs multi-ticker structure
                if len(tickers) == 1:
                    df = data
                else:
                    df = data[ticker]
                
                df = df.dropna()
                if len(df) < 200: continue # Need enough history for SMA200
                
                # --- INDICATORS ---
                # 1. ADX (Trend Strength)
                adx = ta.adx(df['High'], df['Low'], df['Close'], length=14)
                current_adx = adx['ADX_14'].iloc[-1]
                
                # 2. RSI (Overbought/Oversold)
                current_rsi = ta.rsi(df['Close'], length=14).iloc[-1]
                
                # 3. SMA 200 (Long Term Trend)
                sma200 = ta.sma(df['Close'], length=200).iloc[-1]
                current_price = df['Close'].iloc[-1]
                
                # 4. Volatility (ATR normalized by price)
                atr = ta.atr(df['High'], df['Low'], df['Close'], length=14).iloc[-1]
                vol_pct = atr / current_price

                metrics[ticker] = {
                    "price": current_price,
                    "adx": current_adx,
                    "rsi": current_rsi,
                    "above_sma200": current_price > sma200,
                    "volatility": vol_pct
                }
            except:
                continue
                
    except Exception as e:
        print(f"  [!] Batch Data Error: {e}")
        
    return metrics

def run_scout():
    print("--- 🔭 SECTOR SCOUT V3 (Deep Space Scanner) STARTED ---")
    
    while True:
        try:
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Launching Scanner...")
            
            # 1. Build the Search List (Core + S&P 500)
            sp500 = get_sp500_tickers()
            if sp500:
                print(f"  [Scan] Successfully loaded {len(sp500)} S&P 500 tickers.")
                # Combine unique tickers
                full_scan_list = list(set(CORE_CONDOR + CORE_WHEEL + CORE_TREND + sp500))
            else:
                print("  [Warn] Using Core lists only (Scrape failed).")
                full_scan_list = list(set(CORE_CONDOR + CORE_WHEEL + CORE_TREND))

            # 2. Analyze the Market
            # Splitting large list into chunks to avoid yfinance timeouts if needed
            # For now, we try all at once. If it hangs, we can reduce.
            stats = get_technical_metrics(full_scan_list)
            
            final_targets = {
                "condor_targets": [],
                "wheel_targets": [],
                "trend_targets": [],
                "updated": str(datetime.datetime.now())
            }

            # 3. FILTER INTO BUCKETS
            for ticker, s in stats.items():
                
                # --- STRATEGY: IRON CONDOR ---
                # Goal: Sideways, Low Volatility, Stable
                # Rules: ADX < 25 (No Trend), RSI 40-60 (Middle), Low Vol (< 2.5%)
                if s['adx'] < 25 and 40 < s['rsi'] < 60 and s['volatility'] < 0.025:
                    final_targets["condor_targets"].append(ticker)

                # --- STRATEGY: THE WHEEL ---
                # Goal: Quality Stock, Buying the Dip (Selling Puts)
                # Rules: RSI < 50 (Discount), Above SMA200 (Long Term Bull Trend)
                # We specifically check for "quality" by ensuring it's in S&P 500 or Core List
                if s['above_sma200'] and s['rsi'] < 55:
                    final_targets["wheel_targets"].append(ticker)

                # --- STRATEGY: TREND / SURVIVOR ---
                # Goal: Momentum, Breakouts
                # Rules: ADX > 25 (Trending)
                if s['adx'] > 25:
                    final_targets["trend_targets"].append(ticker)

            # 4. MERGE CORE LISTS (Ensure favorites are always present if they fit basic criteria)
            # Actually, we should just append the Core lists to ensure they are traded
            # even if the scan logic is strict. Or, we trust the scan. 
            # Let's PRIORITIZE the Core list by adding them to the front if they aren't there.
            
            # Helper to dedupe
            def merge_lists(scanned, core):
                return list(set(scanned + core))

            final_targets["condor_targets"] = merge_lists(final_targets["condor_targets"], CORE_CONDOR)
            final_targets["wheel_targets"]  = merge_lists(final_targets["wheel_targets"], CORE_WHEEL)
            final_targets["trend_targets"]  = merge_lists(final_targets["trend_targets"], CORE_TREND)

            # 5. LIMIT SIZE (Don't overwhelm the bots)
            # Wheel bot can't watch 200 stocks. Let's pick the best 10 + Core.
            # (Simple truncation for now, could sort by 'best fit' later)
            final_targets["condor_targets"] = final_targets["condor_targets"][:15]
            final_targets["wheel_targets"] = final_targets["wheel_targets"][:15]
            final_targets["trend_targets"] = final_targets["trend_targets"][:15]

            # SAVE
            with open(TARGET_FILE, 'w') as f:
                json.dump(final_targets, f, indent=4)

            print(f"  🦅 Condor: {len(final_targets['condor_targets'])} targets")
            print(f"  🚜 Wheel:  {len(final_targets['wheel_targets'])} targets")
            print(f"  🏹 Trend:  {len(final_targets['trend_targets'])} targets")

            time.sleep(SCAN_INTERVAL)

        except Exception as e:
            print(f"[!] Scout Loop Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_scout()