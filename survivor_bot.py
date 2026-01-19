import config
import time
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# --- CONFIGURATION ---
SYMBOLS = ["TQQQ", "SOXL", "FNGU", "UPRO"]
RSI_PERIOD = 14
RSI_OVERSOLD = 30
RSI_EXIT = 55
RISK_PER_TRADE = 0.05

# --- ALPACA KEYS (PASTE YOURS HERE) ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- NOTIFICATIONS ---
DISCORD_URL = config.WEBHOOK_SURVIVOR

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    """Writes trade data to InfluxDB with error reporting"""
    try:
        measurement = "survivor_trades"
        # Line Protocol: measurement,tags fields timestamp
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_data_alpaca(symbol):
    """
    Fetches 2 years of Daily data from Alpaca for the 200 SMA.
    """
    try:
        # 2 years * 365 days = ~730 days of history needed
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=730)

        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=None, # Get as much as possible since start date
            adjustment='all'
        )
        
        bars = data_client.get_stock_bars(req)
        
        if not bars.data:
            return None
            
        df = bars.df
        
        # Handle MultiIndex if present
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol)
            
        # Convert index to correct timezone just in case
        df.index = df.index.tz_convert('US/Eastern')
        
        return df

    except Exception as e:
        print(f"  [!] Alpaca Data Error {symbol}: {e}")
        return None

def run_survivor_bot():
    print(f"--- SURVIVOR BOT (Hybrid V3 + Logger) STARTED ---")
    send_discord("🚑 **Survivor Bot V3 Online**")

    log_to_influx("SYSTEM", "startup", 0, 0)
    
    while True:
        try:
            # Check Market Open
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            buying_power = float(account.buying_power)

            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}
            
            # --- 1. EMERGENCY HARD STOP (Runs First) ---
            for p in positions:
                if p.symbol not in SYMBOLS:
                    continue

                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                pct_diff = (current_price - entry_price) / entry_price
                
                # WIDER STOP for Leveraged ETFs (e.g., -6%)
                MAX_SURVIVOR_LOSS = -0.06 
                
                if pct_diff < MAX_SURVIVOR_LOSS:
                    print(f"🚑 SURVIVOR BAILOUT: {p.symbol} down {pct_diff*100:.2f}%. Selling.")
                    
                    req = MarketOrderRequest(
                        symbol=p.symbol,
                        qty=float(p.qty),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=req)
                    
                    log_to_influx(p.symbol, "survivor_stop", current_price, float(p.qty))
                    send_discord(f"🚑 **SURVIVOR EMERGENCY STOP**\nSold {p.symbol} at ${current_price}\nLoss: {pct_diff*100:.2f}%")
                    continue 

            print(f"\nScanning Basket at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

            for symbol in SYMBOLS:
                try:
                    # --- 2. GET DATA (ALPACA) ---
                    df = get_data_alpaca(symbol)

                    if df is None or len(df) < 200:
                        print(f"    [!] Insufficient data for {symbol} (Need 200 days for SMA)")
                        continue

                    # --- 3. CALCULATE INDICATORS ---
                    # Note: Alpaca returns lowercase column names ('close', not 'Close')
                    
                    # A. The Trend Filter (200 SMA)
                    df['SMA_200'] = ta.sma(df['close'], length=200)

                    # B. The Entry Trigger (RSI 14)
                    df['RSI'] = ta.rsi(df['close'], length=RSI_PERIOD)

                    # C. The "Cheap" Check (Bollinger Bands)
                    bbands = ta.bbands(df['close'], length=20, std=2.0)
                    df = pd.concat([df, bbands], axis=1)

                    # --- 4. PARSE LATEST VALUES ---
                    latest = df.iloc[-1]
                    price = float(latest['close'])
                    rsi = float(latest['RSI'])
                    sma_200 = float(latest['SMA_200'])
                    
                    # pandas_ta names columns: BBL_length_std
                    # We check if the key exists to avoid crashes
                    bbl_key = f'BBL_20_2.0'
                    if bbl_key not in latest:
                        print(f"    [!] Indicator Error: {bbl_key} not found in data.")
                        continue
                        
                    lower_bb = float(latest[bbl_key]) 

                    print(f"    -> {symbol}: Price=${price:.2f} | RSI={rsi:.1f} | SMA200=${sma_200:.2f}")

                    # --- 5. CHECK POSITIONS ---
                    pos_qty = 0
                    if symbol in pos_dict:
                        pos_qty = float(pos_dict[symbol].qty)

                    # --- 6. EXECUTION LOGIC ---
                    
                    # EXIT LOGIC (Take Profit)
                    if pos_qty > 0:
                        if rsi > RSI_EXIT:
                            print(f"    [EXIT] Profit Target Hit on {symbol} (RSI {rsi:.1f})")
                            req = MarketOrderRequest(symbol=symbol, qty=pos_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                            trading_client.submit_order(order_data=req)

                            send_discord(f"💰 **CLOSE {symbol}** (RSI Rebound)\nPrice: ${price:.2f}\nProfit Secured.")
                            log_to_influx(symbol, "sell_survivor", price, pos_qty)
                    
                    # ENTRY LOGIC (Buy the Dip)
                    else:
                        # CRITICAL FILTER: Only buy if Price is ABOVE the 200 SMA
                        is_uptrend = price > sma_200
                        
                        if not is_uptrend:
                            # Below SMA 200 = Bear market, do not buy dips
                            continue 

                        if rsi < RSI_OVERSOLD and price < lower_bb:
                            print(f"    [ENTRY] VALID DIP on {symbol} (Uptrend Confirmed)")
                            
                            # Calculate Position Size
                            risk_amt = equity * RISK_PER_TRADE
                            qty = int(risk_amt / price)
                            
                            if qty > 0 and (qty * price) < buying_power:
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)

                                send_discord(f"🚑 **BUY {symbol}** (Trend Dip)\nPrice: ${price:.2f}\n200 SMA: ${sma_200:.2f}")
                                log_to_influx(symbol, "buy_survivor", price, qty)

                except Exception as e:
                    print(f"    [!] Error processing {symbol}: {e}")
                    continue
            
            # Check every 15 minutes to respect API limits and market pace
            time.sleep(900)

        except Exception as e:
            print(f"Global Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()