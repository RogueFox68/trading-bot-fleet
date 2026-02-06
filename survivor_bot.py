import utils
import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import pandas_ta as ta # Still used for SMA200, but not BBands
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
# Core leveraged ETFs (High Volatility)
CORE_WATCHLIST = ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"]

# --- CREDENTIALS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_SURVIVOR: return 
    try: requests.post(config.WEBHOOK_SURVIVOR, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'survivor_trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_data_alpaca(symbol):
    try:
        # Fetch enough data for SMA200 + Bollinger Bands
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), 
            start=start_time,
            limit=500
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        
        # Helper to handle timezone if needed, but usually raw is fine for indicators
        return df
    except: return None

def run_survivor_bot():
    print(f"--- 🛡️ SURVIVOR BOT (Bollinger Bandit V2) STARTED ---")
    send_discord("**Survivor Bot** Online\nLogic: Manual Bollinger Bands (Pi Safe)")
    
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

            # 2. Build Watchlist (Core + Trend Targets from Scout)
            # Survivor likes high volatility, so it shares the Trend list
            scout_targets = utils.get_active_targets("trend_targets")
            full_watchlist = list(set(CORE_WATCHLIST + scout_targets))
            
            # 3. Account Data
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning {len(full_watchlist)} Targets...")

            for symbol in full_watchlist:
                if symbol in ["BTC/USD", "ETH/USD"]: continue 

                df = get_data_alpaca(symbol)
                if df is None or len(df) < 20: continue

                # --- MANUAL BOLLINGER BANDS CALCULATION ---
                # This bypasses the broken 'numba' dependency on the Pi
                
                # 1. Middle Band = 20 SMA
                df['middle_band'] = df['close'].rolling(window=20).mean()
                
                # 2. Standard Deviation
                df['std_dev'] = df['close'].rolling(window=20).std()
                
                # 3. Upper & Lower Bands (2 Std Devs)
                df['upper_band'] = df['middle_band'] + (2.0 * df['std_dev'])
                df['lower_band'] = df['middle_band'] - (2.0 * df['std_dev'])
                
                # 4. Trend Filter (200 SMA)
                df['sma200'] = ta.sma(df['close'], length=200)

                # Get Latest Candle
                latest = df.iloc[-1]
                price = float(latest['close'])
                lower_band = float(latest['lower_band'])
                upper_band = float(latest['upper_band'])
                # Handle SMA NaN early in history
                sma200 = float(latest['sma200']) if not pd.isna(latest['sma200']) else 0

                # --- LOGIC: MEAN REVERSION ---
                
                # EXIT LOGIC (Sell the Rip)
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    entry_price = float(pos.avg_entry_price)
                    pct_gain = (price - entry_price) / entry_price
                    
                    should_sell = False
                    reason = ""
                    
                    # Sell if Price hits Upper Band OR Stop Loss (-3%)
                    if price >= upper_band:
                        should_sell = True
                        reason = f"Upper Band Hit (${upper_band:.2f})"
                    elif pct_gain < -0.03:
                        should_sell = True
                        reason = "Stop Loss (-3%)"
                        
                    if should_sell:
                        print(f"    📉 SELLING {symbol}: {reason}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                        send_discord(f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                        log_to_influx(symbol, "sell", price, qty)

                # ENTRY LOGIC (Buy the Dip)
                else:
                    # Buy if Price touches Lower Band
                    if price <= lower_band:
                        
                        # CFO CHECK
                        if not utils.check_budget("survivor_bot", trading_client):
                            print(f"    [SKIP] Survivor Budget Exceeded.")
                            continue
                            
                        # Trend Filter: Only buy dips in an Uptrend (Price > SMA200)
                        # Exception: If it's a Core Volatility ETF, we might ignore trend, 
                        # but for safety let's keep the SMA filter.
                        if price > sma200:
                            print(f"    💎 BAND TOUCH: {symbol} (${price:.2f} <= ${lower_band:.2f})")
                            
                            # Size: $1000 fixed for testing or dynamic based on equity
                            account = trading_client.get_account()
                            equity = float(account.portfolio_value)
                            risk_amt = equity * 0.05 # 5% per trade
                            qty = int(risk_amt / price)
                            
                            if qty > 0:
                                print(f"       -> Buying {qty} shares...")
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                send_discord(f"💎 **BOUGHT DIP {symbol}**\nPrice: ${price:.2f}\nLower Band: ${lower_band:.2f}")
                                log_to_influx(symbol, "buy", price, qty)
                        else:
                            # Debug print so we know it sees the dip but rejects it
                            pass 

            time.sleep(60)

        except Exception as e:
            print(f"Survivor Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()