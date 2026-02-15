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
TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"
FAST_EMA = 9
SLOW_EMA = 21
RISK_PER_TRADE = 0.02

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

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

def get_market_regime():
    if not os.path.exists(CONFIG_FILE): return "UNKNOWN"
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            return data.get("global_settings", {}).get("market_condition", "UNKNOWN")
    except: return "UNKNOWN"

def get_dynamic_targets(regime):
    # 1. BEAR MODE: Short the market
    if regime == "BEAR_TREND":
        # Check for specific short targets or default to ETFs
        return ["SQQQ", "SPXU", "UVXY", "SOXS"]

    # 2. BULL/CHOP MODE
    # Default fallback if file is missing/stale
    static_fallback = ["NVDA", "TSLA", "COIN"]
    
    return utils.get_targets_with_freshness_check(TARGET_FILE, "trend_targets", static_fallback)


def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=500
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except: return None

def run_trend_bot():
    print(f"--- 昌 TREND SNIPER (Target Locked) STARTED ---")
    send_discord("**Trend Sniper V4.1** Online\nOwnership Logic Active.")
    
    while True:
        try:
            # 1. Check Market Hours
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print(f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed.", end='\r')
                    time.sleep(900)
                    continue
            except: pass

            # 2. [FIX] CFO Check OUTSIDE loop
            if not utils.check_budget("trend_bot", trading_client):
                 print(f"[{datetime.datetime.now().strftime('%H:%M')}] Trend Budget Paused.")
                 time.sleep(300)
                 continue

            global_regime = get_market_regime()
            targets = get_dynamic_targets(global_regime)
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now(TIMEZONE).strftime('%H:%M')}] Regime: {global_regime} | Targets: {len(targets)}")

            # 3. Only scan OUR targets + OUR existing positions
            # We filter existing positions to only those relevant to Trend Bot strategies
            my_holdings = [p.symbol for p in positions if p.asset_class == AssetClass.US_EQUITY and p.symbol in targets]
            scan_list = list(set(targets + my_holdings))

            for symbol in scan_list:
                if symbol in ["BTC/USD", "ETH/USD"]: continue 
                if "/" in symbol: continue 

                df = get_data_alpaca(symbol)
                if df is None: continue

                # Indicators
                df['ema_fast'] = ta.ema(df['close'], length=FAST_EMA)
                df['ema_slow'] = ta.ema(df['close'], length=SLOW_EMA)
                adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
                
                # ADX Column Fix
                adx_col = [c for c in adx_df.columns if c.startswith('ADX')][0]
                df['adx'] = adx_df[adx_col]

                latest = df.iloc[-1]
                prev = df.iloc[-2]
                
                price = float(latest['close'])
                local_adx = float(latest['adx'])
                
                # Signals
                bull_cross = (latest['ema_fast'] > latest['ema_slow']) and (prev['ema_fast'] <= prev['ema_slow'])
                bear_cross = (latest['ema_fast'] < latest['ema_slow']) and (prev['ema_fast'] >= prev['ema_slow'])

                # --- EXECUTION ---
                
                # A) EXIT LOGIC (Manage existing trades)
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    
                    # Exit Longs on Bear Cross
                    if bull_cross == False and bear_cross == True:
                        print(f"    悼 CLOSE LONG {symbol}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                        send_discord(f"悼 **SELL {symbol}** (Cross)\nPrice: ${price:.2f}")
                        log_to_influx(symbol, "sell", price, qty)

                # B) ENTRY LOGIC (Open new trades)
                elif symbol in targets:
                    # We only trade if ADX > 20 (Trend is strong)
                    if local_adx > 20:
                        should_buy = False
                        
                        if bull_cross:
                            should_buy = True

                        if should_buy:
                            risk_amt = equity * RISK_PER_TRADE
                            # Stop loss approx 2% away
                            qty = int(risk_amt / (price * 0.02))
                            
                            if qty > 0:
                                print(f"    噫 BUY SIGNAL {symbol}")
                                try:
                                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                    send_discord(f"噫 **BUY {symbol}**\nRegime: {global_regime}\nADX: {local_adx:.0f}")
                                    log_to_influx(symbol, "buy", price, qty)
                                except Exception as e:
                                    print(f"    [!] Order Error: {e}")

            time.sleep(60)

        except Exception as e:
            print(f"Trend Bot Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()