"""Collector regressions.

Every defect the review found lived here rather than in the components: the
components were careful and the collector bypassed them. These tests drive
`join_markets` and `observation_at_cutoff` directly, because that is where the
wrong game got attached, the wrong side got scored, and the losses went
uncounted.
"""

import unittest
from datetime import datetime, timedelta, timezone

from collect import (
    Ledger, JoinedMarket, MAX_UNEXPLAINED_LOSS, join_markets, market_start_time,
    observation_at_cutoff, orient_probability,
)
from data.kalshi_history import Coverage
from data.odds_history import SharpQuote

UTC = timezone.utc
GAME1 = datetime(2026, 7, 4, 17, 5, tzinfo=UTC)
GAME2 = datetime(2026, 7, 4, 20, 10, tzinfo=UTC)   # doubleheader, same teams


def market(ticker, start, result="yes", event_ticker=None):
    return {
        "ticker": ticker,
        "event_ticker": event_ticker or ticker.rsplit("-", 1)[0],
        "result": result,
        "game_start_ts": start.timestamp(),
        "settled_ts": (start + timedelta(hours=3)).timestamp(),
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


class Candle:
    def __init__(self, ts, bid, ask, malformed=False):
        self.ts, self.bid_close, self.ask_close = ts, bid, ask
        self.has_malformed_price = malformed

    @property
    def mid(self):
        if self.bid_close is None or self.ask_close is None:
            return None
        return (self.bid_close + self.ask_close) / 2


class StartTimeTest(unittest.TestCase):
    def test_open_time_is_not_a_start_time(self):
        """`open_time` is when the contract was LISTED, which can be days
        before first pitch. Accepting it would make every start-time agreement
        check pass while meaning nothing."""
        self.assertIsNone(market_start_time({"open_time": 1750000000}))

    def test_real_start_fields_are_read(self):
        self.assertIsNotNone(market_start_time({"game_start_ts": 1750000000}))
        self.assertIsNotNone(
            market_start_time({"scheduled_start_time": "2026-07-04T17:05:00Z"}))

    def test_unreadable_start_is_none_not_a_guess(self):
        self.assertIsNone(market_start_time({}))


class JoinTest(unittest.TestCase):
    """The defect: the join compared canonical-id SUFFIXES and ignored the date
    entirely, so it attached the first same-team event anywhere in the window.
    A July 5 market was joined to a July 4 game."""

    def test_joins_the_game_at_the_matching_start(self):
        led = Ledger()
        markets = {"KXMLBGAME-A-MIL": market("KXMLBGAME-A-MIL", GAME1)}
        joined = join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertEqual(len(joined), 1)
        self.assertEqual(joined[0].provider_event_id, "evt-1")

    def test_does_not_join_a_different_day(self):
        led = Ledger()
        markets = {"KXMLBGAME-A-MIL": market("KXMLBGAME-A-MIL",
                                             GAME1 + timedelta(days=1))}
        joined = join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertEqual(joined, [])
        self.assertIn("no_sharp_event_at_that_start", led.rejections)

    def test_doubleheader_games_join_separately(self):
        """Both games have the same teams on the same date; only the start
        separates them."""
        led = Ledger()
        markets = {
            "KXMLBGAME-G1-MIL": market("KXMLBGAME-G1-MIL", GAME1),
            "KXMLBGAME-G2-MIL": market("KXMLBGAME-G2-MIL", GAME2),
        }
        quotes = {"evt-1": [quote("evt-1", GAME1)], "evt-2": [quote("evt-2", GAME2)]}
        joined = {j.market_ticker: j.provider_event_id
                  for j in join_markets(markets, quotes, "MLB", led)}
        self.assertEqual(joined, {"KXMLBGAME-G1-MIL": "evt-1",
                                  "KXMLBGAME-G2-MIL": "evt-2"})

    def test_ambiguous_start_is_rejected_not_resolved(self):
        """Two sharp events inside the agreement window: picking one is how
        the wrong game got attached."""
        led = Ledger()
        markets = {"KXMLBGAME-A-MIL": market("KXMLBGAME-A-MIL", GAME1)}
        quotes = {
            "evt-1": [quote("evt-1", GAME1)],
            "evt-2": [quote("evt-2", GAME1 + timedelta(minutes=5))],
        }
        self.assertEqual(join_markets(markets, quotes, "MLB", led), [])
        self.assertIn("ambiguous_start_match", led.rejections)

    def test_both_team_contracts_join_to_one_game(self):
        led = Ledger()
        markets = {
            "KXMLBGAME-A-MIL": market("KXMLBGAME-A-MIL", GAME1, "yes",
                                      event_ticker="KXMLBGAME-A"),
            "KXMLBGAME-A-BAL": market("KXMLBGAME-A-BAL", GAME1, "no",
                                      event_ticker="KXMLBGAME-A"),
        }
        joined = join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertEqual(len(joined), 2)
        self.assertEqual({j.event_ticker for j in joined}, {"KXMLBGAME-A"})
        self.assertEqual({j.yes_participant for j in joined}, {"MIL", "BAL"})

    def test_unparseable_ticker_and_unreadable_settlement_are_counted(self):
        led = Ledger()
        markets = {
            "garbage": market("garbage", GAME1),
            "KXMLBGAME-A-MIL": market("KXMLBGAME-A-MIL", GAME1, result="void"),
        }
        join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("ticker_unparseable", led.rejections)
        self.assertIn("no_readable_settlement", led.rejections)


class OrientationTest(unittest.TestCase):
    """The defect: the collector always took the HOME probability, while the
    candle and the settlement describe the contract's YES participant. Every
    away-team contract would have received an inverted signal."""

    QUOTE = quote("evt-1", GAME1, home="Baltimore Orioles",
                  away="Milwaukee Brewers", home_price=-140, away_price=120)

    def test_home_contract_gets_the_home_probability(self):
        p = orient_probability(self.QUOTE, "BAL", "MLB")
        self.assertIsNotNone(p)
        self.assertGreater(p, 0.5, "BAL is the -140 favourite")

    def test_away_contract_gets_the_away_probability(self):
        p = orient_probability(self.QUOTE, "MIL", "MLB")
        self.assertIsNotNone(p)
        self.assertLess(p, 0.5, "MIL is the +120 underdog")

    def test_the_two_sides_complement(self):
        self.assertAlmostEqual(
            orient_probability(self.QUOTE, "BAL", "MLB")
            + orient_probability(self.QUOTE, "MIL", "MLB"),
            1.0, places=9)

    def test_unknown_participant_is_rejected(self):
        """An inverted signal is worse than a missing one."""
        self.assertIsNone(orient_probability(self.QUOTE, "NYY", "MLB"))


class AsOfCutoffTest(unittest.TestCase):
    """The defect: the sharp quote was chosen within +-30 minutes of a target
    lead and the exchange candle from a separate +-15 minute window, so one
    predictor could hold a quarter hour more information than the other."""

    def joined(self):
        return JoinedMarket("KXMLBGAME-A-MIL", "KXMLBGAME-A", "MIL", GAME1, 1,
                            "evt-1", None)

    def test_both_sides_are_taken_at_or_before_the_cutoff(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff - timedelta(minutes=2))],
            [Candle(cutoff - timedelta(minutes=1), 0.40, 0.42)],
            cutoff, "MLB", led)
        self.assertIsNotNone(obs)
        self.assertLessEqual(obs.sharp_at, cutoff)
        self.assertLessEqual(obs.exchange_at, cutoff)

    def test_a_candle_after_the_cutoff_is_not_used(self):
        """A price jump between the two timestamps must not leak to one side."""
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff - timedelta(minutes=2))],
            [Candle(cutoff - timedelta(minutes=1), 0.40, 0.42),
             Candle(cutoff + timedelta(minutes=15), 0.80, 0.82)],   # the jump
            cutoff, "MLB", led)
        self.assertAlmostEqual(obs.p_exchange, 0.41, places=8)

    def test_no_candle_at_or_before_the_cutoff_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff - timedelta(minutes=2))],
            [Candle(cutoff + timedelta(minutes=5), 0.40, 0.42)],
            cutoff, "MLB", led))
        self.assertIn("no_exchange_quote_at_cutoff", led.rejections)

    def test_a_stale_sharp_quote_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff,
                   last_update=cutoff - timedelta(hours=4))],
            [Candle(cutoff - timedelta(minutes=1), 0.40, 0.42)],
            cutoff, "MLB", led))
        self.assertTrue(led.rejections)

    def test_a_quote_with_no_update_time_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        q = SharpQuote(cutoff, GAME1, "Milwaukee Brewers", "Baltimore Orioles",
                       120, -140, "pinnacle", "evt-1", None)
        self.assertIsNone(observation_at_cutoff(
            self.joined(), [q], [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB", led))
        self.assertIn("no_fresh_sharp_quote_at_cutoff", led.rejections)

    def test_malformed_exchange_price_is_rejected(self):
        cutoff = GAME1 - timedelta(minutes=60)
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff)],
            [Candle(cutoff, 0.40, 0.42, malformed=True)],
            cutoff, "MLB", led))
        self.assertIn("exchange_quote_malformed", led.rejections)

    def test_cutoff_at_or_after_start_is_rejected(self):
        led = Ledger()
        self.assertIsNone(observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=GAME1, last_update=GAME1)],
            [Candle(GAME1, 0.40, 0.42)], GAME1, "MLB", led))
        self.assertIn("cutoff_at_or_after_start", led.rejections)

    def test_both_timestamps_are_persisted_with_actual_lead(self):
        cutoff = GAME1 - timedelta(minutes=45)
        led = Ledger()
        obs = observation_at_cutoff(
            self.joined(),
            [quote("evt-1", GAME1, snapshot=cutoff, last_update=cutoff)],
            [Candle(cutoff, 0.40, 0.42)], cutoff, "MLB", led)
        self.assertAlmostEqual(obs.minutes_to_start, 45.0, places=6)
        self.assertIsNotNone(obs.sharp_at)
        self.assertIsNotNone(obs.exchange_at)


class LedgerTest(unittest.TestCase):
    """The defect: drops were printed as one aggregate and never reached
    coverage, so a run that discarded most of its markets still reported
    `complete` -- while the README made completeness a go criterion."""

    def test_large_unexplained_loss_fails_coverage(self):
        led = Ledger()
        led.count("markets_enumerated", 100)
        for _ in range(30):
            led.reject("ticker_unparseable", "KX-BAD")
        coverage = led.apply_to(Coverage())
        self.assertFalse(coverage.complete)
        self.assertIn("selected rather than representative", str(coverage))

    def test_small_loss_leaves_coverage_intact(self):
        led = Ledger()
        led.count("markets_enumerated", 100)
        led.reject("ticker_unparseable", "KX-BAD")
        self.assertTrue(led.apply_to(Coverage()).complete)

    def test_eligibility_exclusions_do_not_count_as_loss(self):
        """A market the study deliberately skipped is not a gap in what it
        could see."""
        led = Ledger()
        led.count("markets_enumerated", 100)
        for _ in range(50):
            led.exclude("outside_study_window")
        self.assertTrue(led.apply_to(Coverage()).complete)
        self.assertEqual(led.unexplained_loss_rate(), 0.0)

    def test_partly_malformed_feed_still_scores_but_says_so(self):
        """Enough rows survive to produce numbers; the report must still refuse
        to certify them."""
        led = Ledger()
        led.count("markets_enumerated", 1000)
        led.count("observations_built", 700)
        for _ in range(300):
            led.reject("no_readable_start_time", "KX-X")
        coverage = led.apply_to(Coverage())
        self.assertFalse(coverage.complete)
        self.assertGreater(led.unexplained_loss_rate(), MAX_UNEXPLAINED_LOSS)

    def test_ledger_serialises_for_the_artifacts(self):
        led = Ledger()
        led.count("markets_enumerated", 10)
        led.reject("ticker_unparseable", "KX-BAD")
        payload = led.as_dict()
        self.assertIn("rejections", payload)
        self.assertIn("rejection_examples", payload)
        self.assertEqual(payload["stage_totals"]["markets_enumerated"], 10)


if __name__ == "__main__":
    unittest.main()
