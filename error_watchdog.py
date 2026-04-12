import os
import time
import json
from influxdb import InfluxDBClient
from dotenv import load_dotenv

# Load Docker environment variables
load_dotenv()

INFLUX_HOST = os.getenv('INFLUX_HOST', 'influxdb')
INFLUX_PORT = int(os.getenv('INFLUX_PORT', 8086))
DB_NAME = 'trading_bots'  # Per CLAUDE.md Rule 9, must be exactly this
REGISTRY_FILE = "logs/fleet_error_registry.jsonl"

def setup_influx():
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    # Ensure database exists
    if {'name': DB_NAME} not in client.get_list_database():
        client.create_database(DB_NAME)
    client.switch_database(DB_NAME)
    return client

def tail_and_push(client):
    """Tails the JSONL file and pushes new errors to InfluxDB"""
    print(f"Starting Error Watchdog. Tailing {REGISTRY_FILE}...")
    print(f"Pushing to InfluxDB at {INFLUX_HOST}:{INFLUX_PORT}")
    
    # Ensure the file exists before tailing
    if not os.path.exists(REGISTRY_FILE):
        open(REGISTRY_FILE, 'a').close()

    with open(REGISTRY_FILE, 'r') as f:
        # Seek to the end of the file (only process new errors)
        f.seek(0, 2)
        
        while True:
            line = f.readline()
            if not line:
                time.sleep(1) # Wait for new errors
                continue
                
            try:
                error_data = json.loads(line.strip())
                
                # Format payload for InfluxDB
                json_body = [{
                    "measurement": "bot_error_events",
                    "tags": {
                        "bot_id": error_data.get("bot_id", "unknown"),
                        "component": error_data.get("component", "unknown"),
                        "error_type": error_data.get("error_type", "unknown")
                    },
                    "fields": {
                        "value": 1,
                        "message": error_data.get("message", ""),
                        "context": error_data.get("context", "")
                    }
                }]
                
                client.write_points(json_body)
                print(f"Logged error to InfluxDB: {error_data.get('bot_id')} -> {error_data.get('component')}")
                
            except json.JSONDecodeError:
                pass # Ignore malformed lines to prevent the watchdog from crashing
            except Exception as e:
                print(f"InfluxDB Write Failure (Watchdog surviving): {e}")
                time.sleep(5) # Back off if DB is unreachable

if __name__ == "__main__":
    influx_client = setup_influx()
    tail_and_push(influx_client)
