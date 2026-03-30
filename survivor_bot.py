import utils
import config
from logger import get_logger
import time
import json
import tiered_hold
import os
import datetime
import requests
import pandas as pd
import ta
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
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

# --- LOGGING ---
logger = get_logger("survivor_bot")

# --- CREDENTIALS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
TIMEZONE = pytz.timezone('US/Eastern')

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_SURVIVOR: return 
    try: requests.post(config.WEBHOOK_SURVIVOR, json={"content": msg})
    except: pass

def log_to_influx(symbol, action, price, qty):
    try:
        data_str = f'survivor_trades,symbol={symbol} price={price},action="{action}",qty={qty}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        requests.post(url, data=data_str, timeout=2)
    except: pass

def get_segregated_targets():
    """
    Returns specific survivor targets and a BLACKLIST of other bots' targets.
    This prevents Survivor from selling Trend Bot's positions.
    """
    survivor_targets = []
    blacklist = []
    
    # Helper to extract symbols
    def extract_symbols(raw_list):
        syms = []
        for x in raw_list:
             s, _ = utils.parse_target(x)
             if s: syms.append(s)
        return syms

    # 1. Survivor Targets (Fallback: Empty)
    survivor_targets = utils.get_targets_with_freshness_check(TARGET_FILE, "survivor_targets", [])

    # 2. Blacklist (Trend + Wheel + Condor)
    blacklist = []
    
    # Trend Fallback
    trend_fallback = ["NVDA", "TSLA", "COIN"]
    trend_raw = utils.get_targets_with_freshness_check(TARGET_FILE, "trend_targets", trend_fallback)
    blacklist += extract_symbols(trend_raw)
    
    # Wheel Fallback
    wheel_raw = utils.get_targets_with_freshness_check(TARGET_FILE, "wheel_targets", utils.BOT_MAPPING["wheel_bot"])
    blacklist += extract_symbols(wheel_raw)
    
    # Condor Fallback
    condor_raw = utils.get_targets_with_freshness_check(TARGET_FILE, "condor_targets", utils.BOT_MAPPING["condor_bot"])
    blacklist += extract_symbols(condor_raw)
        

        
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
        df.index = df.index.tz_convert('US/Eastern')
        return df
    except: return None

def run_survivor_bot():
    logger.info(f"--- 🛡️ SURVIVOR BOT (Segregated) STARTED ---")
    send_discord("**Survivor Bot V3.1** Online\nSegregation Protocol Active.")
    
    # Phase 10: Failed order cooldown
    _failed_symbols = set()
    
    while True:
        try:
            # 1. Market Check
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping 15m...")
                    time.sleep(900) # Sleep 15 mins if closed
                    continue
            except: pass

            # 2. Build Watchlist & Blacklist
            raw_scout_targets, blacklist = get_segregated_targets()
            
            # --- PARSE & FILTER (Phase 2) ---
            target_map = {} # symbol -> confidence
            clean_scout_targets = []
            
            for item in raw_scout_targets:
                sym, conf = utils.parse_target(item)
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
                logger.error(f"[!] Config load error: {e}")
                current_regime = "SIDEWAYS"
                current_vix = 15.0

            # EOD Policy Time Check
            now_et = datetime.datetime.now(pytz.timezone('US/Eastern'))
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
                # Phase 10: Skip failed symbols and cryptos
                if symbol in _failed_symbols: continue
                if symbol in ["BTC/USD", "ETH/USD"]: continue 

                df = get_data_alpaca(symbol)
                if df is None: continue

                # Indicators
                df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
                df['sma200'] = SMAIndicator(close=df['close'], window=200).sma_indicator()
                
                latest = df.iloc[-1]
                price = float(latest['close'])
                rsi = float(latest['rsi'])
                sma = float(latest['sma200']) if not pd.isna(latest['sma200']) else 0

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
                    except:
                        live_price = price # fallback to bar close if API fails
                    
                    entry_price = float(pos.avg_entry_price)
                    pct_gain = (live_price - entry_price) / entry_price
                    
                    should_sell = False
                    reason = ""
                    
                    if is_eod_eval:
                        import datetime
                        indicators = {"rsi": float(rsi)}
                        score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price, indicators, current_regime, current_vix, hours_held=24.0)
                        tier = tiered_hold.get_hold_tier(score, "survivor_bot")
                        
                        if tier != "CLOSE_EOD":
                            logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol}. Tier: {tier} (Score: {score})")
                            continue
                            
                    if is_eod_close:
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
                        try:
                            logger.info(f"    📉 SELLING {symbol}: {reason}")
                            
                            # Fractional shares require DAY orders, whole shares can be GTC
                            if qty != sell_qty: 
                                order_tif = TimeInForce.DAY
                                order_qty = qty # sell exact fractional amount
                            else:
                                order_tif = TimeInForce.GTC
                                order_qty = sell_qty
                                
                            trading_client.submit_order(
                                order_data=MarketOrderRequest(
                                    symbol=symbol, 
                                    qty=order_qty, 
                                    side=OrderSide.SELL, 
                                    time_in_force=order_tif,
                                    client_order_id=f"survivor_bot-{symbol}-{int(time.time())}"
                                )
                            )
                            send_discord(f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%")
                            log_to_influx(symbol, "sell", live_price, order_qty)
                        except Exception as e:
                            logger.error(f"    [!] Sell Error {symbol}: {e}")
                            _failed_symbols.add(symbol)

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
                            qty = int(risk_amt / price)
                            
                            # Phase 10: Max position value cap (10% of equity max)
                            max_position_value = equity * 0.10
                            max_qty = int(max_position_value / price)
                            qty = min(qty, max_qty)
                            
                            # Check buying power before ordering
                            order_cost = float(qty) * float(price)
                            buying_power = float(account.non_marginable_buying_power)
                            if order_cost > buying_power:
                                logger.info(f"       [SKIP] {symbol} | Cost ${order_cost:.0f} > Buying Power ${buying_power:.0f}")
                                continue
                            
                            if qty > 0:
                                logger.info(f"       -> Buying {qty} shares (Size: {scaler:.1f}x)...")
                                try:
                                    trading_client.submit_order(order_data=MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY, client_order_id=f"survivor_bot-{symbol}-{int(time.time())}"))
                                    send_discord(f"💎 **BOUGHT DIP {symbol}**\nRSI: {rsi:.0f}\nConfidence: {confidence:.2f}\nGate: {gate}")
                                    log_to_influx(symbol, "buy", price, qty)
                                except Exception as e:
                                    logger.error(f"       [!] Order Error: {e}")
                                    _failed_symbols.add(symbol)
                        else:
                            logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} < {RSI_BUY} but NOT (Uptrend | Core | Scout). SMA: {sma:.2f}")
                    else:
                        logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} >= {RSI_BUY}")

            time.sleep(60)

        except Exception as e:
            logger.error(f"Survivor Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_survivor_bot()