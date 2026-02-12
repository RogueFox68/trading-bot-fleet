import utils
import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import pandas_ta as ta
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
# Core ETFs are always treated as "Long" candidates.
# (Note: Buying SQQQ is technically a "Long" position on a Bear ETF)
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
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=120)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), 
            start=start_time,
            limit=500
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        return df
    except: return None

def get_mission_map():
    """
    Combines Core+Trend (Bull) and Short (Bear) into a unified mission map.
    Returns: {"NVDA": "LONG", "RIVN": "SHORT", ...}
    """
    # 1. Bull List (Core + Trend Targets)
    trend_targets = utils.get_active_targets("trend_targets")
    bull_list = list(set(CORE_WATCHLIST + trend_targets))
    
    # 2. Bear List (Short Targets)
    bear_list = utils.get_active_targets("short_targets")
    
    mission_map = {t: "LONG" for t in bull_list}
    mission_map.update({t: "SHORT" for t in bear_list})
    
    return mission_map

def run_survivor_bot():
    print(f"--- 🩹 SURVIVOR BOT V3 (Bi-Directional) STARTED ---")
    send_discord("**Survivor Bot V3** Online\nBuying Dips (Long) & Shorting Rips (Short).")
    
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
            mission_map = get_mission_map()
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning {len(mission_map)} Targets...")

            for symbol, side in mission_map.items():
                if symbol in ["BTC/USD", "ETH/USD"]: continue 
                if "/" in symbol: continue

                df = get_data_alpaca(symbol)
                if df is None or len(df) < 50: continue

                # --- INDICATORS ---
                # Middle Band = 20 SMA
                df['middle_band'] = df['close'].rolling(window=20).mean()
                df['std_dev'] = df['close'].rolling(window=20).std()
                df['upper_band'] = df['middle_band'] + (2.0 * df['std_dev'])
                df['lower_band'] = df['middle_band'] - (2.0 * df['std_dev'])
                df['sma200'] = ta.sma(df['close'], length=200)

                latest = df.iloc[-1]
                price = float(latest['close'])
                lower_band = float(latest['lower_band'])
                upper_band = float(latest['upper_band'])
                middle_band = float(latest['middle_band'])
                
                # SMA Safety
                sma200 = float(latest['sma200']) if not pd.isna(latest['sma200']) else 0
                if sma200 == 0: continue # Not enough data

                # --- EXIT LOGIC (Manage Existing Positions) ---
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    entry = float(pos.avg_entry_price)
                    
                    should_close = False
                    reason = ""
                    
                    # CASE A: WE ARE LONG (Qty > 0)
                    if qty > 0:
                        pct_gain = (price - entry) / entry
                        # Take Profit: Hit Upper Band or Middle Band (Conservative)
                        if price >= upper_band:
                            should_close = True
                            reason = f"Hit Upper Band (${upper_band:.2f})"
                        # Stop Loss: -3%
                        elif pct_gain < -0.03:
                            should_close = True
                            reason = "Stop Loss (-3%)"

                    # CASE B: WE ARE SHORT (Qty < 0)
                    elif qty < 0:
                        # Short Gain: (Entry - Price) / Entry
                        pct_gain = (entry - price) / entry
                        # Take Profit: Hit Lower Band (Cover)
                        if price <= lower_band:
                            should_close = True
                            reason = f"Hit Lower Band (${lower_band:.2f})"
                        # Stop Loss: -3% (Price went up 3%)
                        elif pct_gain < -0.03:
                            should_close = True
                            reason = "Stop Loss (-3%)"

                    if should_close:
                        action = "SELL" if qty > 0 else "BUY" # Close Long vs Cover Short
                        print(f"    🩹 CLOSING {symbol} ({action}): {reason}")
                        # To close, we do opposite side of current qty
                        close_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=abs(int(qty)), side=close_side, time_in_force=TimeInForce.GTC))
                        send_discord(f"🩹 **CLOSED {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                        log_to_influx(symbol, "close", price, qty)

                # --- ENTRY LOGIC (Open New Positions) ---
                else:
                    # CFO Check
                    if not utils.check_budget("survivor_bot", trading_client): continue

                    # CASE A: LONG MISSION (Buy the Dip)
                    if side == "LONG":
                        # Rule: Uptrend (Price > SMA200) AND Touched Lower Band
                        if price > sma200 and price <= lower_band:
                            print(f"    🟢 LONG DIP: {symbol} (${price:.2f})")
                            
                            account = trading_client.get_account()
                            risk_amt = float(account.portfolio_value) * 0.05
                            qty = int(risk_amt / price)
                            
                            if qty > 0:
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                send_discord(f"🟢 **LONG {symbol}**\nStrategy: Survivor Dip\nPrice: ${price:.2f}")

                    # CASE B: SHORT MISSION (Short the Rip)
                    elif side == "SHORT":
                        # Rule: Downtrend (Price < SMA200) AND Touched Upper Band
                        if price < sma200 and price >= upper_band:
                            print(f"    🔴 SHORT RIP: {symbol} (${price:.2f})")
                            
                            account = trading_client.get_account()
                            risk_amt = float(account.portfolio_value) * 0.05
                            qty = int(risk_amt / price)
                            
                            # Check Buying Power for Shorting
                            if (qty * price) > float(account.buying_power):
                                print("    [!] Insufficient BP to Short.")
                                continue

                            if qty > 0:
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
                                send_discord(f"🔴 **SHORT {symbol}**\nStrategy: Survivor Rip\nPrice: ${price:.2f}")

            time.sleep(60)

        except Exception as e:
            print(f"Survivor Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()