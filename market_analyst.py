import config
import time
import json
import requests
import pandas as pd
import pandas_ta as ta
import datetime
import os
import yfinance as yf # Replaces Alpaca for VIX access

# --- CONFIGURATION ---
CHECK_INTERVAL = 900   # 15 Minutes (Increased tempo from 3600)
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
# Pre-build the URL for efficiency
INFLUX_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"

# --- WEBHOOK ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_OVERSEER: return
    try:
        requests.post(config.WEBHOOK_OVERSEER, json={
            "content": msg, "username": "Market Analyst 🧠"
        })
    except: pass

def log_to_influx(price, vix, adx, regime, sma, ema):
    """
    Writes Market Health to InfluxDB for Grafana.
    Includes 'regime_score' for numeric graphing (1=Bull, -1=Bear).
    """
    try:
        # Map Regime to Number for Grafana Gauges
        if "BULL" in regime: regime_score = 1
        elif "BEAR" in regime: regime_score = -1
        else: regime_score = 0
        
        # Line Protocol: measurement,tags fields timestamp
        # We log everything: Price, VIX, ADX, SMA, EMA, and the Score
        data_str = (f'market_stats,symbol=SPY '
                    f'price={price},vix={vix},adx={adx},sma200={sma},ema20={ema},'
                    f'regime_score={regime_score},regime_str="{regime}"')
        
        requests.post(INFLUX_URL, data=data_str)
    except Exception as e:
        print(f"  [!] Influx Write Error: {e}")

def get_market_data():
    """
    Fetches SPY and VIX. 
    Uses yfinance to get both datasets in a single aligned call.
    """
    try:
        tickers = ["SPY", "^VIX"]
        # Fetch 2 years to ensure valid SMA200 calculation
        data = yf.download(tickers, period="2y", interval="1d", group_by='ticker', progress=False)
        return data["SPY"], data["^VIX"]
    except Exception as e:
        print(f"[!] Data Fetch Error: {e}")
        return None, None

def update_bot_config(regime, vix_val, climate):
    """
    Updates bot_config.json based on:
    1. VIX (Fear) - The 'Shields'
    2. EMA20 (Weather) - The 'Fast Trend'
    """
    try:
        if not os.path.exists(CONFIG_FILE): return

        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)
        
        bots = current_config['bots']
        changes_made = []

        # --- THE PLAYBOOK ---
        
        # Start by assuming everyone is ACTIVE, then we disqualify them
        target_state = {k: "active" for k in bots.keys()}

        # 1. HURRICANE PROTOCOL (High VIX > 25)
        # If fear is high, we kill the income bots immediately.
        if vix_val > 25:
            target_state["condor_bot"] = "paused"   # Wings will break
            target_state["wheel_bot"] = "paused"    # Don't catch knives
            target_state["crypto_grid"] = "paused"  # Correlation to VIX is high
            target_state["survivor_bot"] = "paused" # Too volatile
            target_state["moon_bag"] = "paused"
            
            # Trend Bot is the only one allowed to hunt in a hurricane
            target_state["trend_bot"] = "active"
            
            forced_regime = "CRITICAL_VOLATILITY"

        # 2. STANDARD PROTOCOLS (Normal Volatility)
        else:
            forced_regime = regime
            
            if regime == "BEAR_TREND":
                # Elevator Down (Price < EMA20)
                target_state["survivor_bot"] = "paused" # Long only - dangerous
                target_state["wheel_bot"] = "paused"    # Selling puts is suicide
                target_state["moon_bag"] = "paused"
                target_state["trend_bot"] = "active"    # HUNTING (Inverse ETFs)
                target_state["condor_bot"] = "active"   # Valid if VIX < 25
                target_state["crypto_grid"] = "active"  # Valid (Auto-tunes)

            elif regime == "BULL_TREND":
                # Stairs Up (Price > EMA20 & Trending)
                target_state["crypto_grid"] = "paused"  # Don't grid a rocket
                target_state["trend_bot"] = "active"
                target_state["survivor_bot"] = "active"
                target_state["wheel_bot"] = "active"
                target_state["moon_bag"] = "active"

            else: # SIDEWAYS / CHOP
                target_state["trend_bot"] = "paused"    # Whipsaw risk
                target_state["survivor_bot"] = "paused"
                target_state["condor_bot"] = "active"   # GOLDEN TIME
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
    print("--- 🧠 MARKET ANALYST V4 (Fast Bear + Grafana) STARTED ---")
    print(f"    Checking every {CHECK_INTERVAL/60:.0f} mins | Indicators: EMA20, SMA200, VIX")
    send_discord("🧠 **Analyst V4 Online**\nMonitoring Weather (EMA20) & Fear (VIX)...")

    while True:
        try:
            spy_df, vix_df = get_market_data()
            
            if spy_df is not None and not spy_df.empty:
                # --- CALCULATE INDICATORS ---
                # 1. The Climate (200 SMA) - Long Term
                spy_df['sma200'] = ta.sma(spy_df['Close'], length=200)
                
                # 2. The Weather (20 EMA) - Short Term (Fast Bear Detection)
                spy_df['ema20'] = ta.ema(spy_df['Close'], length=20)
                
                # 3. Trend Strength (ADX)
                adx_df = ta.adx(spy_df['High'], spy_df['Low'], spy_df['Close'], length=14)
                spy_df['adx'] = adx_df['ADX_14']

                # Get Latest Data Points
                latest = spy_df.iloc[-1]
                price = float(latest['Close'])
                sma200 = float(latest['sma200'])
                ema20 = float(latest['ema20'])
                adx = float(latest['adx'])
                
                vix_val = float(vix_df['Close'].iloc[-1])

                # --- REGIME DEFINITION ---
                
                # Climate: Just for context (Are we in a secular Bull or Bear?)
                climate = "MACRO_BULL" if price > sma200 else "MACRO_BEAR"
                
                # Weather: The Actionable Signal
                # Rule 1: If Price < 20 EMA, the elevator is going down. BEAR.
                if price < ema20:
                    regime = "BEAR_TREND"
                # Rule 2: If Price > 20 EMA, check if we are Trending or Chopping
                elif price > ema20:
                    if adx > 25:
                        regime = "BULL_TREND"
                    else:
                        regime = "SIDEWAYS"
                
                print(f"[{datetime.datetime.now().strftime('%H:%M')}] {regime} | VIX: {vix_val:.2f} | Sending to InfluxDB...")
                
                # 1. UPDATE FLEET CONFIG
                update_bot_config(regime, vix_val, climate)
                
                # 2. UPDATE GRAFANA
                log_to_influx(price, vix_val, adx, regime, sma200, ema20)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"[!] Analyst Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()