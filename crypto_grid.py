import config
import time
import requests
import json
import math
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest
from logger import get_logger, registry
import utils

# --- LOGGING ---
logger = get_logger("crypto_grid")

# --- CONFIGURATION ---
SYMBOLS = ["BTC/USD", "ETH/USD", "SOL/USD"]
GRID_WIDTH_PCT = 0.15    # Grid covers +/- 15% of current price
GRID_LEVELS = 8          # More levels = Finer scalping
BUDGET_PER_GRID = 50     # $50 per slice
RECALIBRATE_DELAY = 4    # Cycles out of zone before resetting (Prevent jitter)

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER
DISCORD_URL = config.WEBHOOK_CRYPTO
CONFIG_FILE = "bot_config.json"

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = CryptoHistoricalDataClient()

# --- STATE VARIABLES (Dynamic) ---
# We no longer hardcode these. They are calculated at runtime.
grids = {sym: {"top": 0, "bottom": 0, "size": 0, "prev_zone": GRID_LEVELS // 2, "oob": 0} for sym in SYMBOLS}

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except Exception as e:
        registry.log_error("crypto_grid", "send_discord", e, context=msg[:50])
        logger.error(f"Discord webhook failed: {e}")

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'crypto_trades,symbol={symbol} price={price},action="{action}",qty={qty} {time.time_ns()}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=data_str, timeout=2)
        if r.status_code != 204:
            logger.warning(f"InfluxDB write failed: {r.status_code} {r.text}")
    except Exception as e:
        logger.warning(f"InfluxDB write error: {e}")

def get_market_config():
    """Reads the 'Weather' and CAPITAL_CRUNCH from the config."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            regime = data.get('global_settings', {}).get('market_condition', 'SIDEWAYS')
            crunch = data.get('global_settings', {}).get('CAPITAL_CRUNCH', False)
            return regime, crunch
    except Exception as e:
        registry.log_error("crypto_grid", "get_market_config", e)
        logger.error(f"Failed to read market config: {e}")
        return "SIDEWAYS", False

def get_crypto_price(symbol):
    try:
        req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_crypto_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        logger.error(f"  [!] Price Error {symbol}: {e}")
        return None

def recalibrate_grid(symbol, current_price):
    """Centers the grid around the NEW price."""
    # Grid spans +/- WIDTH_PCT
    grid_top = current_price * (1 + GRID_WIDTH_PCT)
    grid_bottom = current_price * (1 - GRID_WIDTH_PCT)
    zone_size = (grid_top - grid_bottom) / GRID_LEVELS
    
    grids[symbol]["top"] = grid_top
    grids[symbol]["bottom"] = grid_bottom
    grids[symbol]["size"] = zone_size
    grids[symbol]["prev_zone"] = GRID_LEVELS // 2
    grids[symbol]["oob"] = 0
    
    msg = (f"♻️ **Grid Recalibrated ({symbol})**\n"
           f"Center: ${current_price:,.0f}\n"
           f"Range: ${grid_bottom:,.0f} - ${grid_top:,.0f}")
    logger.info(f"    [RECALIBRATE] {symbol} Center: {current_price:.0f} | Range: {grid_bottom:.0f}-{grid_top:.0f}")
    send_discord(msg)

def get_my_budget():
    """Reads the CFO's effective budget for this bot."""
    try:
        with open("effective_budgets.json", "r") as f:
            budgets = json.load(f)
            return budgets.get("crypto_grid", 5000) # Default to $5k if file missing
    except Exception as e:
        logger.warning(f"Could not read budget file: {e}. Using safe default.")
        return 5000

def run_grid_bot():
    logger.info(f"--- 🕸️ CRYPTO GRID BOT V2 (Auto-Tuning) ---")
    
    # 1. Initial Setup
    for symbol in SYMBOLS:
        price = get_crypto_price(symbol)
        while price is None:
            time.sleep(10)
            price = get_crypto_price(symbol)
            
        recalibrate_grid(symbol, price)

    while True:
        try:
            regime, capital_crunch = get_market_config()
            
            # CFO Sync: Ping the accountant to track our position value globally.
            is_budget_ok = utils.check_budget("crypto_grid", trading_client)

            for symbol in SYMBOLS:
                # 2. Get Data
                price = get_crypto_price(symbol)
                
                if price is None:
                    continue
                    
                grid = grids[symbol]

                # 3. Determine Zone
                if price < grid["bottom"]:
                    current_zone = -1
                    grid["oob"] += 1
                elif price > grid["top"]:
                    current_zone = GRID_LEVELS + 1
                    grid["oob"] += 1
                else:
                    if grid["size"] > 0:
                        current_zone = int((price - grid["bottom"]) / grid["size"])
                    else:
                        current_zone = GRID_LEVELS // 2
                    grid["oob"] = 0 # Reset if we are back in range

                # 4. Status Display
                status_msg = f"{symbol} ${price:,.0f} | Zone: {current_zone} | Regime: {regime}"
                if grid["oob"] > 0:
                    status_msg += f" | OOB: {grid['oob']}/{RECALIBRATE_DELAY}"

                # 5. AUTO-RECALIBRATE LOGIC
                # If we are lost in the woods for too long, move the base.
                if grid["oob"] >= RECALIBRATE_DELAY:
                    recalibrate_grid(symbol, price)
                    time.sleep(5)
                    continue

                # 6. TRADING LOGIC
                previous_zone = grid["prev_zone"]
                if current_zone != previous_zone and 0 <= current_zone <= GRID_LEVELS:
                    
                    logger.info(f"[{symbol}] Zone Change: {previous_zone} -> {current_zone} | Price: ${price:.0f}")

                    # PRICE DROP -> BUY (Accumulate)
                    if current_zone < previous_zone:
                        # TREND FILTER: Don't buy if the Analyst says BEAR_TREND
                        if "BEAR" in regime:
                            logger.info(f"    [SKIP] Bear Trend Detected. Buying Paused in Zone {current_zone} for {symbol}.")
                        elif capital_crunch:
                            logger.warning(f"    [SKIP] CAPITAL_CRUNCH active. Buy paused for {symbol}.")
                        else:
                            # NEW: Check Global Budget Enforcement
                            my_budget = get_my_budget()
                            try:
                                pos = trading_client.get_open_position(symbol)
                                current_val = float(pos.market_value)
                            except:
                                current_val = 0.0

                            if current_val >= my_budget:
                                logger.warning(f"    [BUDGET STOP] {symbol} Current value ${current_val:.2f} >= Budget ${my_budget:.2f}. Skipping buy.")
                                # We still allow the loop to continue so we can SELL if price rises
                            else:
                                logger.info(f"    [BUY] {symbol} Dropped to Zone {current_zone}")
                                
                                # Check Cash & Global Budget limits
                                acct = trading_client.get_account()
                                buying_power = float(acct.buying_power)
                                
                                if not is_budget_ok:
                                    logger.warning(f"    [SKIP] {symbol} Grid buy $50 > Available (budget limit)")
                                    
                                elif buying_power > BUDGET_PER_GRID:
                                    qty = BUDGET_PER_GRID / price
                                    c_id = f"crypto_grid-{symbol.replace('/', '')}-{int(time.time())}"
                                    req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, client_order_id=c_id)
                                    trading_client.submit_order(order_data=req)
                                    
                                    send_discord(f"🟢 **GRID BUY {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}")
                                    log_to_influx(symbol, "grid_buy", price, qty)
                                else:
                                    logger.warning(f"    [SKIP] {symbol} Low Balance: ${buying_power:.2f} (Need ${BUDGET_PER_GRID})")

                    # PRICE RISE -> SELL (Take Profit)
                    elif current_zone > previous_zone:
                        logger.info(f"    [SELL] {symbol} Rose to Zone {current_zone}")
                        
                        # Verify Inventory
                        qty_to_sell = BUDGET_PER_GRID / price
                        current_qty_held = 0.0
                        
                        # [FIX] Robust Position Fetching
                        try:
                            pos = trading_client.get_open_position(symbol)
                            current_qty_held = float(pos.qty)
                        except Exception as e1:
                            # Fallback for BTCUSD vs BTC/USD mismatch
                            try:
                                 alt_sym = symbol.replace("/", "")
                                 pos = trading_client.get_open_position(alt_sym)
                                 current_qty_held = float(pos.qty)
                            except Exception as e2: 
                                 registry.log_error("crypto_grid", "check_inventory", e2, context=f"{symbol} + {alt_sym}")
                                 current_qty_held = 0.0
                        
                        if current_qty_held >= qty_to_sell:
                            # Full Sell
                            c_id = f"crypto_grid-{symbol.replace('/', '')}-{int(time.time())}"
                            req = MarketOrderRequest(symbol=symbol, qty=qty_to_sell, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=c_id)
                            trading_client.submit_order(order_data=req)

                            send_discord(f"🔴 **GRID SELL {symbol}**\nPrice: ${price:,.2f}\nZone: {current_zone}")
                            log_to_influx(symbol, "grid_sell", price, qty_to_sell)
                            
                        elif current_qty_held > (qty_to_sell * 0.1):
                            # [FIX] Partial Sell (Sweep Dust)
                            logger.info(f"    [SWEEP] {symbol} Selling remaining {current_qty_held:.6f} (Target: {qty_to_sell:.6f})")
                            c_id = f"crypto_grid-{symbol.replace('/', '')}-{int(time.time())}"
                            req = MarketOrderRequest(symbol=symbol, qty=current_qty_held, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=c_id)
                            trading_client.submit_order(order_data=req)
                            
                            send_discord(f"🧹 **GRID SWEEP {symbol}**\nSold remaining {current_qty_held:.4f}\nPrice: ${price:,.2f}")
                            log_to_influx(symbol, "grid_sweep", price, current_qty_held)
                            
                        else:
                            # [FIX] Clean Log (No Warn) using Info
                            logger.info(f"    [SKIP] {symbol} Sell Signal but Zero Inventory (Ghost Signal handled).")

                grid["prev_zone"] = current_zone
            
            # Sleep at the end of the full multi-symbol loop
            time.sleep(30)

        except Exception as e:
            registry.log_error("crypto_grid", "main_loop", e)
            logger.error(f"Grid Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_grid_bot()