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
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"
FAST_EMA = 9
SLOW_EMA = 21
RISK_PER_TRADE = 0.02

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except: pass

def get_mission_targets():
    """Loads BOTH Bull and Bear lists from the Scout."""
    bull_targets = utils.get_active_targets("trend_targets")
    bear_targets = utils.get_active_targets("short_targets")
    return bull_targets, bear_targets

def run_trend_bot():
    print(f"--- 🏹 TREND BOT V6 (Switch Hitter) ---")
    send_discord("**Trend Bot V6 Online**\nReady to Long or Short.")
    
    while True:
        try:
            # 1. Market Hours Check
            try:
                if not trading_client.get_clock().is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            # 2. Load Targets
            bull_list, bear_list = get_mission_targets()
            
            # Combine for scanning
            # We map the ticker to its "Mission Type" (Long or Short)
            mission_map = {t: "LONG" for t in bull_list}
            mission_map.update({t: "SHORT" for t in bear_list})
            
            if not mission_map:
                print("    💤 No targets. Sleeping...")
                time.sleep(300)
                continue

            print(f"\nScanning {len(mission_map)} Targets ({len(bull_list)} Bull, {len(bear_list)} Bear)...")
            
            account = trading_client.get_account()
            equity = float(account.equity)
            buying_power = float(account.buying_power)

            # 3. Execution Loop
            for symbol, side in mission_map.items():
                if "/" in symbol: continue 

                # Fetch Data
                start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
                req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=start_time, limit=200)
                bars = data_client.get_stock_bars(req)
                if not bars.data: continue
                df = bars.df.xs(symbol)
                
                # Indicators
                df['ema_fast'] = ta.ema(df['close'], length=FAST_EMA)
                df['ema_slow'] = ta.ema(df['close'], length=SLOW_EMA)
                adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
                df['adx'] = adx_df[adx_df.columns[0]] # ADX_14

                latest = df.iloc[-1]
                prev = df.iloc[-2]
                price = float(latest['close'])
                adx = float(latest['adx'])
                
                # CFO Check (Simple)
                if not utils.check_budget("trend_bot", trading_client): continue

                # --- LOGIC: THE SWITCH HITTER ---
                
                # SCENARIO A: LONG MISSION
                if side == "LONG":
                    # Bull Cross: Fast crosses ABOVE Slow
                    bull_cross = (latest['ema_fast'] > latest['ema_slow']) and (prev['ema_fast'] <= prev['ema_slow'])
                    
                    if bull_cross and adx > 25:
                        print(f"    🚀 LONG SIGNAL: {symbol}")
                        qty = int((equity * RISK_PER_TRADE) / price)
                        if qty > 0:
                            trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                            send_discord(f"🚀 **BUY {symbol}** (Trend Long)\nPrice: ${price:.2f}")

                # SCENARIO B: SHORT MISSION
                elif side == "SHORT":
                    # Bear Cross: Fast crosses BELOW Slow
                    bear_cross = (latest['ema_fast'] < latest['ema_slow']) and (prev['ema_fast'] >= prev['ema_slow'])
                    
                    if bear_cross and adx > 25:
                        print(f"    📉 SHORT SIGNAL: {symbol}")
                        qty = int((equity * RISK_PER_TRADE) / price)
                        
                        # Check Margin for Shorting
                        if qty * price > buying_power:
                            print("    [!] Insufficient BP to Short.")
                            continue
                            
                        if qty > 0:
                            # SELL TO OPEN
                            trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                            send_discord(f"📉 **SHORT {symbol}** (Trend Short)\nPrice: ${price:.2f}\nBetting on downside.")

            time.sleep(60)

        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()