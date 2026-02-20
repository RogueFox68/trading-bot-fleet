import config
import time
import json
import os
import datetime
import requests
import pandas as pd
import ta
from ta.trend import EMAIndicator, ADXIndicator
import pytz
import utils
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"

# --- LOGGING ---
from logger import get_logger
logger = get_logger("trend_bot")
FAST_EMA = 9
SLOW_EMA = 21
RISK_PER_TRADE = 0.02

# Momentum Confirmation Settings
MOMENTUM_BARS = 5           # Fast EMA must be above Slow EMA for N consecutive bars
MOMENTUM_ADX_MIN = 25       # Higher bar than crossover (20) - need confirmed trend
MOMENTUM_PULLBACK_PCT = 0.01  # Price must be within 1% of fast EMA (buying the dip)
MOMENTUM_SIZE_MULT = 0.8    # Slightly smaller than crossover (joining late)

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str)
    except: pass

def get_market_regime():
    if not os.path.exists(CONFIG_FILE): return "UNKNOWN"
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            return data.get("global_settings", {}).get("market_condition", "UNKNOWN")
    except: return "UNKNOWN"

def get_dynamic_targets(regime):
    # 1. BEAR MODE: Short the market
    if regime == "BEAR_TREND":
        # Check for specific short targets or default to ETFs
        return ["SQQQ", "SPXU", "UVXY", "SOXS"]

    # 2. BULL/CHOP MODE
    # Default fallback if file is missing/stale
    static_fallback = ["NVDA", "TSLA", "COIN"]
    
    return utils.get_targets_with_freshness_check(TARGET_FILE, "trend_targets", static_fallback)


def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=500
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except: return None

def run_trend_bot():
    logger.info(f"--- 昌 TREND SNIPER (Target Locked) STARTED ---")
    send_discord("**Trend Sniper V4.1** Online\nOwnership Logic Active.")
    
    while True:
        try:
            # 1. Check Market Hours
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping...")
                    time.sleep(900)
                    continue
            except: pass

            # 2. [FIX] CFO Check OUTSIDE loop
            if not utils.check_budget("trend_bot", trading_client):
                 logger.info(f"Trend Budget Paused.")
                 time.sleep(300)
                 continue

            global_regime = get_market_regime()
            raw_targets = get_dynamic_targets(global_regime)
            
            # --- PARSE TARGETS (Phase 2) ---
            target_map = {} # symbol -> confidence
            clean_targets = []
            for item in raw_targets:
                sym, conf = utils.parse_target(item)
                if sym:
                    clean_targets.append(sym)
                    target_map[sym] = conf
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            logger.info(f"Regime: {global_regime} | Targets: {len(clean_targets)}")

            if not clean_targets and not my_holdings:
                 logger.info("    💤 Standby Mode: No targets or holdings. Sleeping...")
                 time.sleep(60)
                 continue

            # 3. Only scan OUR targets + OUR existing positions
            # We filter existing positions to only those relevant to Trend Bot strategies
            
            # NOTE: If we hold it, we manage it. Even if it dropped from targets list, 
            # we should probably still scan it to see if we need to close?
            # Existing logic only included holdings if they WERE in targets. 
            # I will preserve existing logic for safety, but use clean_targets.
            my_holdings = [p.symbol for p in positions if p.asset_class == AssetClass.US_EQUITY and p.symbol in clean_targets]
            scan_list = list(set(clean_targets + my_holdings))

            for symbol in scan_list:
                if symbol in ["BTC/USD", "ETH/USD"]: continue 
                if "/" in symbol: continue 
                
                # Retrieve Data
                df = get_data_alpaca(symbol)
                if df is None: continue

                # --- INDICATORS (Switched to 'ta' lib) ---
                # EMA
                df['ema_fast'] = EMAIndicator(close=df['close'], window=FAST_EMA).ema_indicator()
                df['ema_slow'] = EMAIndicator(close=df['close'], window=SLOW_EMA).ema_indicator()
                
                # ADX
                # adx_indicator.adx() returns the ADX line
                adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
                df['adx'] = adx_indicator.adx()

                latest = df.iloc[-1]
                prev = df.iloc[-2]
                
                price = float(latest['close'])
                local_adx = float(latest['adx'])
                
                # Signals
                # Signals — Crossover (existing)
                bull_cross = (latest['ema_fast'] > latest['ema_slow']) and (prev['ema_fast'] <= prev['ema_slow'])
                bear_cross = (latest['ema_fast'] < latest['ema_slow']) and (prev['ema_fast'] >= prev['ema_slow'])

                # Signals — Momentum Confirmation (new)
                momentum_ok = False
                if len(df) >= MOMENTUM_BARS + 1:
                    recent = df.iloc[-(MOMENTUM_BARS + 1):]
                    ema_aligned = all(
                        recent['ema_fast'].iloc[i] > recent['ema_slow'].iloc[i] 
                        for i in range(len(recent))
                    )
                    # Price must be near the fast EMA (pullback to support)
                    ema_fast_val = float(latest['ema_fast'])
                    pullback = abs(price - ema_fast_val) / ema_fast_val
                    near_ema = pullback <= MOMENTUM_PULLBACK_PCT
                    
                    momentum_ok = ema_aligned and near_ema and (local_adx > MOMENTUM_ADX_MIN)

                # --- EXECUTION ---
                
                # A) EXIT LOGIC (Manage existing trades)
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    
                    # Exit Longs on Bear Cross
                    if bull_cross == False and bear_cross == True:
                        logger.info(f"    悼 CLOSE LONG {symbol}")
                        trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
                        send_discord(f"悼 **SELL {symbol}** (Cross)\nPrice: ${price:.2f}")
                        log_to_influx(symbol, "sell", price, qty)

                # B) ENTRY LOGIC (Open new trades)
                elif symbol in clean_targets:
                    # We only trade if ADX > 20 (Trend is strong)
                    if local_adx > 20:
                        should_buy = False
                        entry_type = ""
                        size_mult = 1.0
                        
                        # Entry Path 1: Fresh Crossover (original)
                        if bull_cross:
                            should_buy = True
                            entry_type = "Crossover"
                            size_mult = 1.0
                        
                        # Entry Path 2: Momentum Confirmation (new)
                        # Only fires if crossover didn't — no double entries
                        elif momentum_ok:
                            should_buy = True
                            entry_type = "Momentum"
                            size_mult = MOMENTUM_SIZE_MULT

                        if should_buy:
                            # --- DYNAMIC SIZING (Phase 2) ---
                            confidence = target_map.get(symbol, 0.5)
                            scaler = 0.5 + confidence
                            scaled_risk = RISK_PER_TRADE * scaler * size_mult
                            
                            risk_amt = equity * scaled_risk
                            # Stop loss approx 2% away
                            qty = int(risk_amt / (price * 0.02))
                            
                            if qty > 0:
                                logger.info(f"    噫 BUY SIGNAL {symbol} ({entry_type} | Conf: {confidence:.2f}, Size: {scaler * size_mult:.1f}x)")
                                try:
                                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
                                    send_discord(f"噫 **BUY {symbol}** ({entry_type})\nRegime: {global_regime}\nADX: {local_adx:.0f}\nConfidence: {confidence:.2f}")
                                    log_to_influx(symbol, "buy", price, qty)
                                except Exception as e:
                                    logger.error(f"    [!] Order Error: {e}")
                        else:
                            logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} > 20 but no Crossover or Momentum trigger.")
                    else:
                        logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} <= 20 (Trend too weak)")

            time.sleep(60)

        except Exception as e:
            logger.error(f"Trend Bot Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()