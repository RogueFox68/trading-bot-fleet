"""
config_docker.py - Environment-Variable-Aware Config Wrapper

Drop-in replacement for config.py inside the Docker container.
Reads from environment variables first, falls back to the on-disk config.py.

USAGE:
  The entrypoint copies this file into the code directory.
  Bots that `import config` will pick up the on-disk config.py as usual.
  
  To use env-var overrides, change bot imports from:
      import config
  to:
      import config_docker as config
  
  OR: Modify the on-disk config.py to use os.environ.get() directly.
  
  The recommended migration path is to modify config.py itself (see
  config_envaware.py for a drop-in replacement you can rename to config.py).
"""

import os

# ---- Alpaca ----
API_KEY       = os.environ.get("ALPACA_API_KEY", "")
SECRET_KEY    = os.environ.get("ALPACA_SECRET_KEY", "")
PAPER         = os.environ.get("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# ---- Discord ----
DISCORD_TOKEN      = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

# ---- InfluxDB ----
# Inside Docker network, use container name 'influxdb' instead of 'localhost'
INFLUX_HOST    = os.environ.get("INFLUX_HOST", "influxdb")
INFLUX_PORT    = int(os.environ.get("INFLUX_PORT", "8086"))
INFLUX_DB_NAME = os.environ.get("INFLUX_DB_NAME", "trading_bots")

# ---- Discord Webhooks ----
WEBHOOK_WHEEL     = os.environ.get("WEBHOOK_WHEEL", "")
WEBHOOK_TREND     = os.environ.get("WEBHOOK_TREND", "")
WEBHOOK_SURVIVOR  = os.environ.get("WEBHOOK_SURVIVOR", "")
WEBHOOK_CONDOR    = os.environ.get("WEBHOOK_CONDOR", "")
WEBHOOK_CRYPTO    = os.environ.get("WEBHOOK_CRYPTO", "")
WEBHOOK_MOONBAG   = os.environ.get("WEBHOOK_MOONBAG", "")
WEBHOOK_OVERSEER  = os.environ.get("WEBHOOK_OVERSEER", "")

# ---- Fallback: try to import from on-disk config.py ----
# This lets you run with EITHER env vars OR the existing config.py.
# Env vars always win if set; unset vars fall back to config.py values.
try:
    import importlib.util
    import sys
    
    _config_path = os.path.join(os.path.dirname(__file__), "config.py")
    if os.path.exists(_config_path):
        spec = importlib.util.spec_from_file_location("_disk_config", _config_path)
        _disk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_disk)
        
        # For each setting, use env var if set, else fall back to disk
        _this = sys.modules[__name__]
        for _attr in dir(_disk):
            if _attr.startswith("_"):
                continue
            _env_val = getattr(_this, _attr, "")
            _disk_val = getattr(_disk, _attr, "")
            # If env var is empty/default but disk has a value, use disk
            if not _env_val and _disk_val:
                setattr(_this, _attr, _disk_val)
except Exception:
    pass  # No disk config found, rely purely on env vars
