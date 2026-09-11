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
import datetime as dt_mod
import unittest

import pandas as pd

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
