"""Wheel Bot — options premium selling (cash-secured puts + covered calls).

Ported onto the fleet_bot runner. Strategy semantics unchanged from the
post-PAAS hardening:

  entry:   sell 5%-OTM (confidence-adjusted) puts 25-45 DTE on scout wheel
           targets; covered calls on wheel-owned stock (including assigned
           lots that dropped off the scout list — that IS the wheel). New
           puts gate on VIX/regime/crunch/budget; covered calls are exempt
           (they add no collateral and reduce risk on held stock).
  manage:  take profit at 50% premium capture; force-close/force-roll
           levers from bot_config; ITM contracts at DTE <= 5 are closed
           outright via the escalating price ladder (midpoint -> half-cross
           -> ask) so a wide spread can never park a risk-reducing exit;
           stale contracts (DTE <= 10) roll to the next 25-45 DTE window.

Budget: the CFO number now comes from utils.get_budget_dollars (effective
budget -> cfo base allocation -> fail-closed 0.0) instead of a private
effective_budgets.json read with a magic $20k fallback. The collateral
commitment math is unchanged.

All scaffolding (clients, Discord, market hours, regime, budget gate,
open-order tracking) lives in fleet_bot.
"""
import time
import datetime
import re

from alpaca.data.historical import OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType

import config
import utils
import fleet_registry
from fleet_bot import FleetBot
from logger import registry

# --- STRATEGY SETTINGS ---
MIN_DTE = 25
MAX_DTE = 45
TARGET_OTM_PCT = 0.05
MIN_PREMIUM = 0.10
TAKE_PROFIT_PCT = 0.50
MAX_SPREAD_PCT = 0.25
STALE_ROLL_DTE = 10      # start rolling at/below this DTE (entries open 25-45)
FORCE_CLOSE_DTE = 5      # ITM at/below this DTE -> close-only expiry backstop
CLOSE_LADDER_STEPS = (0.0, 0.5, 1.0)  # fraction of mid->ask crossed per attempt
CLOSE_POLL_SECONDS = 10  # fill wait per ladder rung
CLOSE_FAIL_ALERT_AFTER = 3  # consecutive fully-failed closes before alerting

bot = FleetBot("wheel_bot", loop_seconds=900, market_hours=True,
               discord_username="WheelBot 🚜")
logger = bot.logger

# Options quotes need their own client; the runner only carries stock data.
option_data_client = utils.bound_session_timeout(
    OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY))


def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = bot.data_client.get_stock_latest_trade(req)
        return float(res[symbol].price)
    except Exception as e:
        logger.error(f"  [!] Error price {symbol}: {e}")
        return 0.0


def get_option_data(symbol):
    try:
        req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
        res = option_data_client.get_option_latest_quote(req)
        return res[symbol]
    except Exception as e:
        logger.error(f"  [!] Error fetching option quote for {symbol}: {e}")
        return None


def calculate_smart_price(quote, side):
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)

    if ask == 0: return None

    spread = ask - bid
    spread_pct = spread / ask
    midpoint = (bid + ask) / 2

    if spread_pct > MAX_SPREAD_PCT:
        logger.debug(f"    [SKIP] Spread too wide ({spread_pct*100:.1f}%). Bid: {bid} Ask: {ask}")
        return None

    return round(midpoint, 2)


def marketable_close_price(quote, cross_frac):
    """Buy-to-close price that can cross the spread: mid + frac*(ask-mid).

    Unlike calculate_smart_price this never rejects on spread width — for a
    risk-reducing close (near-expiry ITM, force close) paying a wide spread
    beats assignment. cross_frac 0.0 = midpoint, 1.0 = the ask.
    """
    bid = float(quote.bid_price)
    ask = float(quote.ask_price)
    if ask <= 0:
        return None
    mid = (bid + ask) / 2
    return round(mid + (ask - mid) * cross_frac, 2)


# contract symbol -> consecutive cycles where the full close ladder failed
_close_failures = {}

def close_option_position(active_option, tag, reason):
    """Buy-to-close with an escalating price ladder (midpoint -> cross -> ask).

    Each rung re-quotes, submits a limit, waits CLOSE_POLL_SECONDS for the
    fill, cancels, and escalates. The old single midpoint limit with a 10s
    poll effectively never filled on an ITM contract — every 6/30-7/06
    rollcls order died this way and PAAS rode DTE-0 into assignment.
    Returns the fill limit price on success, None on failure; alerts after
    CLOSE_FAIL_ALERT_AFTER consecutive fully-failed cycles.
    """
    sym = active_option.symbol
    qty = abs(int(float(active_option.qty)))
    if qty <= 0:
        return None
    remaining = qty
    for frac in CLOSE_LADDER_STEPS:
        quote = get_option_data(sym)
        if not quote:
            logger.error(f"    [CLOSE ABORTED] No quote for {sym}.")
            break
        price = marketable_close_price(quote, frac)
        if price is None or price <= 0:
            logger.error(f"    [CLOSE ABORTED] No ask for {sym}.")
            break
        req = LimitOrderRequest(
            symbol=sym,
            qty=remaining,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=price,
            client_order_id=f"wheel_bot-{sym}-{tag}-{int(time.time())}"
        )
        try:
            order = utils.submit_and_log_order(bot.trading_client, req, logger, reason=reason)
        except Exception as e:
            logger.error(f"    [CLOSE ABORTED] Submit failed for {sym}: {e}")
            break
        filled = False
        for _ in range(int(CLOSE_POLL_SECONDS / 0.5)):
            time.sleep(0.5)
            o = bot.trading_client.get_order_by_id(order.id)
            if o.status.value == "filled":
                filled = True
                break
            if o.status.value in ("canceled", "expired", "rejected"):
                break
            # 'partially_filled' keeps waiting: it may complete within the window.
        if filled:
            _close_failures.pop(sym, None)
            logger.info(f"    ✅ [CLOSED] {sym} @ ${price} ({reason})")
            return price
        try:
            bot.trading_client.cancel_order_by_id(order.id)
        except Exception:
            pass  # already terminal
        # Subtract whatever DID fill on this rung before escalating, so the next
        # rung re-submits only what's left. Re-sending the original qty after a
        # partial fill would over-close: buying back more contracts than are
        # still short and flipping the position net long.
        try:
            done = bot.trading_client.get_order_by_id(order.id)
            just_filled = int(float(getattr(done, "filled_qty", 0) or 0))
        except Exception:
            just_filled = 0
        if just_filled > 0:
            # This rung's partial fill has no stable broker filled_at, so
            # reconcile_fills' full-fill path can't log it. Record it now so a
            # close completed across partial rungs isn't under-counted in P&L.
            # Idempotent (deterministic stamp) and a no-op unless the order is
            # terminal, so reconcile_fills re-firing on it is harmless.
            utils.log_terminal_partial_fill(done, logger)
            remaining -= just_filled
            logger.info(f"    [CLOSE PARTIAL] {sym} filled {just_filled}; "
                        f"{remaining} contract(s) left.")
            if remaining <= 0:
                _close_failures.pop(sym, None)
                logger.info(f"    ✅ [CLOSED] {sym} via partial fills ({reason})")
                return price
        logger.info(f"    [CLOSE RETRY] {sym} unfilled @ ${price} "
                    f"(cross {int(frac*100)}%). Escalating.")
    fails = _close_failures.get(sym, 0) + 1
    _close_failures[sym] = fails
    if fails >= CLOSE_FAIL_ALERT_AFTER:
        err = Exception(f"buy-to-close for {sym} unfilled through the full "
                        f"midpoint→ask ladder {fails} cycles in a row")
        registry.log_error("wheel_bot", "close_unfilled", err, context=sym)
        bot.notify(f"🚨 **CLOSE FAILING: {sym}**\nBuy-to-close unfilled through "
                   f"the midpoint→ask ladder {fails} cycles in a row ({reason}). "
                   f"Check the book — assignment risk if this is ITM near expiry.")
        _close_failures[sym] = 0  # re-alert only after another full streak
    return None


def find_best_contract(symbol, side, current_price, target_otm=TARGET_OTM_PCT):
    today = datetime.date.today()
    start_date = today + datetime.timedelta(days=MIN_DTE)
    end_date = today + datetime.timedelta(days=MAX_DTE)

    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status="active",
        expiration_date_gte=start_date,
        expiration_date_lte=end_date,
        type=ContractType.PUT if side == "PUT" else ContractType.CALL,
        limit=1000
    )

    try:
        contracts = bot.trading_client.get_option_contracts(req)
        available = contracts.option_contracts
    except Exception as e:
        logger.error(f"  [!] API Error fetching contracts: {e}")
        return None

    if not available: return None

    best_contract = None
    best_score = 1.0

    for c in available:
        strike = float(c.strike_price)
        if side == "PUT" and strike >= current_price: continue
        if side == "CALL" and strike <= current_price: continue

        pct_otm = abs(current_price - strike) / current_price
        score = abs(pct_otm - target_otm)

        if score < best_score:
            best_score = score
            best_contract = c

    return best_contract


def get_busy_tickers():
    """Tickers with OPEN orders, from the runner's per-cycle order fetch.
    Option symbols (e.g. AMD230120P...) parse back to their root."""
    busy_tickers = set()
    for o in bot.open_orders:
        match = re.match(r"^([A-Z]+)\d", o.symbol)
        busy_tickers.add(match.group(1) if match else o.symbol)
    return busy_tickers


def roll_option_position(ticker, active_option, current_stock_price, side, confidence):
    logger.info(f"🌀 [ROLL] Pre-checking next contract for {ticker} {side}...")

    # Step 1: Pre-Check (Find next contract and calculate price before executing any trades)
    dynamic_otm = TARGET_OTM_PCT * (1.5 - confidence)
    new_contract = find_best_contract(ticker, side, current_stock_price, dynamic_otm)

    if not new_contract:
        logger.error(f"  [ROLL ABORTED] No suitable next contract found for {ticker} {side}.")
        return False

    new_quote = get_option_data(new_contract.symbol)
    if not new_quote:
        logger.error(f"  [ROLL ABORTED] Failed to get quote for next contract {new_contract.symbol}.")
        return False

    new_limit_price = calculate_smart_price(new_quote, side)
    if new_limit_price is None:
        logger.error(f"  [ROLL ABORTED] Next contract spread too wide for midpoint pricing.")
        return False

    if new_limit_price < MIN_PREMIUM:
        logger.error(f"  [ROLL ABORTED] Next contract premium too low (${new_limit_price:.2f} < ${MIN_PREMIUM:.2f}).")
        return False

    # Step 2: Close Leg — escalating price ladder (midpoint → cross → ask).
    # The old midpoint-only limit with a single 10s poll effectively never
    # filled on an ITM contract, so every roll died here and the position
    # rode into expiry/assignment (PAAS, 2026-07-02).
    logger.info(f"🌀 [ROLL EXECUTION] Leg 1: Closing {active_option.symbol} (ladder)...")
    close_price = close_option_position(active_option, "rollcls", "Roll close (stale DTE)")
    if close_price is None:
        logger.warning(f"  [ROLL ABORTED] Close leg unfilled for {active_option.symbol}.")
        return False

    # Step 3: Open Leg
    logger.info(f"🌀 [ROLL EXECUTION] Leg 2: Opening {new_contract.symbol} @ limit ${new_limit_price}...")
    open_req = LimitOrderRequest(
        symbol=new_contract.symbol,
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=new_limit_price,
        client_order_id=f"wheel_bot-{new_contract.symbol}-{int(time.time())}"
    )

    try:
        utils.submit_and_log_order(bot.trading_client, open_req, logger)
        bot.notify(f"🌀 **ROLLED {ticker} {side}**\nClosed: {active_option.symbol} @ ${close_price}\nOpened: {new_contract.symbol} @ ${new_limit_price}")
        return True
    except Exception as e:
        logger.error(f"  [CRITICAL ROLL ERROR] Closed {active_option.symbol} but failed to open new contract {new_contract.symbol}! Position is now flat. Error: {e}")
        bot.notify(f"🚨 **CRITICAL ROLL FAILURE: {ticker}**\nClosed position but failed to re-open next leg! Position is flat. Check Alpaca manual execution immediately.")
        registry.log_error("wheel_bot", "roll_critical", e, context=f"flat_on_roll_{ticker}")
        return False


def manage_option(active_option, ticker, busy_tickers, target_map, force_close_list, force_roll_list):
    """Manage one wheel-owned option: harvest, force-close, expiry backstop, roll."""
    # Parse details from the OCC symbol
    occ_part = active_option.symbol[len(ticker):]
    exp_yymmdd = occ_part[:6]
    opt_type = occ_part[6]
    strike = float(occ_part[7:]) / 1000.0
    side = "PUT" if opt_type == 'P' else "CALL"

    try:
        exp_date = datetime.datetime.strptime(exp_yymmdd, "%y%m%d").date()
        dte = (exp_date - datetime.date.today()).days
    except Exception as e:
        logger.error(f"Failed parsing exp date for {active_option.symbol}: {e}")
        dte = 99

    current_stock_price = get_current_price(ticker)
    entry_price = float(active_option.avg_entry_price)
    current_opt_price = float(active_option.current_price)
    qty = float(active_option.qty)

    capture_pct = 0.0
    if entry_price > 0:
        capture_pct = (entry_price - current_opt_price) / entry_price

    logger.info(f"  {ticker:<4} | Option: {active_option.symbol} | Profit: {capture_pct*100:.1f}% | DTE: {dte}")

    # A) TAKE PROFIT (midpoint pricing; not urgent, so no ladder)
    if capture_pct >= TAKE_PROFIT_PCT:
        logger.info(f"    💵 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
        quote = get_option_data(active_option.symbol)
        close_price = calculate_smart_price(quote, "BUY")

        if close_price is None:
            logger.warning(f"    [WAIT] Spread too wide to close safely.")
            return

        req = LimitOrderRequest(
            symbol=active_option.symbol,
            qty=abs(int(qty)),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=close_price,
            client_order_id=f"wheel_bot-{active_option.symbol}-{int(time.time())}"
        )
        utils.submit_and_log_order(bot.trading_client, req, logger)
        bot.notify(f"💵 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
        return

    # B) FORCE CLOSE FROM CONFIG — ladder pricing so a wide spread
    # can't park the manual lever (it used to skip with [WAIT]).
    if ticker in force_close_list:
        logger.info(f"    🚨 [FORCE CLOSE] Config triggered close for {active_option.symbol}")
        if close_option_position(active_option, "forcecls", "Force close (config)"):
            bot.notify(f"🚨 **FORCE CLOSED {active_option.symbol}**")
        return

    # C) EXPIRY BACKSTOP — ITM and nearly expired: close-only. Never
    # wait for a roll target (deep ITM often has none in the next
    # window) and never let a wide spread block a risk-reducing
    # exit. An ITM short left open at expiry becomes assigned
    # stock (the PAAS chain).
    is_itm = (side == "PUT" and 0 < current_stock_price < strike) or \
             (side == "CALL" and current_stock_price > strike)
    if is_itm and dte <= FORCE_CLOSE_DTE:
        logger.info(f"    ⏰ [EXPIRY CLOSE] {active_option.symbol} ITM, DTE {dte} ≤ {FORCE_CLOSE_DTE}")
        if close_option_position(active_option, "expcls",
                                 f"Expiry force close (DTE {dte}, ITM)"):
            bot.notify(f"⏰ **EXPIRY CLOSE {active_option.symbol}**\n"
                       f"ITM with {dte} DTE — closed before assignment.")
        return

    # D) STALE ROLL (DTE <= STALE_ROLL_DTE) OR FORCE ROLL
    is_stale = dte <= STALE_ROLL_DTE
    if ticker in force_roll_list or is_stale:
        reason = f"DTE stale ({dte} days)" if is_stale else "Force roll config"
        logger.info(f"    🌀 [ROLL] Rolling {active_option.symbol} due to: {reason}")
        confidence = target_map.get(ticker, 0.5)
        roll_option_position(ticker, active_option, current_stock_price, side, confidence)


def try_open_position(ticker, target_map, managed_option_tickers, busy_tickers, my_budget):
    """Entry logic for one ticker: covered call on owned stock, else CSP."""
    if ticker in busy_tickers:
        logger.info(f"  {ticker:<4} | [SKIP] Open Order Exists.")
        return
    if ticker in managed_option_tickers:
        return

    # Check stock position — only OUR shares count toward covered
    # calls. Writing calls against another bot's stock turns into a
    # naked call the moment that bot exits (it doesn't know we
    # sold a call on its shares).
    stock_qty = 0
    for p in bot.positions:
        if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
            if utils.get_bot_owner(p.symbol, p.asset_class, bot.trading_client) == "wheel_bot":
                stock_qty = float(p.qty)

    # Covered calls consume no new collateral and reduce risk on
    # stock we already hold, so they bypass the entry gates below.
    will_write_cc = stock_qty >= 100

    current_stock_price = get_current_price(ticker)
    if current_stock_price == 0.0:
        return

    if not bot.budget_ok and not will_write_cc:
        return  # Skip new entries if budget paused, but finish loop

    # Total wheel collateral commitment (short puts at strike collateral)
    total_commitment = 0.0
    for p in bot.positions:
        owner = utils.get_bot_owner(p.symbol, p.asset_class, bot.trading_client)
        if owner == "wheel_bot":
            if p.asset_class == AssetClass.US_OPTION and float(p.qty) < 0:
                match = re.match(r"^[A-Z]{1,6}\d{6}(P|C)(\d{8})$", p.symbol)
                if match and match.group(1) == 'P':
                    strike = float(match.group(2)) / 1000
                    total_commitment += strike * abs(float(p.qty)) * 100
                else:
                    total_commitment += abs(float(p.market_value))
            else:
                total_commitment += abs(float(p.market_value))

    if bot.capital_crunch and not will_write_cc:
        logger.info(f"    [GATE] {ticker:<4} | No new positions — CAPITAL_CRUNCH is active.")
        return

    # Gate new entries by Macro Environment — the registry's gated_when rule
    # (BEAR_TREND/CRITICAL_VOLATILITY or VIX > 22). Covered calls exempt:
    # selling a call on held stock lowers risk, and post-assignment bear
    # regimes are exactly when the wheel needs to keep turning.
    if fleet_registry.is_gated("wheel_bot", bot.regime, bot.vix) and not will_write_cc:
        logger.info(f"    [GATE] {ticker:<4} | No new puts — VIX {bot.vix:.1f} / Regime: {bot.regime}")
        return

    confidence = target_map.get(ticker, 0.5)
    dynamic_otm = TARGET_OTM_PCT * (1.5 - confidence)

    logger.info(f"  {ticker:<4} | ${current_stock_price:>7.2f} | Conf: {confidence:.2f} | Target OTM: {dynamic_otm*100:.1f}%")

    # Re-fetch per ticker: an earlier put this cycle consumes options BP
    acc = bot.trading_client.get_account()
    real_bp = float(acc.options_buying_power)

    # Covered Call?
    if stock_qty >= 100:
        side = "CALL"
        contract = find_best_contract(ticker, "CALL", current_stock_price, dynamic_otm)
    # Cash Secured Put?
    else:
        if real_bp < (current_stock_price * 100):
            logger.warning(f"    [SKIP] Insufficient BP (Need ${current_stock_price*100:.0f})")
            return
        side = "PUT"
        contract = find_best_contract(ticker, "PUT", current_stock_price, dynamic_otm)

    if not contract:
        logger.info(f"    [SKIP] No suitable {side} contract found within {MIN_DTE}-{MAX_DTE} DTE for {ticker}.")
        return

    quote = get_option_data(contract.symbol)
    if not quote:
        logger.info(f"    [SKIP] Failed to fetch quote for {contract.symbol}")
        return

    limit_price = calculate_smart_price(quote, side)
    if limit_price is None:
        return

    if limit_price < MIN_PREMIUM:
        logger.info(f"    [SKIP] Premium too low (${limit_price:.2f} < ${MIN_PREMIUM:.2f})")
        return

    if side == "PUT" and real_bp < (float(contract.strike_price) * 100):
        logger.warning(f"    [SKIP] Strike too expensive for available BP.")
        return

    new_trade_collateral = float(contract.strike_price) * 100 if side == "PUT" else 0
    # Only collateral-adding trades are budget-gated: an assigned
    # lot already sits in total_commitment, and blocking its
    # covered call would freeze the wheel exactly when it needs
    # to keep turning.
    if new_trade_collateral > 0 and total_commitment + new_trade_collateral > my_budget:
        logger.warning(f"    [SKIP] Total commitment (${total_commitment:.0f} + ${new_trade_collateral:.0f}) > Budget (${my_budget:.0f}).")
        return

    logger.info(f"    [ENTRY] Selling {side} on {ticker} @ ${limit_price} (Midpoint)")
    req = LimitOrderRequest(
        symbol=contract.symbol,
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        client_order_id=f"wheel_bot-{contract.symbol}-{int(time.time())}"
    )
    utils.submit_and_log_order(bot.trading_client, req, logger)
    emoji = "🟢" if side == "CALL" else "🔴"
    bot.notify(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}\nConf: {confidence:.2f}")


def cycle(bot):
    raw_targets = bot.targets("wheel_targets")
    target_map = {sym: d.get("confidence", 0.5) for sym, d in raw_targets.items()}
    clean_targets = list(target_map)

    busy_tickers = get_busy_tickers()
    logger.info(f"Scanning {len(clean_targets)} Targets (Busy: {len(busy_tickers)})")

    force_close_list = bot.bot_settings.get("force_close_symbols", [])
    force_roll_list = bot.bot_settings.get("force_roll_symbols", [])

    # --- 1. MANAGE EXISTING OPTIONS (wheel-owned only) ---
    wheel_option_positions = [
        p for p in bot.positions
        if p.asset_class == AssetClass.US_OPTION
        and utils.get_bot_owner(p.symbol, p.asset_class, bot.trading_client) == "wheel_bot"
    ]

    managed_option_tickers = set()
    for active_option in wheel_option_positions:
        # Extract ticker root from OCC symbol (e.g. AAPL260626P00140000 -> AAPL)
        ticker = active_option.symbol
        for i, char in enumerate(active_option.symbol):
            if char.isdigit():
                ticker = active_option.symbol[:i]
                break
        managed_option_tickers.add(ticker)

        if ticker in busy_tickers:
            logger.info(f"  {ticker:<4} | [SKIP] Open Order Exists on managed option.")
            continue

        manage_option(active_option, ticker, busy_tickers, target_map,
                      force_close_list, force_roll_list)

    # --- 2. OPEN NEW POSITIONS ---
    # Wheel-owned stock (assignments resolve to wheel_bot via the
    # option-root ownership claim) must get calls written against it
    # even when the ticker has dropped off today's scout list — that
    # IS the wheel. Everything else about the entry path applies.
    owned_stock = [
        p.symbol for p in bot.positions
        if p.asset_class == AssetClass.US_EQUITY and float(p.qty) >= 100
        and utils.get_bot_owner(p.symbol, p.asset_class, bot.trading_client) == "wheel_bot"
    ]
    cc_extras = [t for t in owned_stock if t not in clean_targets]
    if cc_extras:
        logger.info(f"Covered-call candidates from owned stock: {cc_extras}")

    # One budget number per cycle (effective -> cfo base -> 0.0 fail-closed)
    my_budget = utils.get_budget_dollars("wheel_bot", bot.trading_client, equity=bot.equity)

    for ticker in clean_targets + cc_extras:
        try_open_position(ticker, target_map, managed_option_tickers,
                          busy_tickers, my_budget)


if __name__ == "__main__":
    bot.notify("🚜 **Wheel Bot Online**\nRunner: fleet_bot | Ladder + expiry backstop active.")
    bot.run(cycle)
