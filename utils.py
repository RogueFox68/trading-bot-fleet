import json
from alpaca.trading.enums import AssetClass
from logger import logger

# --- CENTRALIZED ASSET MAP ---
# This defines which bot is allowed to trade which ticker
BOT_MAPPING = {
    "survivor_bot": ["TQQQ", "SQQQ", "SOXL", "SOXS", "FNGU", "UPRO", "SPXL", "SPXS"],
    "wheel_bot": ["DIS", "F", "PLTR"], 
    "condor_bot": ["COIN", "MSTR", "TSLA", "NVDA", "NFLX"],
    "crypto_grid": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "moon_bag": ["BTC/USD", "ETH/USD"]
}

def get_bot_owner(symbol, asset_class):
    """Determines which bot owns a specific position."""
    # 1. Crypto Rules
    if asset_class == AssetClass.CRYPTO:
        return "crypto_grid" # Default owner, Moon Bag shares this space
    
    # 2. Options Rules
    if asset_class == AssetClass.US_OPTION:
        # Check if the root symbol belongs to Wheel
        for ticker in BOT_MAPPING["wheel_bot"]:
            if symbol.startswith(ticker): return "wheel_bot"
        return "condor_bot" # All other options go to Condor

    # 3. Stock Rules
    if symbol in BOT_MAPPING["survivor_bot"]: return "survivor_bot"
    if symbol in BOT_MAPPING["wheel_bot"]: return "wheel_bot"
    
    # 4. Default Aggressive
    return "trend_bot"

def check_budget(bot_name, trading_client):
    """
    Returns True if the bot is under its allocated budget.
    """
    try:
        # 1. Load Config
        with open("bot_config.json", "r") as f:
            config = json.load(f)
        
        # 2. Get Limits
        bot_settings = config["bots"].get(bot_name, {})
        allocation_pct = bot_settings.get("allocation", 0.0)
        
        if allocation_pct == 0.0:
            return True # No limit set, allow trade (or False to be strict)

        # 3. Calculate Equity Share
        account = trading_client.get_account()
        equity = float(account.equity)
        budget_dollars = equity * allocation_pct
        
        # 4. Calculate Current Usage
        positions = trading_client.get_all_positions()
        current_used = 0.0
        
        for p in positions:
            owner = get_bot_owner(p.symbol, p.asset_class)
            
            # Special Case: Crypto Grid and Moon Bag share assets
            if bot_name in ["crypto_grid", "moon_bag"] and owner == "crypto_grid":
                current_used += float(p.market_value)
            elif owner == bot_name:
                current_used += float(p.market_value)

        available = budget_dollars - current_used
        logger.info(f"  [CFO] {bot_name}: Used ${current_used:.0f} / ${budget_dollars:.0f} (Left: ${available:.0f})")
        
        return available > 0

    except Exception as e:
        logger.error(f"  [CFO] Budget Check Error: {e}")
        return True # Default to allow if file error
    
    # utils.py - Add this function at the bottom

def get_active_targets(strategy_key):
    """
    Reads active_targets.json and returns the list for the specific strategy.
    strategy_key examples: 'wheel_targets', 'condor_targets', 'trend_targets'
    """
    try:
        with open("active_targets.json", "r") as f:
            data = json.load(f)
            # Return the specific list, or empty if not found
            return data.get(strategy_key, [])
    except Exception as e:
        logger.error(f"  [Utils] Error reading targets for {strategy_key}: {e}")
        return []

def get_targets_with_freshness_check(file_path, strategy_key, static_fallback):
    import os
    import time
    
    # 1. Resolve Path (Linux-friendly search)
    search_paths = [
        file_path,  # As provided (CWD)
        os.path.join(os.path.dirname(__file__), file_path),  # Same dir as utils.py
        os.path.join(os.path.expanduser("~"), "bots", "repo", file_path),  # Absolute beelink path
        os.path.join("..", file_path),  # Parent directory
    ]
    
    final_path = None
    for p in search_paths:
        if os.path.exists(p):
            final_path = p
            logger.info(f"  [Utils] Found targets at: {p}")
            break
            
    if not final_path:
        logger.warning(f"  [Warning] Target file {file_path} NOT FOUND in search paths. Using Static Fallback.")
        return static_fallback
    
    # 2. Check Freshness (24h = 86400s)
    file_age = time.time() - os.path.getmtime(final_path)

    if file_age > 86400:
        logger.warning(f"  [Warning] Targets are stale ({file_age/3600:.1f} hours old). Using Static Fallback.")
        return static_fallback
        
    # 3. Load Data
    try:
        with open(final_path, 'r') as f:
            data = json.load(f)
            
            # [PHASE 2.5] Check for Success Status
            scan_status = data.get("status", "unknown")
            targets = data.get(strategy_key, [])
            
            if not targets:
                # Case A: Scan ended successfully, but found nothing.
                if scan_status == "success":
                    logger.info(f"  [Info] {strategy_key} empty, but scan SUCCESS. Standby Mode (No Fallback).")
                    return []
                
                # Case B: Old format or missing status -> Assume failure/stale -> Use Fallback
                logger.warning(f"  [Warning] {strategy_key} empty (Status: {scan_status}). Using Static Fallback.")
                return static_fallback
                
            return targets
    except Exception as e:
        logger.error(f"  [Error] Failed to read target file: {e}. Using Static Fallback.")
        return static_fallback

def parse_target(item):
    """
    Normalizes a target item (String or Dict) into a tuple.
    Returns: (symbol, confidence_score)
    Default Confidence for legacy strings = 0.5
    """
    if isinstance(item, dict):
        return item.get("symbol"), item.get("confidence", 0.5)
    elif isinstance(item, str):
        return item, 0.5
    else:
        return None, 0.0