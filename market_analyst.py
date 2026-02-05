# market_analyst.py
import config
import time
import json
import requests
import pandas as pd
import pandas_ta as ta
import datetime
import os
import yfinance as yf # Utilizing yfinance for VIX data

# --- CONFIGURATION ---
CHECK_INTERVAL = 3600  # Run hourly
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
    """Fetches SPY for Trend and VIX for Volatility."""
    try:
        # 1. Fetch SPY from Alpaca (or YF) for Trend
        spy = yf.download("SPY", period="1y", interval="1d", progress=False)
        
        # 2. Fetch VIX from Yahoo Finance (Crucial for Option Sellers)
        vix = yf.download("^VIX", period="5d", interval="1d", progress=False)
        
        return spy, vix
    except Exception as e:
        print(f"[!] Data Fetch Error: {e}")
        return None, None

def update_bot_config(regime, vix_val):
    """Updates bot_config.json based on Regime AND Volatility."""
    try:
        if not os.path.exists(CONFIG_FILE): return

        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)
        
        bots = current_config['bots']
        changes_made = []

        # --- THE NEW PLAYBOOK ---
        
        # DEFAULT: Everything Active
        target_state = {k: "active" for k in bots.keys()}

        # 1. HIGH VOLATILITY SAFETY BREAKER (VIX > 25)
        # When fear is high, options move wildly. Kill the income bots.
        if vix_val > 25:
            target_state["condor_bot"] = "paused" # Wings will get blown out
            target_state["wheel_bot"] = "paused"  # Risk of assignment is too high
            target_state["crypto_grid"] = "paused" 
            regime = "FEAR_UNCERTAINTY"

        # 2. BULL TREND (ADX > 25, Price > SMA)
        elif "BULL" in regime:
            target_state["crypto_grid"] = "paused" # Don't grid against a moonshot
            # Wheel is safe in Bull markets (selling puts)
            # Condor is risky in strong trends, prefer paused or wide wings
            target_state["condor_bot"] = "paused" 

        # 3. BEAR TREND (ADX > 25, Price < SMA)
        elif "BEAR" in regime:
            target_state["survivor_bot"] = "paused" # Don't catch falling knives
            target_state["wheel_bot"] = "paused"
            target_state["moon_bag"] = "paused"

        # 4. CALM SIDEWAYS (The "Goldilocks" Zone for Condors)
        elif regime == "SIDEWAYS":
            target_state["trend_bot"] = "paused" # Whipsaw risk
            # This is where Condor and Grid Bot shine

        # --- APPLY UPDATES ---
        for bot_name, desired_status in target_state.items():
            if bot_name in bots and bots[bot_name]['status'] != desired_status:
                bots[bot_name]['status'] = desired_status
                changes_made.append(f"{bot_name} -> {desired_status}")

        current_config['global_settings']['market_condition'] = regime
        
        if changes_made:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)
            send_discord(f"**Regime Update: {regime} (VIX: {vix_val:.2f})**\n" + "\n".join(changes_made))
        else:
            print(f"  Regime {regime} (VIX {vix_val:.2f}) holds. No changes.")

    except Exception as e:
        print(f"[!] Config Update Error: {e}")

def run_analyst():
    print("--- 🧠 MARKET ANALYST V2 (VIX Aware) STARTED ---")
    
    while True:
        try:
            spy_df, vix_df = get_market_data()
            
            if spy_df is not None and not spy_df.empty:
                # Indicators
                spy_df['sma200'] = ta.sma(spy_df['Close'], length=200)
                adx_df = ta.adx(spy_df['High'], spy_df['Low'], spy_df['Close'], length=14)
                spy_df['adx'] = adx_df['ADX_14']

                latest = spy_df.iloc[-1]
                price = float(latest['Close'])
                sma = float(latest['sma200'])
                adx = float(latest['adx'])
                
                vix_val = float(vix_df['Close'].iloc[-1])

                # Regime Logic
                if adx > 25:
                    regime = "BULL_TREND" if price > sma else "BEAR_TREND"
                else:
                    regime = "SIDEWAYS"

                print(f"[{datetime.datetime.now().strftime('%H:%M')}] SPY=${price:.0f} | ADX={adx:.0f} | VIX={vix_val:.2f} | Regime: {regime}")
                
                update_bot_config(regime, vix_val)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"[!] Analyst Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()