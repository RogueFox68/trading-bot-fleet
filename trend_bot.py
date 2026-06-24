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
import tiered_hold
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"

# --- LOGGING ---
from logger import get_logger, registry
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
TIMEZONE = pytz.timezone('America/New_York')

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_TREND: return
    try: requests.post(config.WEBHOOK_TREND, json={"content": msg})
    except Exception as e:
        registry.log_error("trend_bot", "send_discord", e, context=msg[:50])
        logger.error(f"Discord webhook failed: {e}")

def log_to_influx(symbol, action, price, qty, reason=""):
    try:
        reason_field = f',reason="{reason}"' if reason else ""
        data_str = f'trades,symbol={symbol} price={price},action="{action}",qty={qty}{reason_field} {time.time_ns()}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=data_str, timeout=2)
        if r.status_code != 204:
            logger.warning(f"InfluxDB write failed: {r.status_code} {r.text}")
    except Exception as e:
        logger.warning(f"InfluxDB write error: {e}")

def get_market_regime():
    if not os.path.exists(CONFIG_FILE): return "UNKNOWN"
    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
            return data.get("global_settings", {}).get("market_condition", "UNKNOWN")
    except Exception as e:
        registry.log_error("trend_bot", "get_market_regime", e)
        return "UNKNOWN"

def get_dynamic_targets(regime):
    # 1. BEAR MODE: Short the market
    long_fallback = {}
    short_fallback = {}

    longs = utils.load_and_validate_targets(TARGET_FILE, "trend_targets", long_fallback)
    shorts = utils.load_and_validate_targets(TARGET_FILE, "short_targets", short_fallback)
    
    return longs, shorts


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
        df.index = df.index.tz_convert('America/New_York')
        return df
    except Exception as e:
        registry.log_error("trend_bot", "get_data_alpaca", e, context=symbol)
        return None

def run_trend_bot():
    logger.info(f"--- 昌 TREND SNIPER (Target Locked) STARTED ---")
    send_discord("**Trend Sniper V4.1** Online\nOwnership Logic Active.")
    
    # Phase 10: Failed order cooldown
    _failed_symbols = set()
    
    while True:
        try:
            # 1. Check Market Hours
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping...")
                    time.sleep(900)
                    continue
            except Exception as e:
                registry.log_error("trend_bot", "get_clock", e)

            # 2. Market Regime Check
            global_regime = get_market_regime()
            long_targets, short_targets = get_dynamic_targets(global_regime)
            
            # --- PARSE TARGETS (Phase 2) ---
            target_map_long = {} # symbol -> confidence
            target_map_short = {}
            clean_targets_long = []
            clean_targets_short = []
            
            for sym, data in long_targets.items():
                conf = data.get("confidence", 0.5)
                clean_targets_long.append(sym)
                target_map_long[sym] = conf
            
            for sym, data in short_targets.items():
                conf = data.get("confidence", 0.5)
                clean_targets_short.append(sym)
                target_map_short[sym] = conf
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}

            # Phase 10: Get Pending Orders to avoid buy churn loops
            open_orders = trading_client.get_orders(filter=GetOrdersRequest(status="open"))
            pending_symbols = {o.symbol for o in open_orders}

            # Calculate short exposure for safety cap
            short_exposure = sum(
                abs(float(p.market_value))
                for p in positions
                if p.side == "short" and utils.get_bot_owner(p.symbol, p.asset_class, trading_client) == "trend_bot"
            )

            my_holdings = [p.symbol for p in positions if p.asset_class == AssetClass.US_EQUITY and utils.get_bot_owner(p.symbol, p.asset_class, trading_client) == "trend_bot"]

            logger.info(f"Regime: {global_regime} | Long Targets: {len(clean_targets_long)} | Short Targets: {len(clean_targets_short)}")

            if not (clean_targets_long or clean_targets_short) and not my_holdings:
                 logger.info("    💤 Standby Mode: No targets or holdings. Sleeping...")
                 time.sleep(60)
                 continue

            # Global Risk Map
            try:
                with open('bot_config.json', 'r') as f:
                    config_data = json.load(f)
                    market_weather = config_data.get('global_settings', {})
                    current_regime = market_weather.get('market_condition', 'SIDEWAYS')
                    current_vix = market_weather.get('vix', 15.0)
            except Exception as e:
                registry.log_error("trend_bot", "read_config", e)
                logger.error(f"[!] Config load error: {e}")
                current_regime = "SIDEWAYS"
                current_vix = 15.0

            # EOD Policy Time Check
            now_et = datetime.datetime.now(pytz.timezone('America/New_York'))
            time_str = now_et.strftime('%H:%M')
            is_eod_eval = time_str >= "15:30" and time_str < "15:45"
            is_eod_close = time_str >= "15:45"
            is_eod_skip_entry = time_str >= "14:00"

            # 3. Only scan OUR targets + OUR existing positions
            # We filter existing positions to only those relevant to Trend Bot strategies
            scan_list = list(set(clean_targets_long + clean_targets_short + my_holdings))

            # Batch budget check outside the symbol loop to prevent log spam
            is_budget_ok = utils.check_budget("trend_bot", trading_client)

            for symbol in scan_list:
                # Phase 10: Skip failed symbols and cryptos
                if symbol in _failed_symbols: continue
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

                # --- DIAGNOSTICS ---
                ema_gap = ((latest['ema_fast'] - latest['ema_slow']) / latest['ema_slow']) * 100
                if symbol not in pos_dict and len(df) >= MOMENTUM_BARS + 1:
                    if bull_cross:
                        logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | ADX {local_adx:>5.1f} | EMA Gap {ema_gap:>+5.1f}% | ⚡ CROSSOVER")
                    elif momentum_ok:
                        logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | ADX {local_adx:>5.1f} | EMA Gap {ema_gap:>+5.1f}% | Pullback {pullback*100:.1f}% | 🎯 MOMENTUM")
                    elif ema_aligned and local_adx > MOMENTUM_ADX_MIN:
                        logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | ADX {local_adx:>5.1f} | EMA Gap {ema_gap:>+5.1f}% | Pullback {pullback*100:.1f}% | ⏳ Waiting (pullback > {MOMENTUM_PULLBACK_PCT*100}%)")
                    elif ema_aligned:
                        logger.debug(f"  📊 {symbol:<5} ${price:>8.2f} | ADX {local_adx:>5.1f} | EMA Gap {ema_gap:>+5.1f}% | Pullback {pullback*100:.1f}% | Weak ADX")
                    else:
                        logger.debug(f"  📊 {symbol:<5} ${price:>8.2f} | ADX {local_adx:>5.1f} | EMA Gap {ema_gap:>+5.1f}% | No alignment")

                # --- EXECUTION ---
                
                # A) EXIT LOGIC (Manage existing trades)
                if symbol in pos_dict:
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    sell_qty = int(abs(qty))
                    side = pos.side
                    entry_price = float(pos.avg_entry_price)
                    
                    if sell_qty <= 0:
                        logger.info(f"    [SKIP] {symbol} fractional position ({qty} shares), cannot close")
                        continue

                    # Exit logic depends on side
                    if side == "long":
                        pnl_pct = (price - entry_price) / entry_price
                        
                        # Stop Loss / Take profit / EOD overrides
                        is_held_overnight = False
                        is_eod_window = time_str >= "15:30"
                        if is_eod_window:
                            indicators = {"adx": float(local_adx), "ema_trend_intact": bool(latest['ema_fast'] > latest['ema_slow'])}
                            score = tiered_hold.calculate_hold_score("trend_bot", price, entry_price, indicators, current_regime, current_vix, hours_held=24.0)
                            tier = tiered_hold.get_hold_tier(score, "trend_bot")
                            
                            if tier != "CLOSE_EOD":
                                is_held_overnight = True
                                if is_eod_eval:
                                    logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} LONG. Tier: {tier} (Score: {score})")
                                    continue
                                    
                        if is_eod_close:
                            if is_held_overnight:
                                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} LONG (15:45+ ET).")
                            else:
                                try:
                                    logger.info(f"    📉 EOD LIQUIDATION LONG {symbol} (15:45+ ET)")
                                    req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                    utils.submit_and_log_order(trading_client, req, logger)
                                    send_discord(f"📉 **EOD CLOSE LONG {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                    log_to_influx(symbol, "sell", price, sell_qty, reason="EOD Liquidation")
                                except Exception as e:
                                    logger.error(f"    [!] EOD Close Error {symbol}: {e}")
                                    _failed_symbols.add(symbol)
                        elif pnl_pct <= -0.05:
                            try:
                                logger.info(f"    🛑 STOP LOSS LONG {symbol} ({pnl_pct:.1%})")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"🛑 **STOP LOSS {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "sell", price, sell_qty, reason="Stop Loss")
                            except Exception as e:
                                logger.error(f"    [!] Stop Loss Error {symbol}: {e}")
                                _failed_symbols.add(symbol)
                        elif pnl_pct >= 0.08:
                            try:
                                logger.info(f"    💰 TAKE PROFIT LONG {symbol} ({pnl_pct:.1%})")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"💰 **TAKE PROFIT {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "sell", price, sell_qty, reason="Take Profit")
                            except Exception as e:
                                logger.error(f"    [!] Take Profit Error {symbol}: {e}")
                                _failed_symbols.add(symbol)
                        elif bull_cross == False and bear_cross == True:
                            try:
                                logger.info(f"    📉 CLOSE LONG {symbol} (Crossover)")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"📉 **SELL/CLOSE {symbol}** (Cross)\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "sell", price, sell_qty, reason="Bearish Crossover")
                            except Exception as e:
                                logger.error(f"    [!] Close Long Error {symbol}: {e}")
                                _failed_symbols.add(symbol)
                                
                    elif side == "short":
                        pnl_pct = (entry_price - price) / entry_price # Inverted PnL
                        
                        # Stop Loss / Take profit / EOD overrides
                        is_held_overnight = False
                        is_eod_window = time_str >= "15:30"
                        if is_eod_window:
                            indicators = {"adx": float(local_adx), "ema_trend_intact": bool(latest['ema_fast'] < latest['ema_slow'])}
                            score = tiered_hold.calculate_hold_score("trend_short", price, entry_price, indicators, current_regime, current_vix, hours_held=24.0)
                            tier = tiered_hold.get_hold_tier(score, "trend_short")
                            
                            if tier != "CLOSE_EOD":
                                is_held_overnight = True
                                if is_eod_eval:
                                    logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} SHORT. Tier: {tier} (Score: {score})")
                                    continue
                                    
                        if is_eod_close:
                            if is_held_overnight:
                                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} SHORT (15:45+ ET).")
                            else:
                                try:
                                    logger.info(f"    📉 EOD LIQUIDATION SHORT {symbol} (15:45+ ET)")
                                    req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                    utils.submit_and_log_order(trading_client, req, logger)
                                    send_discord(f"📉 **EOD CLOSE SHORT {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                    log_to_influx(symbol, "buy", price, sell_qty, reason="EOD Liquidation")
                                except Exception as e:
                                    logger.error(f"    [!] EOD Close Short Error {symbol}: {e}")
                                    _failed_symbols.add(symbol)
                        elif pnl_pct <= -0.05:
                            try:
                                logger.info(f"    🛑 STOP LOSS SHORT {symbol} ({pnl_pct:.1%})")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"🛑 **STOP LOSS SHORT {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "buy", price, sell_qty, reason="Stop Loss")
                            except Exception as e:
                                logger.error(f"    [!] Stop Loss Short Error {symbol}: {e}")
                                _failed_symbols.add(symbol)
                        elif pnl_pct >= 0.08:
                            try:
                                logger.info(f"    💰 TAKE PROFIT SHORT {symbol} ({pnl_pct:.1%})")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"💰 **TAKE PROFIT SHORT {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "buy", price, sell_qty, reason="Take Profit")
                            except Exception as e:
                                logger.error(f"    [!] Take Profit Short Error {symbol}: {e}")
                                _failed_symbols.add(symbol)
                        # Exit short if we get a bull cross (trend reversal back up)
                        elif bear_cross == False and bull_cross == True:
                            try:
                                logger.info(f"    📉 CLOSE SHORT {symbol} (Crossover)")
                                req = MarketOrderRequest(symbol=symbol, qty=sell_qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                utils.submit_and_log_order(trading_client, req, logger)
                                send_discord(f"📉 **BUY TO COVER {symbol}** (Bull Cross)\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
                                log_to_influx(symbol, "buy", price, sell_qty, reason="Bullish Crossover")
                            except Exception as e:
                                logger.error(f"    [!] Close Short Error {symbol}: {e}")
                                _failed_symbols.add(symbol)

                # B) LONG ENTRY LOGIC
                # Phase 10: Also check pending orders to avoid churn loops
                elif symbol in clean_targets_long and symbol not in pending_symbols:
                    
                    if is_eod_skip_entry:
                        continue
                    
                    # [FIX] CFO Check specifically for new entries
                    if not is_budget_ok:
                        continue
                    
                    # Phase 10: Minimum price filter
                    MIN_PRICE = 5.00
                    if price < MIN_PRICE:
                        logger.info(f"    [SKIP] {symbol} | Price ${price:.2f} < ${MIN_PRICE:.2f} minimum")
                        continue
                        
                    # We only trade if ADX > 20 (Trend is strong)
                    if local_adx > 20:
                        should_buy = False
                        entry_type = ""
                        size_mult = 1.0
                        
                        # Regime-based sizing for longs
                        if global_regime == "BEAR_TREND":
                            size_mult = 0.5
                            
                        # Entry Path 1: Fresh Crossover (original)
                        if bull_cross:
                            should_buy = True
                            entry_type = "Crossover"
                        
                        # Entry Path 2: Momentum Confirmation (new)
                        # Only fires if crossover didn't — no double entries
                        elif momentum_ok:
                            should_buy = True
                            entry_type = "Momentum"
                            size_mult = float(size_mult) * MOMENTUM_SIZE_MULT

                        if should_buy:
                            # --- DYNAMIC SIZING (Phase 2) ---
                            confidence = target_map_long.get(symbol, 0.5)
                            scaler = 0.5 + confidence
                            scaled_risk = RISK_PER_TRADE * scaler * float(size_mult)
                            
                            risk_amt = equity * scaled_risk
                            
                            # Cap by available CFO budget
                            available_budget = utils.get_available_budget("trend_bot", trading_client)
                            risk_amt = min(risk_amt, available_budget)
                            
                            # Stop loss approx 2% away
                            qty = int(risk_amt / (price * 0.02))
                            
                            # Max position cap (Phase 8 Bugfix)
                            max_position_value = equity * 0.15  # Never more than 15% of portfolio per trade
                            max_qty = int(max_position_value / price)
                            qty = min(qty, max_qty)
                            
                            # Check buying power before ordering
                            order_cost = float(qty) * float(price)
                            buying_power = float(account.regt_buying_power)
                            if order_cost > buying_power:
                                logger.info(f"    [SKIP] {symbol} | Cost ${order_cost:.0f} > RegT Buying Power ${buying_power:.0f}")
                                continue
                                
                            if qty > 0:
                                logger.info(f"    🔼 BUY SIGNAL (LONG) {symbol} ({entry_type} | Conf: {confidence:.2f}, Size: {scaler * float(size_mult):.1f}x, Cost: ${order_cost:.0f})")
                                try:
                                    req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                    utils.submit_and_log_order(trading_client, req, logger)
                                    send_discord(f"🔼 **LONG {symbol}** ({entry_type})\nRegime: {global_regime}\nADX: {local_adx:.0f}\nConfidence: {confidence:.2f}")
                                    log_to_influx(symbol, "buy", price, qty, reason=f"Entry ({entry_type})")
                                except Exception as e:
                                    logger.error(f"    [!] Order Error: {e}")
                                    _failed_symbols.add(symbol)
                        else:
                            logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} > 20 but no Bullish Crossover or Momentum trigger.")
                    else:
                        logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} <= 20 (Trend too weak)")

                # C) SHORT ENTRY LOGIC
                elif symbol in clean_targets_short and symbol not in pending_symbols:
                    
                    if is_eod_skip_entry:
                        continue
                    
                    # [FIX] CFO Check specifically for new entries
                    if not is_budget_ok:
                        continue
                    
                    MIN_PRICE = 10.00 # Higher min price for shorts to avoid extreme volatility
                    if price < MIN_PRICE:
                        logger.info(f"    [SKIP] {symbol} | Price ${price:.2f} < ${MIN_PRICE:.2f} minimum for shorts")
                        continue
                        
                    # Cap short exposure at 30% of portfolio
                    MAX_SHORT_EXPOSURE = 0.30
                    if short_exposure >= equity * MAX_SHORT_EXPOSURE:
                        logger.info(f"    [PAUSE] Max Short Exposure reached (${short_exposure:.0f} >= 30% of equity). Skipping short entry.")
                        continue
                        
                    # We only trade if ADX > 20 (Trend is strong)
                    if local_adx > 20:
                        should_short = False
                        entry_type = ""
                        size_mult = 1.0
                        
                        # Regime-based sizing for shorts
                        if global_regime == "BULL_TREND":
                            size_mult = 0.5
                            
                        # Convert to float to avoid pandas type issues
                        ema_fast_latest = float(latest['ema_fast'])
                        ema_slow_latest = float(latest['ema_slow'])
                        
                        # Short Signals — Momentum Confirmation (mirror of long)
                        momentum_short_ok = False
                        if len(df) >= MOMENTUM_BARS + 1:
                            recent = df.iloc[-(MOMENTUM_BARS + 1):]
                            ema_aligned_bearish = all(
                                float(recent['ema_fast'].iloc[i]) < float(recent['ema_slow'].iloc[i]) 
                                for i in range(len(recent))
                            )
                            # Price must be near the fast EMA (relief bounce / short the rip)
                            ema_fast_val = ema_fast_latest
                            pullback = abs(price - ema_fast_val) / ema_fast_val
                            near_ema = pullback <= MOMENTUM_PULLBACK_PCT
                            
                            momentum_short_ok = ema_aligned_bearish and near_ema and (local_adx > MOMENTUM_ADX_MIN)
                            
                        # Entry Path 1: Fresh Bear Crossover
                        if bear_cross:
                            should_short = True
                            entry_type = "Crossover"
                        
                        # Entry Path 2: Bearish Momentum Confirmation
                        elif momentum_short_ok:
                            should_short = True
                            entry_type = "Momentum"
                            size_mult = float(size_mult) * MOMENTUM_SIZE_MULT

                        if should_short:
                            # --- DYNAMIC SIZING ---
                            confidence = target_map_short.get(symbol, 0.5)
                            scaler = 0.5 + confidence
                            scaled_risk = RISK_PER_TRADE * scaler * float(size_mult)
                            
                            risk_amt = equity * scaled_risk
                            
                            # Cap by available CFO budget
                            available_budget = utils.get_available_budget("trend_bot", trading_client)
                            risk_amt = min(risk_amt, available_budget)
                            
                            # Stop loss approx 2% away
                            qty = int(risk_amt / (price * 0.02))
                            
                            # Max position cap
                            max_position_value = equity * 0.15
                            max_qty = int(max_position_value / price)
                            qty = min(qty, max_qty)
                            
                            # Check buying power before ordering
                            order_cost = float(qty) * float(price)
                            buying_power = float(account.regt_buying_power)
                            if order_cost > buying_power:
                                logger.info(f"    [SKIP] {symbol} | Cost ${order_cost:.0f} > RegT Buying Power ${buying_power:.0f}")
                                continue
                                
                            if qty > 0:
                                logger.info(f"    🩸 BUY SIGNAL (SHORT) {symbol} ({entry_type} | Conf: {confidence:.2f}, Size: {scaler * float(size_mult):.1f}x, Cost: ${order_cost:.0f})")
                                try:
                                    req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, client_order_id=f"trend_bot-{symbol}-{int(time.time())}")
                                    utils.submit_and_log_order(trading_client, req, logger)
                                    send_discord(f"🩸 **SHORT {symbol}** ({entry_type})\nRegime: {global_regime}\nADX: {local_adx:.0f}\nConfidence: {confidence:.2f}")
                                    log_to_influx(symbol, "sell", price, qty, reason=f"Entry ({entry_type})")
                                    
                                    # Add to local short_exposure total so we don't rapid-fire exceed the cap in one loop
                                    short_exposure = float(short_exposure) + (float(qty) * float(price))
                                except Exception as e:
                                    logger.error(f"    [!] Order Error: {e}")
                                    _failed_symbols.add(symbol)
                        else:
                            logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} > 20 but no Bearish Crossover or Momentum trigger.")
                    else:
                         logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} <= 20 (Trend too weak)")

            time.sleep(60)

        except Exception as e:
            registry.log_error("trend_bot", "main_loop", e)
            logger.error(f"Trend Bot Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_trend_bot()