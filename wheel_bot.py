from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
import time
import datetime
import requests
import json
import os
import math
import re
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType, QueryOrderStatus
import config
import utils
from logger import get_logger

# --- LOGGING ---
logger = get_logger("wheel_bot")

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"

MIN_DTE = 25             
MAX_DTE = 45
TARGET_OTM_PCT = 0.05
MIN_PREMIUM = 0.10      
TAKE_PROFIT_PCT = 0.50 
MAX_SPREAD_PCT = 0.25   

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_WHEEL: return
    try:
        payload = {"content": msg, "username": "WheelBot 🚜"}
        requests.post(config.WEBHOOK_WHEEL, json=payload)
    except: pass

def log_to_influx(action, price, symbol, detail):
    try:
        data_str = f'wheel_trades,symbol={symbol} price={price},action="{action}",detail="{detail}",contract="{symbol}"'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_wheel_targets():
    return utils.get_targets_with_freshness_check(
        TARGET_FILE, 
        "wheel_targets", 
        []
    )

def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        logger.error(f"  [!] Error price {symbol}: {e}")
        return 0.0

def get_option_data(symbol):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        return res[symbol]
    except Exception as e:
        logger.error(f"  [!] Error fetching option quote for {symbol}: {e}")
        return None

def calculate_smart_price(quote, side):
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    
    if ask == 0: return None
    
    spread = ask - bid
    spread_pct = spread / ask
    midpoint = (bid + ask) / 2
    
    if spread_pct > MAX_SPREAD_PCT:
        logger.debug(f"    [SKIP] Spread too wide ({spread_pct*100:.1f}%). Bid: {bid} Ask: {ask}")
        return None
        
    return round(midpoint, 2)

def find_best_contract(symbol, side, current_price, target_otm=TARGET_OTM_PCT):
    today = datetime.date.today()
    start_date = today + datetime.timedelta(days=MIN_DTE)
    end_date = today + datetime.timedelta(days=MAX_DTE)
    
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol], 
        status="active",
        expiration_date_gte=start_date,
        expiration_date_lte=end_date,
        type=ContractType.PUT if side == "PUT" else ContractType.CALL,
        limit=1000
    )
    
    try:
        contracts = trading_client.get_option_contracts(req)
        available = contracts.option_contracts
    except Exception as e:
        logger.error(f"  [!] API Error fetching contracts: {e}")
        return None
    
    if not available: return None

    best_contract = None
    best_score = 1.0 

    for c in available:
        strike = float(c.strike_price)
        if side == "PUT" and strike >= current_price: continue
        if side == "CALL" and strike <= current_price: continue
        
        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - target_otm)
        
        if score < best_score:
            best_score = score
            best_contract = c
            
    return best_contract

def get_open_order_tickers():
    """
    Returns a set of tickers that currently have OPEN orders.
    Parses option symbols (e.g., AMD230120P...) back to AMD.
    """
    params = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    orders = trading_client.get_orders(filter=params)
    busy_tickers = set()
    
    for o in orders:
        sym = o.symbol
        # Regex to strip the option suffix (e.g. "AAPL230616C00150000" -> "AAPL")
        # Matches the start of string until the first digit
        match = re.match(r"^([A-Z]+)\d", sym)
        if match:
            root = match.group(1)
            busy_tickers.add(root)
        else:
            # Regular stock order
            busy_tickers.add(sym)
            
    return busy_tickers

def run_wheel_bot():
    logger.info("--- 🚜 FLEET WHEEL BOT (Smart Pricing + Order Awareness) STARTED ---")
    send_discord(f"🚜 **Wheel Bot Online**\nSyncing with Sector Scout...")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping 15m...")
                    time.sleep(900)
                    continue
            except: pass

            raw_targets = get_wheel_targets()
            
            # --- PARSE & CONFIGURE (Phase 2) ---
            target_map = {} # symbol -> confidence
            clean_targets = []
            
            for item in raw_targets:
                s, c = utils.parse_target(item)
                if s:
                    clean_targets.append(s)
                    target_map[s] = c
            
            # [FIX] Get list of tickers that already have pending orders
            busy_tickers = get_open_order_tickers()
            
            logger.info(f"Scanning {len(clean_targets)} Targets (Busy: {len(busy_tickers)})")

            if not clean_targets:
                logger.info("    💤 Standby Mode: No targets found. Sleeping...")
                time.sleep(900)
                continue

            all_positions = trading_client.get_all_positions()

            for ticker in clean_targets:
                # [FIX] Skip if we already have an open order for this ticker
                if ticker in busy_tickers:
                    logger.info(f"  {ticker:<4} | [SKIP] Open Order Exists.")
                    continue

                stock_qty = 0
                active_option = None
                
                # Check positions
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        stock_qty = float(p.qty)
                    elif p.symbol.startswith(ticker) and p.asset_class == AssetClass.US_OPTION:
                        active_option = p
                
                current_stock_price = get_current_price(ticker)
                
                # 2. MANAGE EXISTING OPTION (TAKE PROFIT)
                if active_option:
                    # ... existing management logic ...
                    entry_price = float(active_option.avg_entry_price)
                    current_opt_price = float(active_option.current_price) 
                    qty = float(active_option.qty) 
                    
                    if entry_price > 0:
                        capture_pct = (entry_price - current_opt_price) / entry_price
                        logger.info(f"  {ticker:<4} | Option: {active_option.symbol} | Profit: {capture_pct*100:.1f}%")
                        
                        if capture_pct >= TAKE_PROFIT_PCT:
                            logger.info(f"    💵 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
                            
                            quote = get_option_data(active_option.symbol)
                            close_price = calculate_smart_price(quote, "BUY")
                            
                            if close_price is None:
                                logger.warning(f"    [WAIT] Spread too wide to close safely.")
                                continue

                            req = LimitOrderRequest(
                                symbol=active_option.symbol,
                                qty=abs(int(qty)),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=close_price
                            )
                            trading_client.submit_order(order_data=req)
                            send_discord(f"💵 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
                            log_to_influx("buy_close", close_price, active_option.symbol, "Take Profit")
                            continue 
                    continue

                # 3. OPEN NEW POSITIONS
                if not utils.check_budget("wheel_bot", trading_client):
                     continue # Skip new entries if budget paused, but finish loop
                     
                confidence = target_map.get(ticker, 0.5)
                # Dynamic OTM
                dynamic_otm = TARGET_OTM_PCT * (1.5 - confidence)
                
                logger.info(f"  {ticker:<4} | ${current_stock_price:>7.2f} | Conf: {confidence:.2f} | Target OTM: {dynamic_otm*100:.1f}%")

                contract = None
                side = None

                acc = trading_client.get_account()
                real_bp = float(acc.options_buying_power)

                # Covered Call?
                if stock_qty >= 100:
                    side = "CALL"
                    contract = find_best_contract(ticker, "CALL", current_stock_price, dynamic_otm)
                
                # Cash Secured Put?
                else:
                    # [FIX] Use cached budget check
                    if not is_budget_ok:
                        continue

                    if real_bp < (current_stock_price * 100):
                        logger.warning(f"    [SKIP] Insufficient BP (Need ${current_stock_price*100:.0f})")
                        continue

                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_stock_price, dynamic_otm)

                if contract:
                    quote = get_option_data(contract.symbol)
                    if not quote: 
                        logger.info(f"    [SKIP] Failed to fetch quote for {contract.symbol}")
                        continue
                    
                    limit_price = calculate_smart_price(quote, side)
                    if limit_price is None: 
                        # `calculate_smart_price` already logs the wide spread reason
                        continue 
                    
                    # [OPTIONAL] Adjust Minimum Premium based on confidence? 
                    # For now just use static MIN_PREMIUM

                    if limit_price < MIN_PREMIUM:
                        logger.info(f"    [SKIP] Premium too low (${limit_price:.2f} < ${MIN_PREMIUM:.2f})")
                        continue
                    
                    if side == "PUT" and real_bp < (float(contract.strike_price) * 100):
                        logger.warning(f"    [SKIP] Strike too expensive for available BP.")
                        continue
                else:
                    logger.info(f"    [SKIP] No suitable {side} contract found within {MIN_DTE}-{MAX_DTE} DTE for {ticker}.")
                    continue

                    logger.info(f"    [ENTRY] Selling {side} on {ticker} @ ${limit_price} (Midpoint)")
                    req = LimitOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price
                    )
                    trading_client.submit_order(order_data=req)
                    emoji = "🟢" if side == "CALL" else "🔴"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}\nConf: {confidence:.2f}")
                    log_to_influx(f"sell_{side.lower()}", limit_price, contract.symbol, "Opened Position")

            time.sleep(900)

        except Exception as e:
            logger.error(f"CRITICAL ERROR: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()