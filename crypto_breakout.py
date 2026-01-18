from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import datetime
import time  # <--- FIXED: Added missing import
import requests
import pandas as pd
import config

# --- CONFIGURATION ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"] 
LOOKBACK_ENTRY = 20  # Buy if we break the 20-day high
LOOKBACK_EXIT = 10   # Sell if we break the 10-day low
RISK_PCT = 0.10      # Allocate 10% of equity per trade (Aggressive)

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = CryptoHistoricalDataClient()

def send_discord(msg):
    try:
        # FIXED: Using the specific Moon Bag webhook
        payload = {"content": msg, "username": "MoonBag Bot 🚀"}
        # Checks if the specific key exists, falls back to default if not
        webhook = getattr(config, 'WEBHOOK_MOONBAG')
        requests.post(webhook, json=payload)
    except Exception as e:
        print(f"[!] Discord Error: {e}")

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'breakout_trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_donchian_levels(symbol):
    """
    Calculates the Donchian Channel (20-day High, 10-day Low).
    Returns (entry_level, exit_level, current_price)
    """
    # FIX: Explicitly ask for data starting 60 days ago
    # This ensures we get the full 30-day history we need
    start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)

    req = CryptoBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Day,
        start=start_time,  # <--- Critical Addition
        limit=30
    )
    bars = data_client.get_crypto_bars(req)
    df = bars.df.loc[symbol]
    
    # We exclude the 'current' unfinished candle for calculation
    completed_candles = df.iloc[:-1] 
    
    # Safety Check: Do we actually have enough data?
    if len(completed_candles) < LOOKBACK_ENTRY:
        print(f"    [!] Not enough history for {symbol} (Got {len(completed_candles)} bars)")
        return float('nan'), float('nan'), df.iloc[-1]['close']

    entry_high = completed_candles['high'].tail(LOOKBACK_ENTRY).max()
    exit_low = completed_candles['low'].tail(LOOKBACK_EXIT).min()
    current_price = df.iloc[-1]['close']
    
    return entry_high, exit_low, current_price

def run_breakout_bot():
    print("--- 🚀 MOON BAG BREAKOUT BOT STARTED ---")
    send_discord("🚀 **Moon Bag Bot Online**\nStrategy: Donchian Breakout (20/10)")
    
    while True:
        try:
            account = trading_client.get_account()
            equity = float(account.equity)
            buying_power = float(account.buying_power)
            
            # Get current positions
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: float(p.qty) for p in positions}

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning Markets...")

            for symbol in SYMBOLS:
                try:
                    entry_high, exit_low, current_price = get_donchian_levels(symbol)
                    qty_held = pos_dict.get(symbol, 0)
                    
                    print(f"  {symbol:<8} | Price: ${current_price:,.2f} | Breakout: ${entry_high:,.2f} | Stop: ${exit_low:,.2f}")

                    # --- ENTRY LOGIC ---
                    if qty_held == 0:
                        if current_price > entry_high:
                            print(f"    [SIGNAL] BREAKOUT! Price ${current_price} > ${entry_high}")
                            
                            # Calculate Size
                            target_val = equity * RISK_PCT
                            qty_to_buy = target_val / current_price
                            
                            if (qty_to_buy * current_price) > buying_power:
                                print("    [!] Insufficient Buying Power")
                                continue

                            req = MarketOrderRequest(
                                symbol=symbol,
                                qty=round(qty_to_buy, 4),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.GTC
                            )
                            trading_client.submit_order(order_data=req)
                            
                            send_discord(f"🚀 **MOONSHOT ENTRY: {symbol}**\nBreakout Price: ${current_price}\nTargeting trends.")
                            log_to_influx(symbol, "buy_breakout", current_price, qty_to_buy)

                    # --- EXIT LOGIC ---
                    elif qty_held > 0:
                        if current_price < exit_low:
                            print(f"    [SIGNAL] TRAILING STOP! Price ${current_price} < ${exit_low}")
                            
                            req = MarketOrderRequest(
                                symbol=symbol,
                                qty=qty_held,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC
                            )
                            trading_client.submit_order(order_data=req)
                            
                            send_discord(f"🛑 **STOP LOSS: {symbol}**\nPrice: ${current_price}\nTrend broken.")
                            log_to_influx(symbol, "sell_breakout", current_price, qty_held)
                        else:
                            print(f"    [HOLD] Riding the trend.")

                except Exception as e:
                    print(f"    [!] Error {symbol}: {e}")

            # Sleep for 1 hour (Crypto markets move 24/7)
            time.sleep(3600)

        except Exception as e:
            print(f"Global Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_breakout_bot()