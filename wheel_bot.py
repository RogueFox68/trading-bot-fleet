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
        payload = {"content": msg, "username": "WheelBot 🚜"}
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
    print(f"--- 🚜 FLEET WHEEL BOT (Harvest Mode) STARTED ---")
    send_discord(f"🚜 **Wheel Bot Online**\nTargeting 50% Profit on: {WATCHLIST}")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print(f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed.", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            all_positions = trading_client.get_all_positions()

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning Portfolio & Watchlist...")

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
                    # Note: p.current_price is estimated. For real logic, we might want to fetch quote, 
                    # but for % check, the estimation is usually fine.
                    current_opt_price = float(active_option.current_price) 
                    qty = float(active_option.qty) # Negative for short
                    
                    if entry_price > 0:
                        # Calculate how much of the premium we have kept
                        # Example: Sold for 1.00, now 0.40. Capture = (1.00 - 0.40) / 1.00 = 60%
                        capture_pct = (entry_price - current_opt_price) / entry_price
                        
                        print(f"  {ticker:<4} | Existing Option: {active_option.symbol} | Profit: {capture_pct*100:.1f}%")
                        
                        if capture_pct >= TAKE_PROFIT_PCT:
                            print(f"    💰 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
                            
                            # Get real ASK price for the Limit Order
                            close_price = get_option_price(active_option.symbol, side="ask")
                            if close_price == 0: close_price = current_opt_price * 1.05 # Safety fallback
                            
                            req = LimitOrderRequest(
                                symbol=active_option.symbol,
                                qty=abs(int(qty)), # Buy back the positive amount
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=close_price
                            )
                            trading_client.submit_order(order_data=req)
                            send_discord(f"💰 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
                            log_to_influx("buy_close", close_price, active_option.symbol, "Take Profit")
                            # Don't open a new one same loop
                            continue 
                    
                    # If we have an option and didn't close it, we are done with this ticker for now
                    continue

                # 3. OPEN NEW POSITIONS (If no option exists)
                print(f"  {ticker:<4} | ${current_stock_price:>7.2f} | No Active Option. Hunting...")

                contract = None
                side = None

                # Covered Call?
                if stock_qty >= 100:
                    side = "CALL"
                    contract = find_best_contract(ticker, "CALL", current_stock_price)
                
                # Cash Secured Put?
                else:
                    # Basic check: do we have enough BP?
                    if buying_power < (current_stock_price * 100):
                        print(f"    [SKIP] Insufficient BP for {ticker}")
                        continue
                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_stock_price)

                if contract:
                    limit_price = get_option_price(contract.symbol, side="bid")
                    
                    if limit_price < MIN_PREMIUM:
                        print(f"    [SKIP] Premium too low (${limit_price})")
                        continue
                    
                    if side == "PUT" and buying_power < (float(contract.strike_price) * 100):
                        print(f"    [SKIP] Strike too expensive.")
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
                    emoji = "🟢" if side == "CALL" else "🔴"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}")
                    log_to_influx(f"sell_{side.lower()}", limit_price, contract.symbol, "Opened Position")
                    
                    if side == "PUT": buying_power -= (float(contract.strike_price) * 100)

            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()