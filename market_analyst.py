import config
import time
import json
import requests
import pandas as pd
import pandas_ta as ta
import datetime
import os
import yfinance as yf # Added for VIX & Data

# --- CONFIGURATION ---
CHECK_INTERVAL = 900   # 15 Minutes (Was 3600)
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"

# --- WEBHOOK ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_OVERSEER: return
    try:
        requests.post(config.WEBHOOK_OVERSEER, json={
            "content": msg, "username": "Market Analyst 🧠"
        })
    except: pass

def get_market_data():
    """
    Fetches SPY (Price) and VIX (Fear).
    Using yfinance for both to ensure aligned timestamps and easy VIX access.
    """
    try:
        # Fetch 1 year of history to ensure valid SMA200 calculation
        # We use interval="1d" because we are looking for daily trends, even if checking intraday.
        # (For true intraday "elevator" detection, we could use 1h data, 
        # but 1d candles checked every 15m is standard for 'Day Swing' strategies)
        
        tickers = ["SPY", "^VIX"]
        data = yf.download(tickers, period="2y", interval="1d", group_by='ticker', progress=False)
        
        return data["SPY"], data["^VIX"]
    except Exception as e:
        print(f"[!] Data Fetch Error: {e}")
        return None, None

def update_bot_config(regime, vix_val, climate):
    """
    Updates bot_config.json based on Regime (Weather) AND Volatility.
    """
    try:
        if not os.path.exists(CONFIG_FILE): return

        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)
        
        bots = current_config['bots']
        changes_made = []

        # --- THE PLAYBOOK ---
        
        # 1. DEFAULT STATE: Assume Active
        target_state = {k: "active" for k in bots.keys()}

        # 2. VIX OVERRIDE (The "Hurricane" Protocol)
        if vix_val > 25:
            target_state["condor_bot"] = "paused"  # Wings will break
            target_state["wheel_bot"] = "paused"   # Don't catch knives
            target_state["crypto_grid"] = "paused" # Crypto correlates to VIX often
            target_state["survivor_bot"] = "paused"
            
            # Trend Bot thrives in chaos if it can short (Inverse ETFs)
            target_state["trend_bot"] = "active" 
            
            forced_regime = "CRITICAL_VOLATILITY"

        else:
            forced_regime = regime
            
            # 3. WEATHER BASED DEPLOYMENT
            if regime == "BEAR_TREND":
                # "Elevator Down" - Fast Moving Downside
                target_state["survivor_bot"] = "paused" # Too dangerous
                target_state["wheel_bot"] = "paused"    # Too dangerous
                target_state["moon_bag"] = "paused"     # No breakouts likely
                target_state["trend_bot"] = "active"    # HUNT! (Using Inverse ETFs)
                target_state["condor_bot"] = "active"   # Okay if VIX < 25 (Wide wings)

            elif regime == "BULL_TREND":
                # "Stairs Up"
                target_state["crypto_grid"] = "paused" # Don't grid a rocket
                target_state["trend_bot"] = "active"
                target_state["survivor_bot"] = "active"
                target_state["wheel_bot"] = "active"

            else: # SIDEWAYS / CHOP
                target_state["trend_bot"] = "paused"   # Whipsaw City
                target_state["survivor_bot"] = "paused"
                target_state["condor_bot"] = "active"  # The golden time
                target_state["wheel_bot"] = "active"
                target_state["crypto_grid"] = "active"

        # --- APPLY UPDATES ---
        for bot_name, desired_status in target_state.items():
            if bot_name in bots and bots[bot_name]['status'] != desired_status:
                bots[bot_name]['status'] = desired_status
                changes_made.append(f"{bot_name} -> {desired_status}")

        # Update Tags
        current_config['global_settings']['market_condition'] = forced_regime
        current_config['global_settings']['macro_climate'] = climate

        if changes_made:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)
            
            msg = (f"**Analyst Update ({forced_regime})**\n"
                   f"Weather: {regime} | Climate: {climate}\n"
                   f"VIX: {vix_val:.2f}\n"
                   f"Adjustments:\n" + "\n".join(changes_made))
            send_discord(msg)
            print(f"  -> Config Updated: {len(changes_made)} changes.")
        else:
            print(f"  Fleet holds steady. ({forced_regime})")

    except Exception as e:
        print(f"[!] Config Update Error: {e}")

def run_analyst():
    print("--- 🧠 MARKET ANALYST V3 (Fast Bear) STARTED ---")
    print(f"    Checking every {CHECK_INTERVAL/60:.0f} mins | Indicators: EMA20, SMA200, VIX")
    
    while True:
        try:
            spy_df, vix_df = get_market_data()
            
            if spy_df is not None and not spy_df.empty:
                # --- CALCULATE INDICATORS ---
                # 1. The Climate (200 SMA)
                spy_df['sma200'] = ta.sma(spy_df['Close'], length=200)
                
                # 2. The Weather (20 EMA) - The "Fast" line
                spy_df['ema20'] = ta.ema(spy_df['Close'], length=20)
                
                # 3. Trend Strength (ADX)
                adx_df = ta.adx(spy_df['High'], spy_df['Low'], spy_df['Close'], length=14)
                spy_df['adx'] = adx_df['ADX_14']

                # Get Latest
                latest = spy_df.iloc[-1]
                price = float(latest['Close'])
                sma200 = float(latest['sma200'])
                ema20 = float(latest['ema20'])
                adx = float(latest['adx'])
                
                vix_val = float(vix_df['Close'].iloc[-1])

                # --- REGIME DEFINITION ---
                
                # Climate Check (Informational Only)
                climate = "MACRO_BULL" if price > sma200 else "MACRO_BEAR"
                
                # Weather Check (Operational)
                # If Price is below the 20 EMA, we are in a short-term downtrend (Elevator Down)
                if price < ema20:
                    regime = "BEAR_TREND"
                # If Price is above 20 EMA, check if it's trending or chopping
                elif price > ema20:
                    if adx > 25:
                        regime = "BULL_TREND"
                    else:
                        regime = "SIDEWAYS"
                
                print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Analysis:")
                print(f"  Price: ${price:.2f} | VIX: {vix_val:.2f}")
                print(f"  Weather (EMA20): ${ema20:.2f} -> {regime}")
                print(f"  Climate (SMA200): ${sma200:.2f} -> {climate}")
                
                update_bot_config(regime, vix_val, climate)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"[!] Analyst Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()