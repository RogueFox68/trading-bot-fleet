from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
import datetime
import time  # <--- FIXED: Added missing import
import requests
import pandas as pd
import config
from logger import get_logger
import utils

# --- LOGGING ---
logger = get_logger("moon_bot")

# --- CONFIGURATION ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"] 
LOOKBACK_ENTRY = 20  # Buy if we break the 20-day high
LOOKBACK_EXIT = 10   # Sell if we break the 10-day low
RISK_PCT = 0.10      # Allocate 10% of equity per trade (Aggressive)

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = CryptoHistoricalDataClient()

def send_discord(msg):
    # Canonical key is WEBHOOK_MOONBAG; fall back to the legacy WEBHOOK_MOONBOT name.
    webhook = getattr(config, 'WEBHOOK_MOONBAG', '') or getattr(config, 'WEBHOOK_MOONBOT', '')
    if not webhook or "YOUR" in webhook:
        return
    try:
        payload = {"content": msg, "username": "Moon Bot 🚀"}
        requests.post(webhook, json=payload)
    except Exception as e:
        logger.error(f"[!] Discord Error: {e}")

def get_donchian_levels(symbol):
    try:
        # 1. Fetch History for Levels
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=60)
        req = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=30
        )
        bars = data_client.get_crypto_bars(req)
        df = bars.df.loc[symbol]
        
        # Exclude current incomplete bar for levels
        completed_candles = df.iloc[:-1] 
        
        entry_high = completed_candles['high'].tail(LOOKBACK_ENTRY).max()
        exit_low = completed_candles['low'].tail(LOOKBACK_EXIT).min()
        
        # 2. Fetch REAL-TIME Price for Execution (The Fix)
        trade_req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        trade = data_client.get_crypto_latest_trade(trade_req)
        current_price = float(trade[symbol].price)
        
        return entry_high, exit_low, current_price
    except Exception as e:
        logger.error(f"Data Error {symbol}: {e}")
        return None, None, None

def run_breakout_bot():
    logger.info("--- 🚀 MOON BOT BREAKOUT BOT STARTED ---")
    send_discord("🚀 **Moon Bot Online**\nStrategy: Donchian Breakout (20/10)")
    
    while True:
        try:
            account = trading_client.get_account()
            equity = float(account.equity)
            buying_power = float(account.buying_power)
            
            # Get current positions
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: float(p.qty) for p in positions}

            logger.info(f"Scanning Markets... Equity: ${equity:,.2f}")

            for symbol in SYMBOLS:
                try:
                    entry_high, exit_low, current_price = get_donchian_levels(symbol)
                    if current_price is None: continue

                    qty_held = pos_dict.get(symbol, 0)
                    
                    logger.info(f"  {symbol:<8} | Price: ${current_price:,.2f} | Breakout: ${entry_high:,.2f} | Stop: ${exit_low:,.2f}")

                    # --- ENTRY LOGIC ---
                    if qty_held == 0:
                        if current_price > entry_high:
                            logger.info(f"    [SIGNAL] BREAKOUT! Price ${current_price} > ${entry_high}")
                            
                            is_budget_ok = utils.check_budget("moon_bot", trading_client)
                            if not is_budget_ok:
                                logger.warning(f"    [SKIP] Breakout buy blocked — CFO Budget limit reached.")
                                continue
                                
                            # Calculate Size
                            target_val = equity * RISK_PCT
                            qty_to_buy = target_val / current_price
                            
                            if (qty_to_buy * current_price) > buying_power:
                                logger.info("    [!] Insufficient Buying Power")
                                continue

                            c_id = f"moon_bot-{symbol.replace('/', '')}-{int(time.time())}"
                            req = MarketOrderRequest(
                                symbol=symbol,
                                qty=round(qty_to_buy, 4),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.GTC,
                                client_order_id=c_id
                            )
                            utils.submit_and_log_order(trading_client, req, logger, log_action="buy_breakout")
                            
                            send_discord(f"🚀 **MOONSHOT ENTRY: {symbol}**\nBreakout Price: ${current_price}\nTargeting trends.")

                    # --- EXIT LOGIC ---
                    elif qty_held > 0:
                        if current_price < exit_low:
                            logger.info(f"    [SIGNAL] TRAILING STOP! Price ${current_price} < ${exit_low}")
                            
                            c_id = f"moon_bot-{symbol.replace('/', '')}-{int(time.time())}"
                            req = MarketOrderRequest(
                                symbol=symbol,
                                qty=qty_held,
                                side=OrderSide.SELL,
                                time_in_force=TimeInForce.GTC,
                                client_order_id=c_id
                            )
                            utils.submit_and_log_order(trading_client, req, logger, log_action="sell_breakout")
                            
                            send_discord(f"🛑 **STOP LOSS: {symbol}**\nPrice: ${current_price}\nTrend broken.")
                        else:
                            logger.info(f"    [HOLD] Riding the trend.")

                except Exception as e:
                    logger.error(f"    [!] Error {symbol}: {e}")

            # Sleep for 1 hour (Crypto markets move 24/7)
            time.sleep(3600)

        except Exception as e:
            logger.error(f"Global Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_breakout_bot()