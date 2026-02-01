import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import pandas_ta as ta
import pytz
import utils
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json" # <--- NEW: Dynamic List
STATUS_FILE = "market_status.json"
FAST_EMA = 9
SLOW_EMA = 21
ADX_THRESHOLD = 25
RISK_PER_TRADE = 0.02

# --- CREDENTIALS & CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

# --- INFLUX & DISCORD (Helpers) ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_targets():
    """Reads the dynamic list from Sector Scout."""
    default = ["NVDA", "TSLA", "COIN"] # Fallback
    if not os.path.exists(TARGET_FILE): return default
    try:
        with open(TARGET_FILE, 'r') as f:
            data = json.load(f)
            return data.get("targets", default)
    except: return default

def get_market_regime():
    if not os.path.exists(STATUS_FILE): return "UNKNOWN"
    try:
        with open(STATUS_FILE, 'r') as f:
            return json.load(f).get("regime", "UNKNOWN")
    except: return "UNKNOWN"

def get_data_alpaca(symbol):
    try:
        # Get enough data for EMA21 and ADX
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        req = StockBarsRequest(symbol_or_symbols=[symbol], timeframe=TimeFrame(15, TimeFrameUnit.Minute), start=start_time, limit=500)
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except: return None

def run_trend_bot():
    print(f"--- TREND SNIPER (Dynamic Hunter) STARTED ---")
    send_discord("**Trend Sniper V3 (Dynamic)** Online")
    
    while True:
        try:
            # 1. Check Clock
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print("Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            # 2. Load Intel
            symbols = get_targets()
            global_regime = get_market_regime()
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now(TIMEZONE).strftime('%H:%M')}] Regime: {global_regime} | Targets: {len(symbols)}")

            # 3. Scan Targets
            # We scan the Dynamic List + Anything we currently hold (to manage exits)
            scan_list = list(set(symbols + [p.symbol for p in positions if p.asset_class == AssetClass.US_EQUITY]))

            for symbol in scan_list:
                if symbol in ["BTC/USD", "ETH/USD"]: continue # Skip crypto
                
                df = get_data_alpaca(symbol)
                if df is None: continue

                # Calculate Indicators
                df['ema_fast'] = ta.ema(df['close'], length=FAST_EMA)
                df['ema_slow'] = ta.ema(df['close'], length=SLOW_EMA)
                adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
                df = pd.concat([df, adx_df], axis=1)

                latest = df.iloc[-1]
                prev = df.iloc[-2]
                
                # Dynamic column name handling for ADX
                adx_col = [c for c in latest.index if c.startswith('ADX')][0]
                local_adx = float(latest[adx_col])
                price = float(latest['close'])
                
                # --- THE OVERRIDE LOGIC ---
                # Default: Obey Global Regime
                can_trade = True
                
                if "CHOP" in global_regime:
                    # override if THIS stock is trending hard (ADX > 30)
                    if local_adx > 30:
                        can_trade = True
                        print(f"    ! {symbol} defying CHOP (ADX {local_adx:.1f})")
                    else:
                        can_trade = False
                
                # Signals
                bull_cross = (latest['ema_fast'] > latest['ema_slow']) and (prev['ema_fast'] <= prev['ema_slow'])
                bear_cross = (latest['ema_fast'] < latest['ema_slow']) and (prev['ema_fast'] >= prev['ema_slow'])
                trend_up = latest['ema_fast'] > latest['ema_slow']
                trend_down = latest['ema_fast'] < latest['ema_slow']

                # --- EXECUTION ---
                
                # EXIT LOGIC (Always Active)
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    side = pos.side # 'long' or 'short'
                    
                    if side == 'long' and bear_cross:
                        print(f"    📉 CLOSE LONG {symbol}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                        send_discord(f"📉 **SELL {symbol}** (Cross)")
                        log_to_influx(symbol, "sell", price, qty)
                        
                    elif side == 'short' and bull_cross:
                        print(f"    📈 CLOSE SHORT {symbol}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=abs(qty), side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
                        send_discord(f"📈 **COVER {symbol}** (Cross)")
                        log_to_influx(symbol, "buy_cover", price, abs(qty))

                # ENTRY LOGIC (If Allowed)
                elif can_open_new and symbol not in pos_dict and symbol in SYMBOLS:
    
                    # [NEW] CFO CHECK
                    if not utils.check_budget("trend_bot", trading_client):
                        print(f"    [SKIP] Trend Bot Budget Exceeded.")
                        continue
                    if bull_cross and local_adx > 20:
                        risk_amt = equity * RISK_PER_TRADE
                        # Simple stop at recent low (approx 2% risk)
                        stop_dist = price * 0.02
                        qty = int(risk_amt / stop_dist)
                        
                        if qty > 0:
                            print(f"    🚀 BUY SIGNAL {symbol}")
                            trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                            send_discord(f"🚀 **BUY {symbol}** (Sector Play)")
                            log_to_influx(symbol, "buy", price, qty)
                    
                    elif bear_cross and local_adx > 20:
                        risk_amt = equity * RISK_PER_TRADE
                        stop_dist = price * 0.02
                        qty = int(risk_amt / stop_dist)

                        if qty > 0:
                            print(f"    🐻 SHORT SIGNAL {symbol}")
                            trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                            send_discord(f"🐻 **SHORT {symbol}** (Sector Play)")
                            log_to_influx(symbol, "sell_short", price, qty)

            time.sleep(60)

        except Exception as e:
            print(f"Trend Bot Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()