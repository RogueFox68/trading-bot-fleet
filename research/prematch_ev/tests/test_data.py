"""Data-layer regressions.

One assertion carries this file: AN UNREADABLE RESPONSE IS NOT AN EMPTY ONE.
Both produce zero rows, they mean opposite things, and a reader that cannot
tell them apart will conclude "no edge on this window" from a window it failed
to read. This is the fleet's `_influx_series` lesson in a different database.
"""

import unittest
from datetime import datetime, timezone

from data.kalshi_history import Coverage, parse_candles, settlement_outcome
from data.odds_history import CreditLedger, estimate_credits, parse_snapshot


class CoverageTest(unittest.TestCase):
    def test_starts_complete_and_latches_incomplete(self):
        c = Coverage()
        self.assertTrue(c.complete)
        c.fail("something broke")
        self.assertFalse(c.complete)
        self.assertIn("something broke", str(c))

    def test_merge_propagates_failure_but_not_success(self):
        c = Coverage()
        c.merge(Coverage())
        self.assertTrue(c.complete)
        c.merge(Coverage().fail("page 3 timed out"))
        self.assertFalse(c.complete)
        # A later good merge must not clear an earlier failure.
        c.merge(Coverage())
        self.assertFalse(c.complete)


class CandleParseTest(unittest.TestCase):
    GOOD = {"candlesticks": [{
        "end_period_ts": 1758400000,
        "yes_bid": {"close_dollars": 0.52}, "yes_ask": {"close_dollars": 0.54},
        "price": {"close_dollars": 0.53, "mean_dollars": 0.53},
        "volume_fp": 120, "open_interest_fp": 900,
    }]}

    def test_parses_bid_ask_and_mid(self):
        candles, coverage = parse_candles(self.GOOD)
        self.assertTrue(coverage.complete)
        self.assertAlmostEqual(candles[0].mid, 0.53, places=10)
        self.assertAlmostEqual(candles[0].spread, 0.02, places=10)

    def test_empty_market_is_complete_with_no_rows(self):
        candles, coverage = parse_candles({"candlesticks": []})
        self.assertEqual(candles, [])
        self.assertTrue(coverage.complete)

    def test_error_shapes_are_incomplete_not_empty(self):
        for body in ({"error": "unauthorized"}, [], {"candlesticks": "nope"},
                     "not json at all", None):
            candles, coverage = parse_candles(body)
            self.assertEqual(candles, [])
            self.assertFalse(coverage.complete, f"{body!r} must read INCOMPLETE")

    def test_one_sided_book_has_no_mid(self):
        """Inventing a mid from the last trade would be a proxy for a number
        we do not have."""
        candles, _ = parse_candles(
            {"candlesticks": [{"end_period_ts": 1, "yes_bid": {"close_dollars": 0.5}}]})
        self.assertIsNone(candles[0].mid)
        self.assertIsNone(candles[0].spread)

    def test_candles_come_back_sorted(self):
        body = {"candlesticks": [{"end_period_ts": t} for t in (300, 100, 200)]}
        candles, _ = parse_candles(body)
        self.assertEqual([c.ts.timestamp() for c in candles], [100, 200, 300])

    def test_unparseable_rows_are_reported(self):
        body = {"candlesticks": [{"end_period_ts": 100}, {"no_ts": True}, "junk"]}
        candles, coverage = parse_candles(body)
        self.assertEqual(len(candles), 1)
        self.assertFalse(coverage.complete)


class SettlementTest(unittest.TestCase):
    def test_reads_both_outcomes(self):
        self.assertEqual(settlement_outcome({"result": "yes"}), 1)
        self.assertEqual(settlement_outcome({"result": "NO"}), 0)

    def test_unreadable_result_is_none_not_a_loss(self):
        """Defaulting to 0 would bias every predictor downward by exactly the
        unreadable fraction."""
        for market in ({}, {"result": ""}, {"result": "void"}, {"result": None}):
            self.assertIsNone(settlement_outcome(market))


class SnapshotParseTest(unittest.TestCase):
    def _event(self, book="pinnacle", outcomes=None):
        return {
            "commence_time": "2026-07-04T23:05:00Z",
            "home_team": "New York Yankees", "away_team": "Boston Red Sox",
            "bookmakers": [{"key": book, "markets": [{"key": "h2h", "outcomes":
                outcomes or [{"name": "New York Yankees", "price": -155},
                             {"name": "Boston Red Sox", "price": 135}]}]}],
        }

    def _body(self, events):
        return {"timestamp": "2026-07-04T18:00:00Z", "data": events}

    def test_extracts_sharp_quote(self):
        r = parse_snapshot(self._body([self._event()]))
        self.assertTrue(r.coverage.complete)
        self.assertEqual(len(r.quotes), 1)
        self.assertAlmostEqual(r.quotes[0].minutes_to_start(), 305.0, places=1)

    def test_soft_book_never_substitutes_for_sharp(self):
        """A DraftKings line standing in for Pinnacle would invalidate the
        entire thesis being tested."""
        r = parse_snapshot(self._body([self._event(book="draftkings")]))
        self.assertEqual(len(r.quotes), 0)
        self.assertEqual(r.events_without_sharp_book, 1)
        self.assertTrue(r.coverage.complete)      # covered, just not by Pinnacle

    def test_three_way_market_rejected(self):
        """A draw leg breaks the two-way de-vig math this study assumes."""
        r = parse_snapshot(self._body([self._event(outcomes=[
            {"name": "New York Yankees", "price": 100},
            {"name": "Boston Red Sox", "price": 100},
            {"name": "Draw", "price": 240}])]))
        self.assertEqual(len(r.quotes), 0)

    def test_unreadable_price_drops_the_event(self):
        r = parse_snapshot(self._body([self._event(outcomes=[
            {"name": "New York Yankees", "price": "even"},
            {"name": "Boston Red Sox", "price": 135}])]))
        self.assertEqual(len(r.quotes), 0)

    def test_error_body_is_incomplete(self):
        for body in ({"message": "quota exceeded"}, [], None):
            self.assertFalse(parse_snapshot(body).coverage.complete)


class CreditTest(unittest.TestCase):
    def test_estimate_matches_documented_rate(self):
        self.assertEqual(estimate_credits(130, 8), 130 * 8 * 10)

    def test_ledger_reads_headers_and_survives_junk(self):
        led = CreditLedger()
        led.observe({"x-requests-used": "120", "x-requests-remaining": "880"})
        self.assertEqual((led.used, led.remaining, led.calls), (120, 880, 1))
        led.observe({"x-requests-used": "junk"})
        self.assertEqual(led.used, 120)

    def test_exhaustion_respects_reserve(self):
        led = CreditLedger()
        led.observe({"x-requests-remaining": "40"})
        self.assertTrue(led.exhausted(reserve=50))
        self.assertFalse(led.exhausted(reserve=10))

    def test_unknown_remaining_is_not_exhausted(self):
        """No header must not read as 'out of credits' and halt a paid run."""
        self.assertFalse(CreditLedger().exhausted())


if __name__ == "__main__":
    unittest.main()
