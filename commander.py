import discord
from discord.ext import commands, tasks
import subprocess
import json
import time
import os
import requests
import socket
import asyncio
import datetime
import config  # Ensure your config.py has your keys
from alpaca.trading.client import TradingClient
from logger import registry

# --- CONFIGURATION ---
BOT_CONFIG_FILE = "bot_config.json"
CHECK_INTERVAL = 60
HOSTNAME = socket.gethostname()

# --- DISCORD SETUP ---
# You must put your BOT TOKEN here or in config.py
TOKEN = config.DISCORD_TOKEN
# Tolerate the historical 'DISCORD_CHANNNEL_ID' (triple-N) typo that exists in some config.py copies.
CHANNEL_ID = getattr(config, "DISCORD_CHANNEL_ID", None) or getattr(config, "DISCORD_CHANNNEL_ID", None)
if CHANNEL_ID is None:
    print("[!] config.DISCORD_CHANNEL_ID is not set — commander cannot reach Discord.")

# --- GLOBALS ---
LAST_STALE_ALERT = 0
STALE_ALERT_COOLDOWN = 3600  # 1 Hour

# Per-process alert throttle. The watchdog runs every 60s, so an un-throttled
# alert on a down process repeats sixty times an hour, per bot — and a fleet-wide
# event (see PAUSED_BY_COMMANDER) fires all nine at once. That is how a real
# incident gets buried: the 2026-09 storm was ~9 identical "CRASHED" pings every
# cycle, and the one process that WAS genuinely failing looked like the rest.
DOWN_ALERT_COOLDOWN = 900   # 15 min per process
_last_down_alert = {}       # bot_name -> monotonic-ish time of its last ping

# Bots THIS process stopped via the auto-pause branch (config said "paused").
# Restarting one of those is a RESUME, not a crash — without this, every
# analyst-driven pause/resume cycle (VIX>28 and back) reported the entire fleet
# as having crashed.
PAUSED_BY_COMMANDER = set()

# Enable Message Content Intent (Required for commands)
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# --- ALPACA CLIENT (For Status) ---
try:
    trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
except:
    trading_client = None

# --- ORIGINAL SUPERVISOR LOGIC ---

def log_process_to_influx(proc):
    """Logs PM2 metrics to InfluxDB (Keeps Grafana happy)."""
    try:
        name = proc.get('name')
        pm2_env = proc.get('pm2_env', {})
        status = pm2_env.get('status')
        status_code = 1 if status == 'online' else 0
        # `pm2 jlist` reports live resource usage under 'monit', NOT 'pm2_env'
        # (which carries status/uptime/restart_time). Reading them off pm2_env
        # meant bot_monitor.memory and .cpu were hardcoded 0 in Grafana — the
        # fleet's only per-process memory series has been flat since day one.
        monit = proc.get('monit', {}) or {}
        memory = monit.get('memory', 0) or 0
        cpu = monit.get('cpu', 0) or 0
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

def _recent_errors(pm2_env, lines=6):
    """Tail of the process's PM2 error log, so a down-bot alert says WHY.
    Best-effort: an unreadable log costs the detail, never the alert."""
    path = pm2_env.get('pm_err_log_path')
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, 'r', errors='replace') as f:
            tail = [ln.rstrip() for ln in f.readlines()[-lines:] if ln.strip()]
    except Exception:
        return ""
    if not tail:
        return ""
    body = "\n".join(tail)[-700:]   # keep the Discord message well under 2000
    return f"```\n{body}\n```"


async def _alert_down(bot_name, actual_status, pm2_env, resuming):
    """One throttled Discord message about a process that is not running.

    Throttled PER BOT (DOWN_ALERT_COOLDOWN) and worded for what actually
    happened — 'stopped' is not a crash, and a resume after a config-driven
    pause is not an incident at all."""
    if resuming:
        # Commander stopped it itself; bringing it back is routine.
        return

    now = time.time()
    if now - _last_down_alert.get(bot_name, 0) < DOWN_ALERT_COOLDOWN:
        return
    _last_down_alert[bot_name] = now

    restarts = pm2_env.get('restart_time', 0)
    if actual_status == 'errored':
        headline = f"🔴 **{bot_name.upper()} CRASHED**"
        detail = (f"PM2 gave up after repeated exits "
                  f"({restarts} restarts). Restarting once more...")
    else:
        headline = f"⚠️ **{bot_name.upper()} NOT RUNNING**"
        detail = (f"Status: `{actual_status}` while bot_config says active "
                  f"({restarts} restarts). Restarting...")

    msg = (f"{headline}\n{detail}\n"
           f"_Further alerts for this process are muted "
           f"{DOWN_ALERT_COOLDOWN // 60} min._")
    errors = _recent_errors(pm2_env)
    if errors:
        msg += f"\n{errors}"

    try:
        channel = await bot.fetch_channel(int(CHANNEL_ID))
        await channel.send(msg)
    except Exception as e:
        # Rule: no bare `except: pass` on anything that matters. A watchdog
        # that cannot reach Discord is exactly what the error registry is for.
        registry.log_error("commander", "alert_down", e, context=bot_name)
        print(f"[!] Down-alert send failed for {bot_name}: {e}")


async def manage_fleet(pm2_list, bot_config):
    """Auto-Restarts bots based on config."""
    if not bot_config: return

    procs = {p['name']: p for p in pm2_list}
    current_state = {name: p['pm2_env']['status'] for name, p in procs.items()}
    target_bots = bot_config.get("bots", {})

    for bot_name, details in target_bots.items():
        script = details.get('script')
        desired_status = details.get('status')
        actual_status = current_state.get(bot_name, "missing")

        # AUTO-REVIVE LOGIC
        if desired_status == "active":
            if actual_status == "missing":
                subprocess.run(['pm2', 'start', script, '--name', bot_name])
                PAUSED_BY_COMMANDER.discard(bot_name)
                print(f"  [+] Launching {bot_name}...")
            elif actual_status in ['stopped', 'errored']:
                # Alert BEFORE restart — but only when this is news. A bot
                # commander itself paused is being RESUMED, not revived.
                resuming = bot_name in PAUSED_BY_COMMANDER
                await _alert_down(bot_name, actual_status,
                                  procs.get(bot_name, {}).get('pm2_env', {}),
                                  resuming)

                subprocess.run(['pm2', 'restart', bot_name])
                PAUSED_BY_COMMANDER.discard(bot_name)
                print(f"  [{'>' if resuming else '!'}] "
                      f"{'Resuming' if resuming else 'Reviving'} {bot_name}...")
            elif actual_status == 'online':
                # Healthy again: clear the throttle so the NEXT genuine failure
                # alerts immediately instead of waiting out an old cooldown.
                _last_down_alert.pop(bot_name, None)
                PAUSED_BY_COMMANDER.discard(bot_name)

        # AUTO-PAUSE LOGIC
        elif desired_status == "paused" and actual_status == "online":
            subprocess.run(['pm2', 'stop', bot_name])
            PAUSED_BY_COMMANDER.add(bot_name)
            print(f"  [-] Pausing {bot_name}...")

# --- BACKGROUND TASK (The Watchdog) ---

@tasks.loop(seconds=CHECK_INTERVAL)
async def watchdog_task():
    """Runs every 60s: Logs to Influx & Enforces Config."""
    global LAST_STALE_ALERT
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
                await manage_fleet(pm2_list, config_data) # UPDATED: await async function
                
    except Exception as e:
        print(f"[!] Watchdog Error: {e}")

# [NEW] Stale Target Check with Cooldown (Skipped on Weekends)
    try:
        # 0 = Monday, ..., 4 = Friday, 5 = Saturday, 6 = Sunday
        current_day = datetime.datetime.today().weekday()
        
        # Only run the stale target check on weekdays
        if current_day < 5:  
            target_file = "active_targets.json"
            
            if os.path.exists(target_file):
                file_age = time.time() - os.path.getmtime(target_file)
                if file_age > 86400: # 24 Hours
                    # Check Cooldown
                    if (time.time() - LAST_STALE_ALERT) > STALE_ALERT_COOLDOWN:
                        try:
                            channel = await bot.fetch_channel(int(CHANNEL_ID))
                            await channel.send(
                                f"⚠️ **STALE TARGETS DETECTED**\n"
                                f"File age: {file_age/3600:.1f} hours\n"
                                f"Bots are using fallback watchlists.\n"
                                f"_The scout on the Corsair writes this file; "
                                f"a stale one means its run or the SCP failed._"
                            )
                            LAST_STALE_ALERT = time.time()
                        except Exception as e:
                            registry.log_error("commander", "stale_target_alert", e)
                            print(f"[!] Stale-target alert send failed: {e}")
            else:
                 # If missing entirely
                 # We might want cooldown here too, but missing file is more critical
                 if (time.time() - LAST_STALE_ALERT) > STALE_ALERT_COOLDOWN:
                     try:
                        channel = await bot.fetch_channel(int(CHANNEL_ID))
                        await channel.send(
                            "🚨 **TARGETS FILE MISSING**\n"
                            "Check the sector scout on the Corsair.")
                        LAST_STALE_ALERT = time.time()
                     except Exception as e:
                        registry.log_error("commander", "missing_target_alert", e)
                        print(f"[!] Missing-target alert send failed: {e}")

    except Exception as e:
        print(f"[!] Stale Check Error: {e}")

@watchdog_task.before_loop
async def before_watchdog():
    await bot.wait_until_ready()

# --- DISCORD COMMANDS ---

# Find the on_ready function
@bot.event
async def on_ready():
    print(f'✅ COMMANDER ONLINE as {bot.user}')
    
    # Prevent "Task already running" error on reconnects
    if not watchdog_task.is_running():
        watchdog_task.start()

    try:
        # CHANGE THIS: Use fetch_channel (Async API Call) instead of get_channel (Cache)
        # Also ensure ID is an integer
        channel = await bot.fetch_channel(int(CHANNEL_ID))
        await channel.send("🛰️ **COMMANDER ONLINE.** Watchdog active.")
    except Exception as e:
        print(f"❌ Failed to send startup ping: {e}")

@bot.command()
async def status(ctx):
    """/status - detailed fleet report"""
    # 1. Get PM2 Status
    result = subprocess.run(['pm2', 'jlist'], stdout=subprocess.PIPE, text=True)
    process_list = json.loads(result.stdout)
    
    # 2. Get Account Equity
    equity_str = "N/A"
    if trading_client:
        try:
            acct = trading_client.get_account()
            equity_val = float(acct.portfolio_value)
            equity_str = f"${equity_val:,.2f}"
        except Exception as e:
            equity_str = "N/A ⚠️"
            registry.log_error("commander", "status_equity", e)

    # 3. Get Target File Age
    target_age_str = "Missing ❌"
    if os.path.exists("active_targets.json"):
        age_hours = (time.time() - os.path.getmtime("active_targets.json")) / 3600
        target_age_str = f"{age_hours:.1f}h"
        if age_hours > 24: target_age_str += " ⚠️"
        else: target_age_str += " ✅"

    # 4. Regime / VIX provenance (written by market_analyst)
    regime_str = "unknown ⚠️"
    cfg = load_bot_config()
    if cfg:
        gs = cfg.get("global_settings", {})
        source = gs.get("vix_source", "unknown")
        flag = " ⚠️ STALE" if gs.get("data_stale") else ""
        regime_str = (f"{gs.get('market_condition', '?')} | "
                      f"VIX {gs.get('vix', '?')} via `{source}`{flag}")

    msg = f"🤖 **FLEET STATUS**\n"
    msg += f"💰 **Equity**: {equity_str}\n"
    msg += f"📁 **Targets**: {target_age_str}\n"
    msg += f"🌡️ **Regime**: {regime_str}\n"
    msg += "--------------------------------\n"

    for proc in process_list:
        name = proc['name']
        env = proc['pm2_env']
        status = env['status']
        icon = "🟢" if status == "online" else "🔴"
        
        # Uptime
        uptime_str = "0s"
        if status == 'online':
            uptime_ms = int((time.time() * 1000) - env.get('pm_uptime', time.time()*1000))
            uptime_min = uptime_ms / 60000
            if uptime_min > 60:
                uptime_str = f"{uptime_min/60:.1f}h"
            else:
                 uptime_str = f"{uptime_min:.0f}m"

        restarts = env.get('restart_time', 0)
        
        msg += f"{icon} **{name}**\n"
        msg += f"   Status: {status}\n"
        msg += f"   Uptime: {uptime_str} | Restarts: {restarts}\n"

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
# Guarded so the module can be imported for tests and by fleet_doctor's
# import check. PM2 runs this file as a script, so this still fires.
if __name__ == "__main__":
    bot.run(TOKEN)