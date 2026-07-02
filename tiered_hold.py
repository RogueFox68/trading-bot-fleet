import datetime
from logger import get_logger

logger = get_logger("tiered_hold")

HOLD_THRESHOLDS = {
    "trend_bot": {
        "HOLD_SWING": 50,       
        "HOLD_OVERNIGHT": 25,   
        "CLOSE_EOD": -999       
    },
    "survivor_bot": {
        "HOLD_SWING": 55,       
        "HOLD_OVERNIGHT": 30,
        "CLOSE_EOD": -999
    }
}

OVERNIGHT_STOPS = {
    "HOLD_OVERNIGHT": {
        "stop_loss_pct": -0.03,      
        "trailing_stop_pct": -0.025,  
        "max_hold_days": 3,           
        "premarket_check": True       
    },
    "HOLD_SWING": {
        "stop_loss_pct": -0.05,       
        "trailing_stop_pct": -0.04,
        "max_hold_days": 7,           
        "premarket_check": True
    }
}

def calculate_hold_score(bot_type, current_price, avg_entry, indicators, regime, vix, entry_confidence=None, hours_held=None):
    """
    Evaluates an open position and returns an integer score determining its hold tier.
    """
    score = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    
    # --- Factor 1: P&L direction (max +30 pts) ---
    if avg_entry > 0:
        pnl_pct = (current_price - avg_entry) / avg_entry
    else:
        pnl_pct = 0.0
        
    if bot_type == "trend_short":
        pnl_pct = -pnl_pct  # Invert for shorts
        bot_type = "trend_bot" # normalize back
    elif bot_type == "trend_long":
        bot_type = "trend_bot"
        
    if pnl_pct > 0.02:    score += 30  # >2% profit
    elif pnl_pct > 0.005: score += 20  # >0.5% profit  
    elif pnl_pct > -0.005:score += 10  # Roughly flat
    elif pnl_pct > -0.02: score += 0   # Small loss
    else:                 score -= 20  # Significant loss
    
    # --- Factor 2: Entry signal still valid (max +25 pts) ---
    if bot_type == "trend_bot":
        adx = indicators.get("adx", 0)
        ema_intact = indicators.get("ema_trend_intact", False)
        
        if adx > 25 and ema_intact:
            score += 25  # Strong trend still running
        elif adx > 20:
            score += 10  # Moderate trend
            
    elif bot_type == "survivor_bot":
        rsi = indicators.get("rsi", 50)
        if rsi < 35 and pnl_pct < 0.03:
            score += 25  # Still in deep dip territory, thesis alive
        elif rsi < 45:
            score += 10  # Recovering but not done
    
    # --- Factor 3: Entry confidence (max +20 pts) ---
    if entry_confidence is not None:
        if entry_confidence >= 0.70: score += 20
        elif entry_confidence >= 0.60: score += 10
    
    # --- Factor 4: Hold duration bonus (max +15 pts) ---
    if hours_held is not None:
        if hours_held > 48: score += 15   # 2+ days held
        elif hours_held > 24: score += 10  # 1+ day held
        elif hours_held > 6: score += 5    # Survived intraday
    
    # --- Factor 5: Market regime penalty (max -30 pts) ---
    if regime == "CRITICAL_VOLATILITY": score -= 30
    elif regime == "BEAR_TREND": score -= 15
    # SIDEWAYS: no penalty
    
    # --- Factor 6: VIX penalty ---
    if vix > 30: score -= 15
    elif vix > 25: score -= 10
    
    return score

def max_hold_days_for_tier(tier):
    """Max days a position may be held for a given hold tier.

    Returns the tier's `max_hold_days` from OVERNIGHT_STOPS, or None for tiers
    with no overnight allowance (CLOSE_EOD is liquidated same day). Used as a
    hard backstop so a position can't be held indefinitely once its entry time
    is known again.
    """
    params = OVERNIGHT_STOPS.get(tier)
    return params["max_hold_days"] if params else None

def get_hold_tier(score, bot_type):
    # Normalize bot_type
    if bot_type.startswith("trend_"): bot_type = "trend_bot"
    
    thresholds = HOLD_THRESHOLDS.get(bot_type, HOLD_THRESHOLDS["trend_bot"])
    if score >= thresholds["HOLD_SWING"]:
        return "HOLD_SWING"
    elif score >= thresholds["HOLD_OVERNIGHT"]:
        return "HOLD_OVERNIGHT"
    return "CLOSE_EOD"

def premarket_check(symbol, last_close_price, data_client):
    """
    Run at 8:00 AM ET before market open to check gap down logic.
    Returns percentage gap.
    """
    try:
        from alpaca.data.requests import StockLatestTradeRequest
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade = data_client.get_stock_latest_trade(req)
        premarket_price = float(trade[symbol].price)
        
        if last_close_price > 0:
            gap_pct = (premarket_price - last_close_price) / last_close_price
            return gap_pct, premarket_price
    except Exception as e:
        logger.error(f"[!] Premarket fetch failed for {symbol}: {e}")
    return 0.0, last_close_price
