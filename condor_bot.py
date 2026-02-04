import utils
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
# [FIX] Group positions by Root Symbol (Reassemble the Condor)
            condor_groups = {}
            active_tickers = set()

            for p in positions:
                if p.asset_class != AssetClass.US_OPTION: continue
                
                # Identify the root symbol (e.g. "TSLA" from "TSLA23...")
                root = None
                for ticker in TARGETS:
                    if p.symbol.startswith(ticker):
                        root = ticker
                        break
                
                # Only manage if it belongs to this bot
                if root and utils.get_bot_owner(root, AssetClass.US_OPTION) == "condor_bot":
                    if root not in condor_groups:
                        condor_groups[root] = []
                    condor_groups[root].append(p)
                    active_tickers.add(root)

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning (Active Condors: {len(active_tickers)}/{MAX_POSITIONS})...")

            # --- MANAGEMENT: Check Net P&L of Each Condor ---
            for root, legs in condor_groups.items():
                total_entry_cost = 0.0  
                total_current_value = 0.0
                
                for leg in legs:
                    qty = float(leg.qty)
                    entry_price = float(leg.avg_entry_price)
                    current_price = float(leg.current_price)
                    
                    # Cost = Price * Qty * 100
                    # For Sells (Qty < 0), this adds negative cost (Credit)
                    total_entry_cost += (entry_price * qty * 100)
                    total_current_value += (current_price * qty * 100)

                # Iron Condor is a CREDIT strategy. 
                # initial_credit will be positive (e.g., $100).
                initial_credit = -total_entry_cost
                current_debit_to_close = -total_current_value 
                
                # Safety: Ensure we actually received credit (valid condor)
                if initial_credit > 0:
                    profit = initial_credit - current_debit_to_close
                    capture_pct = profit / initial_credit
                    
                    print(f"    🦅 {root} Net P&L: ${profit:.2f} ({capture_pct*100:.1f}%)")

                    if capture_pct >= TAKE_PROFIT_PCT:
                        print(f"    💰 [HARVEST] {root} hit {TAKE_PROFIT_PCT*100:.0f}% target. Closing all {len(legs)} legs.")
                        
                        for leg in legs:
                            qty = float(leg.qty)
                            side = OrderSide.BUY if qty < 0 else OrderSide.SELL
                            
                            # Aggressive Limit to ensure exit
                            price = get_option_price(leg.symbol, "ask" if side == OrderSide.BUY else "bid")
                            limit = price * 1.05 if side == OrderSide.BUY else price * 0.95
                            
                            try:
                                req = LimitOrderRequest(
                                    symbol=leg.symbol,
                                    qty=abs(int(qty)),
                                    side=side,
                                    time_in_force=TimeInForce.DAY,
                                    limit_price=round(limit, 2)
                                )
                                trading_client.submit_order(order_data=req)
                                print(f"       -> Sent Close for {leg.symbol}")
                            except Exception as e:
                                print(f"       [!] Error closing {leg.symbol}: {e}")
                                
                        send_discord(f"💰 **CONDOR CLOSED: {root}**\nNet Profit: ${profit:.2f} ({capture_pct*100:.0f}%)")
                        log_to_influx("close_condor", root, profit, "Take Profit")

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
                    
                    
                    print(f"    -> 🦅 FOUND CONDOR! Executing Safely...")

                    if not utils.check_budget("condor_bot", trading_client):
                        print("    [SKIP] Condor Budget Exceeded.")
                        break 
                    
                    # Define Wings (Protection) and Body (Risk)
                    # We MUST fill wings first.
                    wings = [
                        (put_long, "PUT", OrderSide.BUY, "Long Put Wing"),
                        (call_long, "CALL", OrderSide.BUY, "Long Call Wing")
                    ]
                    
                    body = [
                        (put_short, "PUT", OrderSide.SELL, "Short Put Body"),
                        (call_short, "CALL", OrderSide.SELL, "Short Call Body")
                    ]
                    
                    # STEP 1: BUY WINGS (And Wait for Fill)
                    wings_filled = True
                    for contract, type, side, desc in wings:
                        # Inside the loop for buying wings
                        raw_price = get_option_price(contract.symbol, "ask") * 1.05
                        limit_price = round(raw_price, 2) # <--- ROUND TO 2 DECIMALS
                        print(f"       Buying {desc} (${limit_price:.2f})...")
                        
                        try:
                            # Submit Order
                            req = LimitOrderRequest(
                                symbol=contract.symbol, qty=1, side=side,
                                time_in_force=TimeInForce.DAY, limit_price=limit_price
                            )
                            order = trading_client.submit_order(order_data=req)
                            
                            # POLLING LOOP: Wait up to 10 seconds for fill
                            filled = False
                            for _ in range(10):
                                time.sleep(1)
                                updated_order = trading_client.get_order_by_id(order.id)
                                if updated_order.status == 'filled':
                                    filled = True
                                    print(f"       ✅ {desc} Filled!")
                                    break
                            
                            if not filled:
                                print(f"       ❌ {desc} Failed to fill in time. Aborting Condor.")
                                # Critical: Cancel the order so we don't get filled later unexpectedly
                                trading_client.cancel_order_by_id(order.id)
                                wings_filled = False
                                break
                                
                        except Exception as e:
                            print(f"       Order Failed: {e}")
                            wings_filled = False
                            break
                    
                    # STEP 2: SELL BODY (Only if Wings are locked in)
                    if wings_filled:
                        print("       Wings Secured. Selling Body...")
                        for contract, type, side, desc in body:
                            # Inside the loop for selling body
                            raw_price = get_option_price(contract.symbol, "bid") * 0.95
                            limit_price = round(raw_price, 2) # <--- ROUND TO 2 DECIMALS
                            
                            req = LimitOrderRequest(
                                symbol=contract.symbol, qty=1, side=side,
                                time_in_force=TimeInForce.DAY, limit_price=limit_price
                            )
                            try:
                                trading_client.submit_order(order_data=req)
                                print(f"       ✅ {desc} Sent.")
                            except Exception as e:
                                print(f"       ❌ Failed to sell {desc}: {e}")
                                
                                # <--- ADD THIS BLOCK --->
                                
                        send_discord(f"🦅 **CONDOR DEPLOYED: {ticker}**\nWings Secured. Body Sold.\nRange: ${put_short.strike_price} - ${call_short.strike_price}")
                        log_to_influx("open_condor", ticker, price, "Strategy Executed")
                        # <---------------------->
                    
                    else:
                        print("    [ABORT] Wings failed to fill. Cancelling strategy for this ticker.")
                    
                    # Stop after one attempt per cycle
                    break

            time.sleep(1800) # Check every 30 mins

        except Exception as e:
            print(f"Critical Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_condor_bot()