import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import utils
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"
RISK_PER_TRADE = 0.02

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except: pass

def get_market_regime():
    if not os.path.exists(CONFIG_FILE): return "UNKNOWN"
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f).get("global_settings", {}).get("market_condition", "UNKNOWN")
    except: return "UNKNOWN"

def get_mission_targets(regime):
    """
    PRIORITY SYSTEM:
    1. Dragnet Targets (Specific Opportunities) -> Always Traded.
    2. Regime Targets (General Market) -> Traded if Dragnet is empty.
    """
    # 1. Check Dragnet (The Scout's Orders)
    dragnet_targets = utils.get_active_targets("trend_targets")
    if dragnet_targets:
        print(f"    🎯 PRIORITY: Engaging {len(dragnet_targets)} Dragnet Targets.")
        return dragnet_targets

    # 2. If Empty, check Regime (The General Flow)
    if "BEAR" in regime:
        print("    🐻 BEAR REGIME: Engaging Defensive ETFs.")
        return ["SQQQ", "SPXU", "SOXS"]
    
    # 3. If Sideways and No Targets -> Sleep
    print("    💤 SIDEWAYS & No Targets. Standing down.")
    return []

def run_trend_bot():
    print(f"--- 🏹 TREND BOT V5 (Dragnet Aware) ---")
    send_discord("**Trend Bot V5 Online**\nReady to engage Dragnet targets.")
    
    while True:
        try:
            # Check Hours
            try:
                if not trading_client.get_clock().is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            # Load Intel
            regime = get_market_regime()
            targets = get_mission_targets(regime)
            
            if not targets:
                time.sleep(300) # Sleep 5 mins if nothing to do
                continue

            # ... [EXECUTION LOGIC REMAINS SAME] ...
            # (Fetching Data, Checking ADX, Placing Trades)
            # We assume the standard execution loop here...
            
            print(f"\nScanning {len(targets)} Targets ({regime})...")
            
            for symbol in targets:
                # [Standard Analysis Code Here - Kept from V4]
                # If ADX > 20 and Trend Aligns -> Buy
                pass 
                # (You don't need to rewrite the whole execution block, 
                # just the target selection logic above is the key change).

            time.sleep(60)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()