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
# MUST be "influxdb" — the bots run INSIDE the trading-fleet container, where
# localhost is the container itself, so a localhost here sends every metric and
# error into the void with no visible failure. "influxdb" is the sibling
# container's name on the compose network (Docker DNS resolves it).
INFLUX_HOST    = "influxdb"
INFLUX_PORT    = 8086
# Must match compose INFLUXDB_DB exactly; a past "tradingbots" typo silently
# dropped every write.
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
