"""Trend Bot — EMA momentum, long/short.

Ported onto the fleet_bot runner. Strategy semantics unchanged from V4.x:

  entry:  ADX(14) > 20 on 15m bars, then either a fresh EMA 9/21 crossover
          or momentum confirmation (5 aligned bars, price within 1% of the
          fast EMA, ADX > 25 at 0.8x size). Longs from trend_targets, shorts
          from short_targets; regime halves size against the prevailing tape;
          shorts capped at 30% of equity.
  exit:   -5% stop, +8% take profit, opposite crossover, tiered-hold EOD
          policy, max-hold backstop for aged positions. Only positions this
          bot OWNS are managed (a target symbol can be held by another bot,
          or be unowned quarantined stock).

All scaffolding (clients, Discord, market hours, regime, ownership priming,
budget gate, EOD windows, cooldowns, order tagging) lives in fleet_bot.
"""
import datetime

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from ta.trend import EMAIndicator, ADXIndicator

import tiered_hold
import utils
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
FAST_EMA = 9
SLOW_EMA = 21
RISK_PER_TRADE = 0.02
MAX_POSITION_PCT = 0.15    # never more than 15% of portfolio per trade
MIN_PRICE_LONG = 5.00
MIN_PRICE_SHORT = 10.00    # higher floor for shorts (volatility)
MAX_SHORT_EXPOSURE = 0.30  # total shorts capped at 30% of equity

# Momentum Confirmation Settings
MOMENTUM_BARS = 5             # fast EMA above slow for N consecutive bars
MOMENTUM_ADX_MIN = 25         # higher bar than crossover (20) — confirmed trend
MOMENTUM_PULLBACK_PCT = 0.01  # price within 1% of fast EMA (buying the dip)
MOMENTUM_SIZE_MULT = 0.8      # slightly smaller than crossover (joining late)

STOP_LOSS = -0.05
TAKE_PROFIT = 0.08

bot = FleetBot("trend_bot", loop_seconds=60, market_hours=True)
logger = bot.logger


def get_data_alpaca(symbol):
    try:
        start_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start_time,
            limit=500
        )
        bars = bot.data_client.get_stock_bars(req)
        if not bars.data: return None
        df = bars.df.xs(symbol)
        df.index = df.index.tz_convert('America/New_York')
        return df
    except Exception as e:
        registry.log_error("trend_bot", "get_data_alpaca", e, context=symbol)
        return None


def momentum_signal(df, price, local_adx, bearish=False):
    """(momentum_ok, ema_aligned, pullback) for the momentum-confirmation
    entry: N consecutive aligned bars, price near the fast EMA, strong ADX."""
    if len(df) < MOMENTUM_BARS + 1:
        return False, False, None
    recent = df.iloc[-(MOMENTUM_BARS + 1):]
    if bearish:
        aligned = all(float(recent['ema_fast'].iloc[i]) < float(recent['ema_slow'].iloc[i])
                      for i in range(len(recent)))
    else:
        aligned = all(float(recent['ema_fast'].iloc[i]) > float(recent['ema_slow'].iloc[i])
                      for i in range(len(recent)))
    ema_fast_val = float(df['ema_fast'].iloc[-1])
    pullback = abs(price - ema_fast_val) / ema_fast_val
    near_ema = pullback <= MOMENTUM_PULLBACK_PCT
    return (aligned and near_ema and local_adx > MOMENTUM_ADX_MIN), aligned, pullback


def close_position(symbol, pos_side, sell_qty, reason, notify_msg):
    """One exit path for every close: SELL closes longs, BUY covers shorts."""
    close_side = OrderSide.SELL if pos_side == "long" else OrderSide.BUY
    return bot.submit(
        bot.market_order(symbol, sell_qty, close_side, TimeInForce.GTC),
        reason=reason, notify=notify_msg
    )


def manage_position(symbol, pos, latest, price, local_adx, bull_cross, bear_cross):
    """Exit logic for one held position. Mirrors V4.x behavior exactly."""
    # Manage only positions that are OURS. A target symbol can be held by
    # another bot (or unowned, e.g. quarantined assignment stock) — exiting
    # it liquidates a strategy we don't run.
    if utils.get_bot_owner(symbol, pos.asset_class, bot.trading_client) != "trend_bot":
        return

    qty = float(pos.qty)
    sell_qty = int(abs(qty))
    side = pos.side
    if sell_qty <= 0:
        logger.info(f"    [SKIP] {symbol} fractional position ({qty} shares), cannot close")
        return

    is_long = side == "long"
    entry_price = float(pos.avg_entry_price)
    hours_held = bot.hours_held(symbol)
    ema_intact = bool(latest['ema_fast'] > latest['ema_slow']) if is_long \
                 else bool(latest['ema_fast'] < latest['ema_slow'])
    indicators = {"adx": float(local_adx), "ema_trend_intact": ema_intact}
    bt = "trend_long" if is_long else "trend_short"
    pnl_pct = (price - entry_price) / entry_price if is_long \
              else (entry_price - price) / entry_price
    side_txt = side.upper() if isinstance(side, str) else str(side).upper()

    # --- MAX-HOLD BACKSTOP (orphan protection) ---
    # Hard time-based exit so no position can bleed indefinitely. Runs every
    # cycle (not just the EOD window).
    if hours_held is not None:
        mh_score = tiered_hold.calculate_hold_score(bt, price, entry_price, indicators,
                                                    bot.regime, bot.vix, hours_held=hours_held)
        mh_tier = tiered_hold.get_hold_tier(mh_score, bt)
        max_days = tiered_hold.max_hold_days_for_tier(mh_tier)
        if max_days is not None and (hours_held / 24.0) >= max_days:
            logger.info(f"    ⏳ MAX HOLD EXIT {side_txt} {symbol} — held {hours_held/24:.1f}d ≥ {max_days}d cap (tier {mh_tier})")
            close_position(symbol, side, sell_qty, "Max Hold Exceeded",
                           f"⏳ **MAX HOLD CLOSE {side_txt} {symbol}**\nHeld {hours_held/24:.1f}d (cap {max_days}d)\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
            return

    # --- TIERED HOLD (EOD policy) ---
    is_held_overnight = False
    if bot.time_str >= "15:30":
        score = tiered_hold.calculate_hold_score(bt, price, entry_price, indicators,
                                                 bot.regime, bot.vix, hours_held=hours_held)
        tier = tiered_hold.get_hold_tier(score, bt)
        if tier != "CLOSE_EOD":
            is_held_overnight = True
            if bot.is_eod_eval:
                logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} {side_txt}. Tier: {tier} (Score: {score})")
                return

    if bot.is_eod_close:
        if is_held_overnight:
            logger.info(f"    [HOLD] 🌙 Overriding EOD sweep for {symbol} {side_txt} (15:45+ ET).")
        else:
            logger.info(f"    📉 EOD LIQUIDATION {side_txt} {symbol} (15:45+ ET)")
            close_position(symbol, side, sell_qty, "EOD Liquidation",
                           f"📉 **EOD CLOSE {side_txt} {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
        return

    if pnl_pct <= STOP_LOSS:
        logger.info(f"    🛑 STOP LOSS {side_txt} {symbol} ({pnl_pct:.1%})")
        close_position(symbol, side, sell_qty, "Stop Loss",
                       f"🛑 **STOP LOSS {side_txt} {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
    elif pnl_pct >= TAKE_PROFIT:
        logger.info(f"    💰 TAKE PROFIT {side_txt} {symbol} ({pnl_pct:.1%})")
        close_position(symbol, side, sell_qty, "Take Profit",
                       f"💰 **TAKE PROFIT {side_txt} {symbol}**\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
    elif is_long and (not bull_cross and bear_cross):
        logger.info(f"    📉 CLOSE LONG {symbol} (Crossover)")
        close_position(symbol, side, sell_qty, "Bearish Crossover",
                       f"📉 **SELL/CLOSE {symbol}** (Cross)\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")
    elif (not is_long) and (not bear_cross and bull_cross):
        logger.info(f"    📉 CLOSE SHORT {symbol} (Crossover)")
        close_position(symbol, side, sell_qty, "Bullish Crossover",
                       f"📉 **BUY TO COVER {symbol}** (Bull Cross)\nPrice: ${price:.2f}\nPnL: {pnl_pct:.2%}")


def try_entry(symbol, is_long, price, local_adx, df, bull_cross, bear_cross,
              target_map, cycle_state):
    """Entry logic for one candidate, long or short. Mirrors V4.x exactly."""
    if bot.is_eod_skip_entry:
        return
    if not bot.budget_ok:
        return

    min_price = MIN_PRICE_LONG if is_long else MIN_PRICE_SHORT
    if price < min_price:
        suffix = "" if is_long else " for shorts"
        logger.info(f"    [SKIP] {symbol} | Price ${price:.2f} < ${min_price:.2f} minimum{suffix}")
        return

    if not is_long and cycle_state["short_exposure"] >= bot.equity * MAX_SHORT_EXPOSURE:
        logger.info(f"    [PAUSE] Max Short Exposure reached (${cycle_state['short_exposure']:.0f} >= 30% of equity). Skipping short entry.")
        return

    if local_adx <= 20:
        logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} <= 20 (Trend too weak)")
        return

    # Regime-based sizing: halve size when entering against the tape
    size_mult = 1.0
    if is_long and bot.regime == "BEAR_TREND":
        size_mult = 0.5
    elif (not is_long) and bot.regime == "BULL_TREND":
        size_mult = 0.5

    cross = bull_cross if is_long else bear_cross
    momentum_ok, _, _ = momentum_signal(df, price, local_adx, bearish=not is_long)

    if cross:
        entry_type = "Crossover"
    elif momentum_ok:
        entry_type = "Momentum"
        size_mult *= MOMENTUM_SIZE_MULT
    else:
        trigger = "Bullish" if is_long else "Bearish"
        logger.info(f"    [SKIP] {symbol} | ADX {local_adx:.0f} > 20 but no {trigger} Crossover or Momentum trigger.")
        return

    confidence = target_map.get(symbol, 0.5)
    qty, order_cost = bot.size_position(price, confidence, RISK_PER_TRADE,
                                        size_mult=size_mult,
                                        max_position_pct=MAX_POSITION_PCT)
    if qty <= 0:
        return

    scaler = 0.5 + confidence
    if is_long:
        logger.info(f"    🔼 BUY SIGNAL (LONG) {symbol} ({entry_type} | Conf: {confidence:.2f}, Size: {scaler * size_mult:.1f}x, Cost: ${order_cost:.0f})")
        bot.submit(
            bot.market_order(symbol, qty, OrderSide.BUY, TimeInForce.DAY),
            reason=f"Entry ({entry_type})",
            notify=f"🔼 **LONG {symbol}** ({entry_type})\nRegime: {bot.regime}\nADX: {local_adx:.0f}\nConfidence: {confidence:.2f}"
        )
    else:
        logger.info(f"    🩸 BUY SIGNAL (SHORT) {symbol} ({entry_type} | Conf: {confidence:.2f}, Size: {scaler * size_mult:.1f}x, Cost: ${order_cost:.0f})")
        order = bot.submit(
            bot.market_order(symbol, qty, OrderSide.SELL, TimeInForce.DAY),
            reason=f"Entry ({entry_type})",
            notify=f"🩸 **SHORT {symbol}** ({entry_type})\nRegime: {bot.regime}\nADX: {local_adx:.0f}\nConfidence: {confidence:.2f}"
        )
        if order is not None:
            # Count it now so we can't rapid-fire past the cap within one cycle
            cycle_state["short_exposure"] += float(qty) * float(price)


def cycle(bot):
    long_raw = bot.targets("trend_targets")
    short_raw = bot.targets("short_targets")
    target_map_long = {s: d.get("confidence", 0.5) for s, d in long_raw.items()}
    target_map_short = {s: d.get("confidence", 0.5) for s, d in short_raw.items()}

    my_holdings = bot.my_symbols()

    logger.info(f"Regime: {bot.regime} | Long Targets: {len(target_map_long)} | Short Targets: {len(target_map_short)}")

    if not (target_map_long or target_map_short) and not my_holdings:
        logger.info("    💤 Standby Mode: No targets or holdings. Sleeping...")
        return

    # Current short book (ours only) for the exposure cap
    cycle_state = {
        "short_exposure": sum(
            abs(float(p.market_value))
            for p in bot.positions
            if p.side == "short"
            and utils.get_bot_owner(p.symbol, p.asset_class, bot.trading_client) == "trend_bot"
        )
    }

    scan_list = list(set(list(target_map_long) + list(target_map_short) + my_holdings))

    for symbol in scan_list:
        if bot.in_cooldown(symbol):
            continue
        if "/" in symbol:  # crypto never belongs to trend
            continue

        df = get_data_alpaca(symbol)
        if df is None: continue

        df['ema_fast'] = EMAIndicator(close=df['close'], window=FAST_EMA).ema_indicator()
        df['ema_slow'] = EMAIndicator(close=df['close'], window=SLOW_EMA).ema_indicator()
        adx_indicator = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=14)
        df['adx'] = adx_indicator.adx()

        latest = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(latest['close'])
        local_adx = float(latest['adx'])

        bull_cross = (latest['ema_fast'] > latest['ema_slow']) and (prev['ema_fast'] <= prev['ema_slow'])
        bear_cross = (latest['ema_fast'] < latest['ema_slow']) and (prev['ema_fast'] >= prev['ema_slow'])

        # --- DIAGNOSTICS (long-side view, entry candidates only) ---
        if symbol not in bot.pos_dict and len(df) >= MOMENTUM_BARS + 1:
            momentum_ok, ema_aligned, pullback = momentum_signal(df, price, local_adx)
            ema_gap = ((latest['ema_fast'] - latest['ema_slow']) / latest['ema_slow']) * 100
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

        if symbol in bot.pos_dict:
            manage_position(symbol, bot.pos_dict[symbol], latest, price, local_adx,
                            bull_cross, bear_cross)
        elif symbol in target_map_long and symbol not in bot.pending_symbols:
            try_entry(symbol, True, price, local_adx, df, bull_cross, bear_cross,
                      target_map_long, cycle_state)
        elif symbol in target_map_short and symbol not in bot.pending_symbols:
            try_entry(symbol, False, price, local_adx, df, bull_cross, bear_cross,
                      target_map_short, cycle_state)


if __name__ == "__main__":
    bot.notify("**Trend Sniper V5.0** Online\nRunner: fleet_bot | Ownership Logic Active.")
    bot.run(cycle)
