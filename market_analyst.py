import config
import time
import json
import requests
import pandas as pd
import numpy as np
import datetime
import os
import yfinance as yf

# --- CONFIGURATION ---
CHECK_INTERVAL = 900   # 15 Minutes
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"

# --- INFLUXDB ---
# Market Analyst is co-located with DB, use localhost to bypass firewalls
INFLUX_HOST = "localhost"
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
INFLUX_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"

# --- NATIVE MATH ENGINE ---
class TechnicalMath:
    @staticmethod
    def get_sma(series, window):
        return series.rolling(window=window).mean()

    @staticmethod
    def get_ema(series, window):
        return series.ewm(span=window, adjust=False).mean()

    @staticmethod
    def get_adx(high, low, close, window=14):
        plus_dm = high.diff()
        minus_dm = low.diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        tr = (high - low).abs() # Simplified TR for speed
        tr = tr.replace(0, np.nan)
        plus_di = 100 * (plus_dm.ewm(alpha=1/window, adjust=False).mean() / tr)
        minus_di = 100 * (minus_dm.abs().ewm(alpha=1/window, adjust=False).mean() / tr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        return dx.ewm(alpha=1/window, adjust=False).mean()

# --- WEBHOOK ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_OVERSEER: return
    try:
        requests.post(config.WEBHOOK_OVERSEER, json={
            "content": msg, "username": "Market Analyst 🧠"
        })
    except: pass

def log_to_influx(price, vix, adx, regime, sma, ema):
    try:
        regime_score = 1 if "BULL" in regime else (-1 if "BEAR" in regime else 0)
        data_str = (f'market_regime,symbol=SPY '
                    f'price={price},vix={vix},adx={adx},sma200={sma},ema20={ema},'
                    f'regime_score={regime_score},regime="{regime}" {time.time_ns()}')
        r = requests.post(INFLUX_URL, data=data_str, timeout=2)
        if r.status_code != 204:
            print(f"   [!] InfluxDB write failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"   [!] InfluxDB write error: {e}")

def get_market_data():
    try:
        data = yf.download(["SPY", "^VIX"], period="1y", interval="1d", group_by='ticker', progress=False)
        return data["SPY"], data["^VIX"]
    except: return None, None

def update_bot_config(regime, vix_val, climate):
    """
    UPDATED LOGIC:
    - Normal Market (VIX < 28): Keep ALL bots ACTIVE. Let them decide.
    - Hurricane (VIX > 28): PAUSE everything.
    """
    if not os.path.exists(CONFIG_FILE): return

    try:
        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)
        
        bots = current_config['bots']
        changes_made = []

        # --- THE NEW LOGIC ---
        # Default: Everyone is ON deck.
        target_status = "active"
        
        # Exception: The Apocalypse
        if vix_val > 28.0:
            target_status = "paused"
            forced_regime = "CRITICAL_VOLATILITY"
        else:
            forced_regime = regime

        # Apply Status to ALL Bots
        # (We no longer micromanage who is paused during 'Sideways')
        for bot_name in bots.keys():
            # Accountant always runs
            if bot_name == "accountant": continue
            
            # Phase 7: Disable Condor Bot Revivals
            # We completely skip condor_bot here so that any manual state 
            # set by the user in bot_config.json is never overwritten by the analyst.
            if bot_name == "condor_bot":
                continue
            
            if bots[bot_name]['status'] != target_status:
                bots[bot_name]['status'] = target_status
                changes_made.append(f"{bot_name} -> {target_status}")

        # Update Global Weather Report
        current_config['global_settings']['market_condition'] = forced_regime
        current_config['global_settings']['macro_climate'] = climate
        current_config['global_settings']['vix'] = round(vix_val, 2)

        if changes_made or current_config['global_settings'].get('market_condition') != forced_regime:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)
            
            if changes_made:
                msg = (f"**Analyst Protocol Change**\n"
                       f"Condition: {forced_regime} (VIX: {vix_val:.2f})\n"
                       f"Adjustments: {', '.join(changes_made)}")
                send_discord(msg)
                print(f"  -> Config Updated: {len(changes_made)} changes.")
        
    except Exception as e:
        print(f"[!] Config Update Error: {e}")

def run_analyst():
    print("--- 🧠 MARKET ANALYST V5 (The Meteorologist) ---")
    print("    Role: Reporting Weather. Pausing ONLY for Emergencies (VIX > 28).")
    
    while True:
        try:
            spy_df, vix_df = get_market_data()
            
            if spy_df is not None and not spy_df.empty:
                # Math
                spy_df['sma200'] = TechnicalMath.get_sma(spy_df['Close'], 200)
                spy_df['ema20'] = TechnicalMath.get_ema(spy_df['Close'], 20)
                spy_df['adx'] = TechnicalMath.get_adx(spy_df['High'], spy_df['Low'], spy_df['Close'], 14)

                latest = spy_df.iloc[-1]
                vix_val = float(vix_df['Close'].iloc[-1])
                
                # Determine Regime
                price = float(latest['Close'])
                sma200 = float(latest['sma200'])
                ema20 = float(latest['ema20'])
                adx = float(latest['adx'])
                
                climate = "MACRO_BULL" if price > sma200 else "MACRO_BEAR"
                
                if price < ema20: regime = "BEAR_TREND"
                elif price > ema20 and adx > 25: regime = "BULL_TREND"
                else: regime = "SIDEWAYS"

                print(f"[{datetime.datetime.now().strftime('%H:%M')}] {regime} | VIX: {vix_val:.2f}")
                
                # Push Data
                update_bot_config(regime, vix_val, climate)
                log_to_influx(price, vix_val, adx, regime, sma200, ema20)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"[!] Analyst Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()