from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
import time
import datetime
import requests
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
import config  # Importing your central keys

# --- CONFIGURATION ---
# The "Income Engine" List
WATCHLIST = ["DIS", "PLTR", "F"] 
MIN_DTE = 25             
MAX_DTE = 45
TARGET_OTM_PCT = 0.05    

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    try:
        payload = {"content": msg, "username": "WheelBot 治"}
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

def find_best_contract(symbol, side, current_price):
    """
    Finds a contract ~30-45 days out that is ~5% Out-of-the-Money.
    """
    # print(f"  Scanning {symbol} Option Chain for {side}...")
    
    today = datetime.date.today()
    start_date = today + datetime.timedelta(days=MIN_DTE)
    end_date = today + datetime.timedelta(days=MAX_DTE)
    
    req = GetOptionContractsRequest(
        underlying_symbol=symbol,
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
        
        # FILTER 1: Basic Logic
        if side == "PUT" and strike >= current_price: continue
        if side == "CALL" and strike <= current_price: continue
        
        # FILTER 2: Target 5% OTM
        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - TARGET_OTM_PCT)
        
        if score < best_score:
            best_score = score
            best_contract = c
            
    return best_contract

def run_wheel_bot():
    print(f"--- 治 FLEET WHEEL BOT STARTED ---")
    print(f"Targets: {WATCHLIST}")
    send_discord(f"治 **Fleet Wheel Online**\nTargets: {WATCHLIST}")
    log_to_influx("startup", 0, "None", "Bot Started")
    
    while True:
        try:
            # 1. Market Hours Check
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    print(f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed. Sleeping...", end='\r')
                    time.sleep(60)
                    continue
            except: pass

            # 2. Get Global Account Data (Buying Power)
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            
            # 3. Get All Positions Once
            all_positions = trading_client.get_all_positions()

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Cycling through Watchlist...")

            for ticker in WATCHLIST:
                # --- State Tracking for THIS Ticker ---
                has_stock = False
                stock_qty = 0
                has_option = False
                
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        has_stock = True
                        stock_qty = float(p.qty)
                    # Check for options related to our ticker (Alpaca option symbols start with the ticker)
                    elif p.symbol.startswith(ticker) and p.asset_class == AssetClass.US_OPTION:
                        has_option = True
                
                current_price = get_current_price(ticker)
                print(f"  {ticker:<4} | ${current_price:>7.2f} | Stock: {stock_qty:>3} | Option: {'YES' if has_option else 'NO '}")

                # --- DECISION LOGIC ---
                
                # SCENARIO A: WAITING
                if has_option:
                    continue # Move to next ticker

                # SCENARIO B: SELL COVERED CALL (Own Stock)
                elif has_stock and stock_qty >= 100:
                    print(f"    [SIGNAL] {ticker}: Selling COVERED CALL.")
                    contract = find_best_contract(ticker, "CALL", current_price)
                    if contract:
                        req = MarketOrderRequest(
                            symbol=contract.symbol,
                            qty=1,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY
                        )
                        trading_client.submit_order(order_data=req)
                        send_discord(f"到 **SOLD CALL {ticker}**\nStrike: ${contract.strike_price}")
                        log_to_influx("sell_call", current_price, contract.symbol, "Opened Covered Call")

                # SCENARIO C: SELL CASH SECURED PUT (No Stock, No Option)
                elif not has_stock and not has_option:
                    # Check if we can afford it! 
                    # Approximate cost = Price * 100 (for a Cash Secured Put)
                    # Ideally we check Strike * 100, but we haven't found the contract yet.
                    if buying_power < (current_price * 100):
                        print(f"    [SKIP] Insufficient BP for {ticker}")
                        continue

                    print(f"    [SIGNAL] {ticker}: Selling SECURED PUT.")
                    contract = find_best_contract(ticker, "PUT", current_price)
                    
                    if contract:
                        # Double check specific strike price affordability
                        if buying_power < (float(contract.strike_price) * 100):
                             print(f"    [SKIP] Strike {contract.strike_price} too expensive.")
                             continue

                        req = MarketOrderRequest(
                            symbol=contract.symbol,
                            qty=1,
                            side=OrderSide.SELL,
                            time_in_force=TimeInForce.DAY
                        )
                        trading_client.submit_order(order_data=req)
                        send_discord(f"悼 **SOLD PUT {ticker}**\nStrike: ${contract.strike_price}")
                        log_to_influx("sell_put", current_price, contract.symbol, "Opened Secured Put")
                        
                        # Reduce our 'local' tracking of BP so we don't overspend on the next ticker in the loop
                        buying_power -= (float(contract.strike_price) * 100)

            # Sleep 15 minutes after checking the whole list
            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()