from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
import time
import datetime
import requests
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
import config
import utils

# --- CONFIGURATION ---
WATCHLIST = ["DIS", "PLTR", "F"] 
MIN_DTE = 25             
MAX_DTE = 45
TARGET_OTM_PCT = 0.05
MIN_PREMIUM = 0.10      # Will not sell options for less than $10
TAKE_PROFIT_PCT = 0.50  # Close position if we captured 50% of max profit

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_WHEEL: return
    try:
        payload = {"content": msg, "username": "WheelBot 囿"}
        requests.post(config.WEBHOOK_WHEEL, json=payload)
    except: pass

def log_to_influx(action, price, symbol, detail):
    try:
        data_str = f'wheel_trades,symbol={symbol} price={price},action="{action}",detail="{detail}",contract="{symbol}"'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        print(f"  [!] Error price {symbol}: {e}")
        return 0.0

def get_option_price(symbol, side="bid"):
    """
    Fetches the current BID (for selling) or ASK (for buying/closing).
    """
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        quote = res[symbol]
        return float(quote.bid_price) if side == "bid" else float(quote.ask_price)
    except Exception as e:
        print(f"  [!] Error fetching option quote for {symbol}: {e}")
        return 0.0

def find_best_contract(symbol, side, current_price):
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
        print(f"  [!] API Error fetching contracts: {e}")
        return None
    
    if not available: return None

    best_contract = None
    best_score = 1.0 

    for c in available:
        strike = float(c.strike_price)
        if side == "PUT" and strike >= current_price: continue
        if side == "CALL" and strike <= current_price: continue
        
        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - TARGET_OTM_PCT)
        
        if score < best_score:
            best_score = score
            best_contract = c
            
    return best_contract

def run_wheel_bot():
    print(f"--- 囿 FLEET WHEEL BOT (Harvest Mode) STARTED ---")
    send_discord(f"囿 **Wheel Bot Online**\nTargeting 50% Profit on: {WATCHLIST}")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print(f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed. Sleeping 15m...", end='\r')
                    time.sleep(900)
                    continue
            except: pass

            # Note: We fetch account here, but we will RE-FETCH inside the loop for accuracy
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning Portfolio & Watchlist...")
            all_positions = trading_client.get_all_positions()

            for ticker in WATCHLIST:
                stock_qty = 0
                active_option = None
                
                # 1. SCAN EXISTING POSITIONS
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        stock_qty = float(p.qty)
                    elif p.symbol.startswith(ticker) and p.asset_class == AssetClass.US_OPTION:
                        active_option = p
                
                current_stock_price = get_current_price(ticker)
                
                # 2. MANAGE EXISTING OPTION (TAKE PROFIT)
                if active_option:
                    entry_price = float(active_option.avg_entry_price)
                    current_opt_price = float(active_option.current_price) 
                    qty = float(active_option.qty) # Negative for short
                    
                    if entry_price > 0:
                        capture_pct = (entry_price - current_opt_price) / entry_price
                        print(f"  {ticker:<4} | Existing Option: {active_option.symbol} | Profit: {capture_pct*100:.1f}%")
                        
                        if capture_pct >= TAKE_PROFIT_PCT:
                            print(f"    腸 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
                            
                            close_price = get_option_price(active_option.symbol, side="ask")
                            if close_price == 0: close_price = current_opt_price * 1.05
                            
                            req = LimitOrderRequest(
                                symbol=active_option.symbol,
                                qty=abs(int(qty)),
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=close_price
                            )
                            trading_client.submit_order(order_data=req)
                            send_discord(f"腸 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
                            log_to_influx("buy_close", close_price, active_option.symbol, "Take Profit")
                            continue 
                    continue

                # 3. OPEN NEW POSITIONS (If no option exists)
                print(f"  {ticker:<4} | ${current_stock_price:>7.2f} | No Active Option. Hunting...")

                contract = None
                side = None

                # [FIX] Real-Time Account Fetch for accurate Buying Power
                acc = trading_client.get_account()
                real_bp = float(acc.options_buying_power)

                # Covered Call?
                if stock_qty >= 100:
                    side = "CALL"
                    contract = find_best_contract(ticker, "CALL", current_stock_price)
                
                # Cash Secured Put?
                else:
                    # [NEW] CFO CHECK
                    if not utils.check_budget("wheel_bot", trading_client):
                        # Skip if global budget is hit
                        continue

                    # [FIX] Check against REAL BP, not estimated
                    if real_bp < (current_stock_price * 100):
                        print(f"    [SKIP] Insufficient Real-Time BP (${real_bp:.0f}) for {ticker}")
                        continue

                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_stock_price)

                if contract:
                    limit_price = get_option_price(contract.symbol, side="bid")
                    
                    if limit_price < MIN_PREMIUM:
                        print(f"    [SKIP] Premium too low (${limit_price})")
                        continue
                    
                    # [FIX] Double check BP specifically for this contract strike
                    if side == "PUT" and real_bp < (float(contract.strike_price) * 100):
                        print(f"    [SKIP] Strike too expensive for current BP.")
                        continue

                    print(f"    [ENTRY] Selling {side} on {ticker} @ ${limit_price}")
                    req = LimitOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price
                    )
                    trading_client.submit_order(order_data=req)
                    emoji = "泙" if side == "CALL" else "閥"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}")
                    log_to_influx(f"sell_{side.lower()}", limit_price, contract.symbol, "Opened Position")

            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()