import config
import time
import datetime
import requests
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest

# --- CONFIGURATION ---
TARGETS = ["COIN", "MSTR", "TSLA", "NVDA", "NFLX"] 
MIN_DTE = 25              # Days to Expiration (Start)
MAX_DTE = 45              # Days to Expiration (End)
WING_WIDTH_PCT = 0.05     # How wide the spread wings are (Protection)
SHORT_OTM_PCT = 0.08      # Sell the "Body" 8% away from price (~20 Delta)
TAKE_PROFIT_PCT = 0.50    # Close spread at 50% profit
MAX_POSITIONS = 3         # Don't overleverage

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

# --- WEBHOOK (Reuse Wheel or generic) ---
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

def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)
        return float(res[symbol].price)
    except: return 0.0

def get_option_price(symbol, side="bid"):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        return float(res[symbol].bid_price) if side == "bid" else float(res[symbol].ask_price)
    except: return 0.0

def find_strike(symbol, type, expiry_start, expiry_end, target_price, is_buy=False):
    """Finds the contract closest to the target price."""
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
    print(f"--- 🦅 IRON CONDOR BOT (Range Eater) STARTED ---")
    send_discord("🦅 **Iron Condor Bot Online**\nFeeding on Theta in choppy markets.")
    
    while True:
        try:
            # 1. Market Check
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print("Market Closed. Sleeping...", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            positions = trading_client.get_all_positions()
            active_tickers = set([p.symbol.split("2")[0] for p in positions if p.asset_class == AssetClass.US_OPTION])
            
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning for Condor Opportunities...")

            # --- MANAGEMENT: Check Existing Spreads ---
            # Simplified Management: We treat all options for a ticker as one "Unit" for display,
            # but we close individual legs if they hit profit.
            # (Ideally, we close the whole spread, but leg-by-leg is safer for a simple bot V1)
            
            for p in positions:
                if p.asset_class == AssetClass.US_OPTION:
                    # Check for Take Profit
                    entry = float(p.avg_entry_price)
                    current = float(p.current_price) # Estimated
                    qty = float(p.qty)
                    
                    # We only manage the SHORT legs (Sold positions) for profit
                    # The Long legs are just insurance.
                    if qty < 0 and entry > 0:
                        profit_pct = (entry - current) / entry
                        if profit_pct >= TAKE_PROFIT_PCT:
                            print(f"    💰 [PROFIT] {p.symbol} reached {profit_pct*100:.1f}% profit. Closing.")
                            # Buy to Close
                            limit = get_option_price(p.symbol, "ask") * 1.05 # Aggressive fill
                            req = LimitOrderRequest(
                                symbol=p.symbol, qty=abs(int(qty)), side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY, limit_price=limit
                            )
                            trading_client.submit_order(order_data=req)
                            send_discord(f"💰 **CONDOR PROFIT**\nClosed {p.symbol} @ {profit_pct*100:.0f}% Gain")
                            log_to_influx("close_leg", p.symbol, limit, "Take Profit")

            # --- ENTRY: Find New Condors ---
            if len(active_tickers) >= MAX_POSITIONS:
                print("    Max positions reached. Skipping entry.")
            else:
                for ticker in TARGETS:
                    if ticker in active_tickers: continue
                    
                    price = get_current_price(ticker)
                    if price == 0: continue
                    
                    print(f"  Analysing {ticker} (${price:.2f})...")
                    
                    # Calculate Strikes
                    # Short Put (Body): Price - 8%
                    # Long Put (Wing): Price - 13%
                    # Short Call (Body): Price + 8%
                    # Long Call (Wing): Price + 13%
                    
                    put_short_price = price * (1 - SHORT_OTM_PCT)
                    put_long_price = price * (1 - (SHORT_OTM_PCT + WING_WIDTH_PCT))
                    call_short_price = price * (1 + SHORT_OTM_PCT)
                    call_long_price = price * (1 + (SHORT_OTM_PCT + WING_WIDTH_PCT))
                    
                    start_date = datetime.date.today() + datetime.timedelta(days=MIN_DTE)
                    end_date = datetime.date.today() + datetime.timedelta(days=MAX_DTE)
                    
                    # Fetch Contracts
                    put_short = find_strike(ticker, "PUT", start_date, end_date, put_short_price)
                    put_long = find_strike(ticker, "PUT", start_date, end_date, put_long_price)
                    call_short = find_strike(ticker, "CALL", start_date, end_date, call_short_price)
                    call_long = find_strike(ticker, "CALL", start_date, end_date, call_long_price)
                    
                    if not (put_short and put_long and call_short and call_long):
                        print("    -> Failed to find all 4 legs.")
                        continue
                        
                    # Execution: "Legging In" (Safest Order: Buy Wings First -> Sell Body)
                    # This ensures you have the collateral (Buying Power) before selling.
                    
                    print(f"    -> 🦅 FOUND CONDOR! Sending Orders...")
                    
                    legs = [
                        (put_long, "PUT", OrderSide.BUY, "Long Wing"),
                        (call_long, "CALL", OrderSide.BUY, "Long Wing"),
                        (put_short, "PUT", OrderSide.SELL, "Short Body"),
                        (call_short, "CALL", OrderSide.SELL, "Short Body")
                    ]
                    
                    for contract, type, side, desc in legs:
                        # Get Price
                        limit_price = get_option_price(contract.symbol, "ask" if side == OrderSide.BUY else "bid")
                        
                        # Safety check for bad data
                        if limit_price <= 0.01: limit_price = 0.05 
                        
                        print(f"       {side} {type} {contract.strike_price} @ ${limit_price}")
                        req = LimitOrderRequest(
                            symbol=contract.symbol, qty=1, side=side,
                            time_in_force=TimeInForce.DAY, limit_price=limit_price
                        )
                        trading_client.submit_order(order_data=req)
                        time.sleep(1) # Small delay to ensure sequence
                    
                    send_discord(f"🦅 **OPENED CONDOR {ticker}**\nRange: ${put_short.strike_price} - ${call_short.strike_price}")
                    log_to_influx("open_condor", ticker, price, "4 Legs Executed")
                    
                    # Stop after opening one to avoid blasting the API
                    break 

            time.sleep(1800) # Check every 30 mins

        except Exception as e:
            print(f"Critical Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_condor_bot()