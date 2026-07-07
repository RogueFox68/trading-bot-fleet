import json
import time
import re
import requests
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus
from logger import logger, registry
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config
import fleet_registry

# --- CENTRALIZED TRADE LOGGING ---
# Maps the bot tag embedded in client_order_id ('{bot}-{symbol}-{ts}') to its
# InfluxDB measurement. Derived from fleet_registry; retired tags (condor)
# stay mapped so historical orders never misattribute to another measurement.
MEASUREMENT_BY_BOT = {
    name: cfg["measurement"] for name, cfg in fleet_registry.BOTS.items()
}
MEASUREMENT_BY_BOT.update(fleet_registry.RETIRED_BOT_MEASUREMENTS)

def _log_fill_to_influx(order, logger, reason="", action=None):
    """Write a trade row to InfluxDB from a CONFIRMED Alpaca fill.

    Measurement is derived from the bot tag in client_order_id
    ('{bot}-{symbol}-{ts}'). Never writes for an unconfirmed order, so
    expired/pending/rejected orders produce no row (kills phantom logs).
    """
    try:
        if order is None or getattr(order, "filled_avg_price", None) is None:
            return  # not filled -> do not log
        c_id = order.client_order_id or ""
        bot = next((b for b in MEASUREMENT_BY_BOT if c_id.startswith(f"{b}-")), None)
        measurement = MEASUREMENT_BY_BOT.get(bot, "trades")
        act = action or ("buy" if order.side == OrderSide.BUY else "sell")
        # Use the broker fill time, not now() — also fixes time skew. Floored
        # to the millisecond: get_order_by_id and get_orders serialize
        # filled_at with sub-µs differences, so raw-ns stamps made the
        # submit-time write and the reconciled write land ~1µs apart as
        # duplicate points instead of overwriting one another (inflating
        # SUM(qty)). Same-symbol fills are never <1ms apart for these bots.
        if getattr(order, "filled_at", None):
            ns = int(order.filled_at.timestamp() * 1000) * 1_000_000
        else:
            ns = time.time_ns()
        reason_field = f',reason="{reason}"' if reason else ""
        line = (f'{measurement},symbol={order.symbol} '
                f'price={float(order.filled_avg_price)},action="{act}",'
                f'qty={float(order.filled_qty)}{reason_field} {ns}')
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=line, timeout=2)
        if r.status_code != 204:
            logger.warning(f"InfluxDB trade write failed: {r.status_code} {r.text}")
    except Exception as e:
        logger.warning(f"InfluxDB trade write error: {e}")

# --- SAFETY LIMITS ---
MAX_DAILY_LOSS = -5000.0
MAX_SYMBOL_EXPOSURE = 5000.0
MAX_ORDER_NOTIONAL = 20000.0

if getattr(config, "PAPER", True):
    logger.info("="*50)
    logger.info("[!!! PAPER TRADING MODE ACTIVE !!!]")
    logger.info("="*50)
else:
    logger.info("="*50)
    logger.info("[!!! LIVE TRADING DANGER !!!]")
    logger.info("="*50)

# --- CENTRALIZED ASSET MAP ---
# Static baseline ownership claims, derived from fleet_registry
# (crypto tickers only; equities/options resolve dynamically from order tags)
BOT_MAPPING = {
    name: list(cfg["static_symbols"]) for name, cfg in fleet_registry.BOTS.items()
}

# Cache to avoid reading the file on every position in the loop
_ownership_cache = {}
_ownership_cache_time = 0

# Bot tags that can appear in a client_order_id ('{bot}-{symbol}-{ts}').
# Includes retired tags (condor) that still exist on historical orders.
_KNOWN_BOT_TAGS = list(fleet_registry.BOTS) + list(fleet_registry.RETIRED_BOT_MEASUREMENTS)

# OCC option symbol: root + YYMMDD + P/C + strike*1000 (e.g. PAAS260702P00048000)
_OCC_SYMBOL_RE = re.compile(r"^([A-Z]{1,6})\d{6}[PC]\d{8}$")

def _fetch_orders_covering(trading_client, held_symbols=None, require_fill=False,
                           page_size=500, max_orders=None, status="all", after=None):
    """Fetch recent Alpaca orders newest-first, paging past the 500-order cap.

    A single GetOrdersRequest is capped at limit=500. A high-velocity bot
    (crypto_grid submits/cancels dozens–hundreds of orders a day) can flood that
    window so an opening order for an aged equity position scrolls out of it.
    When that happens both ownership resolution and entry-time lookup lose the
    position: get_bot_owner falls through to its default and get_position_entry_times
    returns nothing, so stop-loss / max-hold protection silently stops firing
    (the GEN/APTV orphaning bug).

    We page backwards via `until` (submitted_at) so coverage no longer depends on
    order velocity. When `held_symbols` is given we stop as soon as every held
    symbol is covered (a filled order too, if `require_fill`), keeping the common
    case to a single page; only a genuinely aged position pages deep. Returns a
    de-duplicated list, newest-first.

    `status` and `after` are passed straight through to each page request, so a
    caller with no coverage target (held_symbols=None) can sweep a whole time
    window — e.g. reconcile_fills paging every CLOSED order back to `after`,
    rather than trusting one bounded limit=500 read behind a crypto flood.
    """
    if max_orders is None:
        # With a coverage target we can stop early, so page deep enough to reach
        # a weeks-old opening order behind a crypto flood. Without one, just take
        # a modest recent window (callers that need guaranteed coverage pass
        # held_symbols / prime the cache).
        max_orders = 12000 if held_symbols else 1500

    orders = []
    seen_ids = set()
    remaining = set(held_symbols) if held_symbols else None
    until = None
    while len(orders) < max_orders:
        try:
            kwargs = {"status": status, "limit": page_size}
            if after is not None:
                kwargs["after"] = after
            if until is not None:
                kwargs["until"] = until
            page = trading_client.get_orders(filter=GetOrdersRequest(**kwargs))
        except Exception as e:
            registry.log_error("utils", "fetch_orders_paginated", e)
            logger.error(f"  [Orders] paginated order fetch failed: {e}")
            break
        if not page:
            break

        oldest = None
        added = 0
        for o in page:
            oid = getattr(o, "id", None)
            if oid is not None and oid in seen_ids:
                continue
            if oid is not None:
                seen_ids.add(oid)
            orders.append(o)
            added += 1
            sub = getattr(o, "submitted_at", None)
            if sub is not None and (oldest is None or sub < oldest):
                oldest = sub
            if remaining is not None and o.symbol in remaining:
                if not require_fill or getattr(o, "filled_at", None) is not None:
                    remaining.discard(o.symbol)

        # Every held symbol covered -> done (the common, cheap case).
        if remaining is not None and not remaining:
            break
        # Short read (last page) or no forward progress -> stop.
        if len(page) < page_size or oldest is None or oldest == until or added == 0:
            break
        until = oldest

    return orders

def _build_order_based_map(trading_client, held_symbols=None):
    """
    Builds a symbol -> bot_name lookup from Alpaca order history.
    Parses the custom client_order_id (e.g., 'survivor_bot-AAPL-170...').
    Cached for 60 seconds to avoid API spam. When `held_symbols` is provided the
    cache is only reused if it already covers every one of those symbols, so an
    aged position can't be served a stale, incomplete map.
    """
    import time
    global _ownership_cache, _ownership_cache_time

    now = time.time()
    if _ownership_cache and (now - _ownership_cache_time) < 60:
        if not held_symbols or all(s in _ownership_cache for s in held_symbols):
            return _ownership_cache

    owner_map = {}

    # 1. Load static mapping as absolute baseline
    for bot_name, symbols in BOT_MAPPING.items():
        for sym in symbols:
            owner_map[sym] = bot_name

    # 2. Walk order history (paged for full coverage) for definitive tags.
    #    Newest-first: the first tagged order for a symbol wins and overrides the
    #    static baseline, so the most recent owner sticks.
    order_set = set()
    root_claims = {}
    orders = _fetch_orders_covering(trading_client, held_symbols=held_symbols)
    for o in orders:
        # client_order_id format: "{bot_name}-{symbol}-{timestamp}"
        c_id = o.client_order_id
        if not c_id:
            continue
        bot = next((b for b in _KNOWN_BOT_TAGS if c_id.startswith(f"{b}-")), None)
        if bot is None:
            continue
        if o.symbol not in order_set:
            owner_map[o.symbol] = bot
            order_set.add(o.symbol)
        # A tagged OPTION order is also a (weaker) claim on its underlying:
        # assignment/exercise turns the contract into bare stock that never
        # gets a tagged order of its own, so without the root claim that
        # stock resolves to nobody (the PAAS assignment-liquidation bug).
        # Retired bots don't claim roots — nothing manages their positions.
        occ = _OCC_SYMBOL_RE.match(o.symbol or "")
        if occ and bot in fleet_registry.BOTS and occ.group(1) not in root_claims:
            root_claims[occ.group(1)] = bot

    # Direct symbol tags always outrank root inference: an explicit order on
    # the bare symbol is an unambiguous claim; the root claim only covers
    # shares that appeared without one (assignment/exercise).
    for root, bot in root_claims.items():
        owner_map.setdefault(root, bot)

    _ownership_cache = owner_map
    _ownership_cache_time = now
    return owner_map

def prime_ownership(trading_client, held_symbols):
    """Warm the 60s ownership cache with full coverage for `held_symbols`.

    Bots call this once per cycle after fetching positions. Subsequent
    get_bot_owner() lookups (which don't take held_symbols) then resolve even
    weeks-old positions from the primed cache instead of a bounded window.
    """
    return _build_order_based_map(trading_client, held_symbols=held_symbols)

def get_bot_owner(symbol, asset_class, trading_client):
    """Determines which bot owns a specific position based on order history.

    Resolution: crypto -> crypto_grid; options -> tag (contract or root), else
    wheel_bot; stocks -> tag, else root inferred from a bot's own option
    orders (assigned/exercised stock), else None. None means NO bot manages
    the position — callers compare against their own name, so an unowned
    position simply drops out of every bot's holdings (quarantined) while the
    accountant's orphan sweep keeps alerting on it.
    """
    # 1. Crypto Rules
    if asset_class == AssetClass.CRYPTO:
        return "crypto_grid"
    
    # 2. Build dynamic map from order history
    owner_map = _build_order_based_map(trading_client)
    
    # 3. Options Rules — extract root symbol if contract string
    if asset_class == AssetClass.US_OPTION:
        root = symbol
        for i, char in enumerate(symbol):
            if char.isdigit():
                root = symbol[:i]
                break

        # Check dynamic map for the specific contract OR the root.
        # Historical condor orders still resolve to condor_bot via the tag;
        # untagged options default to wheel_bot (the only live options bot
        # since condor retired).
        owner = owner_map.get(symbol) or owner_map.get(root)
        if owner in ("wheel_bot", "condor_bot"):
            return owner

        return owner if owner else "wheel_bot"
    
    # 4. Stock/ETF Rules — check dynamic map for exact symbol (includes roots
    #    inferred from a bot's own option orders, e.g. assigned stock -> wheel)
    owner = owner_map.get(symbol)
    if owner:
        return owner

    # 5. No tag anywhere (manual order, or an opening order gone from history
    #    entirely). Do NOT hand it to a bot: a default owner lets a strategy
    #    manage and liquidate shares it never bought (trend_bot dumped
    #    wheel-assigned PAAS on 2026-07-06 through exactly this path). Leave
    #    it unowned — no bot trades it — and surface it; the accountant's
    #    orphan sweep alerts until a human resolves it.
    _note_ownership_fallback(symbol)
    return None

# Throttle so a persistently-untagged symbol doesn't spam the log/metric each cycle.
_fallback_log_times = {}

def _note_ownership_fallback(symbol):
    """Surface an untagged equity that no bot owns.

    Logs a throttled warning (once / 10 min per symbol) and writes an
    'ownership_fallback' InfluxDB metric, so an unowned position is visible
    (and stays visible) instead of being silently absorbed by a default owner.
    """
    now = time.time()
    if now - _fallback_log_times.get(symbol, 0) < 600:
        return
    _fallback_log_times[symbol] = now
    logger.warning(f"  [Ownership] {symbol}: no bot tag in order history — "
                   f"leaving unowned (no bot will trade it; see orphan sweep).")
    try:
        line = f'ownership_fallback,symbol={symbol} count=1i {int(now * 1e9)}'
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=line, timeout=2)
        if r.status_code != 204:
            logger.warning(f"  [Ownership] fallback metric write failed: {r.status_code}")
    except Exception as e:
        logger.warning(f"  [Ownership] fallback metric write error: {e}")

def get_position_entry_times(trading_client, held_symbols=None):
    """
    Best-effort {symbol: entry_datetime_utc} for held positions, taken from the
    most recent FILLED order per symbol. For bots that do full entries/exits
    (trend, survivor), the latest fill on a symbol you still hold is its entry,
    so this is a good proxy for hold duration.

    Order history is paged (not a single limit=500 call) so a position whose
    opening fill has scrolled far behind a crypto-order flood still resolves.
    Pass `held_symbols` to guarantee coverage of exactly the positions you hold
    while keeping the common case to one page. Returns {} on error; callers treat
    a missing symbol as 'unknown duration'.
    """
    entry_times = {}
    try:
        orders = _fetch_orders_covering(trading_client, held_symbols=held_symbols,
                                        require_fill=True)
        for o in orders:
            filled_at = getattr(o, "filled_at", None)
            if filled_at is None:
                continue
            prev = entry_times.get(o.symbol)
            if prev is None or filled_at > prev:
                entry_times[o.symbol] = filled_at
    except Exception as e:
        registry.log_error("utils", "get_position_entry_times", e)
    return entry_times

def check_budget_details(bot_name, trading_client):
    """
    Returns (is_ok, budget_dollars, total_used)
    """
    import os
    try:
        account = trading_client.get_account()
        equity = float(account.equity)
        budget_dollars = 0.0
        
        # 1. Try fetching Dynamic Effective Budget
        if os.path.exists("effective_budgets.json"):
            try:
                with open("effective_budgets.json", "r") as f:
                    effective = json.load(f)
                    if bot_name in effective:
                        budget_dollars = float(effective[bot_name])
            except Exception as e:
                registry.log_error("utils", "parse_budget", e)
                logger.error(f"  [CFO] Dynamic budget parse error: {e}")
                
        # 2. Fallback when the CFO hasn't written an effective budget yet:
        #    derive from cfo_settings.base_allocations on allocatable equity
        #    (equity minus the unallocated reserve) — the same formula the
        #    reallocator uses — so this path can't drift from the CFO
        #    baseline. Legacy bots{}.allocation only covers bots that sit
        #    outside cfo_settings.
        if budget_dollars == 0.0:
            if not os.path.exists("bot_config.json"):
                logger.error("  [CFO] FAIL-CLOSED: bot_config.json missing!")
                return False, 0.0, 0.0

            with open("bot_config.json", "r") as f:
                config_data = json.load(f)
            cfo = config_data.get("cfo_settings", {})
            base = cfo.get("base_allocations", {})
            if bot_name in base:
                reserve_pct = float(cfo.get("unallocated_reserve", 0.0))
                allocation_pct = float(base[bot_name]) * (1.0 - reserve_pct)
            else:
                allocation_pct = config_data.get("bots", {}).get(bot_name, {}).get("allocation", 0.0)

            if allocation_pct == 0.0:
                logger.error(f"  [CFO] FAIL-CLOSED: No allocation configured for {bot_name}")
                return False, equity, 0.0 # No limit set -> Reject
            budget_dollars = equity * allocation_pct
        
        # 4. Calculate Current Usage
        positions = trading_client.get_all_positions()
        current_used = 0.0
        
        for p in positions:
            owner = get_bot_owner(p.symbol, p.asset_class, trading_client)
            
            # Special Case: Crypto Grid and Moon Bot share assets
            if bot_name in ["crypto_grid", "moon_bot"] and owner == "crypto_grid":
                current_used += abs(float(p.market_value))
            elif owner == bot_name:
                if p.asset_class == AssetClass.US_OPTION:
                    # Options: use cost basis or absolute market value as "capital at risk"
                    current_used += abs(float(p.market_value))
                else:
                    current_used += abs(float(p.market_value))

        # 5. Calculate Pending Order Usage
        open_orders = trading_client.get_orders(filter=GetOrdersRequest(status="open"))
        pending_used = 0.0
        
        for o in open_orders:
            # Check if this order originated from this specific bot
            is_my_order = False
            if o.client_order_id and o.client_order_id.startswith(f"{bot_name}-"):
                is_my_order = True
            
            # If the bot placed the order, we need to account for the capital lock
            if is_my_order:
                unfilled_qty = float(o.qty) - float(o.filled_qty)
                if unfilled_qty <= 0: continue
                
                # Equities (Buy Orders)
                if o.asset_class == AssetClass.US_EQUITY and o.side == OrderSide.BUY:
                    if o.limit_price:
                        pending_used += unfilled_qty * float(o.limit_price)
                        
                # Options (Sell to Open / CSPs / CCs)
                elif o.asset_class == AssetClass.US_OPTION and o.side == OrderSide.SELL:
                    # Parse strike price from standard OCC symbol (e.g., AAPL240119P00150000)
                    # The last 8 characters represent the strike price multiplied by 1000
                    try:
                        strike_str = o.symbol[-8:] 
                        strike_price = float(strike_str) / 1000.0
                        
                        # Options are 100 shares per contract
                        pending_used += unfilled_qty * strike_price * 100.0
                    except Exception as e:
                        registry.log_error("utils", "parse_strike", e, context=o.symbol)
                        logger.error(f"  [CFO] Error parsing strike from {o.symbol}: {e}")

        total_used = current_used + pending_used
        available = budget_dollars - total_used
        
        if pending_used > 0:
            logger.info(f"  [CFO] {bot_name}: Used ${total_used:.0f} [Pos: ${current_used:.0f} | Pend: ${pending_used:.0f}] / Limit: ${budget_dollars:.0f} (Left: ${available:.0f})")
        else:
            logger.info(f"  [CFO] {bot_name}: Used ${total_used:.0f} / ${budget_dollars:.0f} (Left: ${available:.0f})")
        
        return available > 0, budget_dollars, total_used

    except Exception as e:
        registry.log_error("utils", "check_budget", e)
        logger.error(f"  [CFO] Budget Check Error: {e}")
        return True, 0.0, 0.0

def check_budget(bot_name, trading_client):
    """
    Returns True if the bot is under its dynamically allocated or static budget.
    """
    is_ok, _, _ = check_budget_details(bot_name, trading_client)
    return is_ok

def get_available_budget(bot_name, trading_client):
    """
    Returns the remaining budget in dollars for the bot.
    """
    is_ok, budget_dollars, total_used = check_budget_details(bot_name, trading_client)
    return max(0.0, budget_dollars - total_used)

def submit_and_log_order(trading_client, order_data, logger, reason="", log_action=None):
    """
    Submits an order and polls for a few seconds to log fill-confirmation.
    Includes fail-closed safety checks before execution.

    On a confirmed market-order fill, writes the authoritative trade row to
    InfluxDB (real fill price/qty/time) via _log_fill_to_influx. Limit orders
    return pending and are intentionally not logged here.
    """
    import time
    try:
        # --- SAFETY GATES ---
        try:
            account = trading_client.get_account()
            
            # 1. Daily Loss Cap Check
            if account.last_equity and account.equity:
                today_pnl = float(account.equity) - float(account.last_equity)
                if today_pnl < MAX_DAILY_LOSS:
                    raise Exception(f"Daily Loss Cap Exceeded! PnL: ${today_pnl:.2f} < Limit: ${MAX_DAILY_LOSS:.2f}")

            # 2+3. Notional & Symbol Exposure Caps — applied to any EQUITY
            # order that OPENS or INCREASES exposure: buys beyond a held short
            # (long entries) and sells beyond a held long (short entries).
            # Orders that only reduce an existing position are risk-reducing
            # and exempt (a buy-to-cover must never be blocked by a cap).
            # Crypto is exempt (grid trades $50 slices; the stock price lookup
            # can't quote it anyway); options keep their own collateral/BP
            # gates in the bots.
            symbol = getattr(order_data, 'symbol', None)
            qty_attr = getattr(order_data, 'qty', None)
            is_option = bool(re.match(r"^[A-Z]{1,6}\d{6}[PC]\d{8}$", symbol or ""))
            is_crypto = "/" in (symbol or "")
            if symbol and qty_attr and not is_option and not is_crypto:
                qty = float(qty_attr)

                # Approximate value for safety checks
                est_price = 0.0
                if getattr(order_data, 'limit_price', None):
                    est_price = float(order_data.limit_price)
                else:
                    # For market orders, fetch a rough current price from Alpaca
                    from alpaca.data.requests import StockLatestTradeRequest
                    from alpaca.data.historical import StockHistoricalDataClient
                    try:
                        dc = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
                        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
                        res = dc.get_stock_latest_trade(req)
                        est_price = float(res[symbol].price)
                    except Exception as e:
                        logger.warning(f"  [SAFETY] price lookup failed for {symbol}: {e}")

                positions = trading_client.get_all_positions()
                held_qty = sum(float(p.qty) for p in positions if p.symbol == symbol)  # signed: long > 0, short < 0

                if order_data.side == OrderSide.BUY:
                    # Buying while short covers up to |held_qty| before opening long
                    opening_qty = qty if held_qty >= 0 else max(0.0, qty + held_qty)
                else:
                    # Selling while long closes up to held_qty before opening short
                    opening_qty = qty if held_qty <= 0 else max(0.0, qty - held_qty)

                if opening_qty > 0 and est_price > 0:
                    notional = opening_qty * est_price
                    if notional > MAX_ORDER_NOTIONAL:
                        raise Exception(f"Order Notional Cap Exceeded! Notional: ${notional:.2f} > Limit: ${MAX_ORDER_NOTIONAL:.2f}")

                    symbol_exposure = sum(
                        abs(float(p.market_value))
                        for p in positions
                        if p.symbol == symbol
                    )
                    projected_exposure = symbol_exposure + notional
                    if projected_exposure > MAX_SYMBOL_EXPOSURE:
                        raise Exception(f"Symbol Exposure Cap Exceeded for {symbol}! Projected: ${projected_exposure:.2f} > Limit: ${MAX_SYMBOL_EXPOSURE:.2f}")
                        
        except Exception as gate_err:
            logger.error(f"  [SAFETY GATE TRIGGERED] Order Blocked: {gate_err}")
            raise gate_err

        # --- SUBMIT ORDER ---
        order = trading_client.submit_order(order_data)
        logger.info(f"  [ORDER SUBMITTED] ID: {order.id} | Symbol: {order.symbol} | Side: {order.side} | Qty: {order.qty} | Status: {order.status.value} | Type: {order.type.value}")
        
        # Check if market order, poll for fill
        is_market = isinstance(order_data, MarketOrderRequest)
        
        if is_market:
            # Poll up to 5s (10 * 0.5s) for the FULL fill. A market order can fill
            # across several prints (partial fills); we must wait for the cumulative
            # fill and log filled_qty/filled_avg_price once. Logging on the first
            # 'partially_filled' poll records only the first print and drops the rest.
            max_attempts = 10
            for attempt in range(max_attempts):
                time.sleep(0.5)
                updated_order = trading_client.get_order_by_id(order.id)
                status = updated_order.status.value
                if status == "filled":
                    logger.info(f"  [FILL CONFIRMED] ID: {updated_order.id} | Status: {status} | Fill Price: ${updated_order.filled_avg_price} | Qty: {updated_order.filled_qty}")
                    _log_fill_to_influx(updated_order, logger, reason=reason, action=log_action)
                    return updated_order
                elif status in ["canceled", "expired", "rejected"]:
                    logger.warning(f"  [ORDER FAILED] ID: {updated_order.id} | Status: {status} | Filled: {updated_order.filled_qty}")
                    # Terminal with a partial fill: log it here. These never reach a
                    # 'filled' status, so reconcile_fills() skips them (no filled_at)
                    # and this is the only capture. The helper no-ops when nothing
                    # filled, so a zero-fill cancel writes no row.
                    _log_fill_to_influx(updated_order, logger, reason=reason, action=log_action)
                    return updated_order
                # 'partially_filled' / 'new' / 'accepted' / 'pending_new' -> keep polling

            # Poll window elapsed without a full fill. Do NOT log a partial here: the
            # order can still complete, and reconcile_fills() will capture the final
            # cumulative fill from Alpaca. A now()-stamped partial would double-count
            # against that reconciled row (which is keyed on the broker filled_at).
            logger.info(f"  [ORDER PENDING] ID: {updated_order.id} | Status: {updated_order.status.value} | Filled: {updated_order.filled_qty}/{updated_order.qty}")
            return updated_order
        else:
            limit_price_str = f" @ ${order.limit_price}" if hasattr(order, 'limit_price') and order.limit_price else ""
            logger.info(f"  [LIMIT ORDER PENDING] ID: {order.id}{limit_price_str}")
            return order
    except Exception as e:
        logger.error(f"  [ORDER SUBMIT ERROR] Failed to submit order: {e}")
        raise e
    
    # utils.py - Add this function at the bottom

def load_and_validate_targets(file_path, strategy_key, static_fallback):
    import os
    import time
    from datetime import datetime, timezone
    
    # 1. Resolve Path
    search_paths = [
        file_path,
        os.path.join(os.path.dirname(__file__), file_path),
        os.path.join(os.path.expanduser("~"), "bots", "repo", file_path),
        os.path.join("..", file_path),
    ]
    
    final_path = None
    for p in search_paths:
        if os.path.exists(p):
            final_path = p
            logger.info(f"  [Utils] Found targets at: {p}")
            break
            
    if not final_path:
        logger.warning(f"  [Warning] Target file {file_path} NOT FOUND in search paths. Using Static Fallback.")
        return static_fallback
    
    try:
        with open(final_path, 'r') as f:
            data = json.load(f)
            
            # Version & Schema Check
            version = data.get("version")
            if version != "1.1":
                logger.warning(f"  [Warning] Unrecognized schema version: {version}. Using Static Fallback.")
                return static_fallback
                
            # Timezone-aware timestamp check
            updated_str = data.get("updated")
            if not updated_str:
                logger.warning("  [Warning] Missing 'updated' timestamp. Using Static Fallback.")
                return static_fallback
                
            try:
                # Parse ISO-8601 (e.g. 2026-06-08T08:15:00Z)
                updated_str = updated_str.replace("Z", "+00:00")
                updated_dt = datetime.fromisoformat(updated_str)
                now_dt = datetime.now(timezone.utc)
                age_seconds = (now_dt - updated_dt).total_seconds()
                
                if age_seconds > 86400:
                    logger.warning(f"  [Warning] Targets are stale ({age_seconds/3600:.1f} hours old). Using Static Fallback.")
                    return static_fallback
            except Exception as e:
                registry.log_error("utils", "parse_timestamp", e)
                logger.warning(f"  [Warning] Invalid timestamp format: {updated_str}. Using Static Fallback.")
                return static_fallback
                
            # Success Check
            scan_status = data.get("status", "unknown")
            if scan_status != "success":
                logger.warning(f"  [Warning] Scan status not 'success' ({scan_status}). Using Static Fallback.")
                return static_fallback
                
            # Extract category
            targets = data.get(strategy_key)
            
            if targets is None or not targets:
                logger.info(f"  [Info] {strategy_key} empty, but scan SUCCESS. Standby Mode (No Fallback).")
                return {} # Dictionary format now!
                
            return targets
            
    except Exception as e:
        registry.log_error("utils", "load_and_validate", e, context=file_path)
        logger.error(f"  [Error] Failed to read/validate target file: {e}. Using Static Fallback.")
        return static_fallback

# Bots whose fills are reconciled from Alpaca's order feed. Crypto bots are
# intentionally excluded (registry reconciled=False): their action vocabulary
# (grid_buy / grid_sweep / buy_breakout ...) can't be reconstructed from an
# order, and a generic buy/sell would clobber it on rewrite.
RECONCILED_BOTS = tuple(
    name for name, cfg in fleet_registry.BOTS.items() if cfg["reconciled"]
)

# Safety cap on how many closed orders reconcile_fills will page through in one
# cycle. `after` (the lookback window) is the natural stop; this only bounds a
# pathological flood so the accountant cycle can't run away. Hitting it is
# logged loudly (coverage may be incomplete that cycle).
RECONCILE_MAX_ORDERS = 10000

def _fill_action(order):
    """Best-effort trade action for a reconciled fill, from the order side/symbol.

    Options (OCC symbol) -> buy_close / sell_put / sell_call; equities -> buy/sell.
    Equity buy/sell matches what submit_and_log_order already logs, so a
    reconciled rewrite is idempotent rather than a relabel.
    """
    is_option = re.match(r"^[A-Z]{1,6}\d{6}([PC])\d{8}$", order.symbol or "")
    if order.side == OrderSide.BUY:
        return "buy_close" if is_option else "buy"
    if is_option:
        return "sell_put" if is_option.group(1) == "P" else "sell_call"
    return "sell"

def log_terminal_partial_fill(order, logger):
    """Log a TERMINAL order's partial fill as a first-class trade row.

    A close-ladder rung (wheel_bot.close_option_position) can partially fill and
    then be canceled. That partial carries no stable broker `filled_at`, so
    reconcile_fills' full-fill path skips it and the closed qty goes unlogged —
    under-counting realized P&L for a close spread across several partial rungs.

    GUARDRAILS (these keep it from double-counting the full-fill path):
      - status must be TERMINAL (canceled/expired/rejected). A terminal order can
        never later reach 'filled', so no future full-fill row can clash with
        this one. NEVER call this on an OPEN 'partially_filled' order — it may yet
        fully fill and be logged at filled_at, double-counting the partial.
      - filled_qty > 0 with a fill price.
      - filled_at is None: if the broker DID stamp a fill time, the full-fill path
        (_log_fill_to_influx) owns that row — we stay out so the same contracts
        can't land as two differently-stamped points.

    Idempotent: with no filled_at to key on, the row is stamped at a deterministic
    synthetic time — the terminal time (canceled/updated/submitted) floored to the
    ms plus id-derived jitter — so an inline write and any later reconcile re-run
    overwrite ONE point (the same trick as _write_option_event). Callers may fire
    both paths freely. Returns 1 if a row was written, else 0.
    """
    import zlib
    try:
        raw_status = getattr(order, "status", None)
        status = getattr(raw_status, "value", raw_status)
        if status not in ("canceled", "expired", "rejected"):
            return 0
        if getattr(order, "filled_at", None) is not None:
            return 0  # a broker fill stamp -> the full-fill path owns this order
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        price = getattr(order, "filled_avg_price", None)
        if filled_qty <= 0 or price is None:
            return 0

        c_id = getattr(order, "client_order_id", "") or ""
        bot = next((b for b in MEASUREMENT_BY_BOT if c_id.startswith(f"{b}-")), None)
        measurement = MEASUREMENT_BY_BOT.get(bot, "trades")

        # No filled_at exists; synthesize a STABLE stamp from the (immutable, once
        # terminal) order time so re-writes overwrite instead of duplicating.
        term_time = (getattr(order, "canceled_at", None)
                     or getattr(order, "updated_at", None)
                     or getattr(order, "submitted_at", None))
        base_ms = int(term_time.timestamp() * 1000) if term_time else int(time.time() * 1000)
        oid = str(getattr(order, "id", "") or c_id)
        ns = (base_ms + zlib.crc32(oid.encode()) % 1000) * 1_000_000

        action = _fill_action(order)
        line = (f'{measurement},symbol={order.symbol} '
                f'price={float(price)},action="{action}",qty={filled_qty},'
                f'fill_source="terminal_partial",source_order_id="{oid}" {ns}')
        url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
        r = requests.post(url, data=line, timeout=2)
        if r.status_code != 204:
            logger.warning(f"  [TerminalPartial] InfluxDB write failed: {r.status_code} {r.text}")
            return 0
        logger.info(f"  [TerminalPartial] {order.symbol} {action} qty={filled_qty} "
                    f"(order {oid}, {status})")
        return 1
    except Exception as e:
        registry.log_error("utils", "log_terminal_partial_fill", e,
                           context=getattr(order, "symbol", None))
        return 0

def reconcile_fills(trading_client, logger, lookback_days=30):
    """Reconcile bot trade logs against Alpaca's authoritative fills.

    Some fills can't be captured by submit_and_log_order's short poll: wheel's
    LIMIT options fill hours later or expire, and an equity MARKET order can keep
    filling across prints after the poll window closes. The accountant calls this
    each cycle: it reads recent CLOSED orders for RECONCILED_BOTS and writes the
    real cumulative fill (price/qty/time) to the bot's measurement via
    _log_fill_to_influx.

    Fully-filled orders are stamped at their broker filled_at (idempotent re-runs
    overwrite the same series+timestamp instead of duplicating). Terminal orders
    that only PARTIALLY filled (canceled/expired/rejected, no filled_at) are
    recovered via log_terminal_partial_fill at a deterministic synthetic stamp, so
    a close spread across partial rungs isn't under-counted in realized P&L.
    Zero-fill terminations and still-open partials produce no row. Crypto bots are
    excluded (see RECONCILED_BOTS).

    Returns the number of fill rows written/updated.
    """
    from datetime import datetime, timezone, timedelta
    written = 0
    after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    # Page the whole lookback window — never a single limit=500 read. A
    # crypto-order flood, or a catch-up run after downtime, can bury a
    # RECONCILED_BOT's late fill far past the newest 500 CLOSED orders (a wheel
    # option that fills days after submission sorts by its old submitted_at), so
    # a bounded window silently drops it — the same trap that orphaned aged
    # equity positions. _fetch_orders_covering walks back via `until` to `after`.
    orders = _fetch_orders_covering(trading_client, status="closed", after=after,
                                    max_orders=RECONCILE_MAX_ORDERS)
    if len(orders) >= RECONCILE_MAX_ORDERS:
        logger.warning(f"  [Reconcile] hit the {RECONCILE_MAX_ORDERS}-order page "
                       f"cap; fills older than the covered window may be missed "
                       f"this cycle.")

    for o in orders:
        c_id = getattr(o, "client_order_id", "") or ""
        if not any(c_id.startswith(f"{b}-") for b in RECONCILED_BOTS):
            continue
        # Fully-filled orders carry a stable broker filled_at -> stamp there.
        if getattr(o, "filled_at", None) is not None:
            _log_fill_to_influx(o, logger, action=_fill_action(o))
            written += 1
        else:
            # No filled_at: a terminal order (canceled/expired/rejected) that only
            # PARTIALLY filled before it ended. The full-fill path skips it, so
            # recover the partial here (idempotent, synthetic stamp). No-op unless
            # terminal with filled_qty>0 — still-open partials and zero-fill
            # terminations write nothing, so no full-fill row can be double-counted.
            written += log_terminal_partial_fill(o, logger)

    if written:
        logger.info(f"  [Reconcile] wrote/updated {written} fill row(s) from Alpaca.")
    return written

def bound_session_timeout(client, timeout=15):
    """Give an alpaca-py REST client's underlying requests.Session a default
    read timeout.

    The SDK sets none, so one hung socket blocks its caller forever — the
    2026-07-05 accountant stall was reconcile_fills' get_orders hanging on
    `read timeout=None`, which also freezes the orphan sweep and CFO
    reallocation behind it. Wraps the private `_session` attribute (stable
    across alpaca-py releases) and no-ops harmlessly if the layout changes;
    callers that pass their own timeout are untouched.
    """
    session = getattr(client, "_session", None)
    request = getattr(session, "request", None)
    if request is None or getattr(session, "_fleet_timeout", None):
        return client
    def _bounded(method, url, *args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return request(method, url, *args, **kwargs)
    session.request = _bounded
    session._fleet_timeout = timeout
    return client

# --- OPTION LIFECYCLE EVENTS (assignment / exercise / expiration) ---
# These are position mutations with NO order behind them, so submit_and_log_order
# and reconcile_fills can never see them: the 2026-07-02 PAAS assignment turned a
# short put into 100 shares without a single row anywhere. The accountant polls
# Alpaca's account activities for them each cycle.
_OPTION_EVENT_ACTIONS = {"OPASN": "assigned", "OPEXC": "exercised", "OPEXP": "expired"}
_option_events_after = None      # ISO date watermark; restart rescans the lookback
_alerted_event_ids = set()       # best-effort in-process Discord dedupe

def _write_option_event(ev, measurement, logger, alert=None):
    """Write one activity event as a trade row; returns 1 if written.

    Rows are stamped at a deterministic per-event timestamp (activity date +
    id-derived ms jitter), so rescans and restarts overwrite the same point
    instead of duplicating. Actions contain neither 'buy' nor 'sell' on
    purpose: the accountant's realized-P&L pairing must ignore these rows.
    """
    import datetime as dt
    import zlib
    act = _OPTION_EVENT_ACTIONS.get(ev.get("activity_type"))
    sym = ev.get("symbol") or ""
    if not act or not sym:
        return 0
    qty = abs(float(ev.get("qty") or 0))
    occ = _OCC_SYMBOL_RE.match(sym)
    root = occ.group(1) if occ else sym
    strike = (float(sym[-8:]) / 1000.0) if occ else 0.0
    day = ev.get("date") or (ev.get("transaction_time") or "")[:10]
    if not day:
        return 0
    stamp = dt.datetime.fromisoformat(f"{day}T20:00:00+00:00")
    jitter_ms = zlib.crc32(str(ev.get("id")).encode()) % 1000
    ns = (int(stamp.timestamp() * 1000) + jitter_ms) * 1_000_000
    line = (f'{measurement},symbol={sym} '
            f'price={strike},action="{act}",qty={qty},'
            f'reason="option_{act}",underlying="{root}" {ns}')
    url = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/write?db={config.INFLUX_DB_NAME}"
    r = requests.post(url, data=line, timeout=2)
    if r.status_code != 204:
        logger.warning(f"  [OptionEvents] InfluxDB write failed: {r.status_code} {r.text}")
        return 0
    ev_id = ev.get("id")
    if alert and act in ("assigned", "exercised") and ev_id not in _alerted_event_ids:
        _alerted_event_ids.add(ev_id)
        shares = int(qty) * 100
        alert(f"📌 **OPTION {act.upper()}: {sym}**\n"
              f"{int(qty)} contract(s) → {shares} shares {root} @ ${strike:.2f} strike.\n"
              f"Stock resolves to wheel_bot via option-root ownership; "
              f"covered calls resume next wheel cycle.")
    logger.info(f"  [OptionEvents] {act}: {sym} qty={qty} (row @ {day})")
    return 1

def reconcile_option_events(logger, lookback_days=7, alert=None):
    """Poll account activities for OPASN/OPEXC/OPEXP and log them to the wheel
    measurement. Returns the number of rows written. `alert` is an optional
    callable(str) for Discord; expirations write rows but never ping (a put
    expiring worthless is the wheel working).
    """
    global _option_events_after
    import datetime as dt
    base_url = ("https://paper-api.alpaca.markets" if getattr(config, "PAPER", True)
                else "https://api.alpaca.markets")
    headers = {"APCA-API-KEY-ID": config.API_KEY,
               "APCA-API-SECRET-KEY": config.SECRET_KEY,
               "accept": "application/json"}
    after = _option_events_after or (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")
    measurement = fleet_registry.BOTS["wheel_bot"]["measurement"]
    written = 0
    page_token = None
    newest_day = None
    for _ in range(20):  # hard page cap per cycle
        params = {"activity_types": ",".join(_OPTION_EVENT_ACTIONS),
                  "after": after, "direction": "asc", "page_size": 100}
        if page_token:
            params["page_token"] = page_token
        try:
            r = requests.get(f"{base_url}/v2/account/activities",
                             headers=headers, params=params, timeout=15)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            registry.log_error("utils", "reconcile_option_events", e)
            logger.warning(f"  [OptionEvents] activities fetch failed: {e}")
            return written
        if not events:
            break
        for ev in events:
            try:
                written += _write_option_event(ev, measurement, logger, alert=alert)
            except Exception as e:
                registry.log_error("utils", "option_event_row", e,
                                   context=str(ev.get("id")))
        day = events[-1].get("date") or (events[-1].get("transaction_time") or "")[:10]
        if day:
            newest_day = day
        if len(events) < 100:
            break
        page_token = events[-1].get("id")
    # Watermark at the newest event's DATE (not time): the next cycle re-reads
    # that day's few events and idempotently rewrites them, which is cheaper
    # than risking a same-day gap.
    if newest_day:
        _option_events_after = newest_day
    return written