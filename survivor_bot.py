import config
import time
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import pytz
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

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

# --- INFLUXDB (Added for Grafana) ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
TIMEZONE = pytz.timezone('US/Eastern')

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
        df = ticker.history(period="5d", interval="5m")
        if df.empty: return None
        df.index = df.index.tz_convert('US/Eastern')
        df.columns = [c.lower() for c in df.columns]
        return df
    except Exception as e:
        print(f"  [!] Yahoo Data Error {symbol}: {e}")
        return None

def calculate_indicators(df):
    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
    bb = ta.bbands(df['close'], length=20, std=2)
    # Dynamic column finding
    lower_col = [c for c in bb.columns if c.startswith('BBL')][0]
    upper_col = [c for c in bb.columns if c.startswith('BBU')][0]
    df['lower_bb'] = bb[lower_col]
    df['upper_bb'] = bb[upper_col]
    return df

def run_survivor_bot():
    print(f"--- SURVIVOR BOT (Hybrid V3 + Logger) STARTED ---")
    send_discord("🚑 **Survivor Bot V3 Online**")

    # --- ADD THIS LINE ---
    log_to_influx("SYSTEM", "startup", 0, 0)
    # ---------------------
    
    while True:
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
            # ... inside run_survivor_bot loop ...
            
            for p in positions:
                # 1. Only manage Survivor symbols (Ignore UNH, TSLA, etc.)
                if p.symbol not in SYMBOLS:
                    continue

                # 2. EMERGENCY HARD STOP (The "Amnesia" Fix)
                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                pct_diff = (current_price - entry_price) / entry_price
                
                # WIDER STOP for Leveraged ETFs (e.g., -6%)
                # Adjust this: -0.05 is 5%, -0.08 is 8%
                MAX_SURVIVOR_LOSS = -0.06 
                
                if pct_diff < MAX_SURVIVOR_LOSS:
                    print(f"🚨 SURVIVOR BAILOUT: {p.symbol} down {pct_diff*100:.2f}%. Selling.")
                    
                    req = MarketOrderRequest(
                        symbol=p.symbol,
                        qty=float(p.qty),
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC
                    )
                    trading_client.submit_order(order_data=req)
                    
                    log_to_influx(p.symbol, "survivor_stop", current_price, float(p.qty))
                    send_discord(f"🛑 **SURVIVOR EMERGENCY STOP**\nSold {p.symbol} at ${current_price}\nLoss: {pct_diff*100:.2f}%")
                    continue 

                # ... existing RSI logic continues below ...

            print(f"\nScanning Basket at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

for symbol in SYMBOLS:
            try:
                # --- 1. GET DATA ---
                # We need 2 years of Daily data to calculate a valid 200 SMA
                df = yf.download(symbol, period="2y", interval="1d", progress=False)

                if df.empty or len(df) < 200:
                    print(f"    [!] Insufficient data for {symbol}")
                    continue

                # Fix for YFinance MultiIndex issues (common bug in recent versions)
                if isinstance(df.columns, pd.MultiIndex):
                    try:
                        df = df.xs(symbol, axis=1, level=1)
                    except:
                        # Fallback if structure is different
                        df.columns = df.columns.droplevel(1)

                # --- 2. CALCULATE INDICATORS ---
                # A. The Trend Filter (200 SMA)
                df['SMA_200'] = ta.sma(df['Close'], length=200)

                # B. The Entry Trigger (RSI 14)
                df['RSI'] = ta.rsi(df['Close'], length=RSI_PERIOD)

                # C. The "Cheap" Check (Bollinger Bands)
                bbands = ta.bbands(df['Close'], length=20, std=2.0)
                # Concatenate the bands to the main dataframe
                df = pd.concat([df, bbands], axis=1)

                # --- 3. PARSE LATEST VALUES ---
                latest = df.iloc[-1]
                price = float(latest['Close'])
                rsi = float(latest['RSI'])
                sma_200 = float(latest['SMA_200'])
                # pandas_ta names columns: BBL_length_std
                lower_bb = float(latest[f'BBL_20_2.0']) 

                # Debug print to see what the bot is thinking
                print(f"    -> {symbol}: Price=${price:.2f} | RSI={rsi:.1f} | SMA200=${sma_200:.2f}")

                # --- 4. CHECK POSITIONS ---
                # Check if we already own it
                # (Note: Alpaca symbols usually don't have slashes, but ETFs are safe)
                pos_qty = 0
                if symbol in pos_dict:
                    pos_qty = float(pos_dict[symbol].qty)

                # --- 5. EXECUTION LOGIC ---
                
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
                        # If we are below SMA 200, we ignore oversold signals (save money!)
                        continue 

                    if rsi < RSI_OVERSOLD and price < lower_bb:
                        print(f"    [ENTRY] VALID DIP on {symbol} (Uptrend Confirmed)")
                        
                        # Calculate Position Size
                        risk_amt = equity * RISK_PER_TRADE
                        qty = int(risk_amt / price)
                        
                        if qty > 0 and (qty * price) < buying_power:
                            req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                            trading_client.submit_order(order_data=req)

                            send_discord(f"📉 **BUY {symbol}** (Trend Dip)\nPrice: ${price:.2f}\n200 SMA: ${sma_200:.2f}")
                            log_to_influx(symbol, "buy_survivor", price, qty)

            except Exception as e:
                print(f"    [!] Error processing {symbol}: {e}")
                continue

if __name__ == "__main__":
    run_survivor_bot()
