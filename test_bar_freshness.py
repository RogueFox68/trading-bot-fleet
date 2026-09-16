"""Regression: the bars a bot trades on are the NEWEST ones, and fresh.

--- The defect this pins ---------------------------------------------------

Alpaca returns bars ascending from `start` and truncates the response at
`limit`. So `StockBarsRequest(start=<wide window>, limit=<small N>)` returns
the OLDEST N bars in that window. Nothing raises. The frame has the right
columns, the right dtypes and a plausible price — it is simply weeks old, and
every indicator computed on it is confidently wrong.

Measured on 2026-09-11 by reissuing the deployed request shapes from inside
the fleet container:

    survivor 15m     start=-20d,  limit=200  -> newest bar Aug 27 (~2wk old)
    survivor SMA200  start=-460d, limit=250  -> newest bar Jun 5  (~14wk old)
    moon donchian    start=-60d,  limit=30   -> newest bar Aug 12 (~4wk old)
    trend 15m        start=-10d,  limit=500  -> current

Trend was correct only by accident: a 10-day 15m window holds ~280 bars, so
its limit=500 never truncated. That is the whole reason this went unnoticed
for months — the bug is invisible unless limit < bars-in-window, and one of
the four sites happened to be on the right side of that line.

The load-bearing test here is test_no_fetch_site_pairs_start_with_limit: it
reads the source and fails if any bar request reintroduces `limit` next to
`start`. The unit tests below cover the helpers; that one covers the mistake.
"""
import ast
import contextlib
import datetime as dt_mod
import io
import sys
import unittest
from unittest import mock

import pandas as pd

import fleet_doctor
import utils

UTC = dt_mod.timezone.utc
FETCH_SITE_FILES = ["survivor_bot.py", "trend_bot.py", "crypto_breakout.py",
                    "market_analyst.py", "fleet_doctor.py"]
BAR_REQUESTS = {"StockBarsRequest", "CryptoBarsRequest"}


def frame(end, count, step_seconds, price=100.0):
    """`count` bars ending at `end`, ascending — the shape Alpaca returns."""
    idx = pd.to_datetime(
        [end - dt_mod.timedelta(seconds=step_seconds * i) for i in range(count - 1, -1, -1)],
        utc=True)
    return pd.DataFrame({"open": price, "high": price, "low": price,
                         "close": price, "volume": 1.0}, index=idx)


class BarRequestShapeTest(unittest.TestCase):
    """No bar request may pair `start` with `limit`."""

    def test_no_fetch_site_pairs_start_with_limit(self):
        offenders = []
        for path in FETCH_SITE_FILES:
            with open(path) as f:
                tree = ast.parse(f.read(), filename=path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name not in BAR_REQUESTS:
                    continue
                kwargs = {kw.arg for kw in node.keywords}
                if "start" in kwargs and "limit" in kwargs:
                    offenders.append(f"{path}:{node.lineno} {name}(start=..., limit=...)")
        self.assertEqual(
            offenders, [],
            "A bar request pairs `start` with `limit`, which returns the OLDEST "
            "bars in the window, not the newest. Size the window to the history "
            "needed and slice with utils.newest_bars instead.\n  "
            + "\n  ".join(offenders))


class NewestBarsTest(unittest.TestCase):

    def test_takes_the_tail_not_the_head(self):
        now = dt_mod.datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
        df = frame(now, 100, 900)
        got = utils.newest_bars(df, 10)
        self.assertEqual(len(got), 10)
        self.assertEqual(got.index[-1], df.index[-1])          # newest kept
        self.assertEqual(got.index[0], df.index[-10])          # oldest dropped

    def test_shorter_than_requested_is_returned_whole(self):
        df = frame(dt_mod.datetime(2026, 9, 11, tzinfo=UTC), 5, 900)
        self.assertEqual(len(utils.newest_bars(df, 50)), 5)

    def test_no_count_is_a_passthrough(self):
        df = frame(dt_mod.datetime(2026, 9, 11, tzinfo=UTC), 5, 900)
        self.assertIs(utils.newest_bars(df, 0), df)
        self.assertIs(utils.newest_bars(df, None), df)


class BarFreshnessTest(unittest.TestCase):

    def setUp(self):
        self.now = dt_mod.datetime(2026, 9, 11, 16, 0, tzinfo=UTC)

    def test_current_bars_are_fresh(self):
        df = frame(self.now - dt_mod.timedelta(minutes=15), 50, 900)
        self.assertTrue(utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=self.now))

    def test_the_measured_survivor_staleness_is_rejected(self):
        # Aug 27 data on Sep 11 — the actual observed 15m frame.
        df = frame(dt_mod.datetime(2026, 8, 27, 9, 45, tzinfo=UTC), 200, 900)
        self.assertFalse(utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=self.now))

    def test_the_measured_sma200_staleness_is_rejected(self):
        # Jun 5 daily closes on Sep 11 — the actual observed SMA200 frame.
        df = frame(dt_mod.datetime(2026, 6, 5, tzinfo=UTC), 250, 86400)
        self.assertFalse(utils.bars_are_fresh(df, 86400, "survivor_bot", "AAPL",
                                              "SMA200", stale_factor=4.0, now=self.now))

    def test_a_weekend_gap_is_not_stale_for_daily_bars(self):
        # Friday's bar read on Monday must NOT trip the daily guard: a false
        # stale verdict stands a bot down on a perfectly normal market close.
        friday = dt_mod.datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
        monday = dt_mod.datetime(2026, 9, 7, 14, 0, tzinfo=UTC)
        df = frame(friday, 250, 86400)
        self.assertTrue(utils.bars_are_fresh(df, 86400, "survivor_bot", "AAPL",
                                             "SMA200", stale_factor=4.0, now=monday))

    def test_undateable_frame_is_rejected_not_assumed_good(self):
        df = pd.DataFrame({"close": [1.0, 2.0]}, index=[0, 1])
        self.assertFalse(utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=self.now))

    def test_session_open_gap_is_not_an_outage(self):
        # Monday 09:30. The newest 15m bar is Friday's close, ~65h old — any
        # honest intraday bound rejects it, so without session awareness EVERY
        # session open reads as a data outage and stands the bots down.
        friday_close = dt_mod.datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
        monday_open = dt_mod.datetime(2026, 9, 7, 13, 30, tzinfo=UTC)
        df = frame(friday_close, 200, 900)
        self.assertFalse(
            utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=monday_open),
            "the raw bound should reject this; session awareness is what rescues it")
        self.assertTrue(
            utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=monday_open,
                                 session_elapsed=0),
            "a frame at the session open was treated as an outage")

    def test_the_open_allowance_does_not_accept_anything(self):
        # The first attempt at the session allowance returned True BEFORE
        # inspecting the timestamp, so a two-week-old bar sailed through the
        # opening 45 minutes and both equity bots could compute entry signals
        # from it.
        monday_open = dt_mod.datetime(2026, 9, 7, 13, 30, tzinfo=UTC)
        ancient = frame(monday_open - dt_mod.timedelta(days=14), 200, 900)
        self.assertFalse(
            utils.bars_are_fresh(ancient, 900, "survivor_bot", "AAPL",
                                 now=monday_open + dt_mod.timedelta(minutes=30),
                                 session_elapsed=1800),
            "a 14-day-old bar was accepted during the opening allowance")

    def test_the_open_allowance_still_rejects_an_undateable_frame(self):
        df = pd.DataFrame({"close": [1.0, 2.0]}, index=[0, 1])
        self.assertFalse(
            utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL",
                                 now=dt_mod.datetime(2026, 9, 7, 14, 0, tzinfo=UTC),
                                 session_elapsed=1800),
            "an undateable frame skipped the check entirely at the open")

    def test_a_prior_session_bar_is_accepted_across_a_holiday_weekend(self):
        # Friday close -> Tuesday open after a Monday holiday is the longest
        # legitimate gap the equity bots see (~89.5h).
        friday_close = dt_mod.datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
        tuesday_open = dt_mod.datetime(2026, 9, 8, 13, 35, tzinfo=UTC)
        df = frame(friday_close, 200, 900)
        self.assertTrue(
            utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL",
                                 now=tuesday_open, session_elapsed=300))

    def test_the_check_resumes_once_the_session_is_underway(self):
        # 90 minutes in, there has been ample time for bars; a Friday frame is
        # then genuinely stale and must be rejected.
        friday_close = dt_mod.datetime(2026, 9, 4, 20, 0, tzinfo=UTC)
        later = dt_mod.datetime(2026, 9, 7, 15, 0, tzinfo=UTC)
        df = frame(friday_close, 200, 900)
        self.assertFalse(utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL",
                                              now=later, session_elapsed=90 * 60))

    def test_naive_timestamps_are_read_as_utc(self):
        idx = pd.to_datetime([self.now.replace(tzinfo=None) - dt_mod.timedelta(minutes=15)])
        df = pd.DataFrame({"close": [1.0]}, index=idx)
        self.assertTrue(utils.bars_are_fresh(df, 900, "survivor_bot", "AAPL", now=self.now))


class DropFormingBarTest(unittest.TestCase):
    """`df.iloc[:-1]` is only correct when the frame actually ends at now."""

    def test_forming_bar_is_dropped(self):
        now = dt_mod.datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        df = frame(dt_mod.datetime(2026, 9, 11, 0, 0, tzinfo=UTC), 30, 86400)
        got = utils.drop_forming_bar(df, 86400, now=now)
        self.assertEqual(len(got), 29)
        self.assertEqual(got.index[-1], df.index[-2])

    def test_completed_final_bar_is_kept(self):
        # The moon_bot compounding bug: on a frame that already ends in the
        # past, iloc[:-1] threw away a COMPLETED bar and aged the levels by
        # one more day on top of the truncation.
        now = dt_mod.datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        df = frame(dt_mod.datetime(2026, 8, 12, 0, 0, tzinfo=UTC), 30, 86400)
        got = utils.drop_forming_bar(df, 86400, now=now)
        self.assertEqual(len(got), 30)
        self.assertEqual(got.index[-1], df.index[-1])

    def test_undateable_frame_keeps_the_conservative_behavior(self):
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=[0, 1, 2])
        self.assertEqual(len(utils.drop_forming_bar(df, 86400)), 2)


if __name__ == "__main__":
    unittest.main()


# --- The diagnostic itself -------------------------------------------------

class _StubBot:
    def __init__(self):
        self.market_hours = True
        self.session_elapsed = None


class _StubBotModule:
    """Stands in for survivor_bot/trend_bot: a `bot` and a tuple-returning fetcher."""

    BAR_SECONDS = 15 * 60

    def __init__(self, result):
        self.bot = _StubBot()
        self._result = result
        self.calls = 0

    def get_data_alpaca(self, symbol):
        self.calls += 1
        return self._result


class BarProbeContractTest(unittest.TestCase):
    """fleet_doctor's probe must speak the fetchers' actual return contract.

    --- The defect this pins ----------------------------------------------

    `get_data_alpaca` returns `(df, indicators_ok)`. Section 5b called it and
    used the result as a DataFrame:

        df = fetch()                 # df is really a 2-tuple
        if df is None or len(df) == 0:   # len(tuple) == 2, so it passes
        age = utils.bar_age_seconds(df)  # a tuple has no index -> None
        bad(f"{len(df)} bars, but no usable timestamp.")

    which printed, on a live container with a perfectly healthy feed:

        [ FAIL ] survivor_bot 15m (SPY): 2 bars, but no usable timestamp.
        [ FAIL ] trend_bot 15m (SPY): 2 bars, but no usable timestamp.

    "2 bars" was the tuple's arity. The bots themselves were fine — both
    unpack correctly — so the ONLY thing broken was the check built to catch a
    plausible-looking frame that is silently wrong. It reported its own type
    error in exactly that shape: a specific, credible number, produced by
    code that never looked at the data.

    Both defects shipped in the same change, which is why neither review nor
    the suite caught it: the tuple contract and its only out-of-bot caller
    were written together, and no test drove the caller.
    """

    def setUp(self):
        import fleet_doctor
        self.fd = fleet_doctor
        self._saved = {k: sys.modules.get(k)
                       for k in ("survivor_bot", "trend_bot", "crypto_breakout")}
        # crypto_breakout's probe is a separate try/except; keep it out of the way.
        sys.modules["crypto_breakout"] = None
        self.fd._results = []

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        self.fd._results = []

    def _run(self, survivor_result, trend_result):
        mods = {"survivor_bot": _StubBotModule(survivor_result),
                "trend_bot": _StubBotModule(trend_result)}
        sys.modules.update(mods)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fd.check_bar_freshness(config={"bots": {}})
        return mods, [r for r in self.fd._results if r[1] == "bars"]

    def _fresh_frame(self):
        now = dt_mod.datetime.now(UTC)
        return frame(now - dt_mod.timedelta(minutes=5), 200, 900)

    def test_fresh_tuple_passes(self):
        """The live-container case: a healthy feed must not read as a failure."""
        df = self._fresh_frame()
        mods, results = self._run((df, True), (df, True))
        self.assertEqual(mods["survivor_bot"].calls, 1)
        self.assertTrue(results, "the probe emitted nothing at all")
        self.assertFalse([r for r in results if r[0] == "FAIL"],
                         f"healthy frame reported as a failure: {results}")
        # And the count printed is the BAR count, never the tuple's arity.
        self.assertTrue(any("200 bars" in r[2] for r in results), results)
        self.assertFalse(any("2 bars" in r[2] for r in results),
                         "reported the tuple's length as a bar count")

    def test_stale_tuple_fails_with_its_age(self):
        """A stale frame is (df, False) — report the age, not 'no frame'."""
        old = frame(dt_mod.datetime.now(UTC) - dt_mod.timedelta(days=14), 200, 900)
        _, results = self._run((old, False), (old, False))
        fails = [r for r in results if r[0] == "FAIL"]
        self.assertEqual(len(fails), 2, results)
        self.assertTrue(all("bars, newest" in r[2] for r in fails), fails)
        self.assertTrue(all("NOT compute indicators" in r[2] for r in fails), fails)

    def test_failed_fetch_reports_no_frame(self):
        """(None, False) is the empty/raised case and must stay distinguishable."""
        _, results = self._run((None, False), (None, False))
        fails = [r for r in results if r[0] == "FAIL"]
        self.assertEqual(len(fails), 2, results)
        self.assertTrue(all("no frame at all" in r[2] for r in fails), fails)

    def test_probe_primes_session_elapsed_like_a_real_cycle(self):
        """Without this the probe false-FAILs every weekday morning.

        The bot widens its freshness bound by how far into the session it is
        (utils.bars_are_fresh(session_elapsed=...)), a value FleetBot.refresh()
        sets each cycle. fleet_doctor never calls refresh(), so the attribute
        stayed None and the flat 45-minute bound applied — at 09:35, with the
        newest bar being Friday's close, the fleet is correct and the doctor
        calls it an outage.
        """
        import fleet_bot
        df = self._fresh_frame()
        mods, _ = self._run((df, True), (df, True))
        expected = fleet_bot.session_elapsed_seconds(True)
        for name, mod in mods.items():
            self.assertIsNotNone(mod.bot.session_elapsed,
                                 f"{name}: probe left session_elapsed unprimed")
            # Same implementation, so the two agree to within the clock tick.
            self.assertAlmostEqual(mod.bot.session_elapsed, expected, delta=5,
                                   msg=f"{name}: probe computed its own session bound")


class VixSourceClassificationTest(unittest.TestCase):
    """A dead VIX source is a failure only when it was the last one.

    --- The defect this pins ----------------------------------------------

    The chain exists so one provider can die without touching the kill-switch,
    and stooq has returned HTTP 404 since 2026-09-06. Every run nonetheless
    printed it as [ FAIL ], counted it in the summary, and — through
    `return 1 if fails else 0` — made a completely healthy fleet exit non-zero
    forever. That is rule 10 in the section whose own header already says "a
    red line here is not an outage by itself".
    """

    def setUp(self):
        import fleet_doctor
        import market_analyst
        self.fd = fleet_doctor
        self.ma = market_analyst
        self._saved_sources = market_analyst.VIX_SOURCES
        self.fd._results = []

    def tearDown(self):
        self.ma.VIX_SOURCES = self._saved_sources
        self.fd._results = []

    def _run(self, sources):
        self.ma.VIX_SOURCES = sources
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fd.check_vix()
        return [r for r in self.fd._results if r[1] == "vix"]

    @staticmethod
    def _dead():
        def f():
            raise RuntimeError("HTTP 404")
        return f

    def test_dead_source_with_a_spare_is_a_warning(self):
        results = self._run([("cboe", lambda: 15.84),
                             ("stooq", self._dead()),
                             ("yfinance", lambda: 15.84)])
        self.assertFalse([r for r in results if r[0] == "FAIL"],
                         f"a covered source failed the run: {results}")
        warns = [r for r in results if r[0] == "WARN"]
        self.assertEqual(len(warns), 1, results)
        self.assertIn("stooq", warns[0][2])
        self.assertIn("covered", warns[0][2])

    def test_last_source_dying_is_a_failure(self):
        results = self._run([("cboe", self._dead()),
                             ("stooq", self._dead()),
                             ("yfinance", self._dead())])
        fails = [r for r in results if r[0] == "FAIL"]
        self.assertTrue(any("NO VIX source" in r[2] for r in fails), results)
        # Each individual death is a failure too — nothing is covering them.
        self.assertGreaterEqual(len(fails), 4, results)

    def test_out_of_band_reading_counts_as_dead(self):
        """A garbage number must not be mistaken for a live spare."""
        results = self._run([("cboe", lambda: 900.0), ("stooq", self._dead())])
        fails = [r for r in results if r[0] == "FAIL"]
        self.assertTrue(any("NO VIX source" in r[2] for r in fails), results)


class ErrorRateClassificationTest(unittest.TestCase):
    """fleet_doctor section 7c: a bot that catches its own exception.

    --- The defect this pins ----------------------------------------------

    moon_bot raised KeyError('outbox') on the FIRST statement of every cycle
    from 2026-09-11 16:57Z, about one a minute, ~5,500 errors over four days.
    The doctor reported 34 passed / 0 failed / 2 warnings throughout, because
    every check it had was blind to it:

      * PM2 said `online` — fleet_bot.run catches the exception and sleeps 60s.
      * The import check passed — crypto_breakout imports fine; only its data
        path was broken.

    Neither process health nor import health can see a caught exception. The
    error series is the only witness, and nothing read it. Four days.

    These drive the classifier directly, so the thresholds are exercised
    without a live InfluxDB (rule 25: the interesting half is the check's own
    logic, and "it needs a live database" is why these ship untested).
    """

    # The live shape on 2026-09-15, before Task 1 was deployed.
    MOON = ("moon_bot", "main_loop", "KeyError")

    def test_the_live_moon_bot_data_fails(self):
        findings, offenders = fleet_doctor.classify_error_rates(
            {self.MOON: 5500}, {self.MOON: 60})
        self.assertTrue(findings, "the four-day outage produced no finding")
        status, msg, _ = findings[0]
        self.assertEqual(status, "FAIL")
        self.assertIn("moon_bot", msg)
        self.assertIn("main_loop", msg)
        self.assertIn("KeyError", msg)
        self.assertEqual(offenders[0][0], self.MOON)

    def test_the_cadence_is_named_in_seconds(self):
        findings, _ = fleet_doctor.classify_error_rates(
            {self.MOON: 5500}, {self.MOON: 60})
        self.assertIn("60s", findings[0][1],
                      "one error a minute should be reported as a 60s cadence")

    def test_a_quiet_fleet_produces_no_findings(self):
        # The documented baseline: 1-15 errors a day, fleet-wide.
        groups = {("trend_bot", "main_loop", "APIError"): 7,
                  ("wheel_bot", "close_option", "TimeoutError"): 3}
        findings, _ = fleet_doctor.classify_error_rates(groups, {})
        self.assertEqual(findings, [], "normal traffic must stay silent (rule 10)")

    def test_a_fast_loop_fails_before_it_reaches_the_24h_threshold(self):
        """A crash 45 minutes old must not wait a day to show.

        At moon_bot's 60s error cadence the 24h rule needs 200 errors — over
        three hours. The per-hour rule catches the same bot in 30 minutes.
        """
        key = ("survivor_bot", "main_loop", "ValueError")
        findings, _ = fleet_doctor.classify_error_rates({key: 45}, {key: 45})
        self.assertTrue(findings)
        self.assertEqual(findings[0][0], "FAIL")
        self.assertIn("loop cadence", findings[0][1])

    def test_a_short_burst_that_already_stopped_is_not_a_loop(self):
        """20 errors an hour ago and nothing since is not a per-cycle failure.

        Detection latency is the deliberate cost: at a 60s cadence the
        per-hour rule fires after 30 minutes, and a slower loop (the
        accountant's 300s cycle is 12/h) is caught by the 24h rule instead.
        Firing on any short burst would make the section unreadable.
        """
        key = ("trend_bot", "main_loop", "APIError")
        findings, _ = fleet_doctor.classify_error_rates({key: 20}, {key: 0})
        self.assertEqual(findings, [])

    def test_elevated_but_not_looping_is_a_warning(self):
        key = ("wheel_bot", "close_option", "TimeoutError")
        findings, _ = fleet_doctor.classify_error_rates({key: 80}, {key: 2})
        self.assertEqual([f[0] for f in findings], ["WARN"])
        self.assertIn("wheel_bot", findings[0][1])

    def test_a_bots_errors_are_summed_across_groups_for_the_warning(self):
        groups = {("wheel_bot", "close_option", "TimeoutError"): 30,
                  ("wheel_bot", "main_loop", "APIError"): 30}
        findings, _ = fleet_doctor.classify_error_rates(groups, {})
        self.assertEqual([f[0] for f in findings], ["WARN"])
        self.assertIn("60 errors", findings[0][1])

    def test_a_failing_bot_is_not_also_warned_about(self):
        """One condition, one alert — a duplicate ping is rule 10 noise."""
        groups = {self.MOON: 5500, ("moon_bot", "reconcile_pending", "APIError"): 60}
        findings, _ = fleet_doctor.classify_error_rates(groups, {self.MOON: 60})
        self.assertEqual([f[0] for f in findings], ["FAIL"],
                         "moon_bot should FAIL once, not FAIL and WARN")

    def test_offenders_are_ranked_worst_first_and_capped(self):
        groups = {(f"bot{i}", "main_loop", "E"): i for i in range(1, 12)}
        _, offenders = fleet_doctor.classify_error_rates(groups, {})
        self.assertEqual(len(offenders), fleet_doctor.ERROR_TOP_N)
        self.assertEqual([c for _, c in offenders], [11, 10, 9, 8, 7])

    def test_thresholds_sit_above_the_documented_baseline(self):
        # 1-15 a day was normal; a threshold inside that band would fire on
        # healthy operation, which trains you to ignore the report (rule 10).
        self.assertGreater(fleet_doctor.ERROR_WARN_24H, 15)
        self.assertGreater(fleet_doctor.ERROR_FAIL_24H, fleet_doctor.ERROR_WARN_24H)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeRequests:
    def __init__(self, payload):
        self._payload = payload
        self.queries = []

    def get(self, url, params=None, timeout=None):
        self.queries.append((params or {}).get("q", ""))
        return _FakeResponse(self._payload)


class AccountingAnomalyCheckTest(unittest.TestCase):
    """A metric written EVERY cycle cannot be graded on row presence.

    accountant writes accounting_anomaly unconditionally, zero included —
    precisely so a cleared condition is visible rather than the series just
    stopping. So "warn if it has rows in the last hour" would warn forever on
    a healthy fleet. The signal is count > 0.
    """

    class _Cfg:
        INFLUX_HOST, INFLUX_PORT, INFLUX_DB_NAME = "influxdb", 8086, "trading_bots"

    def _run(self, columns, values):
        payload = {"results": [{"series": [{"columns": columns,
                                            "values": [values]}]}]} if columns \
            else {"results": [{}]}
        req = _FakeRequests(payload)
        fleet_doctor._results.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fleet_doctor._check_accounting_anomaly(self._Cfg(), req)
        statuses = [r[0] for r in fleet_doctor._results]
        fleet_doctor._results.clear()
        return statuses, buf.getvalue()

    def test_a_zero_count_row_is_a_pass(self):
        statuses, _ = self._run(
            ["time", "kind", "count", "affected_bots", "symbols", "bots"],
            [0, "negative_long_cost_basis", 0, 0, "", ""])
        self.assertEqual(statuses, ["PASS"],
                         "a clean row must not warn — it is written every cycle")

    def test_the_observed_anomaly_warns_and_names_the_positions(self):
        statuses, out = self._run(
            ["time", "kind", "count", "affected_bots", "symbols", "bots"],
            [0, "negative_long_cost_basis", 2, 1, "ETHUSD,SOLUSD", "crypto_grid"])
        self.assertEqual(statuses, ["WARN"])
        self.assertIn("ETHUSD", out)
        self.assertIn("SOLUSD", out)
        self.assertIn("crypto_grid", out)

    def test_no_rows_at_all_means_the_accountant_is_not_running(self):
        statuses, out = self._run(None, None)
        self.assertEqual(statuses, ["WARN"])
        self.assertIn("not running", out)


class VixChainDepthTest(unittest.TestCase):
    """With stooq removed the chain is two deep, so one loss is the last spare.

    The old grading said "the chain has a spare" for any live < total, which
    on a two-source chain is exactly wrong: one dead source means the NEXT
    failure drops the fleet onto the stale fail-safe. Rule 27 tolerates a
    covered failure, but the tolerance has to end where the cover does.
    """

    def setUp(self):
        import market_analyst
        self.fd = fleet_doctor
        self.ma = market_analyst
        self._saved = market_analyst.VIX_SOURCES
        self.fd._results = []

    def tearDown(self):
        self.ma.VIX_SOURCES = self._saved
        self.fd._results = []

    def _run(self, sources):
        self.ma.VIX_SOURCES = sources
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.fd.check_vix()
        return [r for r in self.fd._results if r[1] == "vix"], buf.getvalue()

    @staticmethod
    def _dead():
        def f():
            raise RuntimeError("HTTP 404")
        return f

    def test_the_shipped_chain_is_intraday_only(self):
        self.assertEqual([n for n, _ in self.ma.VIX_SOURCES], ["cboe", "yfinance"])

    def test_one_of_two_live_warns_that_there_is_no_spare(self):
        results, out = self._run([("cboe", lambda: 15.84),
                                  ("yfinance", self._dead())])
        self.assertFalse([r for r in results if r[0] == "FAIL"],
                         "a covered source must not fail the run (rule 27)")
        self.assertTrue([r for r in results if r[0] == "WARN"
                         and "NO spare" in r[2]],
                        f"losing the last spare was reported as fine: {results}")

    def test_both_live_is_clean(self):
        results, _ = self._run([("cboe", lambda: 15.84),
                                ("yfinance", lambda: 15.9)])
        self.assertFalse([r for r in results if r[0] in ("FAIL", "WARN")], results)

    def test_all_dead_still_fails(self):
        results, _ = self._run([("cboe", self._dead()),
                                ("yfinance", self._dead())])
        self.assertTrue([r for r in results if r[0] == "FAIL"],
                        "an entirely dead chain must fail")


class _StubResponse:
    def __init__(self, status_code=200, payload=None, raises=False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises
        self.text = str(payload)

    def json(self):
        if self._raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class _StubRequests:
    def __init__(self, response):
        self._response = response

    def get(self, url, params=None, timeout=None):
        return self._response


class UnreadableErrorSeriesTest(unittest.TestCase):
    """An unreadable database must never read as a healthy fleet.

    --- The defect this pins ----------------------------------------------

    Section 7c's reader went straight to

        r.json().get("results", [{}])[0].get("series") or []

    which turns EVERY failure shape into an empty list, and an empty list into
    "0 errors". With requests stubbed to return HTTP 401 and
    {"error": "authorization failed"}, or HTTP 200 carrying
    {"results": [{"error": "query timeout exceeded"}]}, check_error_rate
    printed:

        [  ok  ] 0 error(s) in 24h across 0 group(s) — within the 1-15/day
                 baseline band.

    So the one check built to notice a bot failing every cycle reported a
    clean fleet precisely when it could not see. That is rule 25 turned on
    7c itself: it answered with the same confident formatting whether or not
    it had looked — the same shape as section 5b's "2 bars".

    Absence of rows and inability to READ rows are different facts. Only a
    query that succeeded and came back empty may say the fleet is quiet.
    """

    class _Cfg:
        INFLUX_HOST, INFLUX_PORT, INFLUX_DB_NAME = "influxdb", 8086, "trading_bots"

    def _run(self, response):
        req = _StubRequests(response)
        fleet_doctor._results.clear()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with mock.patch.dict(sys.modules, {"requests": req}):
                fleet_doctor.check_error_rate(self._Cfg())
        results = list(fleet_doctor._results)
        fleet_doctor._results.clear()
        return results, buf.getvalue()

    def _assert_blind_not_healthy(self, response, label):
        results, out = self._run(response)
        self.assertFalse([r for r in results if r[0] == "PASS"],
                         f"{label}: an unreadable query reported a healthy fleet")
        self.assertTrue([r for r in results if r[0] == "WARN"
                         and "Could not read bot_error_events" in r[2]],
                        f"{label}: no monitoring-unavailable warning — got {results}")
        self.assertNotIn("0 error(s) in 24h", out,
                         f"{label}: still printed a count it never obtained")

    def test_http_401_with_a_top_level_error(self):
        self._assert_blind_not_healthy(
            _StubResponse(401, {"error": "authorization failed"}), "HTTP 401")

    def test_http_200_with_a_per_result_error(self):
        # InfluxDB 1.x reports query errors per result, under HTTP 200.
        self._assert_blind_not_healthy(
            _StubResponse(200, {"results": [{"error": "query timeout exceeded"}]}),
            "per-result error")

    def test_http_500_with_no_body(self):
        self._assert_blind_not_healthy(_StubResponse(500, {}), "HTTP 500")

    def test_a_non_json_response(self):
        self._assert_blind_not_healthy(
            _StubResponse(200, None, raises=True), "non-JSON")

    def test_a_response_with_no_results_key(self):
        self._assert_blind_not_healthy(_StubResponse(200, {}), "no results key")

    def test_a_genuinely_empty_result_still_passes(self):
        """The whole point of the distinction: quiet is not the same as blind."""
        results, out = self._run(_StubResponse(200, {"results": [{}]}))
        self.assertTrue([r for r in results if r[0] == "PASS"],
                        f"a successful empty query must still report a quiet fleet: {results}")
        self.assertIn("0 error(s) in 24h", out)

    def test_the_anomaly_check_does_not_blame_the_accountant_for_a_bad_query(self):
        """'No rows' means the accountant is dead — but only if the query worked."""
        _, out = self._run(_StubResponse(401, {"error": "authorization failed"}))
        self.assertNotIn("not running", out,
                         "a failed query was reported as the accountant being down")
        self.assertIn("Could not read accounting_anomaly", out)


class CoveredVixFailureIsNotAFleetErrorTest(unittest.TestCase):
    """Sections 6 and 7c must not disagree about a covered VIX source.

    --- The defect this pins ----------------------------------------------

    get_vix_value logged registry.log_error for EACH failed source before
    knowing whether the chain went on to answer. Section 6 grades that same
    condition a warning ("covered — the chain has a live source"), so the two
    halves of one diagnostic reached opposite verdicts on one fact (rule 26),
    and the error-series half could fail the run's exit code for a fallback
    that was working exactly as designed (rule 27).

    On the analyst's 900s cycle a covered failure is ~96 rows a day — already
    past 7c's 50/24h warning bar — and a cycle needing retries can file up to
    FETCH_RETRIES x len(VIX_SOURCES) rows while still succeeding, which
    reaches the 200/24h FAIL bar.
    """

    def setUp(self):
        import market_analyst
        self.ma = market_analyst

    @staticmethod
    def _chain(*pairs):
        def make(v):
            def fetch():
                if isinstance(v, Exception):
                    raise v
                return v
            return fetch
        return tuple((name, make(v)) for name, v in pairs)

    def test_a_covered_failure_writes_no_error_row(self):
        with mock.patch.object(self.ma, "VIX_SOURCES",
                               self._chain(("cboe", RuntimeError("HTTP 503")),
                                           ("yfinance", 15.4))), \
             mock.patch.object(self.ma.registry, "log_error") as le:
            self.assertAlmostEqual(self.ma.get_vix_value(), 15.4)
        self.assertFalse(le.called)

    def test_the_rate_that_used_to_be_produced_would_have_failed_the_run(self):
        """Why this mattered: the old cadence crossed 7c's thresholds."""
        key = ("market_analyst", "get_vix_value", "RuntimeError")
        per_cycle = self.ma.FETCH_RETRIES * len(self.ma.VIX_SOURCES)
        cycles_per_day = 86400 // self.ma.CHECK_INTERVAL

        # One covered failure per cycle: past the WARN bar, permanently.
        findings, _ = fleet_doctor.classify_error_rates(
            {key: cycles_per_day}, {key: cycles_per_day // 24})
        self.assertTrue(findings,
                        "a permanently-warning healthy fleet is the condition "
                        "this fix removes")

        # A retrying-but-succeeding cycle: past the FAIL bar.
        heavy = cycles_per_day * per_cycle
        findings, _ = fleet_doctor.classify_error_rates(
            {key: heavy}, {key: heavy // 24})
        self.assertEqual(findings[0][0], "FAIL")

        # And with the fix there are no rows at all, so neither fires.
        self.assertEqual(fleet_doctor.classify_error_rates({}, {})[0], [])
