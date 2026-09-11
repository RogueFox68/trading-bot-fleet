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
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from ta.momentum import RSIIndicator
import datetime

import tiered_hold
import utils
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
RSI_BUY = 38
RSI_SELL = 70
RSI_WINDOW = 14
RISK_PER_TRADE = 0.05
MIN_PRICE = 5.00          # avoid penny-stock sizing bombs
MAX_POSITION_PCT = 0.10   # max 10% of equity per position

bot = FleetBot("survivor_bot", loop_seconds=60, market_hours=True)
logger = bot.logger


# RSI(14) needs 15 bars; ask for a few sessions so a holiday or a halted
# morning can't leave the window short. NO `limit` alongside `start` - that
# returns the OLDEST bars in the window (see utils.newest_bars), which is how
# this fetch spent months computing RSI on two-week-old prices.
INTRADAY_LOOKBACK_DAYS = 5
INTRADAY_BARS = 200
BAR_SECONDS = 15 * 60


def get_data_alpaca(symbol):
    """(df, indicators_ok). A stale frame returns indicators_ok=False, NOT None.

    The distinction is load-bearing. Returning None for stale bars made the
    caller `continue`, which skipped manage_position entirely - so a stale
    history feed silently suppressed stop losses, take profits, the max-hold
    backstop and EOD liquidation on every held position. That is the exact
    failure this release exists to remove, reintroduced one layer up.
    Indicator eligibility and risk management are separate questions.
    """
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=INTRADAY_LOOKBACK_DAYS)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
        )
        bars = bot.data_client.get_stock_bars(req)
        if not bars.data:
            return None, False
        df = utils.newest_bars(bars.df.xs(symbol), INTRADAY_BARS)
        # Freshness is checked in UTC, before the display-only tz conversion.
        fresh = utils.bars_are_fresh(df, BAR_SECONDS, "survivor_bot", symbol, "15m",
                                     session_elapsed=bot.session_elapsed)
        if len(df) < RSI_WINDOW + 1:
            fresh = False  # not enough history to compute RSI at all
        df.index = df.index.tz_convert('America/New_York')
        return df, fresh
    except Exception as e:
        registry.log_error("survivor_bot", "get_data_alpaca", e, context=symbol)
        return None, False


# Daily SMA200 (long-term trend filter) is computed on DAILY bars and cached
# per symbol — it barely moves intraday, so we refresh every few hours.
_daily_sma_cache = {}  # symbol -> (epoch, sma_value)
_DAILY_SMA_TTL = 6 * 3600
DAILY_BAR_SECONDS = 24 * 3600
# Daily bars legitimately age over a weekend or a holiday run; 4 calendar days
# clears the longest US market close without clearing a real feed outage.
DAILY_STALE_FACTOR = 4.0


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
        )
        bars = bot.data_client.get_stock_bars(req)
        if not bars.data or symbol not in bars.data:
            return None
        df = bars.df.xs(symbol)
        if len(df) < window:
            return None
        # A daily SMA200 built on bars ending three months ago is not a
        # long-term trend filter, it is a lagged one - and it gated entries.
        if not utils.bars_are_fresh(df, DAILY_BAR_SECONDS, "survivor_bot", symbol,
                                    f"SMA{window}", stale_factor=DAILY_STALE_FACTOR):
            return None
        sma = float(utils.newest_bars(df, window)['close'].mean())
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


def manage_position(symbol, pos, rsi, price, indicators_ok=True):
    """Exit logic for one held position.

    Runs whether or not indicators are available: stops, targets, the max-hold
    backstop and EOD liquidation need only a validated live price and the
    entry price. `indicators_ok=False` suppresses exactly the decisions that
    read the bars (the RSI exit, and the RSI input to the hold score).
    """
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

    # A validated live trade, or nothing. The old code fell back to the bar
    # close here, which is only safe while the bars are fresh - and when they
    # are not, that fallback prices a stop loss off a two-week-old close.
    live_price = utils.live_equity_price(bot.data_client, symbol, "survivor_bot")
    if live_price is None:
        if indicators_ok and price is not None:
            live_price = float(price)  # bars are fresh; their close is current
            logger.warning(f"    [{symbol}] live quote unavailable; using the current bar close.")
        else:
            registry.log_error("survivor_bot", "manage_position",
                               Exception("no validated price: live quote failed and bars are stale"),
                               context=symbol)
            logger.error(f"    [!] {symbol} HELD but unmanageable this cycle — no live quote "
                         f"and no fresh bar. Risk exits cannot be evaluated.")
            return

    entry_price = float(pos.avg_entry_price)
    hours_held = bot.hours_held(symbol)
    pct_gain = (live_price - entry_price) / entry_price

    # --- MAX-HOLD BACKSTOP (orphan protection) ---
    # Hard time-based exit so no position can bleed indefinitely.
    # Blind on indicators => pass none. calculate_hold_score defaults a missing
    # RSI to 50 (no thesis credit), which scores LOWER and so biases toward
    # CLOSE_EOD - the safe direction when we cannot see the signal.
    hold_indicators = {"rsi": float(rsi)} if (indicators_ok and rsi is not None) else {}

    if hours_held is not None:
        mh_score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price,
                                                    hold_indicators, bot.regime, bot.vix,
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

    # --- RISK EXITS (evaluated BEFORE any hold branch) ---
    #
    # Ordering here is the whole point. The tiered-hold block below used to run
    # first and `return` outright on a HOLD_OVERNIGHT/HOLD_SWING tier, so from
    # 15:30 ET a position that scored "hold" had NO stop loss and NO take
    # profit — for the rest of the session and straight through the overnight
    # gap, the window where a gap-down actually happens. tiered_hold's own
    # OVERNIGHT_STOPS percentages are still unwired, so nothing downstream
    # covered it either.
    #
    # A hold decision is a decision about the EOD sweep, not a waiver on risk
    # management. Stop/target/signal exits are evaluated first and unconditionally.
    should_sell = False
    reason = ""
    if indicators_ok and rsi is not None and rsi > RSI_SELL:
        should_sell = True
        reason = f"RSI Overbought ({rsi:.0f})"
    elif pct_gain > 0.05:
        should_sell = True
        reason = "Take Profit (+5%)"
    elif pct_gain < -0.03:
        should_sell = True
        reason = "Stop Loss (-3%)"

    # --- TIERED HOLD (EOD policy) ---
    # Only reached when no risk exit fired.
    if not should_sell:
        is_held_overnight = False
        if bot.time_str >= "15:30":
            score = tiered_hold.calculate_hold_score("survivor_bot", live_price, entry_price,
                                                     hold_indicators, bot.regime, bot.vix,
                                                     hours_held=hours_held)
            tier = tiered_hold.get_hold_tier(score, "survivor_bot")
            if tier != "CLOSE_EOD":
                is_held_overnight = True
                if bot.is_eod_eval:
                    logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol}. Tier: {tier} (Score: {score})")
                    return

        if bot.is_eod_close:
            if is_held_overnight:
                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} (15:45+ ET).")
            else:
                should_sell = True
                reason = "EOD Liquidation (15:45+ ET)"

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

        df, indicators_ok = get_data_alpaca(symbol)

        rsi = price = None
        if indicators_ok:
            df['rsi'] = RSIIndicator(close=df['close'], window=RSI_WINDOW).rsi()
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
            # ALWAYS reached for a held symbol, indicators or not. Bad bars
            # stop us opening a position; they never stop us closing one.
            manage_position(symbol, bot.pos_dict[symbol], rsi, price,
                            indicators_ok=indicators_ok)
        elif indicators_ok and symbol not in bot.pending_symbols:
            try_entry(symbol, rsi, price, target_map)


if __name__ == "__main__":
    bot.notify("**Survivor Bot V4.0** Online\nRunner: fleet_bot | Segregation Protocol Active.")
    bot.run(cycle)
