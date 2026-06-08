"""
error_registry.py
Centralized mapping of common error codes across the fleet.
These codes can be used to tag errors sent to Discord for easier filtering and alerting.
"""

ERROR_CODES = {
    "ERR_BUDGET_MISSING": {
        "code": "CFO-001",
        "severity": "CRITICAL",
        "description": "bot_config.json is missing or has no allocation for the bot."
    },
    "ERR_DAILY_LOSS": {
        "code": "SAFE-001",
        "severity": "CRITICAL",
        "description": "Daily loss cap exceeded."
    },
    "ERR_SYMBOL_EXPOSURE": {
        "code": "SAFE-002",
        "severity": "WARNING",
        "description": "Max symbol exposure limit reached."
    },
    "ERR_ORDER_NOTIONAL": {
        "code": "SAFE-003",
        "severity": "WARNING",
        "description": "Single order notional cap exceeded."
    },
    "ERR_STALE_TARGETS": {
        "code": "DATA-001",
        "severity": "WARNING",
        "description": "active_targets.json is too old. Falling back to static."
    },
    "ERR_API_TIMEOUT": {
        "code": "NET-001",
        "severity": "WARNING",
        "description": "Alpaca API timeout or connection error."
    },
    "ERR_INFLUX_WRITE": {
        "code": "DB-001",
        "severity": "WARNING",
        "description": "Failed to write trade to InfluxDB."
    }
}

def get_error_info(key):
    return ERROR_CODES.get(key, {"code": "UNK-000", "severity": "UNKNOWN", "description": "Unknown Error"})
