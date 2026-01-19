import config
import time
import datetime
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass

# --- CONFIGURATION ---
# Mapping Symbols to Bots for Attribution
BOT_MAPPING = {
    "survivor": ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO"],
    "wheel": ["DIS", "F", "PLTR"], # Stocks managed by Wheel
    "crypto": ["BTC/USD", "ETH/USD", "SOL/USD"]
    # "trend" will be the "catch-all" for everything else (NVDA, TSLA, etc)
}

# --- CREDENTIALS ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

# --- CLIENT ---
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

def log_metric(measurement, tags, fields):
    """
    Writes a single point to InfluxDB using Line Protocol.
    tags: dict of string tags (indexed)
    fields: dict of float/int/str values (not indexed)
    """
    try:
        # Format tags: key=value,key=value
        tag_str = ",".join([f"{k}={v}" for k, v in tags.items()])
        
        # Format fields: key=value,key=value
        field_parts = []
        for k, v in fields.items():
            if isinstance(v, str):
                field_parts.append(f'{k}="{v}"')
            else:
                field_parts.append(f'{k}={v}')
        field_str = ",".join(field_parts)

        data_str = f"{measurement},{tag_str} {field_str}"
        
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=5)
    except Exception as e:
        print(f"[!] Influx Write Error: {e}")

def get_bot_name(symbol, asset_class):
    """Determines which bot owns this position."""
    if asset_class == AssetClass.CRYPTO:
        return "crypto_grid"
    
    if asset_class == AssetClass.US_OPTION:
        return "wheel_bot"
    
    if symbol in BOT_MAPPING["survivor"]:
        return "survivor_bot"
    
    if symbol in BOT_MAPPING["wheel"]:
        return "wheel_bot"
        
    # Default to Trend Bot for other tech stocks (NVDA, TSLA, etc.)
    return "trend_bot"

def run_accountant():
    print("--- 🧾 ACCOUNTANT BOT STARTED ---")
    print("Tracking Leaders & Laggards...")

    while True:
        try:
            # 1. FETCH ACCOUNT TOTALS
            account = trading_client.get_account()
            equity = float(account.equity)
            cash = float(account.cash)
            buying_power = float(account.buying_power)
            
            # Log Global Stats
            log_metric(
                measurement="account_stats",
                tags={"type": "global"},
                fields={
                    "equity": equity,
                    "cash": cash,
                    "buying_power": buying_power
                }
            )

            # 2. FETCH POSITIONS & CALCULATE BOT PERFORMANCE
            positions = trading_client.get_all_positions()
            
            # buckets for aggregation
            bot_stats = {
                "survivor_bot": {"allocation": 0.0, "unrealized_pl": 0.0, "positions": 0},
                "trend_bot":    {"allocation": 0.0, "unrealized_pl": 0.0, "positions": 0},
                "wheel_bot":    {"allocation": 0.0, "unrealized_pl": 0.0, "positions": 0},
                "crypto_grid":  {"allocation": 0.0, "unrealized_pl": 0.0, "positions": 0}
            }

            for p in positions:
                symbol = p.symbol
                market_value = float(p.market_value)
                unrealized_pl = float(p.unrealized_pl)
                
                # Identify the owner
                owner = get_bot_name(symbol, p.asset_class)
                
                # Accumulate stats
                if owner in bot_stats:
                    bot_stats[owner]["allocation"] += market_value
                    bot_stats[owner]["unrealized_pl"] += unrealized_pl
                    bot_stats[owner]["positions"] += 1

            # 3. PUSH BOT STATS TO INFLUX
            print(f"\n[{datetime.datetime.now().strftime('%H:%M')}] Update:")
            for bot, stats in bot_stats.items():
                print(f"  {bot:<15} | Alloc: ${stats['allocation']:<8.2f} | P&L: ${stats['unrealized_pl']:<8.2f}")
                
                log_metric(
                    measurement="bot_performance",
                    tags={"bot": bot},
                    fields={
                        "allocation": stats['allocation'],
                        "unrealized_pl": stats['unrealized_pl'],
                        "position_count": stats['positions']
                    }
                )

            # Run every 5 minutes (Trading is slow, no need for rapid updates)
            time.sleep(300)

        except Exception as e:
            print(f"[!] Accountant Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_accountant()