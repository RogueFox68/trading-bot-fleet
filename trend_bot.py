import config
import time
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import pytz
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest

# --- CONFIGURATION ---
# The "Elite 8" (High Profit Factor from Universe Scan)
SYMBOLS = ["CLX", "UNH", "DVN", "FMC", "ADBE", "RSG", "AON", "DUK"]

# Strategy Settings
FAST_EMA = 7
SLOW_EMA = 21
RISK_PER_TRADE = 0.02   # Risk 2% of account per trade based on Stop Loss
MAX_POS_SIZE = 0.25     # HARD CAP: Never put more than 25% of account in one stock

# --- CREDENTIALS (PASTE YOURS) ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER
DISCORD_URL = config.WEBHOOK_TREND

# --- INFLUXDB SETTINGS ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- TIME CONFIG ---
TIMEZONE = pytz.timezone('US/Eastern')
MORNING_START = datetime.time(9, 45)
LUNCH_START   = datetime.time(11, 30)
LUNCH_END     = datetime.time(13, 30)
CLOSE_STOP    = datetime.time(15, 45)

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    """Writes trade data to InfluxDB with error reporting"""
    try:
        # Auto-detect measurement name based on the bot type
        if "crypto" in __file__:
            measurement = "crypto_trades"
        elif "survivor" in __file__:
            measurement = "survivor_trades"
        else:
            measurement = "trades"

        # Line Protocol: measurement,tags fields timestamp
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'

        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        
        # Send the data
        response = requests.post(url, data=data_str)

        # Check for success (204 is the only success code for Influx writes)
        if response.status_code != 204:
            print(f"  [!] InfluxDB Error {response.status_code}: {response.text}")
            
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_data_yahoo(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval="15m")
        if df.empty: return None
        df.index = df.index.tz_convert('US/Eastern')
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"  [!] Yahoo Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['ema7'] = ta.ema(df['close'], length=FAST_EMA)
    df['ema21'] = ta.ema(df['close'], length=SLOW_EMA)
    return df

def check_time_rules():
    now = datetime.datetime.now(TIMEZONE).time()
    if MORNING_START <= now <= LUNCH_START: return True
    if LUNCH_END <= now <= CLOSE_STOP: return True
    return False

def run_trend_bot():
    print(f"--- TREND SNIPER (Elite + Safety Cap) STARTED ---")
    send_discord("昌 **Trend Sniper (Elite) Online**")
    
    # --- ADD THIS LINE ---
    log_to_influx("SYSTEM", "startup", 0, 0)
    # ---------------------

    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            can_open_new = check_time_rules()
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            buying_power = float(account.buying_power)

            # --- THE STICKY LOOP LOGIC ---
           # ... inside the main while True loop ...
            
            # 1. Update Position List
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}
            for p in positions:
                # SKIP Survivor/Crypto positions
                if p.symbol in ["TQQQ", "SQQQ", "SOXL", "SOXS", "BTC/USD"]:
                    continue
                if p.asset_class == AssetClass.US_OPTION:
                    continue
                if p.symbol in ["DIS", "PLTR", "F"]:
                    continue

                # --- EMERGENCY HARD STOP CHECK ---
                # This runs on EVERY loop, so it survives reboots.
                
                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                
                # Calculate current percentage loss
                pct_diff = (current_price - entry_price) / entry_price
                
                # HARD STOP: If we are down more than 3.5% (adjust this number to your risk tolerance)
                MAX_LOSS_PCT = -0.035 
                
                if pct_diff < MAX_LOSS_PCT:
                    print(f"🚨 EMERGENCY STOP: {p.symbol} is down {pct_diff*100:.2f}%. Liquidating.")
                    
                    req = MarketOrderRequest(
                        symbol=p.symbol,
                        qty=float(p.qty),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=req)
                    
                    log_to_influx(p.symbol, "stop_loss", current_price, float(p.qty))
                    send_discord(f"🛑 **HARD STOP TRIGGERED**\nSold {p.symbol} at ${current_price} (Loss: {pct_diff*100:.2f}%)")
                    continue # Skip the rest of the loop for this symbol
                
                # ... existing EMA/Signal logic continues below ...
            
            # Identify Survivor Bot symbols to IGNORE
            IGNORED_SYMBOLS = ["TQQQ", "SQQQ", "SOXL", "SOXS", "BTC/USD"]
            
            # Only manage positions that are in our strategy OR are orphans (not in the ignore list)
            held_tickers = [s for s in pos_dict.keys() if s not in IGNORED_SYMBOLS]

            # Combine Elite List + Valid Held Positions
            scan_list = list(set(SYMBOLS + held_tickers))

            print(f"\nScanning {len(scan_list)} Assets ({'OPEN' if can_open_new else 'PAUSED'}) at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

            for symbol in scan_list:
                df = get_data_yahoo(symbol)
                if df is None or len(df) < 30: continue

                df = calculate_indicators(df)
                # OLD CODE:
                # curr = df.iloc[-1]
                # prev = df.iloc[-2]

                # NEW CODE:
                # Use the last COMPLETED candle for logic
                signal_candle = df.iloc[-2] 
                # Use the current live candle only for price execution
                execution_candle = df.iloc[-1]

                price = execution_candle['close'] # Execute at live price
                
                # Logic uses the confirmed candle (stable for 5-15 mins)
                ema7 = signal_candle['ema7']
                ema21 = signal_candle['ema21']
                
                # Update your logic checks to use 'signal_candle' instead of 'curr'
                # e.g. trend_is_up = ema7 > ema21

                # Check Trends
                bullish_cross = (signal_candle['ema7'] > signal_candle['ema21']) and (df.iloc[-2]['ema7'] <= df.iloc[-2]['ema21'])
                bearish_cross = (signal_candle['ema7'] < signal_candle['ema21']) and (df.iloc[-2]['ema7'] >= df.iloc[-2]['ema21'])
                trend_is_up = ema7 > ema21
                trend_is_down = ema7 < ema21

                print(f"  {symbol:<4} | Price: {price:.2f} | 7EMA: {ema7:.2f} | 21EMA: {ema21:.2f}")

                # --- TRADING LOGIC ---
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    side = pos.side

                    # Exit Long
                    if side == 'long' and trend_is_down:
                        print(f"    [EXIT] Trend Reversal (Long) on {symbol}")
                        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                        trading_client.submit_order(order_data=req)

                        send_discord(f"📉 **CLOSE {symbol}** (Trend Reversal)\nPrice: {price:.2f}")
                        log_to_influx(symbol, "sell", price, qty)

                    # Exit Short
                    elif side == 'short' and trend_is_up:
                        print(f"    [EXIT] Trend Reversal (Short) on {symbol}")
                        req = MarketOrderRequest(symbol=symbol, qty=abs(qty), side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
                        trading_client.submit_order(order_data=req)

                        send_discord(f"💰 **CLOSE {symbol}** (Trend Reversal)\nPrice: {price:.2f}")
                        log_to_influx(symbol, "buy_cover", price, abs(qty))

                # Only enter NEW trades on the 'Official' list
                elif can_open_new and symbol not in pos_dict and symbol in SYMBOLS:
                    # Enter Long
                    if bullish_cross:
                        stop_price = df['low'].iloc[-5:].min()
                        risk = price - stop_price
                        if risk > 0.05:
                            # 1. Calc Risk Size
                            risk_amt = equity * RISK_PER_TRADE
                            risk_qty = int(risk_amt / risk)

                            # 2. Calc Max Cap Size (25% rule)
                            max_cost = equity * MAX_POS_SIZE
                            cap_qty = int(max_cost / price)

                            # 3. Take Smaller
                            qty = min(risk_qty, cap_qty)

                            if qty > 0 and (qty*price) < buying_power:
                                print(f"    [ENTRY] LONG Signal on {symbol}")
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)

                                send_discord(f"🚀 **BUY {symbol}**\nPrice: {price:.2f}\nStop: {stop_price:.2f}")
                                log_to_influx(symbol, "buy", price, qty)

                    # Enter Short
                    elif bearish_cross:
                        stop_price = df['high'].iloc[-5:].max()
                        risk = stop_price - price
                        if risk > 0.05:
                            # 1. Calc Risk Size
                            risk_amt = equity * RISK_PER_TRADE
                            risk_qty = int(risk_amt / risk)

                            # 2. Calc Max Cap Size (25% rule)
                            max_cost = equity * MAX_POS_SIZE
                            cap_qty = int(max_cost / price)

                            # 3. Take Smaller
                            qty = min(risk_qty, cap_qty)

                            if qty > 0 and (qty*price) < buying_power:
                                print(f"    [ENTRY] SHORT Signal on {symbol}")
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)

                                send_discord(f"📉 **SHORT {symbol}**\nPrice: {price:.2f}\nStop: {stop_price:.2f}")
                                log_to_influx(symbol, "sell_short", price, qty)

            time.sleep(60)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()

