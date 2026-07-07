from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, OptionLatestQuoteRequest
import time
import datetime
import requests
import json
import os
import math
import re
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, GetOptionContractsRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, ContractType, QueryOrderStatus
import config
import utils
from logger import get_logger, registry

# --- LOGGING ---
logger = get_logger("wheel_bot")

# --- CONFIGURATION ---
TARGET_FILE = "active_targets.json"

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

# --- CLIENTS ---
trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
option_data_client = OptionHistoricalDataClient(config.API_KEY, config.SECRET_KEY)

def send_discord(msg):
    if "YOUR" in config.WEBHOOK_WHEEL: return
    try:
        payload = {"content": msg, "username": "WheelBot 🚜"}
        requests.post(config.WEBHOOK_WHEEL, json=payload)
    except Exception as e:
        registry.log_error("wheel_bot", "send_discord", e, context=msg[:50])
        logger.error(f"Discord webhook failed: {e}")

def get_wheel_targets():
    return utils.load_and_validate_targets(
        TARGET_FILE, 
        "wheel_targets", 
        {}
    )

def get_current_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        res = data_client.get_stock_latest_trade(req)
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
            order = utils.submit_and_log_order(trading_client, req, logger, reason=reason)
        except Exception as e:
            logger.error(f"    [CLOSE ABORTED] Submit failed for {sym}: {e}")
            break
        filled = False
        for _ in range(int(CLOSE_POLL_SECONDS / 0.5)):
            time.sleep(0.5)
            o = trading_client.get_order_by_id(order.id)
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
            trading_client.cancel_order_by_id(order.id)
        except Exception:
            pass  # already terminal
        # Subtract whatever DID fill on this rung before escalating, so the next
        # rung re-submits only what's left. Re-sending the original qty after a
        # partial fill would over-close: buying back more contracts than are
        # still short and flipping the position net long.
        try:
            done = trading_client.get_order_by_id(order.id)
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
        send_discord(f"🚨 **CLOSE FAILING: {sym}**\nBuy-to-close unfilled through "
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
        contracts = trading_client.get_option_contracts(req)
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

def get_open_order_tickers():
    """
    Returns a set of tickers that currently have OPEN orders.
    Parses option symbols (e.g., AMD230120P...) back to AMD.
    """
    params = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
    orders = trading_client.get_orders(filter=params)
    busy_tickers = set()
    
    for o in orders:
        sym = o.symbol
        # Regex to strip the option suffix (e.g. "AAPL230616C00150000" -> "AAPL")
        # Matches the start of string until the first digit
        match = re.match(r"^([A-Z]+)\d", sym)
        if match:
            root = match.group(1)
            busy_tickers.add(root)
        else:
            # Regular stock order
            busy_tickers.add(sym)
            
    return busy_tickers

def get_my_budget():
    """Reads the CFO's effective budget for this bot."""
    try:
        with open("effective_budgets.json", "r") as f:
            budgets = json.load(f)
            return budgets.get("wheel_bot", 20000)
    except Exception as e:
        return 20000

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
        utils.submit_and_log_order(trading_client, open_req, logger)
        emoji = "🟢" if side == "CALL" else "🔴"
        send_discord(f"🌀 **ROLLED {ticker} {side}**\nClosed: {active_option.symbol} @ ${close_price}\nOpened: {new_contract.symbol} @ ${new_limit_price}")
        return True
    except Exception as e:
        logger.error(f"  [CRITICAL ROLL ERROR] Closed {active_option.symbol} but failed to open new contract {new_contract.symbol}! Position is now flat. Error: {e}")
        send_discord(f"🚨 **CRITICAL ROLL FAILURE: {ticker}**\nClosed position but failed to re-open next leg! Position is flat. Check Alpaca manual execution immediately.")
        registry.log_error("wheel_bot", "roll_critical", e, context=f"flat_on_roll_{ticker}")
        return False

def run_wheel_bot():
    logger.info("--- 🚜 FLEET WHEEL BOT (Smart Pricing + Order Awareness) STARTED ---")
    send_discord(f"🚜 **Wheel Bot Online**\nSyncing with Sector Scout...")
    
    while True:
        try:
            try:
                clock = trading_client.get_clock()
                if not clock.is_open:
                    logger.info(f"Market Closed. Sleeping 15m...")
                    time.sleep(900)
                    continue
            except Exception as e:
                registry.log_error("wheel_bot", "get_clock", e)
                logger.error(f"Failed to check market clock: {e}")

            try:
                with open("bot_config.json", 'r') as f:
                    bot_cfg = json.load(f)
                current_vix = bot_cfg.get('global_settings', {}).get('vix', 0)
                current_regime = bot_cfg.get('global_settings', {}).get('market_condition', 'UNKNOWN')
                capital_crunch = bot_cfg.get('global_settings', {}).get('CAPITAL_CRUNCH', False)
            except Exception as e:
                registry.log_error("wheel_bot", "read_config", e)
                logger.error(f"Failed to read bot_config.json: {e}")
                bot_cfg = {}  # wheel_settings below reads from this; a missing file must not NameError the loop
                current_vix = 0
                current_regime = 'UNKNOWN'
                capital_crunch = False

            raw_targets = get_wheel_targets()
            
            # --- PARSE & CONFIGURE (Phase 2) ---
            target_map = {} # symbol -> confidence
            clean_targets = []
            
            for symbol, data in raw_targets.items():
                confidence = data.get("confidence", 0.5)
                clean_targets.append(symbol)
                target_map[symbol] = confidence
            
            # [FIX] Get list of tickers that already have pending orders
            busy_tickers = get_open_order_tickers()
            
            logger.info(f"Scanning {len(clean_targets)} Targets (Busy: {len(busy_tickers)})")

            # Batch budget check outside the symbol loop to prevent log spam
            is_budget_ok = utils.check_budget("wheel_bot", trading_client)

            all_positions = trading_client.get_all_positions()

            # --- 2. MANAGE EXISTING OPTIONS (Wheel Bot Portfolio Ownership) ---
            # Filter option positions belonging to wheel_bot
            wheel_option_positions = []
            for p in all_positions:
                if p.asset_class == AssetClass.US_OPTION:
                    owner = utils.get_bot_owner(p.symbol, p.asset_class, trading_client)
                    if owner == "wheel_bot":
                        wheel_option_positions.append(p)

            # Keep track of tickers that currently have active options we managed
            managed_option_tickers = set()

            wheel_settings = bot_cfg.get("bots", {}).get("wheel_bot", {})
            force_close_list = wheel_settings.get("force_close_symbols", [])
            force_roll_list = wheel_settings.get("force_roll_symbols", [])

            for active_option in wheel_option_positions:
                # Extract ticker root from OCC symbol (e.g. AAPL260626P00140000 -> AAPL)
                ticker = active_option.symbol
                for i, char in enumerate(active_option.symbol):
                    if char.isdigit():
                        ticker = active_option.symbol[:i]
                        break
                
                managed_option_tickers.add(ticker)
                
                # Check if busy (has pending orders)
                if ticker in busy_tickers:
                    logger.info(f"  {ticker:<4} | [SKIP] Open Order Exists on managed option.")
                    continue

                # Parse details
                occ_part = active_option.symbol[len(ticker):]
                exp_yymmdd = occ_part[:6]
                opt_type = occ_part[6]
                strike_raw = occ_part[7:]
                strike = float(strike_raw) / 1000.0
                side = "PUT" if opt_type == 'P' else "CALL"
                
                # Calculate DTE
                try:
                    exp_date = datetime.datetime.strptime(exp_yymmdd, "%y%m%d").date()
                    today = datetime.date.today()
                    dte = (exp_date - today).days
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
                
                # A) TAKE PROFIT
                if capture_pct >= TAKE_PROFIT_PCT:
                    logger.info(f"    💵 [HARVEST] Profit Target Hit! Closing {active_option.symbol}")
                    quote = get_option_data(active_option.symbol)
                    close_price = calculate_smart_price(quote, "BUY")
                    
                    if close_price is None:
                        logger.warning(f"    [WAIT] Spread too wide to close safely.")
                        continue

                    req = LimitOrderRequest(
                        symbol=active_option.symbol,
                        qty=abs(int(qty)),
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                        limit_price=close_price,
                        client_order_id=f"wheel_bot-{active_option.symbol}-{int(time.time())}"
                    )
                    utils.submit_and_log_order(trading_client, req, logger)
                    send_discord(f"💵 **TOOK PROFIT {ticker}**\nClosed @ ${close_price} ({capture_pct*100:.0f}% Cap)")
                    continue

                # B) FORCE CLOSE FROM CONFIG — ladder pricing so a wide spread
                # can't park the manual lever (it used to skip with [WAIT]).
                if ticker in force_close_list:
                    logger.info(f"    🚨 [FORCE CLOSE] Config triggered close for {active_option.symbol}")
                    if close_option_position(active_option, "forcecls", "Force close (config)"):
                        send_discord(f"🚨 **FORCE CLOSED {active_option.symbol}**")
                    continue

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
                        send_discord(f"⏰ **EXPIRY CLOSE {active_option.symbol}**\n"
                                     f"ITM with {dte} DTE — closed before assignment.")
                    continue

                # D) STALE ROLL (DTE <= STALE_ROLL_DTE) OR FORCE ROLL
                is_stale = dte <= STALE_ROLL_DTE
                if ticker in force_roll_list or is_stale:
                    reason = f"DTE stale ({dte} days)" if is_stale else "Force roll config"
                    logger.info(f"    🌀 [ROLL] Rolling {active_option.symbol} due to: {reason}")

                    confidence = target_map.get(ticker, 0.5)
                    roll_option_position(ticker, active_option, current_stock_price, side, confidence)
                    continue

            # --- 3. OPEN NEW POSITIONS ---
            # Wheel-owned stock (assignments resolve to wheel_bot via the
            # option-root ownership claim) must get calls written against it
            # even when the ticker has dropped off today's scout list — that
            # IS the wheel. Everything else about the entry path applies.
            owned_stock = [
                p.symbol for p in all_positions
                if p.asset_class == AssetClass.US_EQUITY and float(p.qty) >= 100
                and utils.get_bot_owner(p.symbol, p.asset_class, trading_client) == "wheel_bot"
            ]
            cc_extras = [t for t in owned_stock if t not in clean_targets]
            if cc_extras:
                logger.info(f"Covered-call candidates from owned stock: {cc_extras}")

            for ticker in clean_targets + cc_extras:
                # [FIX] Skip if we already have an open order for this ticker
                if ticker in busy_tickers:
                    logger.info(f"  {ticker:<4} | [SKIP] Open Order Exists.")
                    continue

                # Skip if we already have an active option position for this ticker
                if ticker in managed_option_tickers:
                    continue

                stock_qty = 0
                # Check stock position — only OUR shares count toward covered
                # calls. Writing calls against another bot's stock turns into a
                # naked call the moment that bot exits (it doesn't know we
                # sold a call on its shares).
                for p in all_positions:
                    if p.symbol == ticker and p.asset_class == AssetClass.US_EQUITY:
                        if utils.get_bot_owner(p.symbol, p.asset_class, trading_client) == "wheel_bot":
                            stock_qty = float(p.qty)

                # Covered calls consume no new collateral and reduce risk on
                # stock we already hold, so they bypass the entry gates below.
                will_write_cc = stock_qty >= 100

                current_stock_price = get_current_price(ticker)
                if current_stock_price == 0.0:
                    continue

                if not is_budget_ok and not will_write_cc:
                    continue # Skip new entries if budget paused, but finish loop
                     
                # NEW: Budget Enforcement
                my_budget = get_my_budget()
                
                total_commitment = 0.0
                for p in all_positions:
                    owner = utils.get_bot_owner(p.symbol, p.asset_class, trading_client)
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

                if capital_crunch and not will_write_cc:
                    logger.info(f"    [GATE] {ticker:<4} | No new positions — CAPITAL_CRUNCH is active.")
                    continue

                # Gate new entries by Macro Environment (covered calls exempt:
                # selling a call on held stock lowers risk, and post-assignment
                # bear regimes are exactly when the wheel needs to keep turning)
                if (current_vix > 22 or current_regime in ('BEAR_TREND', 'CRITICAL_VOLATILITY')) and not will_write_cc:
                    logger.info(f"    [GATE] {ticker:<4} | No new puts — VIX {current_vix:.1f} / Regime: {current_regime}")
                    continue
                     
                confidence = target_map.get(ticker, 0.5)
                # Dynamic OTM
                dynamic_otm = TARGET_OTM_PCT * (1.5 - confidence)
                
                logger.info(f"  {ticker:<4} | ${current_stock_price:>7.2f} | Conf: {confidence:.2f} | Target OTM: {dynamic_otm*100:.1f}%")

                contract = None
                side = None

                acc = trading_client.get_account()
                real_bp = float(acc.options_buying_power)

                # Covered Call?
                if stock_qty >= 100:
                    side = "CALL"
                    contract = find_best_contract(ticker, "CALL", current_stock_price, dynamic_otm)
                
                # Cash Secured Put?
                else:
                    if real_bp < (current_stock_price * 100):
                        logger.warning(f"    [SKIP] Insufficient BP (Need ${current_stock_price*100:.0f})")
                        continue

                    side = "PUT"
                    contract = find_best_contract(ticker, "PUT", current_stock_price, dynamic_otm)

                if contract:
                    quote = get_option_data(contract.symbol)
                    if not quote: 
                        logger.info(f"    [SKIP] Failed to fetch quote for {contract.symbol}")
                        continue
                    
                    limit_price = calculate_smart_price(quote, side)
                    if limit_price is None: 
                        continue 
                    
                    if limit_price < MIN_PREMIUM:
                        logger.info(f"    [SKIP] Premium too low (${limit_price:.2f} < ${MIN_PREMIUM:.2f})")
                        continue
                    
                    if side == "PUT" and real_bp < (float(contract.strike_price) * 100):
                        logger.warning(f"    [SKIP] Strike too expensive for available BP.")
                        continue
                        
                    new_trade_collateral = float(contract.strike_price) * 100 if side == "PUT" else 0
                    # Only collateral-adding trades are budget-gated: an assigned
                    # lot already sits in total_commitment, and blocking its
                    # covered call would freeze the wheel exactly when it needs
                    # to keep turning.
                    if new_trade_collateral > 0 and total_commitment + new_trade_collateral > my_budget:
                        logger.warning(f"    [SKIP] Total commitment (${total_commitment:.0f} + ${new_trade_collateral:.0f}) > Budget (${my_budget:.0f}).")
                        continue
                        
                    logger.info(f"    [ENTRY] Selling {side} on {ticker} @ ${limit_price} (Midpoint)")
                    req = LimitOrderRequest(
                        symbol=contract.symbol,
                        qty=1,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price,
                        client_order_id=f"wheel_bot-{contract.symbol}-{int(time.time())}"
                    )
                    utils.submit_and_log_order(trading_client, req, logger)
                    emoji = "🟢" if side == "CALL" else "🔴"
                    send_discord(f"{emoji} **SOLD {side} {ticker}**\nStrike: ${contract.strike_price}\nLimit: ${limit_price}\nConf: {confidence:.2f}")
                else:
                    logger.info(f"    [SKIP] No suitable {side} contract found within {MIN_DTE}-{MAX_DTE} DTE for {ticker}.")
                    continue

            time.sleep(900)

        except Exception as e:
            registry.log_error("wheel_bot", "main_loop", e)
            logger.error(f"CRITICAL ERROR: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_wheel_bot()