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
SYMBOLS = ["NVDA", "TSLA", "COIN", "MSTR", "AMD", "PLTR", "META"]
FAST_EMA = 7
SLOW_EMA = 21
RISK_PER_TRADE = 0.02
MAX_POS_SIZE = 0.25

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER
DISCORD_URL = config.WEBHOOK_TREND

# --- INFLUXDB ---
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
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        measurement = "trades"
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=200,
            adjustment='all'
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df
        if isinstance(df.index, pd.MultiIndex): df = df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except Exception as e:
        print(f"  [!] Alpaca Data Error {symbol}: {e}")
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
    send_discord("**Trend Sniper (Elite) Online**")
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

            can_open_new = check_time_rules()
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            buying_power = float(account.buying_power)

            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}
            
            # --- HARD STOP CHECK (The Fix is Here) ---
            for p in positions:
                # FIX: Explicitly ignore CRYPTO and OPTION assets
                # This prevents it from seeing "BTCUSD" and selling it.
                if p.asset_class == AssetClass.CRYPTO: continue 
                if p.asset_class == AssetClass.US_OPTION: continue
                
                # Also ignore specific Survivor symbols just in case
                if p.symbol in ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"]: continue

                entry_price = float(p.avg_entry_price)
                current_price = float(p.current_price)
                pct_diff = (current_price - entry_price) / entry_price
                
                if pct_diff < -0.035:
                    print(f"💥 HARD STOP: {p.symbol} down {pct_diff*100:.2f}%.")
                    req = MarketOrderRequest(symbol=p.symbol, qty=float(p.qty), side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                    trading_client.submit_order(order_data=req)
                    send_discord(f"💥 **HARD STOP** {p.symbol}")
                    continue

            # --- PREPARE SCAN LIST ---
            # FIX: Ensure we don't accidentally add Crypto/Options to the scan list
            held_tickers = []
            for p in positions:
                if p.asset_class == AssetClass.US_EQUITY and \
                   p.symbol not in ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"]:
                    held_tickers.append(p.symbol)

            scan_list = list(set(SYMBOLS + held_tickers))

            print(f"\nScanning {len(scan_list)} Assets ({'OPEN' if can_open_new else 'PAUSED'}) at {datetime.datetime.now(TIMEZONE).strftime('%H:%M')}...")

            for symbol in scan_list:
                df = get_data_alpaca(symbol)
                if df is None or len(df) < 30: continue

                df = calculate_indicators(df)
                signal_candle = df.iloc[-2] 
                execution_candle = df.iloc[-1]
                price = execution_candle['close'] 
                
                ema7 = signal_candle['ema7']
                ema21 = signal_candle['ema21']
                prev_ema7 = df.iloc[-3]['ema7'] 
                prev_ema21 = df.iloc[-3]['ema21']

                trend_is_up = ema7 > ema21
                trend_is_down = ema7 < ema21
                bullish_cross = (ema7 > ema21) and (prev_ema7 <= prev_ema21)
                bearish_cross = (ema7 < ema21) and (prev_ema7 >= prev_ema21)

                print(f"  {symbol:<4} | Price: {price:.2f} | 7EMA: {ema7:.2f} | 21EMA: {ema21:.2f}")

                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    side = pos.side
                    if side == 'long' and trend_is_down:
                        print(f"    [EXIT] Trend Reversal (Long) on {symbol}")
                        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                        trading_client.submit_order(order_data=req)
                        send_discord(f"📉 **CLOSE {symbol}**")
                        log_to_influx(symbol, "sell", price, qty)
                    elif side == 'short' and trend_is_up:
                        print(f"    [EXIT] Trend Reversal (Short) on {symbol}")
                        req = MarketOrderRequest(symbol=symbol, qty=abs(qty), side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
                        trading_client.submit_order(order_data=req)
                        send_discord(f"📈 **CLOSE {symbol}**")
                        log_to_influx(symbol, "buy_cover", price, abs(qty))

                elif can_open_new and symbol not in pos_dict and symbol in SYMBOLS:
                    if bullish_cross:
                        stop_price = df['low'].iloc[-5:].min()
                        if (price - stop_price) > 0.05:
                            risk_qty = int((equity * RISK_PER_TRADE) / (price - stop_price))
                            cap_qty = int((equity * MAX_POS_SIZE) / price)
                            qty = min(risk_qty, cap_qty)
                            if qty > 0 and (qty*price) < buying_power:
                                print(f"    [ENTRY] LONG {symbol}")
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)
                                send_discord(f"🚀 **BUY {symbol}**")
                                log_to_influx(symbol, "buy", price, qty)
                    
                    elif bearish_cross:
                        stop_price = df['high'].iloc[-5:].max()
                        if (stop_price - price) > 0.05:
                            risk_qty = int((equity * RISK_PER_TRADE) / (stop_price - price))
                            cap_qty = int((equity * MAX_POS_SIZE) / price)
                            qty = min(risk_qty, cap_qty)
                            if qty > 0 and (qty*price) < buying_power:
                                print(f"    [ENTRY] SHORT {symbol}")
                                req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                                trading_client.submit_order(order_data=req)
                                send_discord(f"🐻 **SHORT {symbol}**")
                                log_to_influx(symbol, "sell_short", price, qty)

            time.sleep(60)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()