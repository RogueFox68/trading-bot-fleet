import config
import time
import json
import requests
import pandas as pd
import numpy as np
import datetime
import os
from logger import registry, logger
import fleet_registry
import utils

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# SPY — which drives the regime — comes from Alpaca (reliable, already
# authenticated). VIX cannot: Alpaca's data SDK exposes no index feed (there is
# no get_index_* / IndexHistoricalDataClient in alpaca-py) and this account gets
# 403 on the index endpoints, so VIX is fetched from public sources in a
# fallback CHAIN (see VIX_SOURCES below).
#
# 2026-06-24 postmortem: yf.download(["SPY","^VIX"]) returned an empty frame
# (Yahoo rate-limiting), the compute-and-publish block skipped with no error,
# and VIX froze — disabling the VIX>28 kill-switch for ~12 days. That was fixed
# by isolating the fetch and failing LOUD.
#
# 2026-09: Yahoo broke again, and this time it stayed broken. A single external
# provider is a single point of failure for the fleet's kill-switch no matter
# how hard the fetch around it is retried, so VIX now tries several independent
# sources per cycle and takes the first sane reading. yfinance is imported
# LAZILY (see _load_yfinance) — it is the one dependency that has broken by
# itself twice, and a broken install of an optional fallback must not take the
# regime process down at import time.

# --- CONFIGURATION ---
CHECK_INTERVAL = 900   # 15 Minutes
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"

# Data-fetch resilience: retry an empty/failed pull a few times with
# exponential backoff before treating the cycle as a (loud) failure.
FETCH_RETRIES = 3
FETCH_BACKOFF = 3      # seconds base; 3s, 6s, 12s
MIN_BARS = 200         # need >=200 daily closes for SMA200

# --- VIX SOURCE CHAIN ---
# Every source below returns the VIX in TRUE INDEX POINTS, so the 22/28 gates
# downstream need no recalibration whichever one answers. A dollar-priced proxy
# (VIXY/VXX) is deliberately NOT in this chain: it would need calibration and a
# mis-scaled number feeding a kill-switch is worse than no number at all — the
# stale fail-safe already handles "no number".
VIX_HTTP_TIMEOUT = 8   # per-request; the chain is tried FETCH_RETRIES times
VIX_MIN, VIX_MAX = 5.0, 150.0   # sanity band; rejects garbage/rate-limit rows
VIX_USER_AGENT = "trading-fleet/1.0"

# Staleness fail-safe. If no fully-successful SPY+VIX fetch lands in this long,
# stop trusting the frozen (possibly low) VIX and degrade to an elevated-risk
# posture so the VIX kill-switch fails SAFE instead of silently disabling.
# Deliberately spans >2 cycles so a transient Yahoo blip doesn't flip it.
STALE_REGIME_SECONDS = 45 * 60
# VIX written during the stale fail-safe: above wheel_bot's vix>22 entry gate
# (pauses its NEW entries) and enough with CRITICAL_VOLATILITY to gate crypto,
# but BELOW the 28 full-fleet kill so a mere data outage can't self-inflict a
# total halt. Always paired with data_stale=True so it is never mistaken for a
# live reading.
STALE_VIX_SENTINEL = 25.0

# At most one failure ping to Discord per hour (registry.log_error still fires
# every cycle, so Grafana/error_watchdog sees the full cadence).
FAIL_ALERT_THROTTLE = 3600

# --- INFLUXDB ---
INFLUX_HOST = config.INFLUX_HOST
INFLUX_PORT = config.INFLUX_PORT
INFLUX_DB_NAME = config.INFLUX_DB_NAME
INFLUX_URL = f"http://{INFLUX_HOST}:{INFLUX_PORT}/write?db={INFLUX_DB_NAME}"

# --- ALPACA DATA CLIENT (SPY bars; VIX has no Alpaca index feed) ---
# Bounded read timeout so a hung Alpaca socket can't silently freeze the loop
# (the same failure mode that stalled the accountant on 2026-07-05).
_data_client = utils.bound_session_timeout(
    StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
)

# --- NATIVE MATH ENGINE ---
class TechnicalMath:
    @staticmethod
    def get_sma(series, window):
        return series.rolling(window=window).mean()

    @staticmethod
    def get_ema(series, window):
        return series.ewm(span=window, adjust=False).mean()

    @staticmethod
    def get_adx(high, low, close, window=14):
        plus_dm = high.diff()
        minus_dm = low.diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        # Full True Range (matches market_scanner.TechnicalMath.get_adx)
        tr = pd.concat([
            (high - low),
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        tr = tr.replace(0, np.nan)
        plus_di = 100 * (plus_dm.ewm(alpha=1/window, adjust=False).mean() / tr)
        minus_di = 100 * (minus_dm.abs().ewm(alpha=1/window, adjust=False).mean() / tr)
        dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
        return dx.ewm(alpha=1/window, adjust=False).mean()

# --- WEBHOOK ---
def send_discord(msg):
    if "YOUR" in config.WEBHOOK_OVERSEER: return
    try:
        requests.post(config.WEBHOOK_OVERSEER, json={
            "content": msg, "username": "Market Analyst 🧠"
        })
    except Exception as e:
        registry.log_error("market_analyst", "send_discord", e, context=msg[:50])
        logger.error(f"Discord webhook failed: {e}")

def log_to_influx(price, vix, adx, regime, sma, ema):
    """Write one fresh market_regime row. Only called on a fully-successful
    fetch, so the presence/recency of a row is the fleet's 'regime is live'
    signal (the accountant's staleness watchdog keys off this measurement's
    last-write time). Write failures route through registry.log_error — a
    silently-dropped write is exactly what let this pipeline die unseen."""
    try:
        regime_score = 1 if "BULL" in regime else (-1 if "BEAR" in regime else 0)
        # vix_source is a TAG (indexed) so Grafana can show which provider is
        # carrying the kill-switch, and alert when the chain falls through.
        source = last_vix_source or "unknown"
        data_str = (f'market_regime,symbol=SPY,vix_source={source} '
                    f'price={price},vix={vix},adx={adx},sma200={sma},ema20={ema},'
                    f'regime_score={regime_score},regime="{regime}" {time.time_ns()}')
        r = requests.post(INFLUX_URL, data=data_str, timeout=2)
        if r.status_code != 204:
            registry.log_error("market_analyst", "log_to_influx",
                               Exception(f"HTTP {r.status_code}: {r.text[:120]}"))
            logger.error(f"   [!] InfluxDB write failed: {r.status_code} {r.text}")
    except Exception as e:
        registry.log_error("market_analyst", "log_to_influx", e)
        logger.error(f"   [!] InfluxDB write error: {e}")

def get_spy_data():
    """Daily SPY OHLC from Alpaca as a DataFrame (Open/High/Low/Close), retried
    with backoff. Returns None on repeated empty/short/failed responses — the
    caller treats None as a LOUD failure, never a silent skip."""
    req = StockBarsRequest(
        symbol_or_symbols=MARKET_SYMBOL,
        timeframe=TimeFrame.Day,
        start=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400),
    )
    for attempt in range(FETCH_RETRIES):
        try:
            df = _data_client.get_stock_bars(req).df
            if df is not None and not df.empty:
                # BarSet.df is a (symbol, timestamp) MultiIndex; pull SPY out.
                if isinstance(df.index, pd.MultiIndex):
                    df = df.xs(MARKET_SYMBOL, level="symbol")
                out = pd.DataFrame({
                    "Open": df["open"], "High": df["high"],
                    "Low": df["low"], "Close": df["close"],
                }).dropna()
                if len(out) >= MIN_BARS:
                    return out
                logger.error(f"[Analyst] SPY bars too short: {len(out)} < {MIN_BARS} "
                             f"(attempt {attempt + 1})")
            else:
                logger.error(f"[Analyst] SPY bars empty (attempt {attempt + 1})")
        except Exception as e:
            registry.log_error("market_analyst", "get_spy_data", e,
                               context=f"attempt {attempt + 1}")
            logger.error(f"[Analyst] SPY fetch failed (attempt {attempt + 1}): {e}")
        if attempt < FETCH_RETRIES - 1:
            time.sleep(FETCH_BACKOFF * (2 ** attempt))
    return None

# --- VIX SOURCES -----------------------------------------------------------
# Each fetcher returns a float in VIX points, or None if THIS source could not
# answer. Raising is fine too — the chain catches, logs, and moves on. None of
# them may raise out of get_vix_value(): its contract is `float | None`.

def _vix_from_stooq():
    """stooq.com delayed CSV quote. No key, no cookie, plain requests."""
    r = requests.get("https://stooq.com/q/l/?s=^vix&f=sd2t2ohlc&h&e=csv",
                     timeout=VIX_HTTP_TIMEOUT,
                     headers={"User-Agent": VIX_USER_AGENT})
    if r.status_code != 200 or not r.text:
        raise RuntimeError(f"HTTP {r.status_code}")
    # Symbol,Date,Time,Open,High,Low,Close  -> Close is the VIX level.
    lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise ValueError(f"short CSV: {r.text[:80]!r}")
    close = lines[-1].split(",")[-1].strip()
    if not close or close.upper() == "N/D":
        raise ValueError(f"no quote: {r.text[:80]!r}")
    return float(close)


def _vix_from_cboe():
    """CBOE's own delayed-quote JSON — the index's home exchange."""
    r = requests.get(
        "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json",
        timeout=VIX_HTTP_TIMEOUT, headers={"User-Agent": VIX_USER_AGENT})
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    data = (r.json() or {}).get("data") or {}
    for key in ("current_price", "close", "last_trade_price", "prev_day_close"):
        val = data.get(key)
        if val not in (None, "", 0):
            return float(val)
    raise ValueError(f"no usable price key in {sorted(data)[:8]}")


# yfinance is imported lazily: it is an OPTIONAL fallback here, and a broken
# install of it (it has changed its HTTP stack under us, and 2026-09 shipped a
# curl_cffi dependency) must not crash the regime process at import time — the
# fleet's whole kill-switch hangs off this module staying alive.
yf = None
_yf_unavailable = False


def _load_yfinance():
    """Import yfinance on first use. Returns the module, or None if it can't be
    imported (logged once, then remembered)."""
    global yf, _yf_unavailable
    if yf is not None or _yf_unavailable:
        return yf
    try:
        import yfinance as _yf_mod
    except Exception as e:
        _yf_unavailable = True
        registry.log_error("market_analyst", "_load_yfinance", e,
                           context="VIX fallback source disabled")
        logger.error(f"[Analyst] yfinance import failed ({e}) — that VIX "
                     f"fallback is disabled; other sources still apply.")
        return None
    yf = _yf_mod
    return yf


def _vix_from_yfinance():
    """Yahoo via yfinance. Last in the chain: it broke the fleet's VIX twice
    (2026-06 rate-limiting, 2026-09 outright), but it costs nothing to keep as
    a fallback and it recovers on its own when Yahoo comes back."""
    mod = _load_yfinance()
    if mod is None:
        return None
    df = mod.download("^VIX", period="5d", interval="1d", progress=False)
    if df is None or df.empty:
        return None
    # yfinance may hand back MultiIndex columns (e.g. ('Close','^VIX')) for a
    # single ticker depending on version — flatten to the field.
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Close" not in df.columns:
        return None
    close = df["Close"]
    if isinstance(close, pd.DataFrame):   # duplicate-labeled cols
        close = close.iloc[:, 0]
    close = pd.to_numeric(close, errors="coerce").dropna()
    if close.empty:
        return None
    return float(close.iloc[-1])


# Tried in order, first sane reading wins. Ordered cheapest/most-reliable first;
# yfinance last because it is the one that has failed the fleet before.
VIX_SOURCES = (
    ("stooq", _vix_from_stooq),
    ("cboe", _vix_from_cboe),
    ("yfinance", _vix_from_yfinance),
)

# Name of the source that answered the last successful get_vix_value(), or None.
# Published to bot_config.global_settings.vix_source and tagged on the InfluxDB
# market_regime row, so which provider is actually carrying the kill-switch is
# visible without shell access to the box.
last_vix_source = None


def get_vix_value():
    """Latest VIX spot in index points, from the first source in VIX_SOURCES
    that answers with a sane value. Retries the whole chain with backoff.

    Returns None (LOUD — the caller alerts and eventually engages the stale
    fail-safe) only when every source failed on every attempt. Contract is
    unchanged from the yfinance-only version: no args, `float | None`."""
    global last_vix_source
    for attempt in range(FETCH_RETRIES):
        for name, fetch in VIX_SOURCES:
            try:
                val = fetch()
            except Exception as e:
                registry.log_error("market_analyst", "get_vix_value", e,
                                   context=f"{name} attempt {attempt + 1}")
                logger.error(f"[Analyst] VIX({name}) failed "
                             f"(attempt {attempt + 1}): {e}")
                continue
            if val is None:
                logger.error(f"[Analyst] VIX({name}) returned no data "
                             f"(attempt {attempt + 1}).")
                continue
            if not (VIX_MIN <= val <= VIX_MAX):
                # Out-of-band means the source is serving garbage (a rate-limit
                # page, a zeroed row). Rejecting is the point: a bad number here
                # silently mis-sets the 22/28 gates.
                registry.log_error(
                    "market_analyst", "get_vix_value",
                    ValueError(f"VIX {val} outside [{VIX_MIN}, {VIX_MAX}]"),
                    context=f"{name} attempt {attempt + 1}")
                logger.error(f"[Analyst] VIX({name}) out of band: {val}")
                continue
            if name != last_vix_source:
                logger.warning(f"[Analyst] VIX source now '{name}' "
                               f"(was '{last_vix_source}'): {val:.2f}")
            last_vix_source = name
            return float(val)
        if attempt < FETCH_RETRIES - 1:
            time.sleep(FETCH_BACKOFF * (2 ** attempt))
    last_vix_source = None
    return None


def update_bot_config(regime, vix_val, climate, data_stale=False):
    """
    - Normal Market (VIX < 28): keep ALL bots ACTIVE; let them decide.
    - Hurricane (VIX > 28): PAUSE everything (the emergency kill-switch).

    `data_stale` marks that regime/VIX could not be refreshed and this posture
    is the fail-safe substitute, not a live reading — persisted to
    global_settings so consumers/humans can tell the difference.
    """
    if not os.path.exists(CONFIG_FILE):
        registry.log_error("market_analyst", "update_bot_config",
                           FileNotFoundError(f"{CONFIG_FILE} missing"))
        logger.error(f"[Analyst] {CONFIG_FILE} missing — cannot publish regime.")
        return

    try:
        with open(CONFIG_FILE, 'r') as f:
            current_config = json.load(f)

        bots = current_config['bots']
        changes_made = []

        # Default: everyone is on deck. Exception: the apocalypse (VIX > 28).
        if vix_val > 28.0:
            target_status = "paused"
            forced_regime = "CRITICAL_VOLATILITY"
        else:
            target_status = "active"
            forced_regime = regime

        for bot_name in bots.keys():
            # Only manage registry bots that aren't manual_state. Infra entries
            # in bot_config (accountant) aren't registry bots -> skipped;
            # manual_state bots (moon_bot) keep the status the user set.
            reg = fleet_registry.BOTS.get(bot_name)
            if reg is None or reg["manual_state"]:
                continue

            if bots[bot_name]['status'] != target_status:
                bots[bot_name]['status'] = target_status
                changes_made.append(f"{bot_name} -> {target_status}")

        gs = current_config.setdefault('global_settings', {})
        vix_rounded = round(vix_val, 2)
        # Persist whenever ANY published field moves — not only on a regime-label
        # flip or a status change. VIX drifts every cycle and macro_climate can
        # flip (MACRO_BEAR<->MACRO_BULL on price vs SMA200) while the label holds
        # (e.g. SIDEWAYS). Bots read THIS file, not InfluxDB, so a label-only write
        # guard left their routine VIX gating (wheel vix>22, etc.) running off a
        # frozen VIX. The kill-switch still fired — crossing 28 flips the label and
        # forces a write — but everything below it was stale.
        # Which provider carried this reading. On the stale fail-safe there is
        # no provider, so it is recorded as such rather than left showing the
        # last one that worked.
        vix_source = "stale_failsafe" if data_stale else (last_vix_source or "unknown")

        prev_published = (gs.get('market_condition'), gs.get('macro_climate'),
                          gs.get('vix'), gs.get('data_stale', False),
                          gs.get('vix_source'))
        new_published = (forced_regime, climate, vix_rounded, data_stale, vix_source)

        gs['market_condition'] = forced_regime
        gs['macro_climate'] = climate
        gs['vix'] = vix_rounded
        gs['data_stale'] = data_stale
        gs['vix_source'] = vix_source

        if changes_made or prev_published != new_published:
            # Stamp the persisted-time only when we actually write, so
            # regime_updated reflects the last time a published value landed.
            gs['regime_updated'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)

            if changes_made:
                flag = " [STALE FAIL-SAFE]" if data_stale else ""
                msg = (f"**Analyst Protocol Change**{flag}\n"
                       f"Condition: {forced_regime} "
                       f"(VIX: {vix_val:.2f} via {vix_source})\n"
                       f"Adjustments: {', '.join(changes_made)}")
                send_discord(msg)
                print(f"  -> Config Updated: {len(changes_made)} changes.")

    except Exception as e:
        registry.log_error("market_analyst", "update_bot_config", e)
        logger.error(f"[!] Config Update Error: {e}")

def _classify_regime(price, ema20, adx):
    """Pure regime rule (unchanged from V5): below EMA20 is a downtrend; above
    EMA20 with a trending ADX is an uptrend; otherwise chop."""
    if price < ema20:
        return "BEAR_TREND"
    if price > ema20 and adx > 25:
        return "BULL_TREND"
    return "SIDEWAYS"

def compute_regime(spy_df):
    """Regime + climate + the fields InfluxDB logs, from the SPY OHLC frame."""
    spy_df = spy_df.copy()
    spy_df['sma200'] = TechnicalMath.get_sma(spy_df['Close'], 200)
    spy_df['ema20'] = TechnicalMath.get_ema(spy_df['Close'], 20)
    spy_df['adx'] = TechnicalMath.get_adx(spy_df['High'], spy_df['Low'], spy_df['Close'], 14)

    latest = spy_df.iloc[-1]
    price = float(latest['Close'])
    sma200 = float(latest['sma200'])
    ema20 = float(latest['ema20'])
    adx = float(latest['adx'])

    climate = "MACRO_BULL" if price > sma200 else "MACRO_BEAR"
    regime = _classify_regime(price, ema20, adx)

    return regime, climate, price, sma200, ema20, adx

# Throttle state for the Discord failure ping.
_last_fail_alert = 0

def _alert_failure(missing, stale_secs):
    """Throttled Discord ping when the data fetch is failing."""
    global _last_fail_alert
    now = time.time()
    if now - _last_fail_alert < FAIL_ALERT_THROTTLE:
        return
    _last_fail_alert = now
    send_discord(
        f"🚨 **Market Analyst: market data fetch FAILING**\n"
        f"Missing: {', '.join(missing)}\n"
        f"No fresh regime for {stale_secs / 60:.0f} min — VIX kill-switch is "
        f"running blind. Degrades to elevated-risk posture "
        f"(CRITICAL_VOLATILITY, VIX {STALE_VIX_SENTINEL}) at "
        f"{STALE_REGIME_SECONDS / 60:.0f} min."
    )

def run_analyst():
    print("--- 🧠 MARKET ANALYST V6 (Alpaca SPY + hardened VIX) ---")
    print("    SPY via Alpaca; VIX via source chain "
          f"({', '.join(n for n, _ in VIX_SOURCES)}) — no Alpaca index feed.")
    print("    Loud on failure; degrades safe on staleness. Pause on VIX > 28.")

    # Start optimistic: the stale clock only starts counting once fetches begin
    # failing. A wedged first fetch reaches the fail-safe after STALE_REGIME_SECONDS.
    last_good = time.monotonic()

    while True:
        try:
            spy_df = get_spy_data()
            vix_val = get_vix_value()

            if spy_df is not None and vix_val is not None:
                regime, climate, price, sma200, ema20, adx = compute_regime(spy_df)
                stale_flag = " [was stale]" if (time.monotonic() - last_good) > STALE_REGIME_SECONDS else ""
                print(f"[{datetime.datetime.now().strftime('%H:%M')}] "
                      f"{regime} | VIX: {vix_val:.2f}{stale_flag}")

                # Publish: config drives gating/kill-switch; influx row is the
                # fleet's 'regime is live' heartbeat for the staleness watchdog.
                update_bot_config(regime, vix_val, climate, data_stale=False)
                log_to_influx(price, vix_val, adx, regime, sma200, ema20)
                last_good = time.monotonic()
            else:
                # LOUD failure — never a silent skip. Log every cycle (Grafana
                # cadence) + throttled Discord ping.
                missing = []
                if spy_df is None:
                    missing.append("SPY(Alpaca)")
                if vix_val is None:
                    missing.append("VIX(" +
                                   "/".join(n for n, _ in VIX_SOURCES) + ")")
                stale_secs = time.monotonic() - last_good
                registry.log_error(
                    "market_analyst", "get_market_data",
                    Exception(f"empty/failed data: {', '.join(missing)}"),
                    context=f"stale={stale_secs / 60:.0f}m")
                logger.error(f"[Analyst] data fetch incomplete: missing {missing}; "
                             f"regime NOT refreshed (stale {stale_secs / 60:.0f}m).")
                _alert_failure(missing, stale_secs)

                # Fail-safe: once blind long enough, stop trusting the frozen low
                # VIX and force an elevated-risk posture. Deliberately DO NOT write
                # a market_regime InfluxDB row here — the accountant's staleness
                # watchdog keys off that measurement's last-write time, so a
                # sentinel row would mask the very outage we're signaling.
                if stale_secs > STALE_REGIME_SECONDS:
                    logger.error(
                        f"[Analyst] STALE FAIL-SAFE engaged ({stale_secs / 60:.0f}m > "
                        f"{STALE_REGIME_SECONDS / 60:.0f}m): forcing CRITICAL_VOLATILITY "
                        f"+ VIX {STALE_VIX_SENTINEL} (data_stale).")
                    update_bot_config("CRITICAL_VOLATILITY", STALE_VIX_SENTINEL,
                                      "MACRO_BEAR", data_stale=True)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            registry.log_error("market_analyst", "run_analyst", e)
            logger.error(f"[!] Analyst Error: {e}", exc_info=True)
            time.sleep(60)

if __name__ == "__main__":
    run_analyst()
