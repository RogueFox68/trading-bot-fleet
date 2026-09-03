"""Survivor Bot — RSI mean-reversion dip buyer.

First bot ported onto the fleet_bot runner (the pilot for the plug-and-play
architecture). Strategy semantics are unchanged from V3.x:

  entry: RSI(14) on 15m bars < 38, gated by daily-SMA200 uptrend OR scout
         approval; confidence-scaled sizing (5% risk, 10% max position)
  exit:  RSI > 70, +5% take profit, -3% stop loss, tiered-hold EOD policy,
         max-hold backstop for aged positions

All scaffolding (clients, Discord, market hours, regime, ownership priming,
budget gate, EOD windows, cooldowns, order tagging) lives in fleet_bot.
"""
import time

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from ta.momentum import RSIIndicator
import datetime

import tiered_hold
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
RSI_BUY = 38
RSI_SELL = 70
RISK_PER_TRADE = 0.05
MIN_PRICE = 5.00          # avoid penny-stock sizing bombs
MAX_POSITION_PCT = 0.10   # max 10% of equity per position

bot = FleetBot("survivor_bot", loop_seconds=60, market_hours=True)
logger = bot.logger


def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=20)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=200
        )
        bars = bot.data_client.get_stock_bars(req)
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
        bars = bot.data_client.get_stock_bars(req)
        if not bars.data or symbol not in bars.data:
            return None
        df = bars.df.xs(symbol)
        if len(df) < window:
            return None
        sma = float(df['close'].tail(window).mean())
        _daily_sma_cache[symbol] = (now, sma)
        # The scout rotates targets 3x daily out of a ~4800-symbol universe, so
        # evict past-TTL entries instead of accumulating one per symbol ever
        # scanned for the life of the process.
        for sym, (seen, _) in list(_daily_sma_cache.items()):
            if (now - seen) >= _DAILY_SMA_TTL:
                del _daily_sma_cache[sym]
        return sma
    except Exception as e:
        registry.log_error("survivor_bot", "get_trend_sma", e, context=symbol)
        return None


def get_segregated_targets():
    """Survivor targets plus a blacklist of trend/wheel targets, so Survivor
    never trades (or sells) another bot's picks."""
    survivor_targets = bot.targets("survivor_targets")
    blacklist = set(bot.targets("trend_targets")) | set(bot.targets("wheel_targets"))
    return survivor_targets, blacklist


def manage_position(symbol, pos, rsi, price):
    """Exit logic for one held position. Mirrors V3.x behavior exactly."""
    if symbol in bot.pending_symbols:
        logger.debug(f"  [SKIP] {symbol} already has a pending order.")
        return

    qty = float(pos.qty)
    sell_qty = int(abs(qty))

    # Fractional shares require DAY orders; whole shares can be GTC
    if qty != sell_qty:
        order_tif, order_qty = TimeInForce.DAY, qty
    else:
        order_tif, order_qty = TimeInForce.GTC, sell_qty

    # Fetch live price for P&L (bar close can lag)
    try:
        trade_req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
        live_price = float(bot.data_client.get_stock_latest_trade(trade_req)[symbol].price)
    except Exception as e:
        registry.log_error("survivor_bot", "fetch_live_price", e, context=symbol)
        live_price = price  # fallback to bar close if API fails

    entry_price = float(pos.avg_entry_price)
    hours_held = bot.hours_held(symbol)
    pct_gain = (live_price - entry_price) / entry_price

    # --- MAX-HOLD BACKSTOP (orphan protection) ---
    # Hard time-based exit so no position can bleed indefinitely.
    if hours_held is not None:
        mh_score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price,
                                                    {"rsi": float(rsi)}, bot.regime, bot.vix,
                                                    hours_held=hours_held)
        mh_tier = tiered_hold.get_hold_tier(mh_score, "survivor_bot")
        max_days = tiered_hold.max_hold_days_for_tier(mh_tier)
        if max_days is not None and (hours_held / 24.0) >= max_days:
            logger.info(f"    ⏳ MAX HOLD EXIT {symbol} — held {hours_held/24:.1f}d ≥ {max_days}d cap (tier {mh_tier})")
            bot.submit(
                bot.market_order(symbol, order_qty, OrderSide.SELL, order_tif),
                reason=f"Max Hold Exceeded [held {hours_held/24:.1f}d]",
                notify=f"⏳ **MAX HOLD CLOSE {symbol}**\nHeld {hours_held/24:.1f}d (cap {max_days}d)\nP&L: {pct_gain*100:.2f}%"
            )
            return

    # --- TIERED HOLD (EOD policy) ---
    is_held_overnight = False
    if bot.time_str >= "15:30":
        score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price,
                                                 {"rsi": float(rsi)}, bot.regime, bot.vix,
                                                 hours_held=hours_held)
        tier = tiered_hold.get_hold_tier(score, "survivor_bot")
        if tier != "CLOSE_EOD":
            is_held_overnight = True
            if bot.is_eod_eval:
                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol}. Tier: {tier} (Score: {score})")
                return

    should_sell = False
    reason = ""
    if bot.is_eod_close:
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
        logger.info(f"    📉 SELLING {symbol}: {reason}")
        bot.submit(
            bot.market_order(symbol, order_qty, OrderSide.SELL, order_tif),
            reason=reason,
            notify=f"💰 **SOLD {symbol}**\nReason: {reason}\nP&L: {pct_gain*100:.2f}%"
        )


def try_entry(symbol, rsi, price, target_map):
    """Entry logic for one candidate. Mirrors V3.x behavior exactly."""
    if bot.is_eod_skip_entry:
        return
    if not bot.budget_ok:
        return
    if price < MIN_PRICE:
        logger.info(f"    [SKIP] {symbol} | Price ${price:.2f} < ${MIN_PRICE:.2f} minimum")
        return
    if rsi >= RSI_BUY:
        logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} >= {RSI_BUY}")
        return

    # None = daily bars unavailable = trend unknown, NOT an uptrend
    sma = get_trend_sma(symbol, 200)
    is_uptrend = bool(sma) and price > sma
    is_scout_approved = symbol in target_map

    if not (is_uptrend or is_scout_approved):
        sma_txt = f"{sma:.2f}" if sma else "N/A"
        logger.info(f"    [SKIP] {symbol} | RSI {rsi:.0f} < {RSI_BUY} but NOT (Uptrend | Scout). SMA: {sma_txt}")
        return

    confidence = target_map.get(symbol, 0.5)
    gate = "Scout" if is_scout_approved else "Uptrend"
    logger.info(f"    💎 DIP DETECTED: {symbol} (RSI {rsi:.0f}, Conf {confidence:.2f}, Gate: {gate})")

    qty, order_cost = bot.size_position(price, confidence, RISK_PER_TRADE,
                                        max_position_pct=MAX_POSITION_PCT)
    if qty <= 0:
        return

    logger.info(f"       -> Buying {qty} shares (Conf: {confidence:.2f}, Cost: ${order_cost:.0f})...")
    bot.submit(
        bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.DAY),
        reason="Bought Dip",
        notify=f"💎 **BOUGHT DIP {symbol}**\nRSI: {rsi:.0f}\nConfidence: {confidence:.2f}\nGate: {gate}"
    )


def cycle(bot):
    raw_scout_targets, blacklist = get_segregated_targets()

    target_map = {}   # symbol -> confidence
    for sym, data in raw_scout_targets.items():
        if sym and sym not in blacklist:
            target_map[sym] = data.get("confidence", 0.5)

    owned_symbols = bot.my_symbols()
    scan_list = list(set(list(target_map) + owned_symbols))

    logger.info(f"Scanning {len(scan_list)} Targets (Watchlist + Owned, "
                f"Ignored {len(blacklist)} Blacklist) | ET: {bot.time_str}")

    for symbol in scan_list:
        if bot.in_cooldown(symbol):
            continue
        if "/" in symbol:  # crypto never belongs to survivor
            continue

        df = get_data_alpaca(symbol)
        if df is None: continue

        df['rsi'] = RSIIndicator(close=df['close'], window=14).rsi()
        latest = df.iloc[-1]
        price = float(latest['close'])
        rsi = float(latest['rsi'])

        # --- DIAGNOSTICS ---
        if symbol not in bot.pos_dict:
            sma = get_trend_sma(symbol, 200)
            sma_status = "above" if (sma and price > sma) else ("BELOW" if sma else "N/A")
            gate_available = "Scout" if symbol in target_map else "Uptrend only"
            if rsi < RSI_BUY:
                logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available} | 🎯 ENTRY ZONE")
            elif rsi < RSI_BUY + 5:
                logger.info(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available} | ⏳ Near threshold")
            else:
                logger.debug(f"  📊 {symbol:<5} ${price:>8.2f} | RSI {rsi:>5.1f} | SMA200 {sma_status} | Gate: {gate_available}")

        if symbol in bot.pos_dict:
            manage_position(symbol, bot.pos_dict[symbol], rsi, price)
        elif symbol not in bot.pending_symbols:
            try_entry(symbol, rsi, price, target_map)


if __name__ == "__main__":
    bot.notify("**Survivor Bot V4.0** Online\nRunner: fleet_bot | Segregation Protocol Active.")
    bot.run(cycle)
