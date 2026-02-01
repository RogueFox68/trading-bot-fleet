import config
import time
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoLatestTradeRequest

# --- CONFIGURATION (UPDATE THESE FROM YOUR CHART) ---
SYMBOL = "BTC/USD"       # The Coin to trade
GRID_TOP = 85000         # The "Ceiling" of your consolidation
GRID_BOTTOM = 70000      # The "Floor" of your consolidation
GRID_LEVELS = 6          # How many zones to slice it into
BUDGET_PER_GRID = 50     # How much $ to buy per level (keep it small for testing)

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER
DISCORD_URL = config.WEBHOOK_CRYPTO

# --- INFLUXDB (For Grafana) ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- CLIENTS ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = CryptoHistoricalDataClient()

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    """Writes trade data to InfluxDB with error reporting"""
    try:
        measurement = "crypto_trades"
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_crypto_price(symbol):
    """
    Fetches the latest trade price from Alpaca (Replacing Coinbase).
    """
    try:
        req = CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_crypto_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        print(f"  [!] Price Error {symbol}: {e}")
        return None

def run_grid_bot():
    print(f"--- CRYPTO GRID BOT ({SYMBOL}) STARTED ---")
    print(f"Range: ${GRID_BOTTOM} - ${GRID_TOP} | Levels: {GRID_LEVELS}")
    send_discord(f"🕸️ **Grid Bot Online**...")

    log_to_influx(SYMBOL, "startup", 0, 0)

    # Calculate Grid Zones
    zone_size = (GRID_TOP - GRID_BOTTOM) / GRID_LEVELS
    previous_zone = -1 # Start unknown

    while True:
        try:
            price = get_crypto_price(SYMBOL)
            if price is None:
                time.sleep(60)
                continue

            # Calculate which "Zone" we are in (0 is bottom, 4 is top)
            if price < GRID_BOTTOM:
                current_zone = -1 # Below Range (Danger!)
            elif price > GRID_TOP:
                current_zone = GRID_LEVELS # Above Range (Moon!)
            else:
                current_zone = int((price - GRID_BOTTOM) / zone_size)

            print(f"  {SYMBOL} | Price: ${price:,.2f} | Zone: {current_zone} (Prev: {previous_zone})", end='\r')

            # --- TRADING LOGIC ---
            # Only trade if we CHANGED zones
            if current_zone != previous_zone and previous_zone != -1:

                # 1. PRICE DROPPED A ZONE -> BUY (Accumulate)
                if current_zone < previous_zone:
                    print(f"\n    [BUY] Price dropped to Zone {current_zone}")
                    qty = BUDGET_PER_GRID / price
                    req = MarketOrderRequest(symbol=SYMBOL, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC)
                    trading_client.submit_order(order_data=req)

                    send_discord(f"🟢 **GRID BUY {SYMBOL}**\nPrice: ${price:,.2f}\nZone: {current_zone}")
                    log_to_influx(SYMBOL, "grid_buy", price, qty)

                # 2. PRICE ROSE A ZONE -> SELL (Take Profit)
                elif current_zone > previous_zone:
                    print(f"\n    [SELL] Price rose to Zone {current_zone}")
                    qty_to_sell = BUDGET_PER_GRID / price
                    
                    # Check if we actually have it first
                    current_qty_held = 0.0
                    try:
                        # Alpaca stores positions as "BTCUSD", not "BTC/USD"
                        pos_symbol = SYMBOL.replace("/", "") 
                        pos = trading_client.get_open_position(pos_symbol)
                        current_qty_held = float(pos.qty)
                    except:
                        # If 404 (Position Not Found), we hold 0.
                        current_qty_held = 0.0
                    
                    if current_qty_held >= qty_to_sell:
                        req = MarketOrderRequest(symbol=SYMBOL, qty=qty_to_sell, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                        trading_client.submit_order(order_data=req)

                        send_discord(f"🔴 **GRID SELL {SYMBOL}**\nPrice: ${price:,.2f}\nZone: {current_zone}")
                        log_to_influx(SYMBOL, "grid_sell", price, qty_to_sell)
                    else:
                        print(f"    [!] Signal to Sell, but insufficient qty. Held: {current_qty_held}")

            # Update State
            previous_zone = current_zone

            # Crypto moves fast, check every 30 seconds
            time.sleep(30)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_grid_bot()