import utils
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
RISK_PER_TRADE = 0.05 # Aggressive sizing for mean reversion

# --- CREDENTIALS & CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

# --- INFLUX & DISCORD ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_SURVIVOR: return # Reusing Trend webhook for now
    try: requests.post(config.WEBHOOK_SURVIVOR, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'survivor_trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_dynamic_targets():
    """Reads the 'Trend' list from the Scout."""
    # Survivor likes high volatility, so it shares targets with Trend Bot
    return utils.get_active_targets("trend_targets")

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
    print(f"--- 🛡️ SURVIVOR BOT (Bollinger Bandit) STARTED ---")
    send_discord("**Survivor Bot (V4)** Online\nStrategy: Dynamic Volatility Harvesting (Bollinger Bands)")
    
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
            full_watchlist = list(set(CORE_WATCHLIST + scout_targets))
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now(TIMEZONE).strftime('%H:%M')}] Scanning {len(full_watchlist)} Targets...")

            for symbol in full_watchlist:
                if symbol in ["BTC/USD", "ETH/USD"]: continue 

                df = get_data_alpaca(symbol)
                if df is None or len(df) < 20: continue

                # --- INDICATORS (UPDATED & FIXED) ---
                # 1. Bollinger Bands (20, 2)
                bbands = ta.bbands(df['close'], length=20, std=2.0)
                
                if bbands is None or bbands.empty:
                    print(f"    [!] Failed to calc BB for {symbol}")
                    continue

                # Merge indicators into main DF
                df = pd.concat([df, bbands], axis=1)

                # 2. RSI (Sanity Check)
                df['rsi'] = ta.rsi(df['close'], length=14)

                latest = df.iloc[-1]
                price = float(latest['close'])
                rsi = float(latest['rsi']) if pd.notna(latest['rsi']) else 50.0

                # --- DYNAMIC COLUMN FINDER ---
                # This fixes the 'BBL_20_2.0' vs 'BBL_20_2' error by grabbing whatever was generated
                # bbands columns usually: [BBL, BBM, BBU, BBB, BBP]
                try:
                    bbl_col = [c for c in bbands.columns if c.startswith('BBL')][0] # Lower
                    bbm_col = [c for c in bbands.columns if c.startswith('BBM')][0] # Mid
                    bbu_col = [c for c in bbands.columns if c.startswith('BBU')][0] # Upper
                    bbb_col = [c for c in bbands.columns if c.startswith('BBB')][0] # Bandwidth
                except IndexError:
                    print(f"    [!] Column Error on {symbol}. Cols: {bbands.columns}")
                    continue

                lower_band = float(latest[bbl_col])
                mid_band = float(latest[bbm_col])
                upper_band = float(latest[bbu_col])
                bandwidth = float(latest[bbb_col])

                # --- EXIT LOGIC ---
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    entry_price = float(pos.avg_entry_price)
                    pct_gain = (price - entry_price) / entry_price
                    
                    should_sell = False
                    reason = ""
                    
                    # Exit 1: Mean Reversion (Price touched the middle band)
                    if price >= mid_band:
                        should_sell = True
                        reason = "Mean Reverted (Hit Mid Band)"
                    
                    # Exit 2: Hard Stop Loss (-4%)
                    elif pct_gain < -0.04:
                        should_sell = True
                        reason = "Stop Loss (-4%)"

                    # Exit 3: RSI Blowout (If we rocketed past upper band)
                    elif price > upper_band and rsi > 75:
                        should_sell = True
                        reason = "Max Extension (Upper Band)"

                    if should_sell:
                        print(f"    📉 SELLING {symbol}: {reason}")
                        try:
                            trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                            send_discord(f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                            log_to_influx(symbol, "sell", price, qty)
                        except Exception as e:
                            print(f"    [!] Sell Error: {e}")

                # --- ENTRY LOGIC (Volatility Scoop) ---
                else:
                    # 1. CRASH CONDITION: Price is BELOW the Lower Band
                    # 2. VOLATILITY CONDITION: Bandwidth > 1.0 (Ensures we don't buy in dead markets)
                    if price < lower_band and bandwidth > 1.0:
                        
                        # [CFO CHECK]
                        if not utils.check_budget("survivor_bot", trading_client):
                            # print(f"    [SKIP] Survivor Budget Exceeded.")
                            continue

                        is_scout_pick = symbol in scout_targets
                        
                        print(f"    💎 VOLATILITY SCOOP: {symbol}")
                        print(f"       Price: {price:.2f} < Band: {lower_band:.2f} | Width: {bandwidth:.2f}")
                        
                        # Size Check
                        risk_amt = equity * RISK_PER_TRADE
                        qty = int(risk_amt / price)
                        
                        if qty > 0:
                            print(f"       -> Buying {qty} shares...")
                            try:
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                source_tag = "SCOUT PICK" if is_scout_pick else "CORE"
                                send_discord(f"💎 **BOUGHT DIP {symbol}** ({source_tag})\nBelow Lower Band\nVol Width: {bandwidth:.2f}")
                                log_to_influx(symbol, "buy", price, qty)
                            except Exception as e:
                                print(f"       Order Failed: {e}")

            time.sleep(60)

        except Exception as e:
            print(f"Survivor Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()