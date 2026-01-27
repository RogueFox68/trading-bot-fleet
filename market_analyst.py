import config
import time
import json
import requests
import pandas as pd
import pandas_ta as ta
import datetime
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
CHECK_INTERVAL = 3600  # Check every hour (Don't flicker too fast)
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"  # The benchmark

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- CLIENT ---
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_OVERSEER: return
    try:
        # Use the Overseer webhook for "Management" announcements
        requests.post(config.WEBHOOK_OVERSEER, json={
            "content": msg, "username": "Market Analyst 🧠"
        })
    except: pass

def log_regime(regime, adx, price, sma):
    """Log the current regime to InfluxDB for Grafana"""
    try:
        data_str = f'market_regime,symbol=SPY regime="{regime}",adx={adx},price={price},sma200={sma}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except Exception as e:
        print(f"[!] Influx Error: {e}")

def get_market_data():
    """Fetch 300 days of SPY data to calculate 200 SMA and ADX."""
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)
        req = StockBarsRequest(
            symbol_or_symbols=[MARKET_SYMBOL],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=None,
            adjustment='all'
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(MARKET_SYMBOL)
        
        return df
    except Exception as e:
        print(f"[!] Data Fetch Error: {e}")
        return None

def update_bot_config(regime):
    """Reads, modifies, and saves the bot_config.json based on regime."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)
        
        bots = current_config['bots']
        changes_made = []

        # --- DEFINING THE PLAYBOOK ---
        # 1. BULL TREND (Easy Mode)
        if regime == "BULL_TREND":
            target_state = {
                "trend_bot": "active",
                "survivor_bot": "active",
                "moon_bag": "active",
                "crypto_grid": "paused", # Don't grid against a moonshot
                "wheel_bot": "active"    # Safe to sell puts in bull market
            }

        # 2. BEAR TREND (Hard Mode)
        elif regime == "BEAR_TREND":
            target_state = {
                "trend_bot": "active",   # It can short
                "survivor_bot": "paused", # Long only - dangerous
                "moon_bag": "paused",    # No breakouts in bear
                "crypto_grid": "paused", # Don't catch falling knives
                "wheel_bot": "paused"    # Don't catch falling knives
            }

        # 3. CHOP / SIDEWAYS (Grind Mode)
        else: # "CHOP"
            target_state = {
                "trend_bot": "paused",   # Whipsaw killer!
                "survivor_bot": "paused",
                "moon_bag": "active",    # Crypto operates independently often
                "crypto_grid": "active", # SHINES here
                "wheel_bot": "active"    # SHINES here (Theta decay)
            }

        # --- APPLYING CHANGES ---
        for bot_name, desired_status in target_state.items():
            if bot_name in bots:
                if bots[bot_name]['status'] != desired_status:
                    bots[bot_name]['status'] = desired_status
                    changes_made.append(f"{bot_name} -> {desired_status}")

        # Update Market Condition Tag
        current_config['global_settings']['market_condition'] = regime

        # SAVE (Only if changed)
        if changes_made:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)
            
            msg = f"**Regime Shift Detected: {regime}**\nAdjusting Fleet:\n" + "\n".join(changes_made)
            print(msg)
            send_discord(msg)
        else:
            print(f"  Regime {regime} holds. No changes.")

    except Exception as e:
        print(f"[!] Config Update Error: {e}")

def run_analyst():
    print("--- 🧠 MARKET ANALYST (Regime Detection) STARTED ---")
    send_discord("🧠 **Analyst Online**\nWatching SPY for Trends...")

    while True:
        try:
            df = get_market_data()
            if df is not None:
                # Calculate Indicators
                df['sma200'] = ta.sma(df['close'], length=200)
                adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
                
                # ADX returns 3 columns: ADX_14, DMP_14, DMN_14. We just want ADX.
                # Creates a column named 'ADX_14' usually.
                adx_col = [c for c in adx_df.columns if c.startswith('ADX')][0]
                df['adx'] = adx_df[adx_col]

                # Get Latest Values
                latest = df.iloc[-1]
                price = float(latest['close'])
                sma = float(latest['sma200'])
                adx = float(latest['adx'])

                # --- DETERMINE REGIME ---
                regime = "CHOP" # Default
                
                if adx > 25:
                    if price > sma:
                        regime = "BULL_TREND"
                    else:
                        regime = "BEAR_TREND"
                else:
                    regime = "CHOP"

                print(f"[{datetime.datetime.now().strftime('%H:%M')}] Analysis: SPY=${price:.2f} | SMA=${sma:.2f} | ADX={adx:.1f} | Regime: {regime}")
                
                log_regime(regime, adx, price, sma)
                update_bot_config(regime)

            # Sleep 1 hour
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"[!] Critical Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()