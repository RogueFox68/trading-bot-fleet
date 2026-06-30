# config.example.py
# Rename to config.py and fill in your keys
# Do NOT commit config.py to version control.

# ---- Alpaca ----
API_KEY       = "PK..."
SECRET_KEY    = "..."
PAPER         = True

# ---- Discord ----
DISCORD_TOKEN      = "..."
DISCORD_CHANNEL_ID = "..."

# ---- InfluxDB ----
INFLUX_HOST    = "localhost"
INFLUX_PORT    = 8086
INFLUX_DB_NAME = "trading_bots"

# ---- Discord Webhooks ----
# Key names must match exactly what the bots read (and config_docker.py).
WEBHOOK_WHEEL     = "https://discord.com/api/webhooks/..."
WEBHOOK_TREND     = "https://discord.com/api/webhooks/..."
WEBHOOK_SURVIVOR  = "https://discord.com/api/webhooks/..."
WEBHOOK_CONDOR    = "https://discord.com/api/webhooks/..."
WEBHOOK_CRYPTO    = "https://discord.com/api/webhooks/..."
WEBHOOK_MOONBAG   = "https://discord.com/api/webhooks/..."
WEBHOOK_OVERSEER  = "https://discord.com/api/webhooks/..."
