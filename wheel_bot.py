from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
import time
import datetime
import requests
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest # Switched to LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
import config

# --- CONFIGURATION ---
WATCHLIST = ["DIS", "PLTR", "F"] 
MIN_DTE = 25             
MAX_DTE = 45
TARGET_OTM_PCT = 0.05
MIN_PREMIUM = 0.10 # Will not sell options for less than $10

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY) # New Client for Quotes

def send_discord(msg):
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

def get_option_bid(symbol):
    """
    Fetches the current BID price for a specific option contract.
    This ensures we use a Limit Order and don't get slipped.
    """
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        # return the Bid price
        return float(res[symbol].bid_price)
    except Exception as e:
        print(f"  [!] Error fetching option quote for {symbol}: {e}")
        return 0.0

def find_best_contract(symbol, side, current_price):
    today = datetime.date.today()
    start_date = today + datetime.timedelta(days=MIN_DTE)
    end_date = today + datetime.timedelta(days=MAX_DTE)
    
    # FIX APPLIED: underlying_symbols must be a list!
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
    
    if not available:
        return None

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
    print(f"--- 🚜 FLEET WHEEL BOT STARTED ---")
    print(f"Targets: {WATCHLIST}")
    send_discord(f"🚜 **Fleet Wheel Online**\nTargets: {WATCHLIST}")
    log_to_influx("startup", 0, "None", "Bot Started")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print(f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed. Sleeping...", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            all_positions = trading_client.get_all_positions()

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Cycling through Watchlist...")

            for ticker in WATCHLIST:
                has_stock = False
                stock_qty = 0
                has_option = False
                
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        has_stock = True
                        stock_qty = float(p.qty)
                    elif p.symbol.startswith(ticker) and p.asset_class == AssetClass.US_OPTION:
                        has_option = True
                
                current_price = get_current_price(ticker)
                print(f"  {ticker:<4} | ${current_price:>7.2f} | Stock: {stock_qty:>3} | Option: {'YES' if has_option else 'NO '}")

                if has_option:
                    continue 

                # --- TRADING LOGIC WITH LIMIT ORDERS ---
                
                contract = None
                side = None

                # Check for Covered Call
                if has_stock and stock_qty >= 100:
                    side = "CALL"
                    contract = find_best_contract(ticker, "CALL", current_price)
                
                # Check for Cash Secured Put
                elif not has_stock:
                    if buying_power < (current_price * 100):
                        print(f"    [SKIP] Insufficient BP for {ticker}")
                        continue
                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_price)

                # EXECUTE IF FOUND
                if contract:
                    # 1. Get the Limit Price (The Bid)
                    limit_price = get_option_bid(contract.symbol)
                    
                    if limit_price < MIN_PREMIUM:
                        print(f"    [SKIP] Premium too low (${limit_price})")
                        continue
                        
                    if side == "PUT" and buying_power < (float(contract.strike_price) * 100):
                        print(f"    [SKIP] Strike {contract.strike_price} too expensive.")
                        continue

                    print(f"    [SIGNAL] Selling {side} on {ticker} @ Limit ${limit_price}")

                    # 2. Submit LIMIT Order (Not Market!)
                    req = LimitOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price
                    )
                    
                    trading_client.submit_order(order_data=req)
                    
                    emoji = "🟢" if side == "CALL" else "🔴"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit Price: ${limit_price}")
                    log_to_influx(f"sell_{side.lower()}", limit_price, contract.symbol, "Opened Position")
                    
                    if side == "PUT":
                        buying_power -= (float(contract.strike_price) * 100)

            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()