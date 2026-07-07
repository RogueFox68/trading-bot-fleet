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

# yfinance is retained ONLY for the VIX spot index. Alpaca's data SDK exposes
# no index feed (there is no get_index_* / IndexHistoricalDataClient in
# alpaca-py), so SPY — which drives the regime — moves to Alpaca (reliable,
# already authenticated) while VIX stays on yfinance but as a hardened,
# isolated single-ticker fetch with retry + LOUD failure. This is the fix for
# the 2026-06-24 silent outage: yf.download(["SPY","^VIX"]) returned an empty
# frame (Yahoo rate-limiting), the compute-and-publish block skipped with no
# error, and VIX froze — disabling the VIX>28 kill-switch for ~12 days.
import yfinance as yf

# --- CONFIGURATION ---
CHECK_INTERVAL = 900   # 15 Minutes
CONFIG_FILE = "bot_config.json"
MARKET_SYMBOL = "SPY"

# Data-fetch resilience: retry an empty/failed pull a few times with
# exponential backoff before treating the cycle as a (loud) failure.
FETCH_RETRIES = 3
FETCH_BACKOFF = 3      # seconds base; 3s, 6s, 12s
MIN_BARS = 200         # need >=200 daily closes for SMA200

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
        data_str = (f'market_regime,symbol=SPY '
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

def get_vix_value():
    """Latest VIX spot from yfinance (single ticker, hardened + retried). Alpaca
    has no index feed, so VIX stays here — but isolated from SPY so a VIX hiccup
    can't take the regime down with it. Returns None (loud) on repeated
    empty/failed responses."""
    for attempt in range(FETCH_RETRIES):
        try:
            df = yf.download("^VIX", period="5d", interval="1d", progress=False)
            if df is not None and not df.empty:
                # yfinance may hand back MultiIndex columns (e.g. ('Close','^VIX'))
                # for a single ticker depending on version — flatten to the field.
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                if "Close" in df.columns:
                    close = df["Close"]
                    if isinstance(close, pd.DataFrame):  # duplicate-labeled cols
                        close = close.iloc[:, 0]
                    close = pd.to_numeric(close, errors="coerce").dropna()
                    if not close.empty:
                        val = float(close.iloc[-1])
                        if val > 0:
                            return val
            logger.error(f"[Analyst] VIX data empty (attempt {attempt + 1})")
        except Exception as e:
            registry.log_error("market_analyst", "get_vix_value", e,
                               context=f"attempt {attempt + 1}")
            logger.error(f"[Analyst] VIX fetch failed (attempt {attempt + 1}): {e}")
        if attempt < FETCH_RETRIES - 1:
            time.sleep(FETCH_BACKOFF * (2 ** attempt))
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
        prev_regime = gs.get('market_condition')
        prev_stale = gs.get('data_stale', False)
        gs['market_condition'] = forced_regime
        gs['macro_climate'] = climate
        gs['vix'] = round(vix_val, 2)
        gs['data_stale'] = data_stale
        gs['regime_updated'] = datetime.datetime.now(datetime.timezone.utc).isoformat()

        if changes_made or prev_regime != forced_regime or prev_stale != data_stale:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(current_config, f, indent=4)

            if changes_made:
                flag = " [STALE FAIL-SAFE]" if data_stale else ""
                msg = (f"**Analyst Protocol Change**{flag}\n"
                       f"Condition: {forced_regime} (VIX: {vix_val:.2f})\n"
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
    print("    SPY via Alpaca; VIX via yfinance (no Alpaca index feed).")
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
                    missing.append("VIX(yfinance)")
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
