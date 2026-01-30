import time
import json
import subprocess
import requests
import socket
import datetime
import os
import shutil
import config  # Ensure config.py has WEBHOOK_OVERSEER and INFLUX details

# --- CONFIGURATION ---
BOT_CONFIG_FILE = "bot_config.json"
CHECK_INTERVAL = 60
HOSTNAME = socket.gethostname()

# --- DISCORD ALERTS ---
def send_discord_alert(msg):
    """Sends admin alerts to the specific Overseer Webhook."""
    try:
        # OLD: webhook = getattr(config, 'WEBHOOK_OVERSEER', config.WEBHOOK_URL)
        
        # NEW: Explicitly use the Overseer webhook
        payload = {
            "content": msg, 
            "username": "Supervisor AI 👁️"
        }
        requests.post(config.WEBHOOK_OVERSEER, json=payload, timeout=5)
    except Exception as e:
        print(f"[!] Discord Error: {e}")

# --- INFLUXDB LOGGING (From Watcher) ---
def log_process_to_influx(proc):
    """
    Logs a single PM2 process's metrics to InfluxDB.
    """
    try:
        name = proc.get('name')
        pm2_env = proc.get('pm2_env', {})
        
        status = pm2_env.get('status')
        status_code = 1 if status == 'online' else 0
        
        memory = pm2_env.get('memory', 0)
        cpu = pm2_env.get('cpu', 0)
        restart_count = pm2_env.get('restart_time', 0)
        uptime = 0
        if status == 'online':
            uptime = int((time.time() * 1000) - pm2_env.get('pm_uptime', time.time()*1000))

        # Format for InfluxDB Line Protocol
        data_str = (
            f'bot_monitor,host={HOSTNAME},bot={name} '
            f'status_code={status_code},memory={memory},cpu={cpu},restarts={restart_count},uptime={uptime}'
        )
        
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=2)
        
    except Exception as e:
        print(f"[!] Influx Error for {name}: {e}")

# --- MANAGEMENT LOGIC (From Overseer) ---
def load_bot_config():
    # 1. Check if active config exists
    if not os.path.exists(BOT_CONFIG_FILE):
        print(f"[!] {BOT_CONFIG_FILE} missing.")
        
        # 2. Look for template
        if os.path.exists("bot_config.template.json"):
            print(f"  -> Found template. Creating new {BOT_CONFIG_FILE}...")
            shutil.copy("bot_config.template.json", BOT_CONFIG_FILE)
        else:
            print("[!] No template found. Cannot start.")
            return None

    # 3. Load the file
    try:
        with open(BOT_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        print(f"[!] JSON Error: {e}")
        return None

def manage_fleet(pm2_list, bot_config_data):
    """
    Compares actual PM2 state vs. Desired Config state.
    """
    if not bot_config_data:
        return

    # Create a map of currently running processes for easy lookup
    # Format: {'crypto_grid': 'online', 'wheel_bot': 'stopped'}
    current_state = {p['name']: p['pm2_env']['status'] for p in pm2_list}

    target_bots = bot_config_data.get("bots", {})
    
    for bot_name, details in target_bots.items():
        script = details.get('script')
        desired_status = details.get('status') # 'active' or 'paused'
        actual_status = current_state.get(bot_name, "missing")

        # 1. Bot should be ACTIVE
        if desired_status == "active":
            if actual_status == "missing":
                print(f"  [+] Launching {bot_name}...")
                subprocess.run(['pm2', 'start', script, '--name', bot_name])
                send_discord_alert(f"🟢 **LAUNCH**: `{bot_name}` started by Supervisor.")
            
            elif actual_status in ['stopped', 'errored']:
                print(f"  [!] Reviving {bot_name}...")
                subprocess.run(['pm2', 'restart', bot_name])
                send_discord_alert(f"⚠️ **REVIVED**: `{bot_name}` was down/stopped. Restarting...")

        # 2. Bot should be PAUSED
        elif desired_status == "paused":
            if actual_status == "online":
                print(f"  [-] Pausing {bot_name}...")
                subprocess.run(['pm2', 'stop', bot_name])
                send_discord_alert(f"⏸️ **PAUSED**: `{bot_name}` stopped by config.")

# --- MAIN LOOP ---
def run_supervisor():
    print("--- 🛡️ FLEET SUPERVISOR ONLINE ---")
    send_discord_alert("🛡️ **Supervisor Online**\nMonitoring Grafana & Enforcing Config.")

    while True:
        try:
            # 1. Get Global PM2 Status (One call for efficiency)
            result = subprocess.run(['pm2', 'jlist'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pm2_list = json.loads(result.stdout)
            
            # 2. Log Metrics to InfluxDB (The Watcher Job)
            for proc in pm2_list:
                log_process_to_influx(proc)
            
            # 3. Read the Brain (Config)
            bot_config = load_bot_config()
            
            # 4. Enforce Orders (The Overseer Job)
            if bot_config:
                # Check for Global Kill Switch
                if bot_config.get("global_settings", {}).get("emergency_stop", False):
                    print("[!!!] EMERGENCY STOP ACTIVE")
                    # Stop everything except the Supervisor itself
                    for p in pm2_list:
                        if p['name'] != 'supervisor' and p['pm2_env']['status'] == 'online':
                            subprocess.run(['pm2', 'stop', p['name']])
                    time.sleep(10)
                    continue

                manage_fleet(pm2_list, bot_config)

        except Exception as e:
            print(f"[!] Main Loop Error: {e}")
            
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_supervisor()