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

# --- ALPACA KEYS ---
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
    try:
        measurement = "survivor_trades"
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_data_alpaca(symbol):
    try:
        # 2 years for SMA 200
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=730)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=None,
            adjustment='all'
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except Exception as e:
        print(f"  [!] Alpaca Data Error {symbol}: {e}")
        return None

def run_survivor_bot():
    print(f"--- SURVIVOR BOT (Hybrid V3 + Alpaca Data) STARTED ---")
    send_discord("🚑 **Survivor Bot V3 Online**")
    log_to_influx("SYSTEM", "startup", 0, 0)
    
    while True:
        try:
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
            
            # --- 1. EMERGENCY STOP ---
            for p in positions:
                if p.symbol not in SYMBOLS: continue
                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                pct_diff = (current_price - entry_price) / entry_price
                MAX_LOSS = -0.06 
                
                if pct_diff < MAX_LOSS:
                    print(f"🚑 SURVIVOR BAILOUT: {p.symbol} down {pct_diff*100:.2f}%. Selling.")
                    req = MarketOrderRequest(symbol=p.symbol, qty=float(p.qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                    trading_client.submit_order(order_data=req)
                    log_to_influx(p.symbol, "survivor_stop", current_price, float(p.qty))
                    send_discord(f"🚑 **STOP LOSS**\nSold {p.symbol} at ${current_price}")
                    continue 

            print(f"\nScanning Basket at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

            for symbol in SYMBOLS:
                try:
                    df = get_data_alpaca(symbol)
                    if df is None or len(df) < 200:
                        print(f"    [!] Insufficient data for {symbol}")
                        continue

                    # Alpaca data is LOWERCASE keys
                    df['sma_200'] = ta.sma(df['close'], length=200)
                    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
                    
                    # Generate Bands (Usually returns Uppercase keys like BBL_...)
                    bbands = ta.bbands(df['close'], length=20, std=2.0)
                    df = pd.concat([df, bbands], axis=1)

                    latest = df.iloc[-1]
                    price = float(latest['close'])
                    rsi = float(latest['rsi'])
                    sma_200 = float(latest['sma_200'])
                    
                    # --- ROBUST COLUMN FINDER ---
                    # We look for ANY column starting with 'BBL' or 'bbl'
                    # The logs showed: 'BBL_20_2.0_2.0'
                    bbl_key = None
                    for col in latest.index:
                        if col.startswith("BBL") or col.startswith("bbl"):
                            bbl_key = col
                            break
                    
                    if not bbl_key:
                        print(f"    [!] BBL Column Missing. Available: {list(latest.index)}")
                        continue
                        
                    lower_bb = float(latest[bbl_key]) 

                    print(f"    -> {symbol}: Price=${price:.2f} | RSI={rsi:.1f} | SMA200=${sma_200:.2f}")

                    # --- EXECUTION ---
                    pos_qty = 0
                    if symbol in pos_dict:
                        pos_qty = float(pos_dict[symbol].qty)

                    if pos_qty > 0:
                        if rsi > RSI_EXIT:
                            print(f"    [EXIT] Profit Target on {symbol}")
                            req = MarketOrderRequest(symbol=symbol, qty=pos_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                            trading_client.submit_order(order_data=req)
                            send_discord(f"💰 **CLOSE {symbol}**\nPrice: ${price:.2f}")
                            log_to_influx(symbol, "sell_survivor", price, pos_qty)
                    
                    else:
                        is_uptrend = price > sma_200
                        if not is_uptrend: continue 

                        if rsi < RSI_OVERSOLD and price < lower_bb:
                            print(f"    [ENTRY] VALID DIP on {symbol}")
                            risk_amt = equity * RISK_PER_TRADE
                            qty = int(risk_amt / price)
                            if qty > 0 and (qty * price) < buying_power:
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)
                                send_discord(f"🚑 **BUY {symbol}**\nPrice: ${price:.2f}")
                                log_to_influx(symbol, "buy_survivor", price, qty)

                except Exception as e:
                    print(f"    [!] Error processing {symbol}: {e}")
            
            time.sleep(900)

        except Exception as e:
            print(f"Global Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()