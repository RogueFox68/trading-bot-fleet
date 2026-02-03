import discord
from discord.ext import commands, tasks
import subprocess
import json
import time
import os
import requests
import socket
import config  # Ensure your config.py has your keys

# --- CONFIGURATION ---
BOT_CONFIG_FILE = "bot_config.json"
CHECK_INTERVAL = 60
HOSTNAME = socket.gethostname()

# --- DISCORD SETUP ---
# You must put your BOT TOKEN here or in config.py
TOKEN = config.DISCORD_TOKEN
CHANNEL_ID = config.DISCORD_CHANNNEL_ID 

# Enable Message Content Intent (Required for commands)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- ORIGINAL SUPERVISOR LOGIC ---

def log_process_to_influx(proc):
    """Logs PM2 metrics to InfluxDB (Keeps Grafana happy)."""
    try:
        name = proc.get('name')
        pm2_env = proc.get('pm2_env', {})
        status = pm2_env.get('status')
        status_code = 1 if status == 'online' else 0
        memory = pm2_env.get('memory', 0)
        cpu = pm2_env.get('cpu', 0)
        restart_count = pm2_env.get('restart_time', 0)
        
        # Calculate uptime
        uptime = 0
        if status == 'online':
            uptime = int((time.time() * 1000) - pm2_env.get('pm_uptime', time.time()*1000))

        data_str = (
            f'bot_monitor,host={HOSTNAME},bot={name} '
            f'status_code={status_code},memory={memory},cpu={cpu},restarts={restart_count},uptime={uptime}'
        )
        
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=2)
    except Exception as e:
        print(f"[!] Influx Error: {e}")

def load_bot_config():
    try:
        with open(BOT_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except:
        return None

def manage_fleet(pm2_list, bot_config):
    """Auto-Restarts bots based on config."""
    if not bot_config: return

    current_state = {p['name']: p['pm2_env']['status'] for p in pm2_list}
    target_bots = bot_config.get("bots", {})
    
    for bot_name, details in target_bots.items():
        script = details.get('script')
        desired_status = details.get('status')
        actual_status = current_state.get(bot_name, "missing")

        # AUTO-REVIVE LOGIC
        if desired_status == "active":
            if actual_status == "missing":
                subprocess.run(['pm2', 'start', script, '--name', bot_name])
                print(f"  [+] Launching {bot_name}...")
            elif actual_status in ['stopped', 'errored']:
                subprocess.run(['pm2', 'restart', bot_name])
                print(f"  [!] Reviving {bot_name}...")

        # AUTO-PAUSE LOGIC
        elif desired_status == "paused" and actual_status == "online":
            subprocess.run(['pm2', 'stop', bot_name])
            print(f"  [-] Pausing {bot_name}...")

# --- BACKGROUND TASK (The Watchdog) ---

@tasks.loop(seconds=CHECK_INTERVAL)
async def watchdog_task():
    """Runs every 60s: Logs to Influx & Enforces Config."""
    try:
        # 1. Get PM2 Status
        result = subprocess.run(['pm2', 'jlist'], stdout=subprocess.PIPE, text=True)
        pm2_list = json.loads(result.stdout)

        # 2. Log to Grafana
        for proc in pm2_list:
            log_process_to_influx(proc)

        # 3. Manage Fleet
        config_data = load_bot_config()
        if config_data:
            # Check Emergency Stop
            if config_data.get("global_settings", {}).get("emergency_stop", False):
                # Panic logic handled by config, but we can enforce it here too if needed
                pass
            else:
                manage_fleet(pm2_list, config_data)
                
    except Exception as e:
        print(f"[!] Watchdog Error: {e}")

@watchdog_task.before_loop
async def before_watchdog():
    await bot.wait_until_ready()

# --- DISCORD COMMANDS ---

@bot.event
async def on_ready():
    print(f'✅ COMMANDER ONLINE as {bot.user}')
    watchdog_task.start() # Start the background loop
    channel = bot.get_channel(CHANNEL_ID)
    if channel:
        await channel.send("🛰️ **COMMANDER ONLINE.** Watchdog active.")

@bot.command()
async def status(ctx):
    """/status - detailed fleet report"""
    result = subprocess.run(['pm2', 'jlist'], stdout=subprocess.PIPE, text=True)
    process_list = json.loads(result.stdout)
    
    msg = "🤖 **FLEET STATUS**\n"
    for proc in process_list:
        name = proc['name']
        status = proc['pm2_env']['status']
        icon = "🟢" if status == "online" else "🔴"
        msg += f"{icon} **{name}**: {status}\n"
    await ctx.send(msg)

@bot.command()
async def stop(ctx, bot_name):
    """/stop bot_name"""
    subprocess.run(['pm2', 'stop', bot_name])
    await ctx.send(f"🛑 Stopping **{bot_name}**...")

@bot.command()
async def start(ctx, bot_name):
    """/start bot_name"""
    subprocess.run(['pm2', 'restart', bot_name])
    await ctx.send(f"🚀 Starting **{bot_name}**...")

@bot.command()
async def panic(ctx):
    """/panic - KILL EVERYTHING"""
    await ctx.send("🚨 **EMERGENCY STOP TRIGGERED** 🚨")
    
    # 1. Update Config to prevent auto-restart
    with open(BOT_CONFIG_FILE, 'r+') as f:
        data = json.load(f)
        data['global_settings']['emergency_stop'] = True
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()

    # 2. Kill processes
    subprocess.run(['pm2', 'stop', 'all'])
    # Restart self (Commander) so we can listen for /resume
    subprocess.run(['pm2', 'restart', 'commander']) 
    
    await ctx.send("🔥 All trading bots halted. Config set to Emergency Stop.")

@bot.command()
async def resume(ctx):
    """/resume - Clear Emergency Stop"""
    with open(BOT_CONFIG_FILE, 'r+') as f:
        data = json.load(f)
        data['global_settings']['emergency_stop'] = False
        f.seek(0)
        json.dump(data, f, indent=4)
        f.truncate()
    await ctx.send("✅ Emergency Stop cleared. Watchdog will revive bots shortly.")

# --- RUN ---
bot.run(TOKEN)