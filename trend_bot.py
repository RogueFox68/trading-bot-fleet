import config
import time
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
# The "Elite 8" (High Profit Factor from Universe Scan)
SYMBOLS = ["NVDA", "TSLA", "COIN", "MSTR", "AMD", "PLTR", "META"]

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
# Market Hours
MORNING_START = datetime.time(9, 45) # Wait 15 mins for open volatility to settle
LUNCH_START   = datetime.time(11, 30)
LUNCH_END     = datetime.time(13, 30)
CLOSE_STOP    = datetime.time(15, 45) # Stop trading 15 mins before close

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    """Writes trade data to InfluxDB with error reporting"""
    try:
        measurement = "trades"
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        response = requests.post(url, data=data_str)
        if response.status_code != 204:
            print(f"  [!] InfluxDB Error {response.status_code}: {response.text}")
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_data_alpaca(symbol):
    """
    Fetches 15-minute bars from Alpaca (Replacing Yahoo Finance).
    """
    try:
        # We need enough data for the 21 EMA. 5 days of 15m bars is plenty.
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
        
        # Request 15-minute bars
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=200, # Only need enough for EMA calc
            adjustment='all' # Adjust for splits/dividends
        )
        
        bars = data_client.get_stock_bars(req)
        
        # If no data found, return None
        if not bars.data:
            return None
            
        # Convert to DataFrame
        df = bars.df
        
        # Handle MultiIndex (Alpaca returns [symbol, timestamp])
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol)

        # Convert UTC timestamp to US/Eastern
        df.index = df.index.tz_convert('US/Eastern')
        
        # Alpaca columns are already lowercase (open, high, low, close, volume)
        return df

    except Exception as e:
        print(f"  [!] Alpaca Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['ema7'] = ta.ema(df['close'], length=FAST_EMA)
    df['ema21'] = ta.ema(df['close'], length=SLOW_EMA)
    return df

def check_time_rules():
    """Returns True if we are allowed to open NEW positions."""
    now = datetime.datetime.now(TIMEZONE).time()
    # Morning Session
    if MORNING_START <= now <= LUNCH_START: return True
    # Afternoon Session
    if LUNCH_END <= now <= CLOSE_STOP: return True
    return False

def run_trend_bot():
    print(f"--- TREND SNIPER (Elite + Safety Cap) STARTED ---")
    send_discord("**Trend Sniper (Elite) Online**")
    
    log_to_influx("SYSTEM", "startup", 0, 0)

    while True:
        try:
            # Check Market Open/Close
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

            # 1. Update Position List
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}
            
            # --- EMERGENCY HARD STOP CHECK ---
            for p in positions:
                # Ignore Crypto/Survivor positions
                if p.symbol in ["TQQQ", "SQQQ", "SOXL", "SOXS", "BTC/USD"] or "/" in p.symbol:
                    continue
                if p.asset_class == AssetClass.US_OPTION:
                    continue
                if p.symbol in ["DIS", "PLTR", "F"]:
                    continue

                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                pct_diff = (current_price - entry_price) / entry_price
                
                # HARD STOP: -3.5%
                MAX_LOSS_PCT = -0.035 
                
                if pct_diff < MAX_LOSS_PCT:
                    print(f"💥 EMERGENCY STOP: {p.symbol} is down {pct_diff*100:.2f}%. Liquidating.")
                    req = MarketOrderRequest(
                        symbol=p.symbol,
                        qty=float(p.qty),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=req)
                    log_to_influx(p.symbol, "stop_loss", current_price, float(p.qty))
                    send_discord(f"💥 **HARD STOP TRIGGERED**\nSold {p.symbol} at ${current_price} (Loss: {pct_diff*100:.2f}%)")
                    continue

            # Identify Survivor/Crypto symbols to IGNORE
            IGNORED_SYMBOLS = ["TQQQ", "SQQQ", "SOXL", "SOXS", "BTC/USD"]
            
            # Combine Elite List + Valid Held Positions
            held_tickers = [s for s in pos_dict.keys() if s not in IGNORED_SYMBOLS and "/" not in s]
            scan_list = list(set(SYMBOLS + held_tickers))

            print(f"\nScanning {len(scan_list)} Assets ({'OPEN' if can_open_new else 'PAUSED'}) at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

            for symbol in scan_list:
                # NEW: Use Alpaca Data
                df = get_data_alpaca(symbol)
                
                if df is None or len(df) < 30: 
                    continue

                df = calculate_indicators(df)

                # Use the last COMPLETED candle for logic (index -2)
                signal_candle = df.iloc[-2] 
                # Use the current live candle only for price execution (index -1)
                execution_candle = df.iloc[-1]

                price = execution_candle['close'] 
                
                ema7 = signal_candle['ema7']
                ema21 = signal_candle['ema21']
                prev_ema7 = df.iloc[-3]['ema7'] # Previous completed candle
                prev_ema21 = df.iloc[-3]['ema21']

                # Check Trends
                trend_is_up = ema7 > ema21
                trend_is_down = ema7 < ema21
                
                # Crossover logic
                bullish_cross = (ema7 > ema21) and (prev_ema7 <= prev_ema21)
                bearish_cross = (ema7 < ema21) and (prev_ema7 >= prev_ema21)

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
                        send_discord(f"📈 **CLOSE {symbol}** (Trend Reversal)\nPrice: {price:.2f}")
                        log_to_influx(symbol, "buy_cover", price, abs(qty))

                # Only enter NEW trades on the 'Official' list
                elif can_open_new and symbol not in pos_dict and symbol in SYMBOLS:
                    # Enter Long
                    if bullish_cross:
                        stop_price = df['low'].iloc[-5:].min()
                        risk = price - stop_price
                        if risk > 0.05:
                            risk_amt = equity * RISK_PER_TRADE
                            risk_qty = int(risk_amt / risk)
                            max_cost = equity * MAX_POS_SIZE
                            cap_qty = int(max_cost / price)
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
                            risk_amt = equity * RISK_PER_TRADE
                            risk_qty = int(risk_amt / risk)
                            max_cost = equity * MAX_POS_SIZE
                            cap_qty = int(max_cost / price)
                            qty = min(risk_qty, cap_qty)

                            if qty > 0 and (qty*price) < buying_power:
                                print(f"    [ENTRY] SHORT Signal on {symbol}")
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)
                                send_discord(f"🐻 **SHORT {symbol}**\nPrice: {price:.2f}\nStop: {stop_price:.2f}")
                                log_to_influx(symbol, "sell_short", price, qty)

            time.sleep(60)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()