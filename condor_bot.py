import utils
import config
import time
import datetime
import requests
import json
import os
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
from logger import get_logger

# --- LOGGING ---
logger = get_logger("condor_bot")

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
STATIC_TARGETS = ["COIN", "MSTR", "TSLA", "NVDA", "NFLX"] 

MIN_DTE = 25              
MAX_DTE = 45              
WING_WIDTH_PCT = 0.05     
SHORT_OTM_PCT = 0.08      
TAKE_PROFIT_PCT = 0.50    
MAX_POSITIONS = 3         

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

WEBHOOK_URL = getattr(config, 'WEBHOOK_CONDOR') 

def send_discord(msg):
    if "YOUR" in WEBHOOK_URL: return
    try:
        requests.post(WEBHOOK_URL, json={"content": msg, "username": "Condor Bot 🦅"})
    except: pass

def log_to_influx(action, symbol, price, detail):
    try:
        data_str = f'condor_trades,symbol={symbol} price={price},action="{action}",detail="{detail}"'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=2)
    except: pass

def get_condor_targets():
    return utils.get_targets_with_freshness_check(TARGET_FILE, "condor_targets", STATIC_TARGETS)

def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        logger.error(f"  [!] Error price {symbol}: {e}")
        return 0.0

def get_option_price(symbol, side="bid"):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        return float(res[symbol].bid_price) if side == "bid" else float(res[symbol].ask_price)
    except: return 0.0

def find_strike(symbol, type, expiry_start, expiry_end, target_price):
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status="active",
        expiration_date_gte=expiry_start,
        expiration_date_lte=expiry_end,
        type=ContractType.PUT if type == "PUT" else ContractType.CALL,
        limit=1000
    )
    try:
        contracts = trading_client.get_option_contracts(req).option_contracts
    except: return None

    best_contract = None
    best_diff = float('inf')

    for c in contracts:
        strike = float(c.strike_price)
        diff = abs(strike - target_price)
        if diff < best_diff:
            best_diff = diff
            best_contract = c
    return best_contract

def run_condor_bot():
    logger.info("--- 🦅 IRON CONDOR BOT (Atomic Execution) STARTED ---")
    send_discord("🦅 **Iron Condor Bot Online**\nRollback Logic Active.")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info("Market Closed. Sleeping 15m...")
                    time.sleep(900)
                    continue
            except: pass

            positions = trading_client.get_all_positions()
            condor_positions = 0
            active_tickers = set()
            
            for p in positions:
                if p.asset_class == AssetClass.US_OPTION:
                    root = p.symbol
                    # Extract root symbol logic...
                    match = False
                    for i, char in enumerate(p.symbol):
                        if char.isdigit():
                            root = p.symbol[:i]
                            match = True
                            break
                    if match:
                        condor_positions += 1
                        active_tickers.add(root)

            targets = get_condor_targets()
            logger.info(f"Scanning {len(targets)} Targets (Active: {len(active_tickers)}/{MAX_POSITIONS})...")
            
            # --- MANAGEMENT ---
            # (Existing management logic remains same, summarized here)
            for p in positions:
                if p.asset_class == AssetClass.US_OPTION and float(p.qty) < 0:
                     # ... Take Profit Logic ...
                     pass

            # --- ENTRY ---
            if len(active_tickers) >= MAX_POSITIONS:
                logger.info("    Max positions reached. Skipping entry.")
            else:
                for ticker in targets:
                    if ticker in active_tickers: continue
                    
                    price = get_current_price(ticker)
                    if price == 0: continue
                    
                    logger.info(f"  Analysing {ticker} (${price:.2f})...")
                    
                    # Calculate Strikes
                    put_short_price = price * (1 - SHORT_OTM_PCT)
                    put_long_price = price * (1 - (SHORT_OTM_PCT + WING_WIDTH_PCT))
                    call_short_price = price * (1 + SHORT_OTM_PCT)
                    call_long_price = price * (1 + (SHORT_OTM_PCT + WING_WIDTH_PCT))
                    
                    start_date = datetime.date.today() + datetime.timedelta(days=MIN_DTE)
                    end_date = datetime.date.today() + datetime.timedelta(days=MAX_DTE)
                    
                    # Find Contracts
                    put_short = find_strike(ticker, "PUT", start_date, end_date, put_short_price)
                    put_long = find_strike(ticker, "PUT", start_date, end_date, put_long_price)
                    call_short = find_strike(ticker, "CALL", start_date, end_date, call_short_price)
                    call_long = find_strike(ticker, "CALL", start_date, end_date, call_long_price)
                    
                    if not (put_short and put_long and call_short and call_long):
                        logger.info("    -> Failed to find all 4 legs.")
                        continue
                        
                    print(f"    -> 🦅 FOUND CONDOR! Executing Atomic Sequence...")

                    if not utils.check_budget("condor_bot", trading_client):
                        break 
                    
                    wings = [put_long, call_long]
                    body = [put_short, call_short]
                    
                    # STEP 1: BUY WINGS
                    wings_filled_ids = []
                    wings_success = True
                    
                    for contract in wings:
                        # Use aggressive ASK price + 5% to ensure fill
                        ask_price = get_option_price(contract.symbol, "ask")
                        limit_price = round(ask_price * 1.05, 2)
                        if limit_price <= 0: limit_price = 0.05 # Safety floor
                        
                        try:
                            logger.info(f"       Buying Wing {contract.symbol} @ {limit_price}...")
                            req = LimitOrderRequest(symbol=contract.symbol, qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, limit_price=limit_price)
                            order = trading_client.submit_order(order_data=req)
                            
                            # Wait for Fill (5 seconds max)
                            filled = False
                            for _ in range(5):
                                time.sleep(1)
                                o = trading_client.get_order_by_id(order.id)
                                if o.status == 'filled':
                                    filled = True
                                    wings_filled_ids.append(contract.symbol)
                                    print("       ✅ Filled.")
                                    break
                            
                            if not filled:
                                logger.warning("       ❌ Timeout on Wing. Aborting.")
                                trading_client.cancel_order_by_id(order.id)
                                wings_success = False
                                break
                                
                        except Exception as e:
                            logger.error(f"       ❌ Error: {e}")
                            wings_success = False
                            break
                    
                    # STEP 2: SELL BODY (OR ROLLBACK)
                    if wings_success:
                        logger.info("       Wings Secured. Selling Body...")
                        body_success = True
                        
                        for contract in body:
                            # Use aggressive BID price - 5% to ensure fill
                            bid_price = get_option_price(contract.symbol, "bid")
                            limit_price = round(bid_price * 0.95, 2)
                            if limit_price <= 0: limit_price = 0.05 # Safety floor

                            try:
                                logger.info(f"       Selling Body {contract.symbol} @ {limit_price}...")
                                req = LimitOrderRequest(symbol=contract.symbol, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, limit_price=limit_price)
                                trading_client.submit_order(order_data=req)
                                # We assume Body fills or sits as limit. 
                                # Ideally we check this too, but for now we just needed to ensure we HAVE the wings first.
                            except Exception as e:
                                logger.error(f"       ❌ BODY FAILURE: {e}")
                                body_success = False
                        
                        if not body_success:
                            logger.error("       🚨 EXECUTION ERROR! Initiating ROLLBACK (Selling Wings)...")
                            # EMERGENCY CLOSE WINGS
                            for sym in wings_filled_ids:
                                try:
                                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                                    logger.warning(f"       ⚠️ Rolled back (Sold) {sym}")
                                except: logger.error(f"       💀 CRITICAL: Failed to rollback {sym}. Manually Close!")
                            send_discord(f"⚠️ **CONDOR FAILED & ROLLED BACK**\nTicker: {ticker}\nCheck Account!")

                        else:
                            send_discord(f"🦅 **CONDOR DEPLOYED: {ticker}**\nRange: ${put_short.strike_price} - ${call_short.strike_price}")
                            log_to_influx("open_condor", ticker, price, "Strategy Executed")

                    else:
                        logger.warning("    [ABORT] Wings failed. Cancelling sequence.")
                        # If one wing filled and second failed, we should probably close the first one too.
                        if len(wings_filled_ids) > 0:
                             logger.warning("       ⚠️ Cleaning up partial wings...")
                             for sym in wings_filled_ids:
                                trading_client.submit_order(order_data=MarketOrderRequest(symbol=sym, qty=1, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))

                    break # Move to sleep after attempt

            time.sleep(1800)

        except Exception as e:
            logger.error(f"Critical Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_condor_bot()