"""Event-aligned reaction measurement: did the exchange follow, and when?

FIXTURES: SYNTHETIC PRICES, REAL TYPES, REAL DETECTOR
-----------------------------------------------------
The price trajectories are synthetic and labelled so -- they are controlled
inputs built to force the timing logic to commit. But nothing here builds a
`MoveTrigger` by hand: `make_trigger` runs the REAL detector over real
`SharpQuote` envelopes and takes what comes out, so the brackets under test
are the brackets the detector actually produces. Candles are real
`data.kalshi_history.Candle` objects.

That matters because this study has shipped four invented-fixture defects,
one of which reached a claim made in review. A hand-built trigger with a
plausible bracket would test this module against my idea of the detector
rather than against the detector.

THE CASES THE OWNER NAMED
-------------------------
  book leads, discrepancy survives   `BookLeadsTest`
  ordering indeterminate             `OrderingTest`
  Kalshi first                       `OrderingTest` -- the NULL HYPOTHESIS
  no response                        `CensoringTest`
  delay misses the gap               `DelayTest`, with its THIRD answer
  between two coarse checkpoints     `CheckpointCounterexampleTest`

The last one is why this module exists at all, so it is written as a
counterexample rather than as a feature test: a move and a complete reaction
that a checkpoint grid cannot see, because both checkpoints read identical
prices.
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.kalshi_history import Candle                          # noqa: E402
from data.odds_history import SharpQuote                        # noqa: E402
from reaction.clocks import envelope_for_sharp_quote            # noqa: E402
from reaction.detector import (                                 # noqa: E402
    detect_moves, fair_probabilities,
)
from reaction.measure import (                                  # noqa: E402
    CANDLE_PERIOD, Bracket, Ordering, ReactionOutcome, ReactionPolicy,
    candle_bracket, measure_all, measure_candle_cadence, measure_reaction,
    order_brackets,
)

UTC = timezone.utc
START = datetime(2026, 9, 14, 0, 20, tzinfo=UTC)      # a real NFL kickoff
EVENT = "odds-evt-401872930"
TICKER = "KXNFLGAME-26SEP13DALNYG-NYG"                # YES = the HOME team
AWAY, HOME = "Dallas Cowboys", "New York Giants"

# The synthetic trajectory, in one place so every case reads the same prices.
# Home shortens: 120/-140 -> 160/-190 is +0.0709 of fair probability.
BEFORE_PRICES, AFTER_PRICES = (120, -140), (160, -190)
EXCHANGE_BEFORE, EXCHANGE_AFTER = 0.56, 0.63


def at(minutes: float) -> datetime:
    """An instant, `minutes` before kickoff. Time runs FORWARD as this falls."""
    return START - timedelta(minutes=minutes)


def env(snapshot: datetime, away: float, home: float, observed: datetime):
    return envelope_for_sharp_quote(SharpQuote(
        snapshot=snapshot, commence_time=START, away_name=AWAY, home_name=HOME,
        away_price=away, home_price=home, book="pinnacle",
        provider_event_id=EVENT, last_update=observed))


def make_trigger(*, baseline_snapshot=at(200), baseline_observed=at(201),
                 moved_snapshot=at(195), moved_observed=at(196),
                 before=BEFORE_PRICES, after=AFTER_PRICES):
    """A REAL trigger, from the REAL detector. Never a hand-built dataclass."""
    result = detect_moves([env(baseline_snapshot, *before, baseline_observed),
                           env(moved_snapshot, *after, moved_observed)])
    assert len(result.triggers) == 1, result.counts()
    return result.triggers[0]


def candle(ts: datetime, mid: float | None, spread: float = 0.02,
           malformed: bool = False) -> Candle:
    """A real `Candle` with a SYNTHETIC mid.

    `mid` is dollars on a $1 contract, which is the same unit as a de-vigged
    fair probability -- that equivalence is what makes a book move and an
    exchange move directly comparable, and it is asserted in `UnitTest`.
    A `None` mid models a one-sided book: `Candle.mid` returns None and the
    minute is unreadable.
    """
    if mid is None:
        return Candle(ts=ts, bid_close=0.5, ask_close=None, price_close=None,
                      price_mean=None, volume=0.0, open_interest=1000.0)
    half = spread / 2.0
    return Candle(ts=ts, bid_close=mid - half, ask_close=mid + half,
                  price_close=mid, price_mean=mid, volume=100.0,
                  open_interest=1000.0, has_malformed_price=malformed)


def series(first: float, last: float, mid: float, **overrides) -> list[Candle]:
    """One candle a minute from `first` to `last` minutes-before-start.

    `overrides` maps a minutes-before-start key to a different mid, so a case
    reads as "flat at 0.56, except 0.63 from minute 145" rather than as a
    hand-assembled list whose holes are accidental. Holes have to be made
    deliberately, with `without`.
    """
    out = []
    minute = first
    while minute >= last:
        out.append(candle(at(minute), overrides.get(minute, mid)))
        minute -= 1
    return out


def step(first: float, last: float, low: float, high: float,
         switch_at: float) -> list[Candle]:
    """Flat at `low`, then flat at `high` from `switch_at` onward."""
    out = []
    minute = first
    while minute >= last:
        out.append(candle(at(minute), high if minute <= switch_at else low))
        minute -= 1
    return out


def without(candles, *minutes) -> list[Candle]:
    """Delete minutes, to make a DELIBERATE hole in the series."""
    drop = {at(m) for m in minutes}
    return [c for c in candles if c.ts not in drop]


def measure(candles, trigger=None, policy=None, yes_is_home=True):
    return measure_reaction(trigger or make_trigger(), candles,
                            market_ticker=TICKER, yes_is_home=yes_is_home,
                            policy=policy)


class UnitTest(unittest.TestCase):
    """The two sides are comparable because they are the same unit."""

    def test_a_candle_mid_and_a_fair_probability_share_a_scale(self):
        """Kalshi serialises dollars on a $1 contract; both live in [0, 1]."""
        _, fair_home, _, _ = fair_probabilities(*BEFORE_PRICES)
        self.assertGreater(fair_home, 0.0)
        self.assertLess(fair_home, 1.0)
        self.assertAlmostEqual(candle(at(100), 0.56).mid, 0.56, places=9)
        self.assertAlmostEqual(fair_home, 0.5643939, places=6)

    def test_the_book_move_is_oriented_onto_the_yes_participant(self):
        """A move is +x for one side and -x for the other."""
        trigger = make_trigger()
        home = measure(series(200, 160, EXCHANGE_BEFORE), trigger,
                       yes_is_home=True)
        away = measure(series(200, 160, EXCHANGE_BEFORE), trigger,
                       yes_is_home=False)
        self.assertAlmostEqual(home.book_delta, 0.0708846, places=6)
        self.assertAlmostEqual(away.book_delta, -0.0708846, places=6)
        self.assertAlmostEqual(home.book_delta, -away.book_delta, places=12)

    def test_a_candle_bracket_is_its_period_wide_and_ends_at_its_stamp(self):
        """`ts` is the period CLOSE, so the value arose in the period before."""
        bracket = candle_bracket(at(100), CANDLE_PERIOD)
        self.assertEqual(bracket.latest, at(100))
        self.assertEqual(bracket.earliest, at(101))
        self.assertEqual(bracket.width_seconds, 60.0)


class BookLeadsTest(unittest.TestCase):
    """The owner's book-leads-with-a-surviving-discrepancy case."""

    def test_the_book_leads_and_the_exchange_follows_ten_minutes_later(self):
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=185))
        self.assertIs(reaction.outcome, ReactionOutcome.RESPONDED)
        self.assertIs(reaction.ordering, Ordering.BOOK_LED)
        self.assertEqual(reaction.lag_earliest_seconds, 540.0)
        self.assertEqual(reaction.lag_latest_seconds, 600.0)
        self.assertAlmostEqual(reaction.exchange_before, 0.56, places=9)
        self.assertAlmostEqual(reaction.exchange_after, 0.63, places=9)
        self.assertAlmostEqual(reaction.exchange_delta, 0.07, places=9)

    def test_there_is_no_point_estimate_of_the_lag(self):
        """A single number would be quoted, averaged and plotted (rule 24)."""
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=185))
        self.assertFalse(hasattr(reaction, "lag_seconds"))
        self.assertNotIn("lag_seconds", reaction.as_dict())
        self.assertEqual(reaction.lag_width_seconds,
                         CANDLE_PERIOD.total_seconds(),
                         "the interval is exactly as wide as the candle "
                         "period: that width IS the censoring")
        self.assertIn("no point estimate", reaction.as_dict()["lag"]["note"])

    def test_the_bracket_comes_from_the_detector_not_from_a_second_derivation(
            self):
        """rule 26: the measurement takes the detector's verdict."""
        trigger = make_trigger()
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=185), trigger)
        self.assertEqual(reaction.book_change.earliest,
                         trigger.book_change_earliest)
        self.assertEqual(reaction.book_change.latest,
                         trigger.book_change_latest)
        self.assertEqual(reaction.book_change.width_seconds,
                         trigger.book_change_bracket_seconds)

    def test_the_reaction_is_dated_from_when_we_could_have_known(self):
        trigger = make_trigger()
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=185), trigger)
        self.assertEqual(reaction.detected_at, trigger.detected_at)
        self.assertEqual(reaction.detected_at, at(195),
                         "the snapshot that revealed the move, not the "
                         "provider's observation stamp at 196")


class OrderingTest(unittest.TestCase):
    """Two brackets that overlap do not order, at any sample size."""

    def test_overlapping_brackets_are_indeterminate(self):
        book = Bracket(at(201), at(196))
        self.assertIs(order_brackets(book, Bracket(at(199), at(198))),
                      Ordering.INDETERMINATE)

    def test_a_missing_endpoint_is_unknown_not_an_order(self):
        """rule 17, on a bracket: unknown is not False."""
        book = Bracket(at(201), at(196))
        self.assertIs(order_brackets(book, Bracket(None, at(198))),
                      Ordering.UNKNOWN)
        self.assertIsNone(book.overlaps(Bracket(None, None)))

    def test_the_exchange_can_lead_which_is_the_null_hypothesis(self):
        """Owner's Kalshi-first case.

        If the exchange consistently moves first, there is no edge here at
        all. A forward-only search could never report this -- it would find
        the exchange already adjusted and call it NO_RESPONSE, filing the
        thesis being FALSIFIED under the same name as the thesis holding.
        """
        reaction = measure(step(215, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=205))
        self.assertIs(reaction.outcome, ReactionOutcome.ALREADY_PRICED)
        self.assertIs(reaction.ordering, Ordering.KALSHI_LED)
        self.assertIsNone(reaction.lag_earliest_seconds,
                          "there is no lag to report: nothing was reacted to")
        self.assertIn("BEFORE the book move became actionable",
                      reaction.detail)

    def test_the_exchange_moving_inside_the_book_bracket_is_indeterminate(self):
        """Owner's indeterminate-ordering case.

        The exchange moved while the book's own change instant was still
        bracketed. Which came first is not a hard question here -- it is an
        unanswerable one, and the audit already said so.
        """
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=198))
        self.assertIs(reaction.outcome, ReactionOutcome.ALREADY_PRICED)
        self.assertIs(reaction.ordering, Ordering.INDETERMINATE)

    def test_the_book_can_lead_and_still_leave_no_opportunity(self):
        """The distinction that keeps outcome and ordering apart.

        The book changed, the provider observed it, and the exchange
        re-priced -- all before our snapshot carried it to us. The book led.
        We had nothing to trade, because the DELIVERY lag ate it. One field
        reporting both would have to pick, and either pick is wrong.
        """
        trigger = make_trigger(moved_snapshot=at(190), moved_observed=at(196))
        self.assertEqual(trigger.detected_at, at(190))
        self.assertEqual(trigger.provider_to_available_seconds, 360.0)
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                switch_at=193), trigger)
        self.assertIs(reaction.outcome, ReactionOutcome.ALREADY_PRICED)
        self.assertIs(reaction.ordering, Ordering.BOOK_LED)


class CensoringTest(unittest.TestCase):
    """A non-answer is reported as a non-answer, never as a zero."""

    def test_no_response_is_right_censored_not_a_measured_zero(self):
        """Owner's no-response case."""
        reaction = measure(series(200, 160, EXCHANGE_BEFORE))
        self.assertIs(reaction.outcome, ReactionOutcome.NO_RESPONSE)
        self.assertEqual(reaction.censored_at_seconds, 1800.0)
        self.assertIsNone(reaction.lag_earliest_seconds)
        self.assertIn("RIGHT-CENSORED", reaction.detail)
        self.assertFalse(reaction.is_measured)

    def test_a_hole_in_the_series_is_blindness_not_silence(self):
        """A missing candle is not a flat price (rule 17).

        The same unanswerable question as the audit's suspension-versus-
        absence, one layer down: a minute with no candle could be an unmoved
        quote or no data, and those give opposite answers.
        """
        holed = without(series(200, 160, EXCHANGE_BEFORE),
                        *range(194, 180, -1))
        reaction = measure(holed)
        self.assertIs(reaction.outcome, ReactionOutcome.BLIND_INTERVAL)
        self.assertIsNotNone(reaction.blind_from)
        self.assertIn("not evidence of no response", reaction.detail)

    def test_a_hole_before_a_response_unbounds_the_lag(self):
        """The exchange moved -- but possibly inside the hole."""
        holed = without(step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                             switch_at=185), *range(194, 186, -1))
        reaction = measure(holed)
        self.assertIs(reaction.outcome, ReactionOutcome.BLIND_INTERVAL)
        self.assertIsNone(reaction.lag_earliest_seconds)
        self.assertAlmostEqual(reaction.exchange_delta, 0.07, places=9)
        self.assertIn("may have happened inside the hole", reaction.detail)

    def test_the_conservative_reading_is_the_default_and_the_flip_is_recorded(
            self):
        """Flipping it is an operator's decision against a verified contract."""
        holed = without(series(200, 160, EXCHANGE_BEFORE),
                        *range(194, 180, -1))
        self.assertFalse(ReactionPolicy().treat_missing_candles_as_unchanged)
        assumed = measure(holed, policy=ReactionPolicy(
            treat_missing_candles_as_unchanged=True))
        self.assertIs(assumed.outcome, ReactionOutcome.NO_RESPONSE)
        self.assertTrue(assumed.as_dict()["policy"]
                        ["treat_missing_candles_as_unchanged"])

    def test_no_exchange_baseline_is_named(self):
        """Nothing at or before the trigger: no response can be measured."""
        reaction = measure(series(190, 160, EXCHANGE_BEFORE))
        self.assertIs(reaction.outcome, ReactionOutcome.NO_BASELINE)
        self.assertIn("nothing to measure a response from", reaction.detail)

    def test_a_move_against_the_book_is_a_divergence_not_a_reaction(self):
        """Signed into the same average, it would cancel real ones out."""
        reaction = measure(step(210, 160, EXCHANGE_BEFORE, 0.49,
                                switch_at=185))
        self.assertIs(reaction.outcome, ReactionOutcome.OPPOSITE_DIRECTION)
        self.assertLess(reaction.exchange_delta, 0)
        self.assertGreater(reaction.book_delta, 0)
        self.assertIn("divergence", reaction.detail)

    def test_an_unreadable_minute_is_a_hole_not_a_price(self):
        """A malformed or one-sided candle is excluded, and leaves blindness.

        `has_malformed_price` means the parser may be behind the wire format;
        a `None` mid means a one-sided book. Neither may be read, and their
        absence is a hole like any other -- not a silently skipped minute.
        """
        flat = series(200, 160, EXCHANGE_BEFORE)
        poisoned = [candle(c.ts, EXCHANGE_AFTER, malformed=True)
                    if c.ts == at(190) else c for c in flat]
        reaction = measure(poisoned)
        self.assertIs(reaction.outcome, ReactionOutcome.BLIND_INTERVAL)
        self.assertNotEqual(reaction.exchange_after, EXCHANGE_AFTER,
                            "a malformed candle's price was read as a move")

        one_sided = [candle(c.ts, None) if c.ts == at(190) else c
                     for c in flat]
        self.assertIs(measure(one_sided).outcome,
                      ReactionOutcome.BLIND_INTERVAL)


class DelayTest(unittest.TestCase):
    """Owner's delay-misses-the-gap case, with its THIRD answer."""

    def setUp(self):
        # exchange follows at minute 185, so the lag interval is [540, 600]
        self.reaction = measure(step(210, 160, EXCHANGE_BEFORE,
                                     EXCHANGE_AFTER, switch_at=185))

    def test_a_delay_inside_the_lag_leaves_the_discrepancy_standing(self):
        self.assertTrue(self.reaction.discrepancy_survives(
            timedelta(seconds=300)))
        self.assertTrue(self.reaction.discrepancy_survives(
            timedelta(seconds=539)))

    def test_a_delay_past_the_lag_misses_it(self):
        self.assertFalse(self.reaction.discrepancy_survives(
            timedelta(seconds=600)))
        self.assertFalse(self.reaction.discrepancy_survives(
            timedelta(seconds=900)))

    def test_a_delay_inside_the_lag_interval_is_undecidable(self):
        """The censoring bites, and a boolean here would be a coin flip.

        At 570s the exchange's new price may or may not already have existed:
        the candle only locates it within `(540, 600]`. Returning True or
        False would be a guess formatted as a measurement.
        """
        for seconds in (541, 570, 599):
            with self.subTest(delay=seconds):
                self.assertIsNone(self.reaction.discrepancy_survives(
                    timedelta(seconds=seconds)))

    def test_an_outcome_with_no_lag_has_no_survives_answer(self):
        """A non-response has no "still" to ask about."""
        no_response = measure(series(200, 160, EXCHANGE_BEFORE))
        self.assertIsNone(no_response.discrepancy_survives(
            timedelta(seconds=60)))

    def test_the_serialised_row_carries_nulls_rather_than_guesses(self):
        row = self.reaction.as_dict()["survives_delay"]
        self.assertIs(row["60s"], True)
        self.assertIs(row["300s"], True)
        self.assertIn("cannot decide", row["note"])


class CheckpointCounterexampleTest(unittest.TestCase):
    """Why this module exists: a coarse grid cannot see a fast round trip.

    The checkpoint study samples fixed leads. Between two of them the book
    can move and move back, and the exchange can follow and follow back --
    so both checkpoints read IDENTICAL prices and zero discrepancy, while a
    complete move-and-reaction happened in between.

    This is a counterexample, not a feature test. It says the checkpoint
    result ("no edge at these leads") does not generalise to "no edge",
    because the sampling cannot represent the event the thesis is about.
    """

    CHECKPOINTS = (180.0, 60.0)          # minutes before kickoff

    def setUp(self):
        # book: 120/-140, out to 160/-190 at minute 150, back at minute 120
        self.quotes = [env(at(180), *BEFORE_PRICES, at(181)),
                       env(at(150), *AFTER_PRICES, at(151)),
                       env(at(120), *BEFORE_PRICES, at(121))]
        # exchange: follows at 145, returns at 115
        self.candles = [
            candle(at(m), EXCHANGE_AFTER if 145 >= m > 115 else EXCHANGE_BEFORE)
            for m in range(180, 59, -1)]

    def test_both_checkpoints_read_identical_prices(self):
        """So the checkpoint study's discrepancy is the same at both."""
        first, last = (at(m) for m in self.CHECKPOINTS)
        book = {t: [q for q in self.quotes
                    if q.provider_snapshot_time <= t][-1] for t in (first, last)}
        self.assertEqual(book[first].payload.away_price,
                         book[last].payload.away_price)
        self.assertEqual(book[first].payload.home_price,
                         book[last].payload.home_price)
        mids = {t: [c for c in self.candles if c.ts <= t][-1].mid
                for t in (first, last)}
        self.assertAlmostEqual(mids[first], mids[last], places=9)
        self.assertAlmostEqual(mids[first], EXCHANGE_BEFORE, places=9)

    def test_the_event_driven_path_finds_the_move_the_grid_cannot(self):
        result = detect_moves(self.quotes)
        self.assertEqual(len(result.triggers), 2, "out, and back")
        self.assertGreater(result.triggers[0].delta_home, 0)
        self.assertLess(result.triggers[1].delta_home, 0)

    def test_and_measures_a_real_reaction_strictly_between_the_checkpoints(
            self):
        trigger = detect_moves(self.quotes).triggers[0]
        reaction = measure(self.candles, trigger)
        self.assertIs(reaction.outcome, ReactionOutcome.RESPONDED)
        self.assertIs(reaction.ordering, Ordering.BOOK_LED)
        self.assertEqual(reaction.lag_earliest_seconds, 240.0)
        self.assertEqual(reaction.lag_latest_seconds, 300.0)
        first, last = (at(m) for m in self.CHECKPOINTS)
        for instant in (reaction.detected_at,
                        reaction.exchange_change.earliest,
                        reaction.exchange_change.latest):
            self.assertGreater(instant, first)
            self.assertLess(instant, last)

    def test_the_counterexample_is_about_sampling_not_about_the_prices(self):
        """The prices at both checkpoints are equal BY CONSTRUCTION.

        Stated explicitly so nobody reads this as a claim that a real book
        does this. It is a demonstration that the grid's sampling cannot
        represent a round trip inside one interval -- which is a property of
        the grid, true whatever the book does.
        """
        self.assertEqual(self.quotes[0].payload.away_price,
                         self.quotes[2].payload.away_price)
        self.assertAlmostEqual(self.candles[0].mid, self.candles[-1].mid,
                               places=9)


class CadenceTest(unittest.TestCase):
    """A transcribed constant that nothing checks can be wrong forever."""

    def test_a_tighter_observed_cadence_means_the_declaration_is_wrong(self):
        """30s gaps cannot come from a 60s grid."""
        stamps = [at(100), at(99.5), at(99)]
        measurement = measure_candle_cadence(stamps)
        self.assertEqual(measurement["min_gap_seconds"], 30.0)
        self.assertFalse(measurement["agrees_with_declared"])

    def test_gaps_wider_than_the_period_are_holes_not_disagreement(self):
        """The distinction the note has to make, or it reads backwards."""
        measurement = measure_candle_cadence(
            [at(100), at(99), at(90), at(89)])
        self.assertEqual(measurement["min_gap_seconds"], 60.0)
        self.assertTrue(measurement["agrees_with_declared"])
        self.assertEqual(measurement["modal_gap_seconds"], 60.0)

    def test_one_timestamp_cannot_measure_a_cadence(self):
        measurement = measure_candle_cadence([at(100)])
        self.assertIsNone(measurement["agrees_with_declared"])
        self.assertIsNone(measurement["min_gap_seconds"])

    def test_the_unanswered_question_has_a_free_command_to_answer_it(self):
        """The module docstring promises one, so it has to exist.

        Whether a quiet minute gets a candle decides the default outcome for
        a hole, and the two readings are opposite. This session cannot check
        it, so the conservative reading ships -- but a stated limitation
        with no route to resolving it is just a limitation, and the docstring
        claims the route is here.
        """
        from reaction.capability import free_verification_commands
        text = "\n".join(free_verification_commands())
        self.assertIn("DOES A QUIET MINUTE GET A CANDLE?", text)
        self.assertIn("treat_missing_candles_as_unchanged", text)
        self.assertIn("Do NOT flip it on an assumption", text)
        self.assertNotIn("apiKey=", text, "a verification command must be free")

    def test_the_declared_period_is_the_audits_candle_floor(self):
        """rule 19: one constant, not two that agree today."""
        from reaction.capability import KALSHI_CANDLE_FLOOR_SECONDS
        self.assertEqual(CANDLE_PERIOD.total_seconds(),
                         KALSHI_CANDLE_FLOOR_SECONDS)
        self.assertEqual(measure_candle_cadence([at(100), at(99)])
                         ["declared_period_seconds"], 60.0)


class PolicyTest(unittest.TestCase):
    """A quiet policy is worse than a loud one."""

    def test_a_nan_or_non_positive_response_threshold_is_refused(self):
        for value in (float("nan"), 0.0, -0.01):
            with self.subTest(min_response=value):
                with self.assertRaises(ValueError):
                    ReactionPolicy(min_response=value)

    def test_a_non_positive_or_non_duration_window_is_refused(self):
        with self.assertRaises(ValueError):
            ReactionPolicy(max_wait=timedelta(0))
        with self.assertRaises(ValueError):
            ReactionPolicy(lookback=timedelta(seconds=-1))
        with self.assertRaises(ValueError):
            ReactionPolicy(candle_period=60)

    def test_the_response_threshold_matches_the_detectors_move_threshold(self):
        """Looser would count noise as a follow; tighter would drop real ones."""
        from reaction.detector import MovePolicy
        self.assertEqual(ReactionPolicy().min_response, MovePolicy().min_move)

    def test_the_policy_rides_on_every_reaction(self):
        row = measure(series(200, 160, EXCHANGE_BEFORE)).as_dict()["policy"]
        self.assertEqual(row["label"], "exploratory-v1")
        self.assertFalse(row["tuned_on_outcomes"])
        self.assertEqual(row["candle_period_seconds"], 60.0)


class BatchTest(unittest.TestCase):
    """Counting: the censored outcomes are reported, never dropped."""

    def test_every_outcome_is_counted_by_name(self):
        result = measure_all([
            (make_trigger(), step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                  switch_at=185), TICKER, True),
            (make_trigger(), series(200, 160, EXCHANGE_BEFORE), TICKER, True),
            (make_trigger(), without(series(200, 160, EXCHANGE_BEFORE),
                                     *range(194, 180, -1)), TICKER, True),
        ])
        self.assertEqual(result.counts(), {
            ReactionOutcome.RESPONDED.value: 1,
            ReactionOutcome.NO_RESPONSE.value: 1,
            ReactionOutcome.BLIND_INTERVAL.value: 1,
        })
        self.assertEqual(len(result.measured), 1)
        self.assertEqual(result.as_dict()["total"], 3)

    def test_the_summary_says_why_measured_is_not_the_denominator(self):
        """`NO_RESPONSE` and `BLIND_INTERVAL` mean opposite things."""
        note = measure_all([]).as_dict()["note"]
        self.assertIn("NOT failures to exclude", note)
        self.assertIn("opposite things", note)

    def test_ordering_is_counted_separately_from_outcome(self):
        result = measure_all([
            (make_trigger(), step(210, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                  switch_at=185), TICKER, True),
            (make_trigger(), step(215, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER,
                                  switch_at=205), TICKER, True),
        ])
        self.assertEqual(result.ordering_counts(),
                         {Ordering.BOOK_LED.value: 1,
                          Ordering.KALSHI_LED.value: 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
