from email import utils
import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
# Core leveraged ETFs we ALWAYS watch (High Volatility is their nature)
CORE_WATCHLIST = ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"]
TARGET_FILE = "active_targets.json" # <--- Reading the Scout's list

# Indicators
RSI_BUY = 30        # Oversold (Buy the dip)
RSI_SELL = 70       # Overbought (Sell the rip)
RISK_PER_TRADE = 0.05 # Aggressive sizing for mean reversion

# --- CREDENTIALS & CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

# --- INFLUX & DISCORD ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return # Reusing Trend webhook for now
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'survivor_trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_dynamic_targets():
    """Reads the 'Hot Sector' list from the Scout."""
    if not os.path.exists(TARGET_FILE): return []
    try:
        with open(TARGET_FILE, 'r') as f:
            data = json.load(f)
            # Filter out ETFs from the scout list so we don't double count, 
            # or keep them if we want to trade the underlying sector ETF.
            return data.get("targets", [])
    except: return []

def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), # 15m candles for intraday dips
            start=start_time,
            limit=200
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except: return None

def run_survivor_bot():
    print(f"--- 🛡️ SURVIVOR BOT (Scout Integrated) STARTED ---")
    send_discord("**Survivor Bot (V3)** Online\nScanning Core + Scout Targets for Dips.")
    
    while True:
        try:
            # 1. Market Check
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            # 2. Build Watchlist
            scout_targets = get_dynamic_targets()
            # Combine Core + Scout (Remove duplicates)
            full_watchlist = list(set(CORE_WATCHLIST + scout_targets))
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now(TIMEZONE).strftime('%H:%M')}] Scanning {len(full_watchlist)} Targets (Core: {len(CORE_WATCHLIST)} | Scout: {len(scout_targets)})")

            for symbol in full_watchlist:
                if symbol in ["BTC/USD", "ETH/USD"]: continue 

                df = get_data_alpaca(symbol)
                if df is None: continue

                # Indicators
                df['rsi'] = ta.rsi(df['close'], length=14)
                df['sma200'] = ta.sma(df['close'], length=200) # Trend filter
                
                latest = df.iloc[-1]
                price = float(latest['close'])
                rsi = float(latest['rsi'])
                sma = float(latest['sma200']) if not pd.isna(latest['sma200']) else 0

                # --- EXIT LOGIC (Take Profit / Stop Loss) ---
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    entry_price = float(pos.avg_entry_price)
                    pct_gain = (price - entry_price) / entry_price
                    
                    # Exit if Overbought (RSI > 70) OR Big Win (+5%) OR Stop Loss (-3%)
                    should_sell = False
                    reason = ""
                    
                    if rsi > RSI_SELL:
                        should_sell = True
                        reason = f"RSI Overbought ({rsi:.0f})"
                    elif pct_gain > 0.05:
                        should_sell = True
                        reason = "Take Profit (+5%)"
                    elif pct_gain < -0.03:
                        should_sell = True
                        reason = "Stop Loss (-3%)"
                        
                    if should_sell:
                        print(f"    📉 SELLING {symbol}: {reason}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                        send_discord(f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                        log_to_influx(symbol, "sell", price, qty)

                # --- ENTRY LOGIC (Buy the Dip) ---
                else:
                    # 1. Basic Condition: OVERSOLD
                    if rsi < RSI_BUY:
                        # [NEW] CFO CHECK
                        if not utils.check_budget("survivor_bot", trading_client):
                            print(f"    [SKIP] Survivor Budget Exceeded.")
                            continue
                        # 2. Safety Filter:
                        # Only buy if the price is ABOVE the 200 SMA (Uptrend Pullback)
                        # OR if it's a Scout Target (The General confirmed the trend)
                        is_scout_pick = symbol in scout_targets
                        is_uptrend = price > sma
                        
                        if is_uptrend or is_scout_pick:
                            print(f"    💎 DIP DETECTED: {symbol} (RSI {rsi:.0f})")
                            
                            # Size Check
                            risk_amt = equity * RISK_PER_TRADE
                            qty = int(risk_amt / price)
                            
                            if qty > 0:
                                print(f"       -> Buying {qty} shares...")
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                source_tag = "SCOUT PICK" if is_scout_pick else "CORE"
                                send_discord(f"💎 **BOUGHT DIP {symbol}** ({source_tag})\nRSI: {rsi:.0f}")
                                log_to_influx(symbol, "buy", price, qty)
                        else:
                            print(f"    ⚠️ Skipping {symbol} (RSI {rsi:.0f} but Below SMA200)")

            time.sleep(60)

        except Exception as e:
            print(f"Survivor Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()