import json
import time
import re
import requests
from alpaca.trading.enums import AssetClass, OrderSide, OrderStatus
from logger import logger, registry
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

import config

# --- CENTRALIZED TRADE LOGGING ---
# Maps the bot tag embedded in client_order_id ('{bot}-{symbol}-{ts}') to its
# InfluxDB measurement. condor_bot is retired (2026-07) but its tag stays
# mapped so any historical condor order that flows through fill logging or
# ownership resolution never misattributes to another bot's measurement.
MEASUREMENT_BY_BOT = {
    "trend_bot":    "trades",
    "survivor_bot": "survivor_trades",
    "wheel_bot":    "wheel_trades",
    "crypto_grid":  "crypto_trades",
    "moon_bot":     "breakout_trades",
    "condor_bot":   "condor_trades",  # retired; historical orders only
}

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
        # Use the broker fill time, not now() — also fixes time skew.
        ns = int(order.filled_at.timestamp() * 1e9) if getattr(order, "filled_at", None) else time.time_ns()
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
# This defines which bot is allowed to trade which ticker
BOT_MAPPING = {
    "survivor_bot": [],
    "wheel_bot": [],
    "crypto_grid": ["BTC/USD", "ETH/USD", "SOL/USD"],
    "moon_bot": ["BTC/USD", "ETH/USD", "SOL/USD"]
}

# Cache to avoid reading the file on every position in the loop
_ownership_cache = {}
_ownership_cache_time = 0

# Bot tags that can appear in a client_order_id ('{bot}-{symbol}-{ts}').
# condor_bot is retired but kept: its tag still exists on historical orders.
_KNOWN_BOT_TAGS = ["survivor_bot", "trend_bot", "wheel_bot", "condor_bot", "crypto_grid", "moon_bot"]

def _fetch_orders_covering(trading_client, held_symbols=None, require_fill=False,
                           page_size=500, max_orders=None):
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
            kwargs = {"status": "all", "limit": page_size}
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
    orders = _fetch_orders_covering(trading_client, held_symbols=held_symbols)
    for o in orders:
        # client_order_id format: "{bot_name}-{symbol}-{timestamp}"
        c_id = o.client_order_id
        if not c_id:
            continue
        if o.symbol in order_set:
            continue
        for bot in _KNOWN_BOT_TAGS:
            if c_id.startswith(f"{bot}-"):
                owner_map[o.symbol] = bot
                order_set.add(o.symbol)
                break

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
    """Determines which bot owns a specific position based on order history."""
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
    
    # 4. Stock/ETF Rules — check dynamic map for exact symbol
    owner = owner_map.get(symbol)
    if owner:
        return owner

    # 5. Default fallback (e.g., manually placed order without tag, or an opening
    #    order that aged out of history entirely). This is a catch-all, not a
    #    durable tag: surface it so orphaned positions don't stay invisible.
    _note_ownership_fallback(symbol)
    return "trend_bot"

# Throttle so a persistently-untagged symbol doesn't spam the log/metric each cycle.
_fallback_log_times = {}

def _note_ownership_fallback(symbol):
    """Surface an untagged equity that resolved only via the trend_bot default.

    Logs a throttled warning (once / 10 min per symbol) and writes an
    'ownership_fallback' InfluxDB metric, so a position whose real tag has aged
    out of order history is visible instead of silently absorbed by the fallback.
    """
    now = time.time()
    if now - _fallback_log_times.get(symbol, 0) < 600:
        return
    _fallback_log_times[symbol] = now
    logger.warning(f"  [Ownership] {symbol}: no bot tag in order history — "
                   f"defaulting to trend_bot (possible orphan).")
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
                
        # 2. Fallback to Static Base Budget
        if budget_dollars == 0.0:
            if not os.path.exists("bot_config.json"):
                logger.error("  [CFO] FAIL-CLOSED: bot_config.json missing!")
                return False, 0.0, 0.0
                
            with open("bot_config.json", "r") as f:
                config_data = json.load(f)
            bot_settings = config_data.get("bots", {}).get(bot_name, {})
            allocation_pct = bot_settings.get("allocation", 0.0)
            
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
# intentionally excluded: their action vocabulary (grid_buy / grid_sweep /
# buy_breakout ...) can't be reconstructed from an order, and a generic buy/sell
# would clobber it on rewrite. Crypto market fills are near-atomic anyway.
RECONCILED_BOTS = ("trend_bot", "survivor_bot", "wheel_bot")

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

def reconcile_fills(trading_client, logger, lookback_days=30):
    """Reconcile bot trade logs against Alpaca's authoritative fills.

    Some fills can't be captured by submit_and_log_order's short poll: wheel's
    LIMIT options fill hours later or expire, and an equity MARKET order can keep
    filling across prints after the poll window closes. The accountant calls this
    each cycle: it reads recent CLOSED orders for RECONCILED_BOTS and writes the
    real cumulative fill (price/qty/time) to the bot's measurement via
    _log_fill_to_influx.

    Only fully-filled orders are written (they carry a stable broker filled_at),
    so the row is stamped at filled_at and re-runs overwrite the same
    series+timestamp (idempotent) instead of duplicating. Unfilled/expired orders
    and still-open partials produce no row. Crypto bots are excluded (see
    RECONCILED_BOTS).

    Returns the number of fill rows written/updated.
    """
    from datetime import datetime, timezone, timedelta
    written = 0
    try:
        after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        req = GetOrdersRequest(status="closed", limit=500, after=after)
        orders = trading_client.get_orders(filter=req)
    except Exception as e:
        registry.log_error("utils", "reconcile_fills", e)
        logger.warning(f"  [Reconcile] Failed to fetch orders: {e}")
        return written

    for o in orders:
        c_id = getattr(o, "client_order_id", "") or ""
        if not any(c_id.startswith(f"{b}-") for b in RECONCILED_BOTS):
            continue
        # Require a broker fill timestamp: guarantees a fully-filled order and a
        # stable idempotency key. Unfilled and partial-then-canceled orders (no
        # filled_at) are skipped here.
        if getattr(o, "filled_at", None) is None:
            continue
        _log_fill_to_influx(o, logger, action=_fill_action(o))
        written += 1

    if written:
        logger.info(f"  [Reconcile] wrote/updated {written} fill row(s) from Alpaca.")
    return written