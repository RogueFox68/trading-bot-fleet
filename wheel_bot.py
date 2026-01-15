from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
import time
import datetime
import requests
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
from alpaca.data.historical.option import OptionHistoricalDataClient

# --- CONFIGURATION ---
SYMBOL = "DIS"           # The Ticker to Wheel
MIN_DTE = 25             # ~1 Month out
MAX_DTE = 45
TARGET_OTM_PCT = 0.05    # Look for strikes 5% out of the money (Proxy for 0.30 Delta)

# --- CREDENTIALS ---
API_KEY = "PK3RUJE6MZ3CJGXFFPHNCPIS2K"
SECRET_KEY = "GQWDMqkghfnHUtbFRKTkdtf9kk4caeZfKE2rwBksJ2mG"
PAPER = True             # Set to False when ready for real money
DISCORD_URL = "https://discordapp.com/api/webhooks/1460421466771423364/u7mvDhvDiEtBhWf7j9KY92DKFRtnR4ajmwg9RFEdd3CfNFuBoqiIlnHb2ld-FdPowEYW"

# --- INFLUXDB (traderpi local) ---
INFLUX_HOST = "192.168.5.27"
INFLUX_PORT = 8086
INFLUX_DB_NAME = "trading_data"

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)


# NEW: Data Client for fetching prices
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

def send_discord(msg):
    """Sends status updates to your phone"""
    try:
        payload = {
            "content": msg,
            "username": "WheelBot 🎡"
        }
        requests.post(DISCORD_URL, json=payload)
    except: pass

def log_to_influx(action, price, symbol, detail):
    """Logs data for your Grafana dashboard"""
    try:
        # Line Protocol: wheel_trades,symbol=DIS price=100,action="sell_put",detail="..."
        data_str = f'wheel_trades,symbol={SYMBOL} price={price},action="{action}",detail="{detail}",contract="{symbol}"'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_current_price(symbol):
    """
    Fetches the latest trade price using the Data Client.
    """
    try:
        # We must create a "Request Object" for the new API
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)

        # The result is a dictionary keyed by symbol
        return float(res[symbol].price)
    except Exception as e:
        print(f"  [!] Error getting price for {symbol}: {e}")
        return 0.0

def find_best_contract(side, current_price):
    """
    Finds a contract ~30-45 days out that is ~5% Out-of-the-Money.
    """
    print(f"  Scanning Option Chain for {side}...")
    
    today = datetime.date.today()
    start_date = today + datetime.timedelta(days=MIN_DTE)
    end_date = today + datetime.timedelta(days=MAX_DTE)
    
    req = GetOptionContractsRequest(
        underlying_symbol=[SYMBOL],
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
        print("  [!] No contracts found in date range.")
        return None

    best_contract = None
    best_score = 1.0 

    for c in available:
        strike = float(c.strike_price)
        
        # FILTER 1: Basic Logic
        # Put: Strike must be BELOW price (to be OTM)
        if side == "PUT" and strike >= current_price: continue
        # Call: Strike must be ABOVE price (to be OTM)
        if side == "CALL" and strike <= current_price: continue
        
        # FILTER 2: Target 5% OTM (The "Safe Yield" Spot)
        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - TARGET_OTM_PCT)
        
        if score < best_score:
            best_score = score
            best_contract = c
            
    return best_contract

def run_wheel_bot():
    print(f"--- 🎡 WHEEL BOT ({SYMBOL}) ONLINE ON TRADERPI ---")
    send_discord(f"🎡 **Wheel Bot Started**\nTarget: {SYMBOL}\nStrategy: Monthly Income")
    log_to_influx("startup", 0, "None", "Bot Started")
    
    while True:
        try:
            # --- NEW: MARKET HOURS CHECK ---
            clock = trading_client.get_clock()
            if not clock.is_open:
                # Calculate wait time (or just sleep 1 min)
                close_msg = f"[{datetime.datetime.now().strftime('%H:%M')}] Market Closed. Sleeping..."
                print(close_msg, end='\r')
                time.sleep(60)
                continue
            # -------------------------------

            # 1. Check Portfolio State (Existing code follows...)
            positions = trading_client.get_all_positions()
            
            has_stock = False
            stock_qty = 0
            has_option = False
            option_position = None
            
            for p in positions:
                if p.symbol == SYMBOL and p.asset_class == AssetClass.US_EQUITY:
                    has_stock = True
                    stock_qty = float(p.qty)
                # Check for options related to our symbol
                elif p.symbol.startswith(SYMBOL) and p.asset_class == AssetClass.US_OPTION:
                    has_option = True
                    option_position = p
            
            current_price = get_current_price(SYMBOL)
            status_msg = f"[{datetime.datetime.now().strftime('%H:%M')}] {SYMBOL}: ${current_price:.2f} | Stock: {stock_qty} | Option: {'OPEN' if has_option else 'NONE'}"
            print(status_msg, end='\r')

            # --- DECISION LOGIC ---
            
            # SCENARIO A: WAITING (We already have an option open)
            if has_option:
                # We do nothing. We let theta decay work for us.
                # The script just loops and monitors.
                pass

            # SCENARIO B: SELL COVERED CALL (We own stock, but no option)
            elif has_stock and stock_qty >= 100:
                print(f"\n  [SIGNAL] Own {stock_qty} shares. Selling COVERED CALL.")
                
                contract = find_best_contract("CALL", current_price)
                if contract:
                    print(f"    Target: {contract.symbol} (Strike: {contract.strike_price})")
                    
                   # Create the order request package first
                    req = MarketOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    # Submit the package
                    trading_client.submit_order(order_data=req)
                    
                    send_discord(f"📞 **SOLD CALL**\nStrike: ${contract.strike_price}\nUnderlying: ${current_price}")
                    log_to_influx("sell_call", current_price, contract.symbol, "Opened Covered Call")
                    time.sleep(60) 

            # SCENARIO C: SELL CASH SECURED PUT (We have cash, no stock, no option)
            elif not has_stock and not has_option:
                print(f"\n  [SIGNAL] Cash heavy. Selling SECURED PUT.")
                
                contract = find_best_contract("PUT", current_price)
                if contract:
                    print(f"    Target: {contract.symbol} (Strike: {contract.strike_price})")
                    
                    req = MarketOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order_data=req)
                    
                    send_discord(f"📉 **SOLD PUT**\nStrike: ${contract.strike_price}\nUnderlying: ${current_price}")
                    log_to_influx("sell_put", current_price, contract.symbol, "Opened Secured Put")
                    time.sleep(60)

            # Sleep 15 minutes to save Pi resources/API limits
            # Options don't move fast enough to need second-by-second updates
            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            send_discord(f"⚠️ **Wheel Bot Error**\n{e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()