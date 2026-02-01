import json
from alpaca.trading.enums import AssetClass

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
        print(f"  [CFO] {bot_name}: Used ${current_used:.0f} / ${budget_dollars:.0f} (Left: ${available:.0f})")
        
        return available > 0

    except Exception as e:
        print(f"  [CFO] Budget Check Error: {e}")
        return True # Default to allow if file error