import config
import time
import datetime
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

# --- CONFIGURATION (UPDATE THESE FROM YOUR CHART) ---
SYMBOL = "BTC/USD"       # The Coin to trade
GRID_TOP = 99000         # The "Ceiling" of your consolidation
GRID_BOTTOM = 88000      # The "Floor" of your consolidation
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

def send_discord(msg):
    if "YOUR" in DISCORD_URL: return
    try:
        requests.post(DISCORD_URL, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    """Writes trade data to InfluxDB with error reporting"""
    try:
        # Auto-detect measurement name based on the bot type
        if "crypto" in __file__:
            measurement = "crypto_trades"
        elif "survivor" in __file__:
            measurement = "survivor_trades"
        else:
            measurement = "trades"

        # Line Protocol: measurement,tags fields timestamp
        data_str = f'{measurement},symbol={symbol} price={price},action="{action}",qty={qty}'

        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        
        # Send the data
        response = requests.post(url, data=data_str)

        # Check for success (204 is the only success code for Influx writes)
        if response.status_code != 204:
            print(f"  [!] InfluxDB Error {response.status_code}: {response.text}")
            
    except Exception as e:
        print(f"  [!] Failed to log to InfluxDB: {e}")

def get_crypto_price(symbol):
    try:
        # Use Alpaca's snapshot API for latest crypto price
        # Note: Depending on your plan, you might need a different data source
        # But for Crypto, TradingClient often has a simple way or we use a public API
        # Let's use a public fallback if Alpaca data is tricky on free tier
        url = f"https://api.coinbase.com/v2/prices/{symbol.replace('/','-')}/spot"
        resp = requests.get(url).json()
        return float(resp['data']['amount'])
    except:
        # Fallback for when Coinbase API is busy, try Coingecko or just print error
        print("  [!] Error fetching price")
        return None

def run_grid_bot():
    print(f"--- CRYPTO GRID BOT ({SYMBOL}) STARTED ---")
    print(f"Range: ${GRID_BOTTOM} - ${GRID_TOP} | Levels: {GRID_LEVELS}")
    send_discord(f"🕸️ **Grid Bot Online**...")

    # --- ADD THIS LINE ---
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

                # ... inside the SELL logic ...

                # 2. PRICE ROSE A ZONE -> SELL (Take Profit)
                elif current_zone > previous_zone:
                    print(f"\n    [SELL] Price rose to Zone {current_zone}")
                    qty = BUDGET_PER_GRID / price
                    
                    # Check if we actually have it first
                    try:
                        # FIX: Remove the slash for the position check
                        # Alpaca stores positions as "BTCUSD", not "BTC/USD"
                        pos_symbol = SYMBOL.replace("/", "") 
                        pos = trading_client.get_open_position(pos_symbol)
                        
                        if float(pos.qty) >= qty:
                            req = MarketOrderRequest(symbol=SYMBOL, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC)
                            trading_client.submit_order(order_data=req)

                            send_discord(f"🔴 **GRID SELL {SYMBOL}**\nPrice: ${price:,.2f}\nZone: {current_zone}")
                            log_to_influx(SYMBOL, "grid_sell", price, qty)
                        else:
                            print(f"    [!] Signal to Sell, but insufficient qty. Held: {pos.qty}")
                            
                    except Exception as e:
                        # FIX: Print the actual error 'e' so we can see what's wrong in the future
                        print(f"    [!] Sell Error: {e}")

            # Update State
            previous_zone = current_zone

            # Crypto moves fast, check every 30 seconds
            time.sleep(30)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_grid_bot()