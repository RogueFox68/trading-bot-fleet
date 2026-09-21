"""Collector regressions.

Fixtures are shaped like the REAL market payload — `settlement_ts` as an ISO
string, `open_time`/`close_time` present, and crucially **no scheduled-start
field**, because the real payloads do not carry one under any name this code
has verified. An earlier version of this file fabricated `game_start_ts`, so
the suite passed while the collector rejected every real market. That is the
invented-fixture defect for the third time; these tests exist to stop a fourth.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import collect
from collect import (
    JoinedMarket, Ledger, in_study_window, join_markets, market_start_time,
    observation_at_cutoff, orient_probability, start_metadata_available,
)
from data.kalshi_history import Coverage
from data.odds_history import SharpQuote

UTC = timezone.utc
GAME1 = datetime(2026, 7, 4, 17, 5, tzinfo=UTC)
GAME2 = datetime(2026, 7, 4, 20, 10, tzinfo=UTC)   # doubleheader, same teams


def market(ticker, settled=None, result="yes", event_ticker=None):
    """Shaped like the real payload: no scheduled-start field of any name."""
    settled = settled or (GAME1 + timedelta(hours=3))
    return {
        "ticker": ticker,
        "event_ticker": event_ticker or ticker.rsplit("-", 1)[0],
        "result": result,
        "open_time": "2026-06-14T19:20:00Z",
        "close_time": settled.isoformat().replace("+00:00", "Z"),
        "settlement_ts": (settled + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
    }


def quote(event_id, start, snapshot=None, last_update=None,
          home="Baltimore Orioles", away="Milwaukee Brewers",
          home_price=-140, away_price=120):
    snapshot = snapshot or (start - timedelta(hours=1))
    return SharpQuote(
        snapshot=snapshot, commence_time=start, away_name=away, home_name=home,
        away_price=away_price, home_price=home_price, book="pinnacle",
        provider_event_id=event_id,
        last_update=last_update or (snapshot - timedelta(minutes=2)),
    )


def milbal(start=GAME1, event="KXMLBGAME-A"):
    return {
        f"{event}-MIL": market(f"{event}-MIL", start + timedelta(hours=3),
                               "yes", event_ticker=event),
        f"{event}-BAL": market(f"{event}-BAL", start + timedelta(hours=3),
                               "no", event_ticker=event),
    }


class Candle:
    def __init__(self, ts, bid, ask, malformed=False):
        self.ts, self.bid_close, self.ask_close = ts, bid, ask
        self.has_malformed_price = malformed

    @property
    def mid(self):
        if self.bid_close is None or self.ask_close is None:
            return None
        return (self.bid_close + self.ask_close) / 2


class StartMetadataTest(unittest.TestCase):
    """No verified scheduled-start key exists. The code must say so rather than
    accept a lifecycle field that happens to be present."""

    def test_no_verified_key_is_configured(self):
        self.assertFalse(start_metadata_available())

    def test_real_payload_yields_no_start(self):
        self.assertIsNone(market_start_time(market("KXMLBGAME-A-MIL")))

    def test_lifecycle_fields_are_not_substituted(self):
        """`open_time` is the listing time; the sample occurrence time lands
        around game END. Neither is first pitch."""
        for field in ("open_time", "close_time", "expiration_time",
                      "expected_expiration_time", "settlement_ts"):
            self.assertIsNone(market_start_time({field: "2026-07-04T17:05:00Z"}),
                              f"{field} must not stand in for scheduled start")

    def test_a_verified_key_is_honoured_once_configured(self):
        """The seam works; only the verified key name is missing."""
        with mock.patch.object(collect, "VERIFIED_START_KEYS_ISO", ("real_start",)):
            self.assertEqual(market_start_time({"real_start": "2026-07-04T17:05:00Z"}),
                             GAME1)


class WindowTest(unittest.TestCase):
    """Without a window filter, every settled market in a series' whole history
    was joined against odds fetched for the requested dates only, so valid
    out-of-window markets became join FAILURES and swamped the denominator."""

    START = datetime(2026, 7, 1, tzinfo=UTC)
    END = datetime(2026, 7, 3, tzinfo=UTC)

    def test_in_window(self):
        self.assertTrue(in_study_window(
            {"settlement_ts": "2026-07-02T02:00:00Z"}, self.START, self.END))

    def test_outside_window(self):
        for ts in ("2026-05-02T02:00:00Z", "2026-09-02T02:00:00Z"):
            self.assertFalse(in_study_window({"settlement_ts": ts},
                                             self.START, self.END))

    def test_late_game_settling_after_midnight_is_kept(self):
        self.assertTrue(in_study_window(
            {"settlement_ts": "2026-07-03T03:30:00Z"}, self.START, self.END))

    def test_unreadable_settlement_is_none_not_a_verdict(self):
        self.assertIsNone(in_study_window({}, self.START, self.END))


class JoinTest(unittest.TestCase):
    """The join matches on PARTICIPANTS, then time. Filtering on time alone made
    every simultaneous game a candidate for every market, so the uniqueness
    check rejected valid data wholesale on any normal slate."""

    def test_joins_on_matchup(self):
        led = Ledger()
        joined = join_markets(milbal(), {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertEqual(len(joined), 2)
        self.assertEqual({j.yes_participant for j in joined}, {"MIL", "BAL"})

    def test_simultaneous_unrelated_game_does_not_block(self):
        """THE regression: a MIL-BAL market was discarded because an unrelated
        BOS-NYY game started at the same minute."""
        led = Ledger()
        quotes = {
            "evt-mil-bal": [quote("evt-mil-bal", GAME1)],
            "evt-bos-nyy": [quote("evt-bos-nyy", GAME1, home="New York Yankees",
                                  away="Boston Red Sox")],
        }
        joined = join_markets(milbal(), quotes, "MLB", led)
        self.assertEqual(len(joined), 2)
        self.assertEqual({j.provider_event_id for j in joined}, {"evt-mil-bal"})
        self.assertEqual(led.rejections, {})

    def test_wrong_opponent_with_shared_team_is_not_matched(self):
        led = Ledger()
        quotes = {"evt-mil-nyy": [quote("evt-mil-nyy", GAME1,
                                        home="New York Yankees",
                                        away="Milwaukee Brewers")]}
        self.assertEqual(join_markets(milbal(), quotes, "MLB", led), [])
        self.assertIn("no_sharp_event_for_matchup", led.rejections)

    def test_doubleheader_without_a_verified_start_key_is_rejected(self):
        """Same teams twice: only a start time separates them, and none is
        available. Rejected explicitly rather than resolved by guess."""
        led = Ledger()
        markets = {**milbal(GAME1, "KXMLBGAME-G1"), **milbal(GAME2, "KXMLBGAME-G2")}
        quotes = {"evt-1": [quote("evt-1", GAME1)], "evt-2": [quote("evt-2", GAME2)]}
        self.assertEqual(join_markets(markets, quotes, "MLB", led), [])
        self.assertIn("doubleheader_unresolvable_no_verified_start_key", led.rejections)

    def test_doubleheader_resolves_once_a_start_key_is_verified(self):
        led = Ledger()
        markets = {}
        for event, start in (("KXMLBGAME-G1", GAME1), ("KXMLBGAME-G2", GAME2)):
            for ticker, m in milbal(start, event).items():
                m["real_start"] = start.isoformat().replace("+00:00", "Z")
                markets[ticker] = m
        quotes = {"evt-1": [quote("evt-1", GAME1)], "evt-2": [quote("evt-2", GAME2)]}
        with mock.patch.object(collect, "VERIFIED_START_KEYS_ISO", ("real_start",)):
            joined = join_markets(markets, quotes, "MLB", led)
        pairs = {j.event_ticker: j.provider_event_id for j in joined}
        self.assertEqual(pairs, {"KXMLBGAME-G1": "evt-1", "KXMLBGAME-G2": "evt-2"})

    def test_joins_record_whether_the_start_was_verified(self):
        led = Ledger()
        joined = join_markets(milbal(), {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertTrue(all(not j.start_verified for j in joined),
                        "an unverified start must be labelled, not implied")

    def test_ticker_with_no_event_at_all_is_counted(self):
        led = Ledger()
        markets = {**milbal(), "garbage": {"ticker": "garbage", "result": "yes"}}
        join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("ticker_unparseable", led.rejections)

    def test_event_whose_participants_cannot_be_derived_is_counted(self):
        """An event_ticker is published but the ticker yields no YES suffix."""
        led = Ledger()
        markets = {**milbal(),
                   "garbage": {"ticker": "garbage", "event_ticker": "garbage",
                               "result": "yes"}}
        join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("event_participants_unresolvable", led.rejections)

    def test_unreadable_settlement_is_counted(self):
        led = Ledger()
        markets = milbal()
        markets["KXMLBGAME-A-MIL"]["result"] = "void"
        joined = join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("no_readable_settlement", led.rejections)
        self.assertEqual([j.yes_participant for j in joined], ["BAL"],
                         "the readable sibling contract still joins")

    def test_unresolvable_sharp_teams_are_counted(self):
        led = Ledger()
        quotes = {"evt-x": [quote("evt-x", GAME1, home="Some FC", away="Other FC")]}
        join_markets(milbal(), quotes, "MLB", led)
        self.assertIn("sharp_teams_unresolvable", led.rejections)


class OrientationTest(unittest.TestCase):
    QUOTE = quote("evt-1", GAME1)

    def test_home_contract_gets_the_home_probability(self):
        self.assertGreater(orient_probability(self.QUOTE, "BAL", "MLB"), 0.5)

    def test_away_contract_gets_the_away_probability(self):
        self.assertLess(orient_probability(self.QUOTE, "MIL", "MLB"), 0.5)

    def test_the_two_sides_complement(self):
        self.assertAlmostEqual(
            orient_probability(self.QUOTE, "BAL", "MLB")
            + orient_probability(self.QUOTE, "MIL", "MLB"), 1.0, places=9)

    def test_unknown_participant_is_rejected(self):
        self.assertIsNone(orient_probability(self.QUOTE, "NYY", "MLB"))


class AsOfCutoffTest(unittest.TestCase):
    """Both forecasts must be information genuinely available at one instant."""

    def joined(self):
        return JoinedMarket("KXMLBGAME-A-MIL", "KXMLBGAME-A", "MIL", GAME1, 1,
                            "evt-1", None)

    def test_both_sides_taken_at_or_before_the_cutoff(self):
        cutoff = GAME1 - timedelta(minutes=60)
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff - timedelta(seconds=30),
                   last_update=cutoff - timedelta(minutes=2))],
            [Candle(cutoff - timedelta(minutes=1), 0.40, 0.42)],
            cutoff, "MLB", Ledger())
        self.assertIsNotNone(obs)
        self.assertLessEqual(obs.sharp_at, cutoff)
        self.assertLessEqual(obs.exchange_at, cutoff)

    def test_a_snapshot_captured_after_the_cutoff_is_rejected(self):
        """THE lookahead regression: a 16:07 snapshot carrying a 16:04 update
        stamp was accepted for a 16:05 decision. That price was not available
        through this feed at 16:05."""
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff + timedelta(minutes=2),
                   last_update=cutoff - timedelta(minutes=1))],
            [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB", led))
        self.assertIn("no_sharp_quote_available_at_cutoff", led.rejections)

    def test_age_is_measured_at_the_decision_not_at_capture(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        old = quote("evt-1", GAME1,
                    snapshot=cutoff - timedelta(hours=5),
                    last_update=cutoff - timedelta(hours=5, minutes=1))
        self.assertIsNone(observation_at_cutoff(
            self.joined(), [old], [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB",
            led, max_quote_age=900))
        self.assertIn("sharp_quote_too_old_at_cutoff", led.rejections)

    def test_all_three_timestamps_are_retained(self):
        cutoff = GAME1 - timedelta(minutes=45)
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff - timedelta(seconds=30),
                   last_update=cutoff - timedelta(minutes=1))],
            [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB", Ledger())
        self.assertIsNotNone(obs.sharp_at)
        self.assertIsNotNone(obs.sharp_snapshot_at)
        self.assertIsNotNone(obs.exchange_at)
        self.assertAlmostEqual(obs.minutes_to_start, 45.0, places=6)

    def test_a_candle_after_the_cutoff_is_not_used(self):
        cutoff = GAME1 - timedelta(minutes=60)
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff)],
            [Candle(cutoff - timedelta(minutes=1), 0.40, 0.42),
             Candle(cutoff + timedelta(minutes=15), 0.80, 0.82)],
            cutoff, "MLB", Ledger())
        self.assertAlmostEqual(obs.p_exchange, 0.41, places=8)

    def test_quote_without_update_time_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        q = SharpQuote(cutoff, GAME1, "Milwaukee Brewers", "Baltimore Orioles",
                       120, -140, "pinnacle", "evt-1", None)
        self.assertIsNone(observation_at_cutoff(
            self.joined(), [q], [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB", led))
        self.assertIn("no_sharp_quote_available_at_cutoff", led.rejections)

    def test_malformed_exchange_price_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff)],
            [Candle(cutoff, 0.40, 0.42, malformed=True)], cutoff, "MLB", led))
        self.assertIn("exchange_quote_malformed", led.rejections)

    def test_cutoff_at_or_after_start_is_rejected(self):
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=GAME1, last_update=GAME1)],
            [Candle(GAME1, 0.40, 0.42)], GAME1, "MLB", led))
        self.assertIn("cutoff_at_or_after_start", led.rejections)


class LedgerTest(unittest.TestCase):
    """Counts are RECORDS and denominators match units. An earlier version
    incremented once per batch and divided odds-event failures by
    exchange-market counts."""

    def test_a_batch_rejection_counts_every_record(self):
        led = Ledger()
        led.count("odds_events", 10_000, unit="event-quotes")
        led.reject("event_without_sharp_book", "snapshot X", count=10_000,
                   stage="odds_events")
        self.assertEqual(led.rejections["event_without_sharp_book"], 10_000)
        self.assertFalse(led.apply_to(Coverage()).complete)

    def test_stages_are_judged_in_their_own_units(self):
        """A clean contract stage must not be rescued by, or blamed for, a
        lossy odds stage."""
        led = Ledger()
        led.count("contracts", 500, unit="contracts")
        led.reject("ticker_unparseable", "KX-BAD", count=5, stage="contracts")
        led.count("odds_events", 100, unit="event-quotes")
        led.reject("event_without_sharp_book", "x", count=90, stage="odds_events")
        lossy = [n for n, _ in led.lossy_stages()]
        self.assertEqual(lossy, ["odds_events"])
        self.assertFalse(led.apply_to(Coverage()).complete)

    def test_small_loss_leaves_coverage_intact(self):
        led = Ledger()
        led.count("contracts", 100, unit="contracts")
        led.reject("ticker_unparseable", "KX-BAD", count=1, stage="contracts")
        self.assertTrue(led.apply_to(Coverage()).complete)

    def test_eligibility_exclusions_do_not_count_as_loss(self):
        """A one-day study against a full season's markets must not read as a
        catastrophic failure."""
        led = Ledger()
        led.count("contracts", 1000, unit="contracts")
        led.exclude("settled_outside_study_window", count=980, stage="contracts")
        led.reject("ticker_unparseable", "KX-BAD", count=1, stage="contracts")
        self.assertTrue(led.apply_to(Coverage()).complete)
        self.assertAlmostEqual(led.stages["contracts"].loss_rate, 1 / 20)

    def test_partly_malformed_feed_still_scores_but_says_so(self):
        led = Ledger()
        led.count("contracts", 1000, unit="contracts")
        led.reject("no_readable_start_time", "KX-X", count=300, stage="contracts")
        coverage = led.apply_to(Coverage())
        self.assertFalse(coverage.complete)
        self.assertIn("contracts", str(coverage))

    def test_ledger_serialises_with_stages_and_units(self):
        led = Ledger()
        led.count("contracts", 10, unit="contracts")
        led.reject("ticker_unparseable", "KX-BAD", count=2, stage="contracts")
        payload = led.as_dict()
        self.assertEqual(payload["stages"]["contracts"]["unit"], "contracts")
        self.assertEqual(payload["rejections"]["ticker_unparseable"]["count"], 2)
        self.assertEqual(payload["rejections"]["ticker_unparseable"]["stage"],
                         "contracts")


if __name__ == "__main__":
    unittest.main()
