#!/bin/bash
# =============================================================================
# Fleet Container Entrypoint
# 1. Validates the code mount (exits hard if missing - never trade stale code)
# 2. Creates log directory
# 3. Ensures bot_config.json exists (commander needs it)
# 4. Starts PM2 with the ecosystem config
# =============================================================================
set -e

APP_DIR="${APP_DIR:-/app/code}"

echo "=========================================="
echo " Trading Bot Fleet - Docker Container"
echo " $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="

# -- Validate that code is mounted --
# No fallback: trading against a stale bundled snapshot with live keys is
# strictly worse than not trading. If this fires, fix the volume mount.
if [ ! -f "$APP_DIR/survivor_bot.py" ]; then
    echo "FATAL: Bot code not found at $APP_DIR"
    echo "  Expected volume mount: -v /home/trader/bots/repo:/app/code"
    echo "  Refusing to start. Fix the mount and restart the container."
    exit 1
fi

# -- Ensure log directory exists --
mkdir -p "$APP_DIR/logs"

# -- Ensure bot_config.json exists (Commander needs it) --
if [ ! -f "$APP_DIR/bot_config.json" ]; then
    if [ -f "$APP_DIR/bot_config.template.json" ]; then
        echo "WARNING: bot_config.json missing, copying from template..."
        cp "$APP_DIR/bot_config.template.json" "$APP_DIR/bot_config.json"
    else
        echo "WARNING: No bot_config.json or template found."
    fi
fi

# -- Start PM2 --
echo "Starting PM2 fleet..."
cd "$APP_DIR"
pm2 start /app/ecosystem.config.js --no-daemon &
PM2_PID=$!

# -- Trap shutdown signals for graceful stop --
cleanup() {
    echo "Received shutdown signal, stopping fleet..."
    pm2 stop all
    pm2 kill
    exit 0
}
trap cleanup SIGTERM SIGINT

# -- Wait for PM2 (keeps container alive) --
wait $PM2_PID
