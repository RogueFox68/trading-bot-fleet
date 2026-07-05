import utils
import config
from logger import get_logger, registry
import time
import json
import tiered_hold
import os
import datetime
import requests
import pandas as pd
import ta
from ta.momentum import RSIIndicator
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json" 

# Indicators
RSI_BUY = 38
RSI_SELL = 70
RISK_PER_TRADE = 0.05
FAILED_SYMBOL_COOLDOWN = 3600  # seconds to skip a symbol after an order error (1h)

# --- LOGGING ---
logger = get_logger("survivor_bot")

# --- CREDENTIALS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('America/New_York')

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_SURVIVOR: return 
    try: requests.post(config.WEBHOOK_SURVIVOR, json={"content": msg})
    except Exception as e:
        registry.log_error("survivor_bot", "send_discord", e, context=msg[:50])
        logger.error(f"Discord webhook failed: {e}")

def get_segregated_targets():
    """
    Returns specific survivor targets and a BLACKLIST of other bots' targets.
    This prevents Survivor from selling Trend Bot's positions.
    """
    # Helper to extract symbols
    def extract_symbols(raw_dict):
        return list(raw_dict.keys()) if isinstance(raw_dict, dict) else []

    # 1. Survivor Targets (Fallback: Empty)
    survivor_targets = utils.load_and_validate_targets(TARGET_FILE, "survivor_targets", {})

    # 2. Blacklist (Trend + Wheel). Fallbacks are empty: when targets are
    #    stale/missing no bot is entering anything, so there is nothing of
    #    theirs to protect.
    blacklist = []

    trend_raw = utils.load_and_validate_targets(TARGET_FILE, "trend_targets", {})
    blacklist += extract_symbols(trend_raw)

    wheel_raw = utils.load_and_validate_targets(TARGET_FILE, "wheel_targets", {})
    blacklist += extract_symbols(wheel_raw)

    return survivor_targets, set(blacklist)

def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute), 
            start=start_time,
            limit=200
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('America/New_York')
        return df
    except Exception as e:
        registry.log_error("survivor_bot", "get_data_alpaca", e, context=symbol)
        return None

# Daily SMA200 (long-term trend filter) is computed on DAILY bars and cached
# per symbol — it barely moves intraday, so we refresh every few hours.
_daily_sma_cache = {}  # symbol -> (epoch, sma_value)
_DAILY_SMA_TTL = 6 * 3600

def get_trend_sma(symbol, window=200):
    """Latest `window`-period SMA on DAILY closes (long-term trend), cached."""
    now = time.time()
    cached = _daily_sma_cache.get(symbol)
    if cached and (now - cached[0]) < _DAILY_SMA_TTL:
        return cached[1]
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=window * 2 + 60)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_time,
            limit=window + 50
        )
        bars = data_client.get_stock_bars(req)
        if not bars.data or symbol not in bars.data:
            return None
        df = bars.df.xs(symbol)
        if len(df) < window:
            return None
        sma = float(df['close'].tail(window).mean())
        _daily_sma_cache[symbol] = (now, sma)
        return sma
    except Exception as e:
        registry.log_error("survivor_bot", "get_trend_sma", e, context=symbol)
        return None

def run_survivor_bot():
    logger.info(f"--- 🛡️ SURVIVOR BOT (Segregated) STARTED ---")
    send_discord("**Survivor Bot V3.1** Online\nSegregation Protocol Active.")
    
    # Phase 10: Failed order cooldown (symbol -> last-failure epoch)
    _failed_symbols = {}
    
    while True:
        try:
            # 1. Market Check
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping 15m...")
                    time.sleep(900) # Sleep 15 mins if closed
                    continue
            except Exception as e:
                registry.log_error("survivor_bot", "get_clock", e)

            # 2. Build Watchlist & Blacklist
            raw_scout_targets, blacklist = get_segregated_targets()
            
            # --- PARSE & FILTER (Phase 2) ---
            target_map = {} # symbol -> confidence
            clean_scout_targets = []
            
            for sym, data in raw_scout_targets.items():
                conf = data.get("confidence", 0.5)
                if sym and sym not in blacklist:
                    clean_scout_targets.append(sym)
                    target_map[sym] = conf
            
            # Use Scout targets directly
            raw_list = clean_scout_targets
            clean_watchlist = [t for t in raw_list if t not in blacklist]
            
            account = trading_client.get_account()
            equity = float(account.portfolio_value)
            positions = trading_client.get_all_positions()
            pos_dict = {p.symbol: p for p in positions}
            # Prime full-coverage ownership + entry times for held equities so an
            # aged position (opening order buried behind a crypto-order flood)
            # still resolves its owner and hold duration this cycle.
            equity_syms = [p.symbol for p in positions if p.asset_class == AssetClass.US_EQUITY]
            utils.prime_ownership(trading_client, equity_syms)
            entry_times = utils.get_position_entry_times(trading_client, held_symbols=equity_syms)

            # Phase 10: Get Pending Orders to avoid buy churn loops
            open_orders = trading_client.get_orders(filter=GetOrdersRequest(status="open"))
            pending_symbols = {o.symbol for o in open_orders}

            # Global Risk Map
            try:
                with open('bot_config.json', 'r') as f:
                    config_data = json.load(f)
                    market_weather = config_data.get('global_settings', {})
                    current_regime = market_weather.get('market_condition', 'SIDEWAYS')
                    current_vix = market_weather.get('vix', 15.0)
            except Exception as e:
                registry.log_error("survivor_bot", "read_config", e)
                logger.error(f"[!] Config load error: {e}")
                current_regime = "SIDEWAYS"
                current_vix = 15.0

            # EOD Policy Time Check
            now_et = datetime.datetime.now(pytz.timezone('America/New_York'))
            time_str = now_et.strftime('%H:%M')
            is_eod_eval = time_str >= "15:30" and time_str < "15:45"
            is_eod_close = time_str >= "15:45"
            is_eod_skip_entry = time_str >= "14:00"

            # Add all survivor-owned positions to the scan list
            owned_symbols = [p.symbol for p in positions
                if utils.get_bot_owner(p.symbol, p.asset_class, trading_client) == "survivor_bot"]
            full_scan_list = list(set(clean_watchlist + owned_symbols))

            logger.info(f"Scanning {len(full_scan_list)} Targets (Watchlist + Owned, Ignored {len(blacklist)} Blacklist) | ET: {time_str}")

            # Batch budget check outside the symbol loop to prevent log spam
            is_budget_ok = utils.check_budget("survivor_bot", trading_client)

            for symbol in full_scan_list:
                # Phase 10: Skip recently-failed symbols (cooldown) and cryptos
                if symbol in _failed_symbols:
                    if (time.time() - _failed_symbols[symbol]) < FAILED_SYMBOL_COOLDOWN:
                        continue
                    del _failed_symbols[symbol]  # cooldown elapsed, allow a retry
                if symbol in ["BTC/USD", "ETH/USD"]: continue

                df = get_data_alpaca(symbol)
                if df is None: continue

                # Indicators (RSI on 15m bars; SMA200 trend filter on DAILY bars)
                df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()

                latest = df.iloc[-1]
                price = float(latest['close'])
                rsi = float(latest['rsi'])
                sma = get_trend_sma(symbol, 200) or 0

                # --- DIAGNOSTICS ---
                if symbol not in pos_dict:
                    is_scout = symbol in target_map
                    sma_status = "above" if price > sma else "BELOW"
                    gate_available = "Scout" if is_scout else "Uptrend only"
                    
                    if rsi < RSI_BUY:
                        logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available} | 🎯 ENTRY ZONE")
                    elif rsi < RSI_BUY + 5:
                        logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available} | ⏳ Near threshold")
                    else:
                        logger.debug(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available}")

                # --- EXIT LOGIC ---
                # Only manage if we are ALLOWED to trade this symbol
                if symbol in pos_dict:
                    # Prevent duplicate sell orders
                    if symbol in pending_symbols:
                        logger.debug(f"  [SKIP] {symbol} already has a pending order.")
                        continue
                        
                    pos = pos_dict[symbol]
                    qty = float(pos.qty)
                    sell_qty = int(abs(qty))
                    
                    # FETCH LIVE PRICE FOR P&L
                    try:
                        trade_req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
                        live_price = float(data_client.get_stock_latest_trade(trade_req)[symbol].price)
                    except Exception as e:
                        registry.log_error("survivor_bot", "fetch_live_price", e, context=symbol)
                        live_price = price # fallback to bar close if API fails
                    
                    entry_price = float(pos.avg_entry_price)
                    entry_dt = entry_times.get(symbol)
                    hours_held = ((datetime.datetime.now(datetime.timezone.utc) - entry_dt).total_seconds() / 3600.0) if entry_dt else None
                    pct_gain = (live_price - entry_price) / entry_price
                    should_sell = False
                    reason = ""

                    # --- MAX-HOLD BACKSTOP (orphan protection) ---
                    # Hard time-based exit so no position can bleed indefinitely.
                    # Score the position to pick its hold tier and close it now if
                    # it's past that tier's max_hold_days. Relies on a resolvable
                    # entry time, which the paged order lookup restores for aged
                    # positions.
                    if hours_held is not None:
                        mh_score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price, {"rsi": float(rsi)}, current_regime, current_vix, hours_held=hours_held)
                        mh_tier = tiered_hold.get_hold_tier(mh_score, "survivor_bot")
                        max_days = tiered_hold.max_hold_days_for_tier(mh_tier)
                        if max_days is not None and (hours_held / 24.0) >= max_days:
                            try:
                                logger.info(f"    ⏳ MAX HOLD EXIT {symbol} — held {hours_held/24:.1f}d ≥ {max_days}d cap (tier {mh_tier})")
                                if qty != sell_qty:
                                    mh_tif, mh_qty = TimeInForce.DAY, qty
                                else:
                                    mh_tif, mh_qty = TimeInForce.GTC, sell_qty
                                utils.submit_and_log_order(
                                    trading_client,
                                    order_data=MarketOrderRequest(symbol=symbol, qty=mh_qty, side=OrderSide.SELL, time_in_force=mh_tif, client_order_id=f"survivor_bot-{symbol}-{int(time.time())}"),
                                    logger=logger,
                                    reason=f"Max Hold Exceeded [held {hours_held/24:.1f}d]"
                                )
                                send_discord(f"⏳ **MAX HOLD CLOSE {symbol}**\nHeld {hours_held/24:.1f}d (cap {max_days}d)\nP&L: {pct_gain*100:.2f}%")
                            except Exception as e:
                                logger.error(f"    [!] Max Hold Exit Error {symbol}: {e}")
                                _failed_symbols[symbol] = time.time()
                            continue
                    
                    is_held_overnight = False
                    is_eod_window = time_str >= "15:30"
                    
                    if is_eod_window:
                        indicators = {"rsi": float(rsi)}
                        score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price, indicators, current_regime, current_vix, hours_held=hours_held)
                        tier = tiered_hold.get_hold_tier(score, "survivor_bot")
                        
                        if tier != "CLOSE_EOD":
                            is_held_overnight = True
                            if is_eod_eval:
                                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol}. Tier: {tier} (Score: {score})")
                                continue
                                
                    if is_eod_close:
                        if is_held_overnight:
                            logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} (15:45+ ET).")
                        else:
                            should_sell = True
                            reason = "EOD Liquidation (15:45+ ET)"
                    elif rsi > RSI_SELL:
                        should_sell = True
                        reason = f"RSI Overbought ({rsi:.0f})"
                    elif pct_gain > 0.05:
                        should_sell = True
                        reason = "Take Profit (+5%)"
                    elif pct_gain < -0.03:
                        should_sell = True
                        reason = "Stop Loss (-3%)"
                        
                    if should_sell:
                        if hours_held is not None:
                            reason = f"{reason} [held {hours_held:.1f}h]"
                        try:
                            logger.info(f"    📉 SELLING {symbol}: {reason}")
                            
                            # Fractional shares require DAY orders, whole shares can be GTC
                            if qty != sell_qty: 
                                order_tif = TimeInForce.DAY
                                order_qty = qty # sell exact fractional amount
                            else:
                                order_tif = TimeInForce.GTC
                                order_qty = sell_qty
                                
                            utils.submit_and_log_order(
                                trading_client,
                                order_data=MarketOrderRequest(
                                    symbol=symbol, 
                                    qty=order_qty, 
                                    side=OrderSide.SELL, 
                                    time_in_force=order_tif,
                                    client_order_id=f"survivor_bot-{symbol}-{int(time.time())}"
                                ),
                                logger=logger,
                                reason=reason
                            )
                            send_discord(f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                        except Exception as e:
                            logger.error(f"    [!] Sell Error {symbol}: {e}")
                            _failed_symbols[symbol] = time.time()
 
                # --- ENTRY LOGIC ---
                # Phase 10: Also check pending orders to avoid buy/buy/buy churn loops
                elif symbol not in pos_dict and symbol not in pending_symbols:
                    
                    # EOD Policy: Skip new entries after 14:00 ET
                    if is_eod_skip_entry:
                        continue
                    
                    # [FIX] CFO Check specifically for new entries
                    if not is_budget_ok:
                        continue
                        
                    # Phase 10: Minimum price filter — avoid penny stock sizing bombs
                    MIN_PRICE = 5.00
                    if price < MIN_PRICE:
                        logger.info(f"    [SKIP] {symbol} | Price ${price:.2f} < ${MIN_PRICE:.2f} minimum")
                        continue
                        
                    if rsi < RSI_BUY:
                        is_uptrend = price > sma
                        is_scout_approved = symbol in target_map  # Scout already vetted this
                        
                        # Only buy if uptrend OR Scout Approved
                        if is_uptrend or is_scout_approved:
                            # --- DYNAMIC SIZING (Phase 2) ---
                            confidence = target_map.get(symbol, 0.5)
                            scaler = 0.5 + confidence
                            scaled_risk = RISK_PER_TRADE * scaler
                            
                            # Log the gate that triggered
                            gate = "Scout" if is_scout_approved else "Uptrend"
                            logger.info(f"    💎 DIP DETECTED: {symbol} (RSI {rsi:.0f}, Conf {confidence:.2f}, Gate: {gate})")
                            
                            risk_amt = equity * scaled_risk
                            
                            # Cap by available CFO budget
                            available_budget = utils.get_available_budget("survivor_bot", trading_client)
                            risk_amt = min(risk_amt, available_budget)
                            
                            qty = int(risk_amt / price)
                            
                            # Phase 10: Max position value cap (10% of equity max)
                            max_position_value = equity * 0.10
                            max_qty = int(max_position_value / price)
                            qty = min(qty, max_qty)
                            
                            # Check buying power before ordering
                            order_cost = float(qty) * float(price)
                            buying_power = float(account.regt_buying_power)
                            if order_cost > buying_power:
                                logger.info(f"       [SKIP] {symbol} | Cost ${order_cost:.0f} > RegT Buying Power ${buying_power:.0f}")
                                continue
                            
                            if qty > 0:
                                logger.info(f"       -> Buying {qty} shares (Size: {scaler:.1f}x, Cost: ${order_cost:.0f})...")
                                try:
                                    utils.submit_and_log_order(
                                        trading_client,
                                        order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=f"survivor_bot-{symbol}-{int(time.time())}"),
                                        logger=logger,
                                        reason="Bought Dip"
                                    )
                                    send_discord(f"💎 **BOUGHT DIP {symbol}**\nRSI: {rsi:.0f}\nConfidence: {confidence:.2f}\nGate: {gate}")
                                except Exception as e:
                                    logger.error(f"       [!] Order Error: {e}")
                                    _failed_symbols[symbol] = time.time()
                        else:
                            logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} < {RSI_BUY} but NOT (Uptrend | Core | Scout). SMA: {sma:.2f}")
                    else:
                        logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} >= {RSI_BUY}")

            time.sleep(60)

        except Exception as e:
            registry.log_error("survivor_bot", "main_loop", e)
            logger.error(f"Survivor Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()