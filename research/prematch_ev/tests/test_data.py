"""Data-layer regressions.

One assertion carries this file: AN UNREADABLE RESPONSE IS NOT AN EMPTY ONE.
Both produce zero rows, they mean opposite things, and a reader that cannot
tell them apart will conclude "no edge on this window" from a window it failed
to read. This is the fleet's `_influx_series` lesson in a different database.
"""

import unittest
from datetime import datetime, timedelta, timezone

from data.kalshi_history import (
    PARTITION_FIELD, Coverage, fetch_candlesticks, parse_candles,
    settlement_outcome, settlement_time, uses_archive,
)
from data.odds_history import (
    CreditLedger, build_snapshot_url, estimate_credits, parse_snapshot,
)


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
    # Copied from the documented wire format: `*_dollars` is a fixed-point
    # STRING, not a JSON number. The previous fixture used numbers because it
    # was invented rather than observed, which is why the parser could only
    # ever agree with it.
    GOOD = {"candlesticks": [{
        "end_period_ts": 1758400000,
        "yes_bid": {"close_dollars": "0.5600"},
        "yes_ask": {"close_dollars": "0.5800"},
        "price": {"close_dollars": "0.5700", "mean_dollars": "0.5700"},
        "volume_fp": "120", "open_interest_fp": 900,
    }]}

    def test_parses_bid_ask_and_mid(self):
        candles, coverage = parse_candles(self.GOOD)
        self.assertTrue(coverage.complete)
        self.assertAlmostEqual(candles[0].mid, 0.57, places=8)
        self.assertAlmostEqual(candles[0].spread, 0.02, places=8)
        self.assertEqual(candles[0].volume, 120.0)

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
    def _event(self, book="pinnacle", outcomes=None, event_id="evt-9"):
        return {
            "id": event_id,
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

    def test_event_without_id_is_counted_not_silently_dropped(self):
        event = self._event()
        del event["id"]
        result = parse_snapshot(self._body([event]))
        self.assertEqual(len(result.quotes), 0)
        self.assertEqual(result.events_without_id, 1)


class StringPriceTest(unittest.TestCase):
    """THE parse regression: string prices decoded to None, so `mid` went with
    them and the collector dropped every real candle -- presenting as thin
    coverage rather than as an error."""

    def _bid(self, node):
        candles, coverage = parse_candles(
            {"candlesticks": [{"end_period_ts": 1, "yes_bid": node,
                               "yes_ask": {"close_dollars": "0.60"}}]})
        return candles[0], coverage

    def test_documented_string_prices_decode(self):
        candle, coverage = self._bid({"close_dollars": "0.5600"})
        self.assertAlmostEqual(candle.bid_close, 0.56, places=8)
        self.assertTrue(coverage.complete)

    def test_numeric_prices_still_decode(self):
        candle, _ = self._bid({"close_dollars": 0.56})
        self.assertAlmostEqual(candle.bid_close, 0.56, places=8)

    def test_absent_is_not_malformed(self):
        for node in ({"close_dollars": None}, {}):
            candle, coverage = self._bid(node)
            self.assertIsNone(candle.bid_close)
            self.assertFalse(candle.has_malformed_price)
            self.assertTrue(coverage.complete, "no quote is not a parser failure")

    def test_malformed_fails_coverage(self):
        """A price we cannot read means the parser may be behind the wire
        format, which is a reason to distrust the rest of the run."""
        for node in ({"close_dollars": "abc"}, {"close_dollars": "1.40"},
                     {"close_dollars": True}, {"close_dollars": []}):
            candle, coverage = self._bid(node)
            self.assertIsNone(candle.bid_close)
            self.assertTrue(candle.has_malformed_price)
            self.assertFalse(coverage.complete)


class PartitionTest(unittest.TestCase):
    """Markets settled before the cutoff are absent from the live endpoint, so
    enumerating only /markets silently omits the older half of a window."""

    CUTOFF = datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_routes_by_settlement_not_candle_age(self):
        before = {"settled_ts": datetime(2026, 5, 15, tzinfo=timezone.utc).timestamp()}
        after = {"settled_ts": datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()}
        self.assertTrue(uses_archive(before, self.CUTOFF))
        self.assertFalse(uses_archive(after, self.CUTOFF))

    def test_boundary_fixture_puts_one_market_on_each_side(self):
        day_before = {"settled_ts": (self.CUTOFF - timedelta(hours=1)).timestamp()}
        day_after = {"settled_ts": (self.CUTOFF + timedelta(hours=1)).timestamp()}
        self.assertNotEqual(uses_archive(day_before, self.CUTOFF),
                            uses_archive(day_after, self.CUTOFF))

    def test_documented_iso_settlement_ts_parses(self):
        """`settlement_ts` is an ISO string with sub-second precision. An
        earlier version accepted it only as a number, fell through to
        `close_time`, and returned a time ~3 minutes early -- enough to route a
        market to the wrong partition near the cutoff."""
        self.assertEqual(
            settlement_time({"close_time": "2026-09-21T02:19:32Z",
                             "settlement_ts": "2026-09-21T02:22:34.56292Z"}),
            datetime(2026, 9, 21, 2, 22, 34, 562920, tzinfo=timezone.utc))

    def test_close_and_expiration_are_not_settlement(self):
        for field in ("close_time", "expiration_time", "close_ts", "expiration_ts"):
            self.assertIsNone(settlement_time({field: "2026-05-20T00:00:00Z"}),
                              f"{field} is a lifecycle fact, not settlement")

    def test_enumeration_provenance_outranks_inference(self):
        """Which endpoint returned the market is what the API itself
        established; a timestamp comparison is only a fallback."""
        early = "2026-05-15T00:00:00Z"
        self.assertFalse(uses_archive(
            {PARTITION_FIELD: "live", "settlement_ts": early}, self.CUTOFF))
        late = "2026-09-15T00:00:00Z"
        self.assertTrue(uses_archive(
            {PARTITION_FIELD: "historical", "settlement_ts": late}, self.CUTOFF))

    def test_unroutable_candles_refuse_rather_than_guess(self):
        """`use_archive=None` must not fall back to the 90-day age heuristic
        this module was told to stop using."""
        candles, coverage = fetch_candlesticks(
            "KX-X", "KXS", datetime(2020, 1, 1, tzinfo=timezone.utc),
            datetime(2020, 1, 2, tzinfo=timezone.utc), use_archive=None)
        self.assertEqual(candles, [])
        self.assertFalse(coverage.complete)
        self.assertIn("refusing to guess", str(coverage))

    def test_unroutable_returns_none_not_a_guess(self):
        self.assertIsNone(uses_archive({}, self.CUTOFF))
        self.assertIsNone(uses_archive({"settled_ts": 1}, None))


class BookSelectionTest(unittest.TestCase):
    """THE credit-burning regression: `regions=us` was requested while the
    parser accepted only Pinnacle, which the provider lists under EU. Every
    call cost 10 credits and returned nothing usable."""

    def test_url_names_the_bookmaker(self):
        url = build_snapshot_url("MLB", datetime(2026, 7, 4, tzinfo=timezone.utc), "K")
        self.assertIn("bookmakers=pinnacle", url)

    def test_url_does_not_fall_back_to_a_region_by_default(self):
        url = build_snapshot_url("MLB", datetime(2026, 7, 4, tzinfo=timezone.utc), "K")
        self.assertNotIn("regions=", url)

    def test_region_only_when_no_book_named(self):
        url = build_snapshot_url("MLB", datetime(2026, 7, 4, tzinfo=timezone.utc), "K",
                                 bookmakers=None, regions="eu")
        self.assertIn("regions=eu", url)

    def test_neither_is_an_error(self):
        with self.assertRaises(ValueError):
            build_snapshot_url("MLB", datetime(2026, 7, 4, tzinfo=timezone.utc), "K",
                               bookmakers=None, regions=None)


class QuoteFreshnessTest(unittest.TestCase):
    """A fresh ENVELOPE does not imply a fresh QUOTE. The provider notes that
    suspended markets linger for ~15 minutes and that its Pinnacle prices come
    from the public site, so an old line beside a moving exchange price reads
    as edge."""

    def body(self, market_update="2026-07-04T17:58:00Z",
             book_update="2026-07-04T17:20:00Z"):
        market = {"key": "h2h", "outcomes": [
            {"name": "New York Yankees", "price": -155},
            {"name": "Boston Red Sox", "price": 135}]}
        if market_update is not None:
            market["last_update"] = market_update
        return {"timestamp": "2026-07-04T18:00:00Z", "data": [{
            "id": "evt-1", "commence_time": "2026-07-04T23:05:00Z",
            "home_team": "New York Yankees", "away_team": "Boston Red Sox",
            "bookmakers": [{"key": "pinnacle", "last_update": book_update,
                            "markets": [market]}]}]}

    def test_fresh_quote_is_fresh(self):
        q = parse_snapshot(self.body()).quotes[0]
        self.assertEqual(q.age_seconds(), 120.0)
        self.assertTrue(q.is_fresh())

    def test_stale_odds_in_a_fresh_envelope_are_flagged(self):
        result = parse_snapshot(self.body(market_update="2026-07-04T15:00:00Z"))
        self.assertFalse(result.quotes[0].is_fresh())
        self.assertEqual(result.quotes_stale, 1)
        self.assertEqual(result.fresh_quotes(), [])

    def test_missing_source_timestamp_is_not_fresh(self):
        """Unknown age is not the same as fresh and must not be treated as it."""
        result = parse_snapshot(self.body(market_update=None, book_update=None))
        self.assertIsNone(result.quotes[0].last_update)
        self.assertIsNone(result.quotes[0].age_seconds())
        self.assertFalse(result.quotes[0].is_fresh())
        self.assertEqual(result.quotes_without_update_time, 1)

    def test_market_stamp_preferred_over_book_envelope(self):
        q = parse_snapshot(self.body()).quotes[0]
        self.assertEqual(q.last_update.strftime("%H:%M"), "17:58")

    def test_provider_event_id_is_preserved(self):
        self.assertEqual(parse_snapshot(self.body()).quotes[0].provider_event_id, "evt-1")

    def test_event_without_an_id_is_dropped(self):
        body = self.body()
        del body["data"][0]["id"]
        self.assertEqual(len(parse_snapshot(body).quotes), 0)


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
