import config
import time
import datetime
import requests
from alpaca.trading.client import TradingClient

# --- CONFIGURATION ---
API_KEY = config.API_KEY
SECRET_KEY = config.SECRET_KEY
PAPER = config.PAPER

# --- INFLUX SETUP ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

def log_metric(measurement, fields_dict):
    """Generic logger for account stats"""
    try:
        # Convert dict to "key=value,key=value" string
        field_str = ",".join([f"{k}={v}" for k, v in fields_dict.items()])
        
        # Line Protocol: measurement,tag=value field=value timestamp
        # We use a tag source=alpaca so we can filter easily
        data_str = f"{measurement},source=alpaca {field_str}"
        
        url = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"
        response = requests.post(url, data=data_str)
        
        if response.status_code != 204:
            print(f"[!] Influx Error: {response.text}")
            
    except Exception as e:
        print(f"[!] Log Error: {e}")

def run_watcher():
    print("--- ACCOUNT WATCHER ONLINE ---")
    print("Logging P&L every 60 seconds...")
    
    while True:
        try:
            # 1. Get Account Data
            account = trading_client.get_account()
            
            # 2. Extract Key Metrics
            equity = float(account.equity)
            cash = float(account.cash)
            # 'last_equity' is the closing value of the previous day
            last_equity = float(account.last_equity) 
            
            # Calculate Day's P&L
            day_pnl = equity - last_equity
            day_pnl_pct = (day_pnl / last_equity) * 100 if last_equity else 0

            print(f"Equity: ${equity:,.2f} | Day P&L: ${day_pnl:,.2f} ({day_pnl_pct:.2f}%)")

            # 3. Log to Influx
            # We log these as floats so Grafana can graph them
            log_metric("account_stats", {
                "equity": equity,
                "cash": cash,
                "day_pnl": day_pnl,
                "day_pnl_pct": day_pnl_pct
            })

            time.sleep(60)

        except Exception as e:
            print(f"CRITICAL: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_watcher()