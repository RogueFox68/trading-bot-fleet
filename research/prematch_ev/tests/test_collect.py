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
from zoneinfo import ZoneInfo
from unittest import mock

import collect
from collect import (
    JoinedMarket, Ledger, cadence_is_viable, decision_cutoffs, exchange_matchup,
    in_study_window, join_markets, market_start_time, observation_at_cutoff,
    orient_probability, snapshots_per_day_for, start_metadata_available,
)
from core.matcher import EVENT_BODY_TIMEZONE
from data.kalshi_history import Coverage
from data.odds_history import SharpQuote

UTC = timezone.utc
GAME1 = datetime(2026, 7, 4, 21, 5, tzinfo=UTC)   # 17:05 ET
GAME2 = datetime(2026, 7, 5, 0, 10, tzinfo=UTC)   # 20:10 ET, same ET day


def event_body(start, teams):
    """A REAL-shaped event body: fixed-width date+time, then the teams.

    e.g. 26SEP152140MIAAZ. Earlier fixtures used synthetic bodies like
    `KXMLBGAME-A`, which carry no start -- so the suite could not have caught
    that the collector had no schedule at all.
    """
    et = start.astimezone(ZoneInfo(EVENT_BODY_TIMEZONE))
    return f"{et.strftime('%y%b%d').upper()}{et.strftime('%H%M')}{teams}"


def event_ticker(start, teams="MILBAL", series="KXMLBGAME"):
    return f"{series}-{event_body(start, teams)}"


def market(ticker, settled=None, result="yes", event=None):
    settled = settled or (GAME1 + timedelta(hours=3))
    return {
        "ticker": ticker,
        "event_ticker": event or ticker.rsplit("-", 1)[0],
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


def milbal(start=None, teams="MILBAL", codes=("MIL", "BAL")):
    """Both team contracts of one game, with a real-shaped event ticker."""
    start = start or GAME1
    ev = event_ticker(start, teams)
    return {
        f"{ev}-{codes[0]}": market(f"{ev}-{codes[0]}", start + timedelta(hours=3),
                                   "yes", event=ev),
        f"{ev}-{codes[1]}": market(f"{ev}-{codes[1]}", start + timedelta(hours=3),
                                   "no", event=ev),
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
    """The scheduled start is in the event TICKER, not in a payload field.

    Earlier versions hunted for a start field, invented seven key names that no
    real payload carries, and rejected every real market while the fixtures --
    which fabricated `game_start_ts` -- agreed with the invention.
    """

    REAL = {"ticker": "KXMLBGAME-26SEP152140MIAAZ-AZ",
            "event_ticker": "KXMLBGAME-26SEP152140MIAAZ", "result": "yes"}

    def test_real_ticker_yields_a_start(self):
        start = market_start_time(self.REAL)
        self.assertIsNotNone(start)
        self.assertEqual(start.astimezone(ZoneInfo(EVENT_BODY_TIMEZONE)).strftime("%Y-%m-%d %H:%M"),
                         "2026-09-15 21:40")

    def test_schedule_is_known_before_any_paid_call(self):
        self.assertTrue(start_metadata_available())

    def test_lifecycle_fields_are_not_substituted(self):
        for field in ("open_time", "close_time", "expiration_time"):
            self.assertIsNone(
                market_start_time({field: "2026-07-04T17:05:00Z"}),
                f"{field} is a lifecycle fact, not scheduled start")

    def test_unparseable_body_yields_no_start(self):
        self.assertIsNone(market_start_time(
            {"ticker": "KXMLBGAME-GARBAGE-MIL", "event_ticker": "KXMLBGAME-GARBAGE"}))

    def test_a_verified_payload_field_still_wins_if_configured(self):
        with mock.patch.object(collect, "VERIFIED_START_KEYS_ISO", ("real_start",)):
            m = dict(self.REAL,
                     real_start=GAME1.isoformat().replace("+00:00", "Z"))
            self.assertEqual(market_start_time(m), GAME1)
            self.assertNotEqual(
                market_start_time(m),
                market_start_time({k: v for k, v in self.REAL.items()}),
                "the verified field must win over the ticker-derived start")


class ExchangeCodeTest(unittest.TestCase):
    """A live run dropped 44 contracts because the exchange says AZ and the
    bookmaker name resolves to ARI."""

    def test_alias_is_applied_to_the_matchup(self):
        markets = milbal(teams="MIAAZ", codes=("MIA", "AZ"))
        self.assertEqual(exchange_matchup(list(markets), "MLB"),
                         frozenset({"MIA", "ARI"}))

    def test_alias_is_applied_to_orientation(self):
        q = quote("e", GAME1, home="Arizona Diamondbacks", away="Miami Marlins")
        self.assertIsNotNone(orient_probability(q, "AZ", "MLB"),
                             "the YES side is an EXCHANGE code")

    def test_arizona_game_joins_end_to_end(self):
        led = Ledger()
        markets = milbal(teams="MIAAZ", codes=("MIA", "AZ"))
        quotes = {"evt-az": [quote("evt-az", GAME1,
                                   home="Arizona Diamondbacks",
                                   away="Miami Marlins")]}
        self.assertEqual(len(join_markets(markets, quotes, "MLB", led)), 2)
        self.assertEqual(led.rejections, {})

    def test_unknown_code_is_rejected_by_name(self):
        """One run must enumerate the whole alias gap."""
        led = Ledger()
        markets = milbal(teams="MILZZZ", codes=("MIL", "ZZZ"))
        join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertTrue(any(r.startswith("unknown_exchange_code:")
                            for r in led.rejections),
                        f"expected a named code, got {list(led.rejections)}")


class JoinTest(unittest.TestCase):
    """Identity is (matchup, DATE), then time for a same-day doubleheader."""

    def test_joins_on_matchup_and_date(self):
        led = Ledger()
        joined = join_markets(milbal(), {"evt-1": [quote("evt-1", GAME1)]},
                              "MLB", led)
        self.assertEqual(len(joined), 2)
        self.assertEqual({j.yes_participant for j in joined}, {"MIL", "BAL"})
        self.assertTrue(all(j.start_verified for j in joined))

    def test_simultaneous_unrelated_game_does_not_block(self):
        led = Ledger()
        quotes = {
            "evt-mil-bal": [quote("evt-mil-bal", GAME1)],
            "evt-bos-nyy": [quote("evt-bos-nyy", GAME1, home="New York Yankees",
                                  away="Boston Red Sox")],
        }
        joined = join_markets(milbal(), quotes, "MLB", led)
        self.assertEqual(len(joined), 2)
        self.assertEqual({j.provider_event_id for j in joined}, {"evt-mil-bal"})

    def test_consecutive_day_rematch_is_not_a_doubleheader(self):
        """THE regression: 54 contracts were dropped as unresolvable
        doubleheaders because candidates were indexed by team pair alone. A
        series on successive dates is ordinary."""
        led = Ledger()
        day2 = GAME1 + timedelta(days=1)
        markets = {**milbal(GAME1), **milbal(day2)}
        quotes = {"evt-d1": [quote("evt-d1", GAME1)],
                  "evt-d2": [quote("evt-d2", day2)],
                  "evt-d3": [quote("evt-d3", GAME1 + timedelta(days=2))]}
        joined = join_markets(markets, quotes, "MLB", led)
        self.assertEqual(len(joined), 4, f"rejections: {dict(led.rejections)}")
        by_day = {j.event_ticker: j.provider_event_id for j in joined}
        self.assertEqual(set(by_day.values()), {"evt-d1", "evt-d2"})

    def test_same_day_doubleheader_is_separated_by_time(self):
        led = Ledger()
        markets = {**milbal(GAME1), **milbal(GAME2)}
        quotes = {"evt-1": [quote("evt-1", GAME1)], "evt-2": [quote("evt-2", GAME2)]}
        joined = join_markets(markets, quotes, "MLB", led)
        pairs = {j.event_ticker: j.provider_event_id for j in joined}
        self.assertEqual(len(pairs), 2)
        self.assertEqual(set(pairs.values()), {"evt-1", "evt-2"})

    def test_wrong_opponent_with_shared_team_is_not_matched(self):
        led = Ledger()
        quotes = {"evt-mil-nyy": [quote("evt-mil-nyy", GAME1,
                                        home="New York Yankees",
                                        away="Milwaukee Brewers")]}
        self.assertEqual(join_markets(milbal(), quotes, "MLB", led), [])
        self.assertIn("no_sharp_event_for_matchup_and_date", led.rejections)

    def test_start_disagreement_rejects_and_is_counted(self):
        """The event-body timezone is an ASSUMPTION; systematic disagreement
        must surface rather than silently shift every timestamp."""
        led = Ledger()
        quotes = {"evt-1": [quote("evt-1", GAME1 + timedelta(hours=5))]}
        markets = milbal()
        joined = join_markets(markets, quotes, "MLB", led)
        self.assertEqual(joined, [])
        self.assertTrue("start_times_disagree" in led.rejections
                        or "no_sharp_event_for_matchup_and_date" in led.rejections)

    def test_crosscheck_stage_is_counted(self):
        led = Ledger()
        join_markets(milbal(), {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("start_time_crosscheck", led.stages)
        self.assertEqual(led.stages["start_time_crosscheck"].considered, 1)

    def test_ticker_with_no_event_at_all_is_counted(self):
        led = Ledger()
        markets = {**milbal(), "garbage": {"ticker": "garbage", "result": "yes"}}
        join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]}, "MLB", led)
        self.assertIn("ticker_unparseable", led.rejections)

    def test_unreadable_settlement_is_counted(self):
        led = Ledger()
        markets = milbal()
        first = sorted(markets)[0]
        markets[first]["result"] = "void"
        joined = join_markets(markets, {"evt-1": [quote("evt-1", GAME1)]},
                              "MLB", led)
        self.assertIn("no_readable_settlement", led.rejections)
        self.assertEqual(len(joined), 1, "the sibling contract still joins")

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

    def test_rejections_without_a_denominator_are_unaccounted_not_zero(self):
        """THE accounting regression. A live run printed
        `join: 0 considered, 100 lost (0.0%)` and certified coverage complete:
        `Ledger.reject()` defaulted to a stage nothing counted, and a zero
        denominator returned a reassuring 0.0%."""
        led = Ledger()
        led.reject("failed", count=100)
        stage = led.stages[next(iter(led.stages))]
        self.assertFalse(stage.accounting_is_valid)
        self.assertIsNone(stage.loss_rate, "never a reassuring zero")
        self.assertTrue(led.unaccounted_stages())
        coverage = led.apply_to(Coverage())
        self.assertFalse(coverage.complete)
        self.assertIn("denominator of zero", str(coverage))

    def test_a_counted_stage_measures_normally(self):
        led = Ledger()
        led.count("contracts", 100, unit="contracts")
        led.reject("x", count=100, stage="contracts")
        self.assertTrue(led.stages["contracts"].accounting_is_valid)
        self.assertEqual(led.stages["contracts"].loss_rate, 1.0)
        self.assertIn("contracts", [n for n, _ in led.lossy_stages()])

    def test_unaccounted_stage_is_rendered_not_hidden(self):
        led = Ledger()
        led.reject("failed", count=100)
        text = led.render()
        self.assertIn("UNACCOUNTED", text)

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
