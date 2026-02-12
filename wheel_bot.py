from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
import time
import datetime
import requests
import math
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType
import config
import utils

# --- CONFIGURATION ---
MIN_DTE = 25             
MAX_DTE = 45
TARGET_OTM_PCT = 0.05   
MIN_PREMIUM = 0.10      
TAKE_PROFIT_PCT = 0.50  

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
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        quote = res[symbol]
        return float(quote.bid_price) if side == "bid" else float(quote.ask_price)
    except Exception as e:
        print(f"  [!] Error fetching option quote for {symbol}: {e}")
        return 0.0

def get_vix_level():
    try:
        vix = yf.Ticker("^VIX").history(period="1d")
        if not vix.empty: return float(vix['Close'].iloc[-1])
    except: pass
    return 15.0 

def find_best_contract(symbol, side, current_price, target_otm=0.05, min_strike=0.0):
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
        
        # BASIC FILTERING
        if side == "PUT" and strike >= current_price: continue
        if side == "CALL" and strike <= current_price: continue
        
        # COST BASIS PROTECTION (Crucial for Assigned Stocks)
        if side == "CALL" and min_strike > 0 and strike < min_strike:
            continue 

        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - target_otm)
        
        if score < best_score:
            best_score = score
            best_contract = c
            
    return best_contract

def run_wheel_bot():
    print(f"--- 🚜 FLEET WHEEL BOT (Cost Basis Aware) STARTED ---")
    
    initial_targets = utils.get_active_targets("wheel_targets")
    if not initial_targets: initial_targets = ["F", "PLTR", "SOFI"] 
    send_discord(f"🚜 **Wheel Bot Online**\nTargets: {initial_targets}")
    
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
            
            active_targets = utils.get_active_targets("wheel_targets")
            if not active_targets: active_targets = ["F", "PLTR", "SOFI"]

            current_vix = get_vix_level()
            dynamic_otm = 0.10 if current_vix > 20.0 else 0.05
            vix_msg = f"VIX: {current_vix:.2f} | Spacing: {dynamic_otm*100:.0f}%"

            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Scanning {len(active_targets)} Targets | {vix_msg}")

            opened_this_cycle = 0

            for ticker in active_targets:
                stock_qty = 0
                avg_entry = 0.0
                active_option = None
                
                # 1. SCAN EXISTING POSITIONS
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        stock_qty = float(p.qty)
                        avg_entry = float(p.avg_entry_price)
                    elif p.symbol.startswith(ticker) and p.asset_class == AssetClass.US_OPTION:
                        active_option = p
                
                current_stock_price = get_current_price(ticker)
                
                # 2. MANAGE EXISTING OPTION (TAKE PROFIT)
                if active_option:
                    entry_price = float(active_option.avg_entry_price)
                    current_opt_price = float(active_option.current_price) 
                    qty = float(active_option.qty) 
                    
                    if entry_price > 0:
                        capture_pct = (entry_price - current_opt_price) / entry_price
                        print(f"  {ticker:<4} | Option: {active_option.symbol} | Profit: {capture_pct*100:.1f}%")
                        
                        if capture_pct >= TAKE_PROFIT_PCT:
                            print(f"    🚜 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
                            close_price = get_option_price(active_option.symbol, side="ask")
                            if close_price == 0: close_price = current_opt_price * 1.05 
                            
                            req = LimitOrderRequest(
                                symbol=active_option.symbol, qty=abs(int(qty)), side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY, limit_price=close_price
                            )
                            trading_client.submit_order(order_data=req)
                            send_discord(f"🚜 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
                            continue 
                    continue

                # 3. OPEN NEW POSITIONS
                print(f"  {ticker:<4} | ${current_stock_price:>7.2f} | No Active Option. Hunting...")

                contract = None
                side = None

                # --- CASE A: COVERED CALL (We own the shares) ---
                if stock_qty >= 100:
                    side = "CALL"
                    
                    # [SAFETY CHECK] COST BASIS PROTECTION
                    # We usually want to sell a strike ABOVE our entry price.
                    # If Current Price < Entry Price, we might need to wait (Baghold)
                    # or sell at Entry Price to break even.
                    
                    min_strike_req = avg_entry
                    print(f"    [INFO] Own Shares @ ${avg_entry:.2f}. Min Strike set to ${min_strike_req:.2f}")

                    # If stock crashed 30%, asking for the old entry price might yield $0 premium.
                    # In that case, find_best_contract returns None, and we HOLD. (Correct behavior).
                    
                    contract = find_best_contract(ticker, "CALL", current_stock_price, 
                                                  target_otm=0.05, min_strike=min_strike_req)
                    
                    if not contract:
                        print(f"    [SKIP] No calls available above cost basis (${min_strike_req:.2f}). Holding.")
                        continue

                # --- CASE B: CASH SECURED PUT (We want to buy) ---
                else:
                    if not utils.check_budget("wheel_bot", trading_client): continue
                    
                    approx_cost = current_stock_price * 100
                    if buying_power < approx_cost:
                        print(f"    [SKIP] Insufficient BP (Need ${approx_cost:.0f})")
                        continue
                        
                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_stock_price, target_otm=dynamic_otm)

                if contract:
                    limit_price = get_option_price(contract.symbol, side="bid")
                    
                    if limit_price < MIN_PREMIUM:
                        print(f"    [SKIP] Premium too low (${limit_price})")
                        continue
                    
                    if side == "PUT":
                        if buying_power < (float(contract.strike_price) * 100):
                            print(f"    [SKIP] Strike too expensive.")
                            continue
                        
                        # Falling Knife Check
                        strike_price = float(contract.strike_price)
                        safety_buffer = (current_stock_price - strike_price) / current_stock_price
                        if safety_buffer < (dynamic_otm * 0.5):
                             print(f"    [SKIP] 🔪 Falling Knife! Buffer shrank to {safety_buffer*100:.1f}%")
                             continue

                    print(f"    [ENTRY] Selling {side} on {ticker} @ ${limit_price}")
                    req = LimitOrderRequest(
                        symbol=contract.symbol, qty=1, side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY, limit_price=limit_price
                    )
                    trading_client.submit_order(order_data=req)
                    
                    emoji = "📞" if side == "CALL" else "📉"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}\nCost Basis Protected: {'YES' if side=='CALL' else 'N/A'}")
                    
                    if side == "PUT": buying_power -= (float(contract.strike_price) * 100)
                    
                    opened_this_cycle += 1
                    if opened_this_cycle >= 1:
                        print("    [PACING] Opened 1 position. Sleeping.")
                        break 

            time.sleep(900)

        except Exception as e:
            print(f"\n[!] CRITICAL ERROR: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()