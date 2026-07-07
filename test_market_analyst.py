"""Regression tests for the market_analyst regime pipeline.

Covers the fix for the 2026-06-24 silent-outage postmortem:
  - SPY moved to Alpaca get_stock_bars (df normalization, empty -> None).
  - VIX hardened on yfinance (empty -> None, no Alpaca index feed exists).
  - VIX>28 emergency pause fires on a simulated high-VIX reading (the headline
    kill-switch that sat inert for ~12 days).
  - Failures are LOUD: an incomplete fetch logs an error and never silently
    skips; the stale fail-safe degrades to an elevated-risk posture instead of
    trusting the frozen low VIX; and no market_regime heartbeat row is written
    while stale (so the accountant watchdog can still see the outage).

Run: python -m unittest test_market_analyst -v
"""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import pandas as pd

import market_analyst as ma


# Mirrors bot_config.template.json closely enough for the analyst's writes.
BASE_CONFIG = {
    "bots": {
        "wheel_bot": {"script": "wheel_bot.py", "status": "active", "allocation": 0.38},
        "trend_bot": {"script": "trend_bot.py", "status": "active", "allocation": 0.28},
        "survivor_bot": {"script": "survivor_bot.py", "status": "active", "allocation": 0.20},
        "crypto_grid": {"script": "crypto_grid.py", "status": "active", "allocation": 0.05},
        "moon_bot": {"script": "crypto_breakout.py", "status": "active", "allocation": 0.04},
        "accountant": {"script": "accountant.py", "status": "active", "allocation": 0.0},
    },
    "global_settings": {
        "market_condition": "SIDEWAYS",
        "emergency_stop": False,
        "macro_climate": "MACRO_BULL",
    },
}

# Non-manual registry bots the analyst is allowed to flip.
MANAGED_BOTS = [n for n, c in ma.fleet_registry.BOTS.items() if not c["manual_state"]]


def _spy_frame(rows=250, base=400.0, multiindex=True):
    """A synthetic SPY OHLC frame shaped like Alpaca's BarSet.df output."""
    ts = pd.date_range("2025-01-01", periods=rows, freq="D")
    closes = [base + i * 0.1 for i in range(rows)]
    data = {
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1_000_000] * rows,
    }
    if multiindex:
        idx = pd.MultiIndex.from_product(
            [["SPY"], ts], names=["symbol", "timestamp"])
        return pd.DataFrame(data, index=idx)
    return pd.DataFrame(data, index=ts)


def _normalized_spy(rows=250, base=400.0):
    """A frame shaped like get_spy_data's OUTPUT (Open/High/Low/Close), for
    tests that patch get_spy_data and feed compute_regime directly."""
    ts = pd.date_range("2025-01-01", periods=rows, freq="D")
    closes = [base + i * 0.1 for i in range(rows)]
    return pd.DataFrame({
        "Open": closes,
        "High": [c + 1 for c in closes],
        "Low": [c - 1 for c in closes],
        "Close": closes,
    }, index=ts)


class ClassifyRegimeTests(unittest.TestCase):
    def test_below_ema_is_bear(self):
        self.assertEqual(ma._classify_regime(price=90, ema20=100, adx=40), "BEAR_TREND")

    def test_above_ema_trending_is_bull(self):
        self.assertEqual(ma._classify_regime(price=110, ema20=100, adx=30), "BULL_TREND")

    def test_above_ema_weak_trend_is_sideways(self):
        self.assertEqual(ma._classify_regime(price=110, ema20=100, adx=15), "SIDEWAYS")


class UpdateBotConfigTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._write(BASE_CONFIG)
        patcher = mock.patch.object(ma, "CONFIG_FILE", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Never actually hit Discord from these tests.
        disc = mock.patch.object(ma, "send_discord")
        disc.start()
        self.addCleanup(disc.stop)

    def tearDown(self):
        os.remove(self.path)

    def _write(self, cfg):
        with open(self.path, "w") as f:
            json.dump(cfg, f)

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_high_vix_pauses_all_managed_bots(self):
        """VIX>28 kill-switch: the exact failure the postmortem flagged as inert.
        A simulated VIX=35 reading must pause every managed bot and force
        CRITICAL_VOLATILITY."""
        ma.update_bot_config("BULL_TREND", 35.0, "MACRO_BULL")
        cfg = self._read()
        gs = cfg["global_settings"]
        self.assertEqual(gs["market_condition"], "CRITICAL_VOLATILITY")
        self.assertEqual(gs["vix"], 35.0)
        self.assertFalse(gs["data_stale"])
        for bot in MANAGED_BOTS:
            self.assertEqual(cfg["bots"][bot]["status"], "paused",
                             f"{bot} should be paused at VIX>28")

    def test_normal_vix_keeps_bots_active(self):
        # Seed everyone paused, then a calm reading must revive them.
        seeded = json.loads(json.dumps(BASE_CONFIG))
        for bot in MANAGED_BOTS:
            seeded["bots"][bot]["status"] = "paused"
        self._write(seeded)

        ma.update_bot_config("BULL_TREND", 15.0, "MACRO_BULL")
        cfg = self._read()
        self.assertEqual(cfg["global_settings"]["market_condition"], "BULL_TREND")
        self.assertEqual(cfg["global_settings"]["vix"], 15.0)
        for bot in MANAGED_BOTS:
            self.assertEqual(cfg["bots"][bot]["status"], "active")

    def test_manual_state_bot_never_touched(self):
        """moon_bot is manual_state — the analyst must never flip it."""
        seeded = json.loads(json.dumps(BASE_CONFIG))
        seeded["bots"]["moon_bot"]["status"] = "paused"  # user's choice
        self._write(seeded)

        ma.update_bot_config("BULL_TREND", 15.0, "MACRO_BULL")  # target = active
        cfg = self._read()
        self.assertEqual(cfg["bots"]["moon_bot"]["status"], "paused")

    def test_infra_entry_not_a_registry_bot_untouched(self):
        # 'accountant' is in bot_config but not fleet_registry -> left alone.
        ma.update_bot_config("BULL_TREND", 35.0, "MACRO_BULL")
        cfg = self._read()
        self.assertEqual(cfg["bots"]["accountant"]["status"], "active")

    def test_stale_failsafe_posture_gates_without_full_pause(self):
        """The stale fail-safe must engage CRITICAL_VOLATILITY + data_stale, but
        with a sentinel VIX BELOW 28 so a data outage can't self-inflict a full
        fleet pmstop. Bots stay 'active' (gated by regime), not 'paused'."""
        ma.update_bot_config("CRITICAL_VOLATILITY", ma.STALE_VIX_SENTINEL,
                             "MACRO_BEAR", data_stale=True)
        cfg = self._read()
        gs = cfg["global_settings"]
        self.assertEqual(gs["market_condition"], "CRITICAL_VOLATILITY")
        self.assertTrue(gs["data_stale"])
        self.assertLess(gs["vix"], 28.0)  # never crosses the full-kill line
        for bot in MANAGED_BOTS:
            self.assertEqual(cfg["bots"][bot]["status"], "active")

    def test_missing_config_logs_error_no_crash(self):
        os.remove(self.path)
        with mock.patch.object(ma.registry, "log_error") as le:
            ma.update_bot_config("BULL_TREND", 15.0, "MACRO_BULL")
            le.assert_called_once()
        # recreate so tearDown's remove succeeds
        self._write(BASE_CONFIG)


class GetSpyDataTests(unittest.TestCase):
    def setUp(self):
        # Kill backoff sleeps so failure paths return instantly.
        s = mock.patch.object(ma.time, "sleep")
        s.start()
        self.addCleanup(s.stop)

    def test_normalizes_multiindex_barset(self):
        bars = types.SimpleNamespace(df=_spy_frame(rows=250, multiindex=True))
        with mock.patch.object(ma._data_client, "get_stock_bars", return_value=bars):
            out = ma.get_spy_data()
        self.assertIsNotNone(out)
        self.assertEqual(list(out.columns), ["Open", "High", "Low", "Close"])
        self.assertGreaterEqual(len(out), ma.MIN_BARS)

    def test_empty_frame_returns_none(self):
        bars = types.SimpleNamespace(df=pd.DataFrame())
        with mock.patch.object(ma._data_client, "get_stock_bars", return_value=bars):
            self.assertIsNone(ma.get_spy_data())

    def test_too_few_bars_returns_none(self):
        bars = types.SimpleNamespace(df=_spy_frame(rows=50, multiindex=True))
        with mock.patch.object(ma._data_client, "get_stock_bars", return_value=bars):
            self.assertIsNone(ma.get_spy_data())

    def test_exception_returns_none_and_logs(self):
        with mock.patch.object(ma._data_client, "get_stock_bars",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(ma.registry, "log_error") as le:
            self.assertIsNone(ma.get_spy_data())
            self.assertTrue(le.called)


class GetVixValueTests(unittest.TestCase):
    def setUp(self):
        s = mock.patch.object(ma.time, "sleep")
        s.start()
        self.addCleanup(s.stop)

    def test_returns_latest_close(self):
        df = pd.DataFrame({"Close": [18.0, 19.5, 21.25]},
                          index=pd.date_range("2025-01-01", periods=3))
        with mock.patch.object(ma.yf, "download", return_value=df):
            self.assertAlmostEqual(ma.get_vix_value(), 21.25)

    def test_multiindex_columns_squeezed(self):
        cols = pd.MultiIndex.from_tuples([("Close", "^VIX")])
        df = pd.DataFrame([[18.0], [30.0]], columns=cols,
                          index=pd.date_range("2025-01-01", periods=2))
        with mock.patch.object(ma.yf, "download", return_value=df):
            self.assertAlmostEqual(ma.get_vix_value(), 30.0)

    def test_empty_returns_none(self):
        with mock.patch.object(ma.yf, "download", return_value=pd.DataFrame()):
            self.assertIsNone(ma.get_vix_value())


class _StopLoop(Exception):
    """Sentinel to break run_analyst after one iteration."""


class RunAnalystLoopTests(unittest.TestCase):
    """End-to-end wiring: one loop iteration for each branch, then bail out via
    a patched time.sleep."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(self.path, "w") as f:
            json.dump(json.loads(json.dumps(BASE_CONFIG)), f)
        p = mock.patch.object(ma, "CONFIG_FILE", self.path)
        p.start()
        self.addCleanup(p.stop)
        d = mock.patch.object(ma, "send_discord")
        d.start()
        self.addCleanup(d.stop)
        # sleep(CHECK_INTERVAL) ends the single iteration we want to test.
        sl = mock.patch.object(ma.time, "sleep", side_effect=_StopLoop)
        sl.start()
        self.addCleanup(sl.stop)

    def tearDown(self):
        os.remove(self.path)

    def _read(self):
        with open(self.path) as f:
            return json.load(f)

    def test_high_vix_cycle_pauses_fleet_and_writes_heartbeat(self):
        """Simulated live VIX=35 through the real loop: fleet pauses AND a fresh
        market_regime heartbeat row is written."""
        with mock.patch.object(ma, "get_spy_data", return_value=_normalized_spy()), \
             mock.patch.object(ma, "get_vix_value", return_value=35.0), \
             mock.patch.object(ma, "log_to_influx") as influx:
            with self.assertRaises(_StopLoop):
                ma.run_analyst()
        cfg = self._read()
        self.assertEqual(cfg["global_settings"]["market_condition"], "CRITICAL_VOLATILITY")
        for bot in MANAGED_BOTS:
            self.assertEqual(cfg["bots"][bot]["status"], "paused")
        influx.assert_called_once()  # heartbeat written on a good fetch

    def test_failed_fetch_is_loud_and_writes_no_heartbeat(self):
        """Incomplete fetch: logs an error, pings (throttled), and writes NO
        market_regime row (so the accountant watchdog still detects staleness)."""
        ma._last_fail_alert = 0
        with mock.patch.object(ma, "get_spy_data", return_value=None), \
             mock.patch.object(ma, "get_vix_value", return_value=None), \
             mock.patch.object(ma, "log_to_influx") as influx, \
             mock.patch.object(ma.registry, "log_error") as le, \
             mock.patch.object(ma, "send_discord") as disc:
            with self.assertRaises(_StopLoop):
                ma.run_analyst()
        self.assertTrue(le.called)         # loud
        self.assertTrue(disc.called)       # alerted
        influx.assert_not_called()         # no false heartbeat

    def test_sustained_staleness_engages_failsafe(self):
        """After STALE_REGIME_SECONDS of failure, the loop degrades to the
        elevated-risk posture (data_stale, sentinel VIX < 28)."""
        ma._last_fail_alert = 0
        # First monotonic() seeds last_good; second is far in the future so the
        # very first failing cycle is already 'stale'.
        clock = iter([1000.0, 1000.0 + ma.STALE_REGIME_SECONDS + 60])
        with mock.patch.object(ma, "get_spy_data", return_value=None), \
             mock.patch.object(ma, "get_vix_value", return_value=None), \
             mock.patch.object(ma, "log_to_influx") as influx, \
             mock.patch.object(ma.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(_StopLoop):
                ma.run_analyst()
        cfg = self._read()
        gs = cfg["global_settings"]
        self.assertEqual(gs["market_condition"], "CRITICAL_VOLATILITY")
        self.assertTrue(gs["data_stale"])
        self.assertLess(gs["vix"], 28.0)
        influx.assert_not_called()  # still no heartbeat while stale


if __name__ == "__main__":
    unittest.main()
