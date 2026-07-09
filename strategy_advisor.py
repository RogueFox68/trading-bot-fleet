"""Paper-only dynamic allocation advisor.

This module turns Alpaca fills plus the accountant's live position snapshot into
bot-level performance metrics and a recommended allocation file. It deliberately
does not write effective_budgets.json; the existing CFO path remains the only
runtime budget authority.
"""
import datetime as dt
import json
import math
import re

import fleet_registry


OUTPUT_FILE = "recommended_allocations.json"
WINDOW_DAYS = (5, 20, 60)
MIN_CLOSED_TRADES = 3
MIN_FILLS = 3
MAX_ALLOCATION_SHIFT = 0.02
MIN_SCORE_SPREAD = 0.03
OPTION_CONTRACT_SIZE = 100

_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[PC]\d{8}$")


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def _as_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def _enum_value(value):
    return str(getattr(value, "value", value)).lower()


def bot_from_client_order_id(client_order_id):
    """Return the active bot tag embedded in client_order_id, or None."""
    cid = client_order_id or ""
    for bot in sorted(fleet_registry.BOTS, key=len, reverse=True):
        if cid.startswith(f"{bot}-"):
            return bot
    return None


def vix_band(vix):
    vix = _as_float(vix, 15.0)
    if vix < 18:
        return "LOW_VIX"
    if vix < 25:
        return "MEDIUM_VIX"
    return "HIGH_VIX"


def regime_context(config_data):
    gs = config_data.get("global_settings", {}) if config_data else {}
    regime = gs.get("market_condition", "SIDEWAYS")
    vix = _as_float(gs.get("vix"), 15.0)
    macro = gs.get("macro_climate", "UNKNOWN")
    sector = gs.get("sector_rotation", "UNKNOWN")
    return {
        "market_condition": regime,
        "vix": round(vix, 2),
        "vix_band": vix_band(vix),
        "macro_climate": macro,
        "sector_rotation": sector,
        "regime_key": f"{regime}|{vix_band(vix)}|{macro}|{sector}",
    }


def fill_from_order(order):
    """Normalize an Alpaca order into a ledger fill row, or None."""
    bot = bot_from_client_order_id(getattr(order, "client_order_id", ""))
    if bot is None:
        return None
    price = _as_float(getattr(order, "filled_avg_price", None), None)
    qty = _as_float(getattr(order, "filled_qty", None), 0.0)
    if price is None or qty <= 0:
        return None
    filled_at = _as_utc(getattr(order, "filled_at", None) or getattr(order, "submitted_at", None))
    if filled_at is None:
        return None

    side = _enum_value(getattr(order, "side", ""))
    symbol = getattr(order, "symbol", "")
    multiplier = OPTION_CONTRACT_SIZE if _OCC_RE.match(symbol or "") else 1
    limit_price = _as_float(getattr(order, "limit_price", None), None)
    adverse_slippage_bps = None
    if limit_price and limit_price > 0:
        if side == "buy":
            adverse_slippage_bps = max(0.0, (price - limit_price) / limit_price * 10000.0)
        elif side == "sell":
            adverse_slippage_bps = max(0.0, (limit_price - price) / limit_price * 10000.0)

    return {
        "bot": bot,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "multiplier": multiplier,
        "notional": price * qty * multiplier,
        "filled_at": filled_at,
        "client_order_id": getattr(order, "client_order_id", ""),
        "adverse_slippage_bps": adverse_slippage_bps,
    }


def fills_from_orders(orders):
    fills = [fill_from_order(o) for o in orders]
    return sorted([f for f in fills if f], key=lambda f: f["filled_at"])


def _empty_realized():
    return {
        "realized_pl": 0.0,
        "closed_trades": 0,
        "winning_trades": 0,
        "win_rate": 0.0,
        "avg_hold_hours": 0.0,
        "max_drawdown": 0.0,
        "avg_adverse_slippage_bps": None,
    }


def realized_metrics(fills):
    """FIFO realized P&L supporting longs, shorts, and option premium trades."""
    metrics = {bot: _empty_realized() for bot in fleet_registry.BOTS}
    lots = {}
    cumulative = {bot: 0.0 for bot in fleet_registry.BOTS}
    peaks = {bot: 0.0 for bot in fleet_registry.BOTS}
    hold_hours = {bot: [] for bot in fleet_registry.BOTS}
    slippage = {bot: [] for bot in fleet_registry.BOTS}

    for fill in sorted(fills, key=lambda f: f["filled_at"]):
        bot = fill["bot"]
        if bot not in metrics:
            continue
        if fill["adverse_slippage_bps"] is not None:
            slippage[bot].append(fill["adverse_slippage_bps"])

        signed_qty = fill["qty"] if fill["side"] == "buy" else -fill["qty"]
        key = (bot, fill["symbol"])
        book = lots.setdefault(key, [])
        remaining = signed_qty

        while abs(remaining) > 1e-9 and book and (book[0]["qty"] * remaining) < 0:
            lot = book[0]
            closed_qty = min(abs(remaining), abs(lot["qty"]))
            if lot["qty"] > 0:
                pnl = (fill["price"] - lot["price"]) * closed_qty * fill["multiplier"]
                lot["qty"] -= closed_qty
                remaining += closed_qty
            else:
                pnl = (lot["price"] - fill["price"]) * closed_qty * fill["multiplier"]
                lot["qty"] += closed_qty
                remaining -= closed_qty

            metrics[bot]["realized_pl"] += pnl
            metrics[bot]["closed_trades"] += 1
            if pnl > 0:
                metrics[bot]["winning_trades"] += 1
            if lot.get("opened_at"):
                hold_hours[bot].append((fill["filled_at"] - lot["opened_at"]).total_seconds() / 3600.0)

            cumulative[bot] += pnl
            peaks[bot] = max(peaks[bot], cumulative[bot])
            metrics[bot]["max_drawdown"] = max(metrics[bot]["max_drawdown"], peaks[bot] - cumulative[bot])

            if abs(lot["qty"]) <= 1e-9:
                book.pop(0)

        if abs(remaining) > 1e-9:
            book.append({"qty": remaining, "price": fill["price"], "opened_at": fill["filled_at"]})

    for bot, data in metrics.items():
        closed = data["closed_trades"]
        data["win_rate"] = data["winning_trades"] / closed if closed else 0.0
        data["avg_hold_hours"] = sum(hold_hours[bot]) / len(hold_hours[bot]) if hold_hours[bot] else 0.0
        data["avg_adverse_slippage_bps"] = (
            sum(slippage[bot]) / len(slippage[bot]) if slippage[bot] else None
        )
    return metrics


def risk_adjusted_score(metric, equity):
    capital = max(metric["capital_used"], equity * 0.01, 1.0)
    total_return = metric["total_pl"] / capital
    drawdown_penalty = metric["max_drawdown"] / capital
    win_bonus = (metric["win_rate"] - 0.5) * 0.20 if metric["closed_trades"] else 0.0
    slippage_penalty = (metric["avg_adverse_slippage_bps"] or 0.0) / 10000.0
    return total_return - (0.50 * drawdown_penalty) + win_bonus - slippage_penalty


def build_window_metrics(fills, unrealized_by_bot, allocation_by_bot, equity, window_days, now=None):
    now = now or utc_now()
    cutoff = now - dt.timedelta(days=window_days)
    window_fills = [f for f in fills if f["filled_at"] >= cutoff]
    realized = realized_metrics(window_fills)
    out = {}
    for bot in fleet_registry.BOTS:
        r = dict(realized.get(bot, _empty_realized()))
        r["fills"] = sum(1 for f in window_fills if f["bot"] == bot)
        r["unrealized_pl"] = round(_as_float(unrealized_by_bot.get(bot)), 2)
        r["capital_used"] = round(_as_float(allocation_by_bot.get(bot)), 2)
        r["capital_utilization"] = r["capital_used"] / equity if equity > 0 else 0.0
        r["total_pl"] = round(r["realized_pl"] + r["unrealized_pl"], 2)
        r["evidence_ok"] = r["closed_trades"] >= MIN_CLOSED_TRADES or r["fills"] >= MIN_FILLS
        r["risk_adjusted_score"] = round(risk_adjusted_score(r, equity), 6) if r["evidence_ok"] else 0.0
        out[bot] = r
    return out


def current_allocations(config_data):
    cfo = config_data.get("cfo_settings", {}) if config_data else {}
    base = cfo.get("base_allocations") or {}
    if base:
        return {bot: _as_float(base.get(bot), 0.0) for bot in fleet_registry.BOTS}
    bots = config_data.get("bots", {}) if config_data else {}
    return {bot: _as_float(bots.get(bot, {}).get("allocation"), 0.0) for bot in fleet_registry.BOTS}


def _minimum_allocations(config_data):
    cfo = config_data.get("cfo_settings", {}) if config_data else {}
    mins = cfo.get("minimum_reserves") or {}
    return {bot: _as_float(mins.get(bot), 0.0) for bot in fleet_registry.BOTS}


def _bot_score(bot, metrics_by_window):
    weighted = 0.0
    weight_sum = 0.0
    signs = []
    closed = 0
    fills = 0
    weights = {"5d": 0.50, "20d": 0.30, "60d": 0.20}
    for window, weight in weights.items():
        metric = metrics_by_window.get(window, {}).get(bot)
        if not metric:
            continue
        closed += metric["closed_trades"]
        fills += metric["fills"]
        if not metric["evidence_ok"]:
            continue
        score = metric["risk_adjusted_score"]
        weighted += score * weight
        weight_sum += weight
        signs.append(1 if score > 0 else (-1 if score < 0 else 0))

    if weight_sum == 0:
        return {"score": 0.0, "confidence": 0.0, "reason": "insufficient_evidence",
                "closed_trades": closed, "fills": fills}

    score = weighted / weight_sum
    non_zero = [s for s in signs if s != 0]
    consistency = (max(non_zero.count(1), non_zero.count(-1)) / len(non_zero)) if non_zero else 0.5
    sample_conf = min(1.0, max(closed / 12.0, fills / 20.0))
    confidence = round(sample_conf * consistency, 3)
    return {"score": round(score, 6), "confidence": confidence,
            "reason": "risk_adjusted_windows", "closed_trades": closed, "fills": fills}


def recommend_allocations(metrics_by_window, config_data, context):
    current = current_allocations(config_data)
    recommended = dict(current)
    mins = _minimum_allocations(config_data)
    bot_scores = {bot: _bot_score(bot, metrics_by_window) for bot in fleet_registry.BOTS}
    eligible = {bot: s for bot, s in bot_scores.items() if s["confidence"] >= 0.35}

    reasons = []
    if len(eligible) < 2:
        reasons.append("insufficient_evidence")
        action = "no_change"
    else:
        best = max(eligible, key=lambda b: eligible[b]["score"])
        worst = min(eligible, key=lambda b: eligible[b]["score"])
        spread = eligible[best]["score"] - eligible[worst]["score"]
        donor_capacity = max(0.0, current.get(worst, 0.0) - mins.get(worst, 0.0))
        if spread < MIN_SCORE_SPREAD or donor_capacity <= 0:
            reasons.append("score_spread_too_small")
            action = "no_change"
        else:
            shift = min(MAX_ALLOCATION_SHIFT, donor_capacity)
            recommended[best] = round(recommended.get(best, 0.0) + shift, 4)
            recommended[worst] = round(recommended.get(worst, 0.0) - shift, 4)
            reasons.append(f"shift_{shift:.2%}_from_{worst}_to_{best}")
            action = "recommend_shift"

    return {
        "mode": "recommendation_only",
        "writes_effective_budgets": False,
        "action": action,
        "context": context,
        "current_allocations": current,
        "recommended_allocations": recommended,
        "bot_scores": bot_scores,
        "reason_codes": reasons,
        "confidence": round(max((s["confidence"] for s in bot_scores.values()), default=0.0), 3),
    }


def source_comparison(metrics_by_window, influx_realized_by_bot=None):
    influx_realized_by_bot = influx_realized_by_bot or {}
    comparison = {}
    sixty_day = metrics_by_window.get("60d", {})
    for bot in fleet_registry.BOTS:
        alpaca_realized = _as_float(sixty_day.get(bot, {}).get("realized_pl"), 0.0)
        influx_realized = _as_float(influx_realized_by_bot.get(bot), 0.0)
        comparison[bot] = {
            "alpaca_ledger_realized_pl": round(alpaca_realized, 2),
            "influx_realized_pl": round(influx_realized, 2),
            "delta": round(alpaca_realized - influx_realized, 2),
        }
    return comparison


def macro_scorecard(metrics_by_window, context):
    return {
        context["regime_key"]: {
            bot: _bot_score(bot, metrics_by_window)
            for bot in fleet_registry.BOTS
        }
    }


def fetch_alpaca_fills(trading_client, lookback_days=max(WINDOW_DAYS), max_orders=10000):
    """Fetch recent closed Alpaca orders and return normalized fill rows."""
    import utils

    after = utc_now() - dt.timedelta(days=lookback_days)
    orders = utils._fetch_orders_covering(
        trading_client,
        status="closed",
        after=after,
        max_orders=max_orders,
    )
    return fills_from_orders(orders)


def build_strategy_report(fills, unrealized_by_bot, allocation_by_bot, equity,
                          config_data, now=None, influx_realized_by_bot=None):
    now = now or utc_now()
    metrics_by_window = {
        f"{days}d": build_window_metrics(
            fills, unrealized_by_bot, allocation_by_bot, equity, days, now=now
        )
        for days in WINDOW_DAYS
    }
    context = regime_context(config_data)
    recommendation = recommend_allocations(metrics_by_window, config_data, context)
    return {
        "version": "1.0",
        "updated": now.isoformat(),
        "source": "alpaca_orders_and_live_positions",
        "status": "success",
        "recommendation": recommendation,
        "windows": metrics_by_window,
        "macro_scorecard": macro_scorecard(metrics_by_window, context),
        "source_comparison": source_comparison(metrics_by_window, influx_realized_by_bot),
        "assumptions": {
            "paper_only": True,
            "does_not_write_effective_budgets": True,
            "thin_samples_hold_current_allocations": True,
        },
    }


def write_strategy_report(report, path=OUTPUT_FILE):
    with open(path, "w") as f:
        json.dump(report, f, indent=4, default=str)


def generate_and_write_report(trading_client, unrealized_by_bot, allocation_by_bot,
                              equity, config_data, logger=None, path=OUTPUT_FILE,
                              influx_realized_by_bot=None):
    fills = fetch_alpaca_fills(trading_client)
    report = build_strategy_report(fills, unrealized_by_bot, allocation_by_bot,
                                   equity, config_data,
                                   influx_realized_by_bot=influx_realized_by_bot)
    write_strategy_report(report, path=path)
    if logger:
        rec = report["recommendation"]
        logger.info(f"[StrategyAdvisor] {rec['action']} | {', '.join(rec['reason_codes'])}")
    return report
