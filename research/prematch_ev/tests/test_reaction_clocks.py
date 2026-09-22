"""Capability audit, separate clocks, and the causal move detector.

WHY THESE TESTS LOOK LIKE THIS
------------------------------
The price trajectories here are SYNTHETIC and labelled so. They are not
claims about what any book did; they are controlled inputs built to make the
timing logic commit to an answer. The owner asked for exactly that separation:
observed payload projections for SCHEMAS, clearly-labelled synthetic series
for TIMING. Where a schema is asserted, the fixture comes from this repo's
real adapters (`SharpQuote`, `Candle`), not from an invented shape.

Every case the owner named that these three layers can decide is here. The
ones that need the reaction measurement (book-leads-Kalshi-follows, ordering
indeterminate, Kalshi-first, no-response, gap-survives-the-delay) belong to
the next increment and are NOT faked here -- a test that pretended to cover
them would be worse than their absence.

THE ONE THAT MATTERS MOST
-------------------------
`ExecutableClockTest` pins that a trigger is dated at the SNAPSHOT that
revealed the move, never at the record's `last_update`. Measuring from
`last_update` would credit the strategy with information it did not have, and
it is the single easiest way to turn this study into a lookahead engine that
reports a beautiful result.

AND THE ONES THAT CAME OUT OF REVIEW
------------------------------------
Six findings on the first version of these three modules, each reproduced
before it was fixed and pinned here afterwards. They share a shape worth
naming: every one passed a check that existed and was pointed at the wrong
clock, the wrong quantity, or the wrong half of an identity.

  `ClockOrderTest`              a stamp from the future gives a NEGATIVE age,
                                and every freshness test ever written is an
                                upper bound
  `DecisionClockFreshnessTest`  a live record 1200s old at receipt passed a
                                900s bound measured to the provider's capture
  `BaselineInvalidationTest`    an interval the detector refused to read left
                                the baseline standing, and a move was claimed
                                across it
  `ContinuityVersusGapTest`     an unchanged price polled steadily advanced
                                nothing, so a live feed read as a gap
  `StreamIdentityTest`          `event:market` let two books share one
                                stream, and the second book's price read as
                                the first one moving
  `FairProbabilityTest`         a private multiplicative de-vig against a
                                study whose frozen baseline is Shin
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.devig import DevigError, devig_american               # noqa: E402
from data.odds_history import SharpQuote                        # noqa: E402
from reaction import capability as cap                          # noqa: E402
from reaction.clocks import (                                   # noqa: E402
    CLOCK_SKEW_TOLERANCE, FutureDataError, Origin, Provenance,
    ReceiptTimeUnknown, SourceEnvelope, assert_no_future_data,
    clock_order_problem, detection_blindness_seconds, earliest_availability,
    envelope_for_candle, envelope_for_sharp_quote,
    first_usable_at_or_after, latest_usable, orientation_key, payload_hash,
    stream_id, usable_at,
)
from reaction.detector import (                                 # noqa: E402
    MoveDetector, MovePolicy, Rejection, fair_probabilities, implied,
)

UTC = timezone.utc
START = datetime(2026, 9, 14, 0, 20, tzinfo=UTC)      # a real NFL kickoff
EVENT = "odds-evt-401872930"


def at(minutes: float) -> datetime:
    """An instant, `minutes` before kickoff. Synthetic, for timing control."""
    return START - timedelta(minutes=minutes)


AWAY, HOME = "Dallas Cowboys", "New York Giants"


def quote(snapshot: datetime, away: float, home: float,
          last_update: datetime | None = None,
          book: str = "pinnacle") -> SharpQuote:
    """A real `SharpQuote` with SYNTHETIC prices.

    The type and field semantics are the adapter's; the numbers are chosen to
    make a specific probability move. `last_update` is the PROVIDER'S
    observation stamp -- the last time the provider's system saw odds for
    this market from the bookmaker -- not the bookmaker's own change time.
    """
    return SharpQuote(
        snapshot=snapshot, commence_time=START,
        away_name=AWAY, home_name=HOME,
        away_price=away, home_price=home, book=book,
        provider_event_id=EVENT,
        last_update=last_update if last_update is not None
        else snapshot - timedelta(seconds=30),
    )


def env(snapshot: datetime, away: float, home: float,
        last_update: datetime | None = None,
        book: str = "pinnacle") -> SourceEnvelope:
    return envelope_for_sharp_quote(
        quote(snapshot, away, home, last_update, book))


def live(snapshot: datetime, away: float, home: float, receipt: datetime,
         last_update: datetime | None = None) -> SourceEnvelope:
    """A LIVE-capture envelope, which has a real local receipt time.

    Historical replay cannot carry one -- `SourceEnvelope.__post_init__`
    refuses it, because we were not listening when it happened -- so a live
    record has to be built by hand. The interval between capture and receipt
    is the whole point: it is the delivery lag a capture-age freshness bound
    cannot see, and it only exists on this path.
    """
    return replace(env(snapshot, away, home, last_update),
                   origin=Origin.LIVE_CAPTURE, local_receipt_time=receipt)


def one_sided(snapshot: datetime, away, home,
              last_update: datetime | None) -> SourceEnvelope:
    """A malformed/degenerate quote on the SAME stream as `env`."""
    return envelope_for_sharp_quote(SharpQuote(
        snapshot=snapshot, commence_time=START, away_name=AWAY, home_name=HOME,
        away_price=away, home_price=home, book="pinnacle",
        provider_event_id=EVENT, last_update=last_update))


class FairProbabilityTest(unittest.TestCase):
    """De-vig within ONE quote, through the study's ONE implementation.

    This module used to carry a PRIVATE multiplicative de-vig while the
    study's frozen baseline is SHIN. Nothing failed: each was internally
    consistent and each repeated itself exactly. They simply disagreed by up
    to 0.005 on real prices, against a 0.01 move threshold -- so a
    near-threshold move existed under one method and not the other, and
    detection and the downstream EV screen would have disagreed about what
    "fair" means.

    So the load-bearing assertion here is AGREEMENT WITH `core.devig`, not
    that this function repeats itself. That is rule 19 pointed at a number
    instead of at a write: correctness is a property of the system, and a
    helper that is perfectly self-consistent can still be the wrong half of
    a disagreement.
    """

    def test_it_is_core_devig_shin_and_not_a_second_implementation(self):
        for away, home in ((120, -140), (160, -190), (-110, -110),
                           (250, -300), (-105, -105)):
            with self.subTest(prices=(away, home)):
                expected = devig_american([away, home]).shin
                fair_away, fair_home, _, _ = fair_probabilities(away, home)
                self.assertAlmostEqual(fair_away, expected[0], places=12)
                self.assertAlmostEqual(fair_home, expected[1], places=12)

    def test_the_two_methods_differ_by_enough_to_move_a_trigger(self):
        """The gap that made a silent choice unsafe, pinned with numbers."""
        _, shin_home, _, disagreement = fair_probabilities(160, -190, "shin")
        _, mult_home, _, _ = fair_probabilities(160, -190, "multiplicative")
        self.assertAlmostEqual(mult_home, 0.6301020, places=6)
        self.assertAlmostEqual(shin_home, 0.6352785, places=6)
        self.assertGreater(abs(shin_home - mult_home), 0.005)
        self.assertAlmostEqual(disagreement, 0.0051765, places=6)
        self.assertGreater(disagreement, MovePolicy().min_move / 2,
                           "over half the move threshold: not a rounding "
                           "difference")

    def test_a_balanced_but_vigged_pair_is_fifty_fifty(self):
        fair_away, fair_home, overround, _ = fair_probabilities(-105, -105)
        self.assertAlmostEqual(fair_away, 0.5, places=9)
        self.assertAlmostEqual(fair_home, 0.5, places=9)
        self.assertAlmostEqual(overround, 1.024390243902439, places=12)

    def test_a_zero_margin_book_is_refused_as_crossed_or_stale(self):
        """100/-100 sums to exactly 1.0, and `core.devig` will not take it.

        No sharp two-way book quotes a 0% margin, so a pair that de-vigs to
        nothing is a crossed or stale quote rather than a free lunch. The old
        private de-vig accepted it happily -- and several fixtures here were
        written against that, which is how an unrealistic price became the
        baseline of a vig-only test.
        """
        self.assertIsNone(fair_probabilities(100, -100))
        with self.assertRaises(DevigError):
            devig_american([100, -100])

    def test_probabilities_sum_to_one_after_devig(self):
        for away, home in ((-140, 120), (250, -300), (-110, -110)):
            with self.subTest(prices=(away, home)):
                fair_away, fair_home, _, _ = fair_probabilities(away, home)
                self.assertAlmostEqual(fair_away + fair_home, 1.0, places=9)

    def test_the_overround_is_carried_out_not_devigged_away(self):
        """A vig-only move is only detectable if the margin survives."""
        _, _, tight, _ = fair_probabilities(-105, -105)
        _, _, wide, _ = fair_probabilities(-120, -120)
        self.assertGreater(wide - tight, MovePolicy().min_move)

    def test_the_favourite_keeps_the_larger_probability(self):
        away_fair, home_fair, _, _ = fair_probabilities(200, -250)
        self.assertGreater(home_fair, away_fair)

    def test_unusable_prices_refuse(self):
        for away, home in ((0, -110), (None, -110), (-110, 0), ("x", -110)):
            with self.subTest(prices=(away, home)):
                self.assertIsNone(fair_probabilities(away, home))

    def test_zero_is_not_odds(self):
        self.assertIsNone(implied(0))


class CapabilityAuditTest(unittest.TestCase):
    """The audit is a control, not commentary."""

    def test_depth_is_unanswerable_on_both_paths(self):
        unanswerable = " ".join(cap.unanswerable_questions()).lower()
        self.assertIn("fillable", unanswerable)
        for source in cap.SOURCES:
            self.assertFalse(source.depth_available, source.source)

    def test_local_receipt_time_is_unknown_for_both_historical_sources(self):
        """Historical replay has no receipt time and may not invent one."""
        for source in cap.SOURCES:
            clock = source.clock(cap.Clock.LOCAL_RECEIPT)
            self.assertIs(clock.evidence, cap.Evidence.UNKNOWN, source.source)
            self.assertFalse(clock.known)

    def test_the_resolution_floor_is_the_coarser_of_the_two_sources(self):
        floor = cap.reaction_resolution_floor_seconds()
        self.assertEqual(floor, 300.0, "the 5-minute odds grid dominates")
        self.assertGreaterEqual(floor, cap.KALSHI_CANDLE_FLOOR_SECONDS)

    def test_ordering_is_interval_censored_not_answerable(self):
        """"Which moved first" cannot be claimed inside the resolution floor."""
        verdict = cap.verdict_for("ordering")
        self.assertIs(verdict.answerability, cap.Answerability.INTERVAL_CENSORED)
        self.assertEqual(verdict.bound_seconds, 300.0)
        self.assertTrue(verdict.may_be_claimed)

    def test_provider_delivery_lag_is_refused_outright(self):
        verdict = cap.verdict_for("delivery lag")
        self.assertIs(verdict.answerability, cap.Answerability.UNANSWERABLE)
        self.assertFalse(verdict.may_be_claimed)

    def test_the_book_change_instant_is_unanswerable_not_answerable(self):
        """The correction the owner's review forced, asserted.

        An earlier version of this audit graded "when did the BOOK move" as
        ANSWERABLE on the strength of the odds archive's `last_update`. That
        field is the last time the PROVIDER'S SYSTEM saw odds for the market
        from the bookmaker -- NOT when the bookmaker changed its price, and
        bookmaker-level `last_update` is deprecated upstream. So the book's
        own change instant is UNANSWERABLE here; what the provider observed
        is a separate question that genuinely is answerable; and when we
        could have known is interval-censored on top of both.

        Three different facts that the one mislabelled field had collapsed
        into a single confident answer.
        """
        self.assertIs(cap.verdict_for("BOOK actually change").answerability,
                      cap.Answerability.UNANSWERABLE)
        self.assertFalse(cap.verdict_for("BOOK actually change").may_be_claimed)
        self.assertIs(cap.verdict_for("PROVIDER last observe").answerability,
                      cap.Answerability.ANSWERABLE)
        self.assertIs(cap.verdict_for("OUR SYSTEM").answerability,
                      cap.Answerability.INTERVAL_CENSORED)

    def test_the_move_magnitude_survives_losing_the_books_clock(self):
        """Not knowing WHEN the book moved does not cost us BY HOW MUCH."""
        verdict = cap.verdict_for("fair probability change")
        self.assertIs(verdict.answerability, cap.Answerability.ANSWERABLE)
        self.assertTrue(verdict.may_be_claimed)

    def test_what_a_live_feed_could_do_is_not_inferable_from_replay(self):
        """A historical snapshot time is an assumption, not a measurement."""
        verdict = cap.verdict_for("LIVE feed")
        self.assertIs(verdict.answerability, cap.Answerability.UNANSWERABLE)
        self.assertFalse(verdict.may_be_claimed)

    def test_measured_cadence_flags_a_wrong_declared_grid(self):
        """A transcribed constant has to be checkable against data.

        Two snapshots 60s apart cannot come from a 5-minute archive, so the
        declared grid is wrong and the audit says so rather than quoting lags
        derived from it.
        """
        tight = [at(100), at(99), at(98)]
        measurement = cap.measure_snapshot_grid(tight)
        self.assertEqual(measurement["min_gap_seconds"], 60.0)
        self.assertFalse(measurement["agrees_with_declared"])
        self.assertIn("*** NO", cap.render(measurement))

    def test_a_five_minute_sample_agrees_with_the_declared_grid(self):
        measurement = cap.measure_snapshot_grid([at(120), at(115), at(110)])
        self.assertTrue(measurement["agrees_with_declared"])

    def test_one_snapshot_cannot_measure_a_cadence(self):
        measurement = cap.measure_snapshot_grid([at(120)])
        self.assertIsNone(measurement["agrees_with_declared"])
        self.assertIsNone(measurement["min_gap_seconds"])

    def test_the_audit_says_it_was_not_verified_in_session(self):
        self.assertFalse(cap.as_dict()["verified_in_session"])
        self.assertIn("NOTHING BELOW WAS RE-VERIFIED", cap.render())

    def test_free_verification_commands_spend_nothing(self):
        text = "\n".join(cap.free_verification_commands())
        self.assertIn("the-odds-api.com", text)
        self.assertIn("docs.kalshi.com", text)
        self.assertNotIn("apiKey=", text)
        self.assertNotIn("--collect", text)


class ExecutableClockTest(unittest.TestCase):
    """A decision runs on when WE knew, never on when the book moved."""

    def test_available_at_is_the_snapshot_not_the_book_update(self):
        moved = at(125)                      # book moved here
        seen = at(120)                       # we first saw it here
        envelope = env(seen, 120, -140, last_update=moved)
        available, reason = envelope.available_at()
        self.assertEqual(available, seen)
        self.assertNotEqual(available, moved)
        self.assertIn("snapshot", reason)

    def test_content_age_reports_how_old_the_price_was(self):
        envelope = env(at(120), 120, -140, last_update=at(125))
        self.assertEqual(envelope.content_age_seconds(), 300.0)

    def test_unknown_last_update_gives_unknown_age_not_zero(self):
        envelope = env(at(120), 120, -140, last_update=None)
        # the helper defaults last_update; build one explicitly without it
        raw = SharpQuote(
            snapshot=at(120), commence_time=START, away_name="a", home_name="b",
            away_price=120, home_price=-140, book="pinnacle",
            provider_event_id=EVENT, last_update=None)
        bare = envelope_for_sharp_quote(raw)
        self.assertIsNone(bare.content_age_seconds())

    def test_a_historical_record_may_not_carry_a_receipt_time(self):
        with self.assertRaises(ValueError):
            SourceEnvelope(
                provenance=Provenance("s", "e", "m", payload_hash({})),
                origin=Origin.HISTORICAL_REPLAY,
                provider_snapshot_time=at(100),
                payload=None,
                local_receipt_time=at(100))

    def test_requiring_a_receipt_time_raises_rather_than_substituting(self):
        envelope = env(at(120), 120, -140)
        with self.assertRaises(ReceiptTimeUnknown):
            envelope.require_receipt_time()

    def test_a_naive_timestamp_is_refused(self):
        with self.assertRaises(ValueError):
            SourceEnvelope(
                provenance=Provenance("s", "e", "m", payload_hash({})),
                origin=Origin.HISTORICAL_REPLAY,
                provider_snapshot_time=datetime(2026, 9, 14, 0, 20),
                payload=None)

    def test_earliest_availability_finds_the_first_snapshot_carrying_a_value(self):
        """The archive re-serves an unchanged price; we knew it from the first."""
        moved = at(126)
        first = env(at(125), 120, -140, last_update=moved)
        again = env(at(120), 120, -140, last_update=moved)
        third = env(at(115), 120, -140, last_update=moved)
        earliest = earliest_availability([third, again, first])
        self.assertEqual(len(earliest), 1, "one value, seen three times")
        self.assertEqual(list(earliest.values())[0], at(125))

    def test_blindness_is_measured_to_the_first_sighting(self):
        moved = at(126)
        first = env(at(125), 120, -140, last_update=moved)
        again = env(at(120), 120, -140, last_update=moved)
        earliest = earliest_availability([first, again])
        self.assertEqual(detection_blindness_seconds(again, earliest), 60.0)

    def test_blindness_is_unknown_without_a_provider_observation_time(self):
        raw = SharpQuote(
            snapshot=at(120), commence_time=START, away_name="a", home_name="b",
            away_price=120, home_price=-140, book="pinnacle",
            provider_event_id=EVENT, last_update=None)
        self.assertIsNone(detection_blindness_seconds(
            envelope_for_sharp_quote(raw)))

    def test_candles_carry_no_provider_observation_time(self):
        """A candle cannot say when inside its period the quote moved."""
        @dataclass
        class Candle:
            ts: datetime
            bid_close: float
            ask_close: float

        envelope = envelope_for_candle(
            Candle(at(100), 0.44, 0.47), "KXNFLGAME-26SEP13DALNYG-DAL")
        self.assertIsNone(envelope.provider_observed_at)
        self.assertIsNone(envelope.content_age_seconds())
        self.assertEqual(envelope.available_at()[0], at(100))
        self.assertEqual(envelope.resolution_seconds, 60.0)


class NoFutureDataTest(unittest.TestCase):
    """Lookahead raises. It has shipped twice in this study already."""

    def test_a_record_from_after_the_decision_is_refused(self):
        later = env(at(100), 120, -140)
        with self.assertRaises(FutureDataError) as caught:
            assert_no_future_data(at(110), later, what="book quote")
        self.assertIn("lookahead", str(caught.exception).lower())

    def test_a_record_available_at_the_decision_is_allowed(self):
        now = env(at(110), 120, -140)
        assert_no_future_data(at(110), now)          # exactly at: allowed

    def test_a_book_move_before_the_decision_is_still_refused_if_unseen(self):
        """THE case. The book moved at 11:20; we saw it at 11:25.

        A decision at 11:22 may not use it, even though `last_update`
        precedes the decision, because the snapshot that revealed it did not.
        """
        envelope = env(at(115), 120, -140, last_update=at(120))
        self.assertLess(envelope.provider_observed_at, at(118))
        with self.assertRaises(FutureDataError):
            assert_no_future_data(at(118), envelope)

    def test_usable_at_filters_and_orders(self):
        envelopes = [env(at(100), 120, -140), env(at(120), 110, -130),
                     env(at(110), 115, -135)]
        usable = usable_at(at(110), envelopes)
        self.assertEqual([e.provider_snapshot_time for e in usable],
                         [at(120), at(110)])
        self.assertEqual(latest_usable(at(110), envelopes)
                         .provider_snapshot_time, at(110))

    def test_latest_usable_is_none_when_nothing_was_available(self):
        self.assertIsNone(latest_usable(at(200), [env(at(100), 120, -140)]))


class DelayedEntryTest(unittest.TestCase):
    """A delay waits for the next quote; it never reaches backwards."""

    def test_the_first_quote_at_or_after_the_delay_is_taken(self):
        envelopes = [env(at(120), 120, -140), env(at(115), 118, -138),
                     env(at(110), 116, -136)]
        chosen, reason = first_usable_at_or_after(
            at(116), envelopes, timedelta(minutes=10))
        self.assertEqual(chosen.provider_snapshot_time, at(115))
        self.assertIn("at or after", reason)

    def test_no_backfill_from_before_the_delay(self):
        """The pre-delay quote must not be served as a delayed fill."""
        envelopes = [env(at(120), 120, -140)]
        chosen, reason = first_usable_at_or_after(
            at(115), envelopes, timedelta(minutes=10))
        self.assertIsNone(chosen, "the only quote predates the delayed decision")
        self.assertIn("censored", reason)

    def test_a_quote_beyond_the_allowed_wait_is_censored_not_filled(self):
        """Owner's case: next quote beyond the allowed wait."""
        envelopes = [env(at(60), 120, -140)]        # an hour later
        chosen, reason = first_usable_at_or_after(
            at(115), envelopes, timedelta(minutes=5))
        self.assertIsNone(chosen)
        self.assertIn("right-censored", reason)
        self.assertIn("not a fill", reason)


class DetectorTest(unittest.TestCase):
    """SYNTHETIC trajectories, built to force each named outcome."""

    def setUp(self):
        self.detector = MoveDetector(MovePolicy())

    def feed(self, *envelopes):
        return self.detector.observe_all(list(envelopes))

    def test_a_first_observation_is_a_baseline_never_a_move(self):
        result = self.feed(env(at(200), 120, -140))
        self.assertEqual(result.triggers, [])
        self.assertEqual(result.counts(),
                         {Rejection.FIRST_OBSERVATION.value: 1})

    def test_a_real_move_triggers_with_both_orientations(self):
        result = self.feed(env(at(200), 120, -140),
                           env(at(195), 160, -190))
        self.assertEqual(len(result.triggers), 1)
        trigger = result.triggers[0]
        self.assertGreater(trigger.delta_home, 0, "home shortened")
        self.assertAlmostEqual(trigger.delta_away, -trigger.delta_home, places=9)
        self.assertEqual(trigger.delta_for(participant_is_home=True),
                         trigger.delta_home)
        self.assertEqual(trigger.delta_for(participant_is_home=False),
                         trigger.delta_away)

    def test_the_trigger_is_dated_at_the_snapshot_not_the_provider_stamp(self):
        result = self.feed(env(at(200), 120, -140, last_update=at(201)),
                           env(at(195), 160, -190, last_update=at(199)))
        trigger = result.triggers[0]
        self.assertEqual(trigger.detected_at, at(195))
        self.assertEqual(trigger.provider_observed_at, at(199))
        self.assertEqual(trigger.provider_to_available_seconds, 240.0)

    def test_the_book_change_instant_is_a_bracket_and_never_a_point(self):
        """There is no `book_moved_at`, because this data has no such field.

        `last_update` is when the PROVIDER saw the odds. The book changed its
        price somewhere after the provider last observed the OLD value and at
        or before it observed the NEW one -- an interval. An earlier version
        of this module called that stamp the book's move time and reported a
        point estimate, which is a claim the source cannot support.
        """
        result = self.feed(env(at(200), 120, -140, last_update=at(201)),
                           env(at(195), 160, -190, last_update=at(199)))
        trigger = result.triggers[0]
        self.assertFalse(hasattr(trigger, "book_moved_at"))
        self.assertEqual(trigger.book_change_earliest, at(201))
        self.assertEqual(trigger.book_change_latest, at(199))
        self.assertEqual(trigger.book_change_bracket_seconds, 120.0)
        bracket = trigger.as_dict()["book_change_bracket"]
        self.assertEqual(bracket["width_seconds"], 120.0)
        self.assertIn("NOT in this data", bracket["note"])

    def test_a_tiny_move_is_below_threshold(self):
        result = self.feed(env(at(200), 120, -140),
                           env(at(195), 121, -141))
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.BELOW_THRESHOLD.value, result.counts())

    def test_a_reconnect_reserving_the_same_price_is_not_a_move(self):
        """Owner's case: a provider reconnect must not masquerade as a move."""
        moved = at(201)
        result = self.feed(env(at(200), 120, -140, last_update=moved),
                           env(at(195), 120, -140, last_update=moved),
                           env(at(190), 120, -140, last_update=moved))
        self.assertEqual(result.triggers, [])
        self.assertEqual(result.counts()[Rejection.UNCHANGED_CONTENT.value], 2)

    def test_a_stale_book_price_in_a_fresh_envelope_does_not_trigger(self):
        """Owner's case: fresh envelopes with stale book prices."""
        result = self.feed(
            env(at(200), 120, -140, last_update=at(201)),
            env(at(195), 160, -190, last_update=at(260)))   # 65 min old
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.STALE_AT_DECISION.value, result.counts())

    def test_there_is_no_separate_capture_age_rejection(self):
        """One name for staleness, and it sits on the DECISION clock.

        A `stale_content` rejection measured to the provider's CAPTURE is
        what let a live record 1200s old at receipt through a 900s bound
        (`DecisionClockFreshnessTest`). Keeping both names would have kept
        both bounds.
        """
        self.assertFalse(any(r.value == "stale_content" for r in Rejection))

    def test_an_unknown_content_age_is_refused_by_default(self):
        baseline = env(at(200), 120, -140, last_update=at(201))
        raw = SharpQuote(
            snapshot=at(195), commence_time=START, away_name="a", home_name="b",
            away_price=160, home_price=-190, book="pinnacle",
            provider_event_id=EVENT, last_update=None)
        result = self.feed(baseline, envelope_for_sharp_quote(raw))
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.UNKNOWN_CONTENT_AGE.value, result.counts())

    def test_a_missing_side_is_named_not_carried_forward(self):
        """Owner's case: missing bid/ask."""
        raw = SharpQuote(
            snapshot=at(195), commence_time=START, away_name="a", home_name="b",
            away_price=None, home_price=-140, book="pinnacle",
            provider_event_id=EVENT, last_update=at(196))
        result = self.feed(env(at(200), 120, -140),
                           envelope_for_sharp_quote(raw))
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.MISSING_SIDE.value, result.counts())

    def test_a_gap_resets_the_baseline_instead_of_claiming_one_move(self):
        """Owner's case: reconnect gap. Inside a hole the book may move often."""
        result = self.feed(env(at(400), 120, -140),
                           env(at(200), 300, -400))      # 200 minutes later
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.GAP.value, result.counts())
        # and the baseline is now the later quote, so the NEXT move is measured
        # from it rather than across the hole
        self.detector.observe(env(at(195), 340, -450))
        self.assertEqual(len(self.detector.result.triggers), 1)

    def test_an_out_of_order_arrival_is_rejected_not_silently_sorted(self):
        """Owner's case: out-of-order updates."""
        result = self.feed(env(at(200), 120, -140),
                           env(at(195), 160, -190),
                           env(at(205), 100, -120))      # earlier than baseline
        self.assertEqual(len(result.triggers), 1)
        self.assertIn(Rejection.OUT_OF_ORDER.value, result.counts())

    def test_a_vig_only_change_is_reported_as_such(self):
        """The book widened its margin without changing its view.

        Both pairs are 50/50 after de-vigging, so the fair probability does
        not move at all; only the overround does. The baseline was 100/-100
        until `core.devig` began (correctly) refusing a zero-margin book as
        crossed or stale -- -105/-105 is the realistic sharp two-way price
        and makes the same point.
        """
        result = self.feed(env(at(200), -105, -105),     # overround 1.0244
                           env(at(195), -120, -120))     # wider, still 50/50
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.VIG_ONLY.value, result.counts())

    def test_a_reversal_produces_two_triggers_with_opposite_signs(self):
        """Owner's case: book reversal."""
        detector = MoveDetector(MovePolicy(debounce=timedelta(0)))
        result = detector.observe_all([
            env(at(200), 120, -140),
            env(at(195), 200, -250),      # home shortens
            env(at(190), 120, -140),      # and back
        ])
        self.assertEqual(len(result.triggers), 2)
        self.assertGreater(result.triggers[0].delta_home, 0)
        self.assertLess(result.triggers[1].delta_home, 0)

    def test_debounce_collapses_a_walked_line_into_one_episode(self):
        """Owner's case: multiple triggers in one game, not counted as many."""
        detector = MoveDetector(MovePolicy(debounce=timedelta(minutes=10)))
        result = detector.observe_all([
            env(at(200), 120, -140),
            env(at(195), 170, -200),
            env(at(193), 220, -270),
            env(at(191), 260, -330),
        ])
        self.assertEqual(len(result.triggers), 1,
                         "one book walking a line is one episode")

    def test_multiple_triggers_survive_when_spaced_beyond_debounce(self):
        """Spacing must clear debounce AND stay inside max_gap.

        Written first with a 40-minute third quote, which the detector
        correctly refused: 40 > the 35-minute `max_gap`, so it reset the
        baseline instead of triggering. The two bounds are independent and
        pull in opposite directions -- debounce sets a FLOOR on spacing, and
        max_gap sets a CEILING -- so a trigger needs to sit between them.
        """
        detector = MoveDetector(MovePolicy(debounce=timedelta(minutes=2)))
        result = detector.observe_all([
            env(at(200), 120, -140),
            env(at(190), 170, -200),     # +10 min: past debounce, inside gap
            env(at(170), 120, -140),     # +20 min: same
        ])
        self.assertEqual(len(result.triggers), 2)

    def test_debounce_and_max_gap_bound_spacing_from_opposite_ends(self):
        """Too close is debounced; too far resets. Neither is a trigger."""
        policy = MovePolicy(debounce=timedelta(minutes=5),
                            max_gap=timedelta(minutes=20))
        too_close = MoveDetector(policy).observe_all([
            env(at(200), 120, -140),
            env(at(199), 170, -200),
            env(at(198), 220, -280),     # 1 min after the trigger: debounced
        ])
        self.assertEqual(len(too_close.triggers), 1)
        self.assertTrue(any("debounced" in r.detail
                            for r in too_close.rejections))

        too_far = MoveDetector(policy).observe_all([
            env(at(200), 120, -140),
            env(at(170), 170, -200),     # 30 min: past max_gap
        ])
        self.assertEqual(too_far.triggers, [])
        self.assertIn(Rejection.GAP.value, too_far.counts())

    def test_the_policy_rides_on_every_trigger(self):
        result = self.feed(env(at(200), 120, -140), env(at(195), 160, -190))
        policy = result.triggers[0].as_dict()["policy"]
        self.assertEqual(policy["label"], "exploratory-v2")
        self.assertEqual(policy["devig_method"], "shin")
        self.assertFalse(policy["tuned_on_outcomes"])

    def test_both_endpoints_of_a_trigger_carry_provenance(self):
        result = self.feed(env(at(200), 120, -140), env(at(195), 160, -190))
        row = result.triggers[0].as_dict()
        for side in ("before", "after"):
            self.assertEqual(len(row[side]["payload_sha256"]), 64)
            self.assertEqual(row[side]["source"], "the_odds_api_historical")
            self.assertIsNone(row[side]["local_receipt_time"])
        self.assertNotEqual(row["before"]["payload_sha256"],
                            row["after"]["payload_sha256"])

    def test_rejections_are_counted_by_name_not_merely_dropped(self):
        result = self.feed(env(at(200), 120, -140),
                           env(at(195), 121, -141),
                           env(at(190), 121, -141))
        counts = result.counts()
        self.assertEqual(sum(counts.values()), result.rejection_total
                         if hasattr(result, "rejection_total") else 3)
        self.assertGreaterEqual(len(counts), 2)


class ClockOrderTest(unittest.TestCase):
    """An impossible clock ordering is refused BEFORE any freshness bound.

    Owner's finding: a record whose `provider_observed_at` sat AFTER its own
    capture produced an age of -300s, and -300 < 900 is true, so it sailed
    through the freshness check and triggered. Every freshness test is an
    upper bound; a negative age is the one input that defeats all of them at
    once. So the ordering is checked first, and separately.
    """

    def test_a_stamp_after_its_own_capture_is_named(self):
        problem = clock_order_problem(env(at(200), 120, -140,
                                          last_update=at(195)))
        self.assertIsNotNone(problem)
        self.assertIn("AFTER", problem)
        self.assertIn("negative", problem)

    def test_a_future_stamp_makes_blindness_negative_which_is_the_tell(self):
        """The exact number from the repro, kept as the signature."""
        bad = env(at(195), 160, -190, last_update=at(190))
        self.assertEqual(detection_blindness_seconds(bad), -300.0)
        self.assertEqual(bad.content_age_seconds(), -300.0)
        self.assertIsNotNone(clock_order_problem(bad))

    def test_small_skew_between_a_providers_own_clocks_is_tolerated(self):
        """Refusing 1s of skew would throw away good records."""
        skewed = env(at(200), 120, -140,
                     last_update=at(200) + timedelta(seconds=1))
        self.assertIsNone(clock_order_problem(skewed))
        beyond = env(at(200), 120, -140,
                     last_update=at(200) + timedelta(seconds=30))
        self.assertIsNotNone(clock_order_problem(beyond))
        self.assertIsNone(clock_order_problem(
            beyond, tolerance=timedelta(seconds=60)),
            "the tolerance is a parameter, not a constant baked into the rule")

    def test_a_receipt_before_the_capture_is_named(self):
        early = replace(env(at(200), 120, -140), origin=Origin.LIVE_CAPTURE,
                        local_receipt_time=at(205))
        problem = clock_order_problem(early)
        self.assertIn("BEFORE", problem)
        self.assertIn("received a record", problem)

    def test_a_response_before_its_request_is_named(self):
        backwards = replace(env(at(200), 120, -140),
                            request_sent_at=at(199),
                            response_received_at=at(201))
        self.assertIn("precedes request_sent_at",
                      clock_order_problem(backwards))

    def test_a_coherent_record_reports_no_problem(self):
        self.assertIsNone(clock_order_problem(env(at(200), 120, -140,
                                                  last_update=at(201))))
        self.assertIsNone(clock_order_problem(
            live(at(200), 120, -140, receipt=at(199), last_update=at(201))))

    def test_the_detector_refuses_it_and_keeps_no_state_from_it(self):
        detector = MoveDetector(MovePolicy())
        detector.observe(env(at(200), 300, -400, last_update=at(195)))
        self.assertEqual(detector.result.triggers, [])
        self.assertEqual(detector.result.counts(),
                         {Rejection.CLOCK_ORDER_INVALID.value: 1})
        # its prices must never anchor a later delta: the next good quote is
        # still a FIRST observation, not a move away from 300/-400
        detector.observe(env(at(190), 120, -140, last_update=at(191)))
        self.assertEqual(detector.result.triggers, [])
        self.assertIn(Rejection.FIRST_OBSERVATION.value,
                      detector.result.counts())

    def test_a_future_stamp_between_two_good_quotes_triggers_nothing(self):
        """The reproduced failure, end to end."""
        detector = MoveDetector(MovePolicy())
        result = detector.observe_all([
            env(at(200), 120, -140, last_update=at(201)),
            env(at(195), 160, -190, last_update=at(190)),   # 300s ahead
        ])
        self.assertEqual(result.triggers, [],
                         "a negative age passed the 900s upper bound")
        self.assertIn(Rejection.CLOCK_ORDER_INVALID.value, result.counts())


class DecisionClockFreshnessTest(unittest.TestCase):
    """The freshness bound sits on the clock the DECISION runs on.

    Owner's finding: a LIVE record captured fresh and received 20 minutes
    later was 1200s old when it became actionable, and the bound measured to
    the provider's CAPTURE called it fresh at 30s. The record was honest; the
    clock was wrong.
    """

    def test_a_live_record_old_at_receipt_is_stale_however_fresh_the_capture(
            self):
        detector = MoveDetector(MovePolicy())      # max_age_at_decision 900s
        result = detector.observe_all([
            live(at(200), 120, -140, receipt=at(199), last_update=at(200.5)),
            live(at(195), 160, -190, receipt=at(175), last_update=at(195.5)),
        ])
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.STALE_AT_DECISION.value, result.counts())
        detail = " ".join(r.detail for r in result.rejections)
        self.assertIn("1230s old when we could act", detail)
        self.assertIn("capture-age bound would have called this fresh", detail)

    def test_the_two_age_clocks_disagree_and_the_decision_one_binds(self):
        delayed = live(at(195), 160, -190, receipt=at(175),
                       last_update=at(195.5))
        bound = MovePolicy().max_age_at_decision.total_seconds()
        self.assertEqual(delayed.content_age_seconds(), 30.0)
        self.assertEqual(delayed.age_at_decision_seconds(), 1230.0)
        self.assertLess(delayed.content_age_seconds(), bound)
        self.assertGreater(delayed.age_at_decision_seconds(), bound)

    def test_for_historical_replay_the_two_clocks_coincide(self):
        """Which is exactly why the capture-age bound looked adequate.

        In replay `available_at` IS the provider snapshot, so the two ages are
        the same number and no test on historical fixtures could tell them
        apart. The defect only exists on the live path -- the path this study
        has not built yet, and the one a pilot would run on.
        """
        record = env(at(195), 160, -190, last_update=at(200))
        self.assertEqual(record.content_age_seconds(), 300.0)
        self.assertEqual(record.age_at_decision_seconds(), 300.0)

    def test_a_live_record_received_promptly_still_triggers(self):
        """The bound must not simply refuse the live path wholesale."""
        result = MoveDetector(MovePolicy()).observe_all([
            live(at(200), 120, -140, receipt=at(199.5), last_update=at(200.5)),
            live(at(195), 160, -190, receipt=at(194.5), last_update=at(195.5)),
        ])
        self.assertEqual(len(result.triggers), 1)
        trigger = result.triggers[0]
        self.assertEqual(trigger.detected_at, at(194.5), "the receipt time")
        self.assertEqual(trigger.age_at_decision_seconds, 60.0)
        self.assertEqual(trigger.capture_age_seconds, 30.0)

    def test_a_missing_receipt_time_on_a_live_record_is_not_a_decision_time(
            self):
        """Rule 17 on the executable clock: unknown is not now."""
        blind = replace(env(at(200), 120, -140), origin=Origin.LIVE_CAPTURE)
        available, reason = blind.available_at()
        self.assertIsNone(available)
        self.assertIn("no receipt time", reason)
        result = MoveDetector(MovePolicy()).observe_all([blind])
        self.assertEqual(result.counts(),
                         {Rejection.NO_AVAILABILITY.value: 1})


class BaselineInvalidationTest(unittest.TestCase):
    """An interval the detector could not read breaks COMPARABILITY.

    Owner's finding: a missing side was correctly named -- and the baseline
    was left standing, so the next good quote closed a delta against a
    pre-hole price. The detector refused to read an interval and then
    reported a move across it.
    """

    def test_a_missing_side_stops_a_move_being_claimed_across_it(self):
        detector = MoveDetector(MovePolicy())
        detector.observe(env(at(200), 120, -140, last_update=at(201)))
        detector.observe(one_sided(at(197), None, -160, at(198)))
        detector.observe(env(at(195), 160, -190, last_update=at(196)))
        self.assertEqual(detector.result.triggers, [],
                         "a move was claimed across an unreadable interval")
        counts = detector.result.counts()
        self.assertEqual(counts[Rejection.MISSING_SIDE.value], 1)
        self.assertEqual(counts[Rejection.BASELINE_INVALIDATED.value], 1)

    def test_every_unusable_interval_invalidates_the_baseline(self):
        """One table, so a new rejection cannot quietly skip the reset."""
        cases = {
            Rejection.MISSING_SIDE:
                dict(away=None, home=-160, update=at(198)),
            Rejection.UNDEVIGGABLE:
                dict(away=100, home=-100, update=at(198)),   # booksum 1.0
            Rejection.UNKNOWN_CONTENT_AGE:
                dict(away=160, home=-190, update=None),
            Rejection.STALE_AT_DECISION:
                dict(away=160, home=-190, update=at(280)),   # 83 min old
            Rejection.CLOCK_ORDER_INVALID:
                dict(away=160, home=-190, update=at(190)),   # from the future
        }
        for expected, spec in cases.items():
            with self.subTest(rejection=expected.value):
                detector = MoveDetector(MovePolicy())
                detector.observe(env(at(200), 120, -140, last_update=at(201)))
                detector.observe(one_sided(at(197), spec["away"], spec["home"],
                                           spec["update"]))
                detector.observe(env(at(195), 160, -190, last_update=at(196)))
                counts = detector.result.counts()
                self.assertIn(expected.value, counts)
                self.assertEqual(
                    detector.result.triggers, [],
                    f"a move was claimed across a {expected.value} interval")
                self.assertEqual(
                    counts.get(Rejection.BASELINE_INVALIDATED.value), 1,
                    f"{expected.value} left the baseline comparable")

    def test_the_stream_recovers_from_the_re_established_baseline(self):
        """Invalidation costs ONE trigger, it does not kill the stream."""
        detector = MoveDetector(MovePolicy())
        detector.observe(env(at(200), 120, -140, last_update=at(201)))
        detector.observe(one_sided(at(197), None, -160, at(198)))
        detector.observe(env(at(195), 160, -190, last_update=at(196)))
        detector.observe(env(at(190), 260, -330, last_update=at(191)))
        self.assertEqual(len(detector.result.triggers), 1)
        trigger = detector.result.triggers[0]
        self.assertEqual(trigger.detected_at, at(190))
        self.assertAlmostEqual(
            trigger.fair_before_home,
            fair_probabilities(160, -190)[1], places=12,
            msg="measured from the re-established baseline, not the pre-hole "
                "quote")


class ContinuityVersusGapTest(unittest.TestCase):
    """Continuity and comparability are different facts about a stream.

    Owner's finding: with one field for both, an unchanged price polled every
    five minutes advanced NEITHER -- so a live feed that had never stopped
    read as `gap_in_input`, and the move at the end of it was refused.

    Three shapes of "the price did not change", and only the third is a gap.
    """

    def test_a_re_served_price_advances_continuity_not_the_baseline(self):
        """Shape one: same prices, FROZEN provider stamp (cache/reconnect)."""
        detector = MoveDetector(MovePolicy())          # max_gap 35 min
        frozen = at(201)
        envelopes = [env(at(200 - 5 * i), 120, -140, last_update=frozen)
                     for i in range(9)]                # 45 min of polling
        envelopes.append(env(at(155), 160, -190, last_update=at(156)))
        result = detector.observe_all(envelopes)
        counts = result.counts()
        self.assertEqual(len(result.triggers), 1,
                         "45 minutes of steady polling is not a gap")
        self.assertNotIn(Rejection.GAP.value, counts)
        self.assertEqual(counts[Rejection.UNCHANGED_CONTENT.value], 8)
        self.assertEqual(counts[Rejection.FIRST_OBSERVATION.value], 1)
        # and the bracket stays HONEST about it: the provider never observed
        # a new value in those 45 minutes, so the book could have changed
        # anywhere inside them
        self.assertEqual(result.triggers[0].book_change_bracket_seconds, 2700.0)

    def test_a_re_observed_unchanged_price_narrows_the_bracket(self):
        """Shape two: same prices, ADVANCING stamp -- the archive's own case.

        Here the provider really did look again and the book really had not
        moved, so each observation narrows when a later change can have
        happened. The content key changes with the stamp, so these are not
        deduplicated: they fall through to the threshold and advance the
        baseline, which is the same continuity by a different route.
        """
        detector = MoveDetector(MovePolicy())
        envelopes = [env(at(200 - 5 * i), 120, -140,
                         last_update=at(201 - 5 * i)) for i in range(9)]
        envelopes.append(env(at(155), 160, -190, last_update=at(156)))
        result = detector.observe_all(envelopes)
        counts = result.counts()
        self.assertEqual(len(result.triggers), 1)
        self.assertNotIn(Rejection.GAP.value, counts)
        self.assertNotIn(Rejection.UNCHANGED_CONTENT.value, counts)
        self.assertEqual(counts[Rejection.BELOW_THRESHOLD.value], 8)
        self.assertEqual(result.triggers[0].book_change_bracket_seconds, 300.0,
                         "one poll interval, not the whole 45 minutes")

    def test_a_genuinely_missing_interval_is_a_gap(self):
        """Shape three: nothing arrived at all. The book may have moved often."""
        result = MoveDetector(MovePolicy()).observe_all([
            env(at(200), 120, -140, last_update=at(201)),
            env(at(150), 160, -190, last_update=at(151)),   # 50 min of silence
        ])
        self.assertEqual(result.triggers, [])
        self.assertIn(Rejection.GAP.value, result.counts())

    def test_a_declared_absence_resets_the_baseline(self):
        """`observe()` sees only what ARRIVED, so absence has to be declared.

        A market omitted entirely from a provider response -- suspended,
        delisted, or simply not carried -- produces no envelope, so nothing
        calls the detector and the hole is invisible to it. The collector
        knows, and `note_gap` is how it says so.
        """
        detector = MoveDetector(MovePolicy())
        first = env(at(200), 120, -140, last_update=at(201))
        stream = first.provenance.market_id
        detector.observe(first)
        detector.note_gap(stream, at(197), "market absent from the response")
        detector.observe(env(at(195), 160, -190, last_update=at(196)))
        counts = detector.result.counts()
        self.assertEqual(detector.result.triggers, [],
                         "no move claimed across a declared absence")
        self.assertEqual(counts[Rejection.DECLARED_GAP.value], 1)
        self.assertEqual(counts[Rejection.BASELINE_INVALIDATED.value], 1)
        detector.observe(env(at(190), 260, -330, last_update=at(191)))
        self.assertEqual(len(detector.result.triggers), 1,
                         "and the stream recovers after it")

    def test_an_unchanged_price_does_not_reset_the_baseline(self):
        """Continuity advances; comparability must not silently restart.

        A re-served price that RESET the baseline would be the mirror defect:
        the move would then be measured from the re-serve rather than from
        the last real change, which for a walked line is a different number.
        """
        detector = MoveDetector(MovePolicy())
        frozen = at(201)
        result = detector.observe_all([
            env(at(200), 120, -140, last_update=frozen),
            env(at(197), 120, -140, last_update=frozen),    # re-served
            env(at(195), 121, -141, last_update=at(196)),   # tiny move
        ])
        self.assertEqual(result.triggers, [])
        counts = result.counts()
        self.assertEqual(counts[Rejection.UNCHANGED_CONTENT.value], 1)
        self.assertIn(Rejection.BELOW_THRESHOLD.value, counts)
        self.assertNotIn(Rejection.BASELINE_INVALIDATED.value, counts)


class RegressedContentTest(unittest.TestCase):
    """An older copy, re-served, is not news.

    A response served from a lagging copy carries what the provider saw
    BEFORE an observation already in hand. It is new content -- a different
    stamp -- so deduplication lets it through, and against the baseline it
    reads as a move back. The newer price arriving again a poll later is then
    a second move, measured from the stale copy. SYNTHETIC trajectories; the
    shape is what a live feed behind a lagging cache would serve.
    """

    def moved(self):
        """Baseline, then a real move: (detector, first trigger)."""
        detector = MoveDetector(MovePolicy())
        detector.observe(env(at(200), 120, -140, last_update=at(201)))
        trigger = detector.observe(env(at(195), 160, -190,
                                       last_update=at(196)))
        self.assertIsNotNone(trigger)
        return detector, trigger

    def test_an_older_copy_re_served_is_not_a_move_back(self):
        detector, _ = self.moved()
        # A minute later the feed answers with the PRE-move copy: fresh
        # enough to act on (seven minutes old), but stamped before the
        # observation already seen. Then a second older copy, newer than the
        # first but still older than the move -- an older copy must not
        # lower the bar for the next one.
        detector.observe(env(at(194), 120, -140, last_update=at(201)))
        detector.observe(env(at(193), 120, -140, last_update=at(198)))
        # and the newer copy again: the same content already in hand
        detector.observe(env(at(190), 160, -190, last_update=at(196)))
        counts = detector.result.counts()
        self.assertEqual(len(detector.result.triggers), 1,
                         "one book move is one trigger")
        self.assertEqual(counts[Rejection.REGRESSED_CONTENT.value], 2)
        self.assertEqual(counts[Rejection.UNCHANGED_CONTENT.value], 1,
                         "the newer copy is a repeat of the newest content, "
                         "not a change from the older one")
        self.assertNotIn(Rejection.BASELINE_INVALIDATED.value, counts)

    def test_the_next_move_is_measured_from_the_newest_observation(self):
        detector, first = self.moved()
        detector.observe(env(at(194), 120, -140, last_update=at(201)))
        second = detector.observe(env(at(190), 260, -330, last_update=at(191)))
        self.assertIsNotNone(second)
        self.assertAlmostEqual(second.fair_before_home, first.fair_after_home,
                               places=12)
        self.assertEqual(second.book_change_earliest, at(196),
                         "the bracket opens at the newest observation, not "
                         "at the stale copy's")

    def test_an_older_copy_still_counts_as_the_feed_answering(self):
        """Continuity, exactly as for an unchanged re-serve: the feed did
        answer, so a later move is not refused as a gap. The bracket stays
        honest instead -- it opens at the newest observation, 40 minutes
        before the move, because nothing newer was seen in between."""
        detector, _ = self.moved()
        detector.observe(env(at(185), 120, -140, last_update=at(197)))
        later = detector.observe(env(at(155), 260, -330, last_update=at(156)))
        self.assertIsNotNone(later, "30 minutes after the last answer is "
                                    "inside max_gap")
        self.assertNotIn(Rejection.GAP.value, detector.result.counts())
        self.assertEqual(later.book_change_bracket_seconds, 2400.0)

    def test_the_same_stamp_is_not_a_regression(self):
        """Only an OLDER stamp is. A price that changed inside the provider's
        one-second stamp is new content at the same instant."""
        detector = MoveDetector(MovePolicy())
        detector.observe(env(at(200), 120, -140, last_update=at(201)))
        trigger = detector.observe(env(at(199), 160, -190,
                                       last_update=at(201)))
        self.assertIsNotNone(trigger)
        self.assertNotIn(Rejection.REGRESSED_CONTENT.value,
                         detector.result.counts())

    def test_a_stale_older_copy_says_stale_first(self):
        """The regression test runs AFTER the freshness checks, so a copy
        that is both older and too old to act on reports the latter -- and
        breaks comparability, as a stale record always has."""
        detector, _ = self.moved()
        detector.observe(env(at(194), 120, -140, last_update=at(260)))
        counts = detector.result.counts()
        self.assertEqual(counts[Rejection.STALE_AT_DECISION.value], 1)
        self.assertNotIn(Rejection.REGRESSED_CONTENT.value, counts)


class StreamIdentityTest(unittest.TestCase):
    """A stream is provider + book + event + market + orientation.

    Owner's finding: `market_id` was `"E1:h2h"`. Two bookmakers' quotes then
    overwrote each other in one piece of detector state, and the second
    book's different price read as the first book moving -- a trigger for an
    event that never happened at either book.
    """

    def test_the_book_is_part_of_the_market_id(self):
        pinnacle = env(at(200), 120, -140, book="pinnacle")
        circa = env(at(195), 160, -190, book="circa")
        self.assertNotEqual(pinnacle.provenance.market_id,
                            circa.provenance.market_id)
        self.assertIn("pinnacle", pinnacle.provenance.market_id)
        self.assertIn("circa", circa.provenance.market_id)
        self.assertNotEqual(pinnacle.provenance.market_id, "E1:h2h")

    def test_two_books_never_share_detector_state(self):
        result = MoveDetector(MovePolicy()).observe_all([
            env(at(200), 120, -140, book="pinnacle"),
            env(at(195), 160, -190, book="circa"),
        ])
        self.assertEqual(result.triggers, [],
                         "one book's price read as another book moving")
        self.assertEqual(result.counts(),
                         {Rejection.FIRST_OBSERVATION.value: 2},
                         "two books, two baselines")

    def test_a_declared_book_is_enforced_at_the_boundary(self):
        """Belt and braces: identity separates them, the policy refuses them.

        Separate streams alone would silently *measure* a second book as its
        own stream. When the study declares one sharp book, a foreign quote
        is named and dropped instead.
        """
        result = MoveDetector(MovePolicy(book="pinnacle")).observe_all([
            env(at(200), 120, -140, book="pinnacle"),
            env(at(195), 160, -190, book="draftkings"),
        ])
        self.assertEqual(result.triggers, [])
        counts = result.counts()
        self.assertEqual(counts[Rejection.WRONG_BOOK.value], 1)
        detail = " ".join(r.detail for r in result.rejections)
        self.assertIn("draftkings", detail)
        self.assertIn("pinnacle", detail)

    def test_the_declared_book_still_admits_its_own_quotes(self):
        result = MoveDetector(MovePolicy(book="Pinnacle")).observe_all([
            env(at(200), 120, -140, book="pinnacle"),
            env(at(195), 160, -190, book="pinnacle"),
        ])
        self.assertEqual(len(result.triggers), 1, "case must not matter")

    def test_a_swapped_orientation_is_not_the_same_stream(self):
        """Re-labelled sides are not a 30-point move."""
        keys = dict(source="the_odds_api_historical", book="pinnacle",
                    event_id=EVENT, market="h2h")
        normal = stream_id(**keys, orientation=orientation_key(AWAY, HOME))
        swapped = stream_id(**keys, orientation=orientation_key(HOME, AWAY))
        self.assertNotEqual(normal, swapped)

    def test_book_case_does_not_fork_a_stream(self):
        keys = dict(source="s", event_id="E1", market="h2h")
        self.assertEqual(stream_id(**keys, book="Pinnacle"),
                         stream_id(**keys, book="pinnacle"))

    def test_the_stream_id_rides_on_every_trigger(self):
        result = MoveDetector(MovePolicy()).observe_all([
            env(at(200), 120, -140), env(at(195), 160, -190)])
        trigger = result.triggers[0]
        self.assertIn("pinnacle", trigger.stream_id)
        self.assertEqual(trigger.event_id, EVENT)
        self.assertEqual(trigger.as_dict()["stream_id"], trigger.stream_id)


class PolicyValidationTest(unittest.TestCase):
    """A policy that quietly disables detection is worse than a loud one."""

    def test_an_unknown_devig_method_is_refused_at_construction(self):
        with self.assertRaises(ValueError):
            MovePolicy(devig_method="power")

    def test_a_nan_threshold_cannot_silently_disable_detection(self):
        """NaN compares False against everything, `< min_move` included."""
        with self.assertRaises(ValueError):
            MovePolicy(min_move=float("nan"))

    def test_a_non_positive_threshold_is_refused(self):
        for value in (0.0, -0.01):
            with self.subTest(min_move=value):
                with self.assertRaises(ValueError):
                    MovePolicy(min_move=value)

    def test_a_negative_bound_would_admit_everything(self):
        with self.assertRaises(ValueError):
            MovePolicy(max_age_at_decision=timedelta(seconds=-1))
        with self.assertRaises(ValueError):
            MovePolicy(max_gap=timedelta(seconds=-1))

    def test_a_bound_that_is_not_a_duration_is_refused(self):
        with self.assertRaises(ValueError):
            MovePolicy(debounce=900)

    def test_the_declared_method_reaches_the_recorded_policy(self):
        self.assertEqual(MovePolicy().as_dict()["devig_method"], "shin")
        self.assertEqual(
            MovePolicy(devig_method="multiplicative").as_dict()["devig_method"],
            "multiplicative")

    def test_the_default_clock_tolerance_is_the_shared_constant(self):
        """rule 26: the detector does not invent a second tolerance."""
        self.assertEqual(MovePolicy().clock_skew_tolerance,
                         CLOCK_SKEW_TOLERANCE)

    def test_a_marginal_trigger_is_flagged_against_devig_disagreement(self):
        """A margin smaller than the methods' own gap is not robust.

        Not a rejection -- the policy named `shin` and shin is what ran. But
        a reader comparing a move against a threshold should be told when the
        margin sits inside the disagreement between the two de-vig methods,
        because the other method would have decided differently.
        """
        pair = [env(at(200), 120, -140, last_update=at(201)),
                env(at(195), 160, -190, last_update=at(196))]
        comfortable = MoveDetector(MovePolicy()).observe_all(pair)
        trigger = comfortable.triggers[0]
        self.assertFalse(trigger.near_threshold_under_devig_disagreement)
        self.assertFalse(
            trigger.as_dict()["near_threshold_under_devig_disagreement"])
        self.assertGreater(trigger.devig_disagreement, 0.0)

        # the same move, against a threshold it only just clears
        margin = trigger.magnitude - trigger.devig_disagreement / 2
        marginal = MoveDetector(MovePolicy(min_move=margin)).observe_all(pair)
        self.assertEqual(len(marginal.triggers), 1)
        self.assertTrue(
            marginal.triggers[0].near_threshold_under_devig_disagreement)


class ReactionCliTest(unittest.TestCase):
    """Drive the real entry point.

    Round 5 of this study shipped two crashes in entry points nothing drove,
    while 171 tests passed. An audit that cannot be run is not an audit.
    """

    def _run(self, argv):
        import json as _json
        from unittest import mock
        import run_reaction
        with mock.patch("builtins.print") as printed:
            code = run_reaction.main(argv)
        text = "\n".join(str(call) for call in printed.call_args_list)
        return code, text

    def test_capability_runs_and_exits_zero(self):
        """A permanent source limitation is not a failing run (rule 27)."""
        code, text = self._run(["--capability"])
        self.assertEqual(code, 0)
        self.assertIn("REACTION RESOLUTION FLOOR", text)
        self.assertIn("UNANSWERABLE", text)

    def test_capability_names_the_five_minute_bound(self):
        _, text = self._run(["--capability"])
        self.assertIn("300s", text)
        self.assertIn("not second-resolution evidence", text)

    def test_capability_json_is_machine_readable(self):
        import json as _json
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(["--capability", "--json"])
        self.assertEqual(code, 0)
        payload = _json.loads("\n".join(chunks))
        self.assertFalse(payload["verified_in_session"])
        self.assertEqual(payload["reaction_resolution_floor_seconds"], 300.0)
        self.assertEqual(len(payload["sources"]), 2)
        unanswerable = [q for q in payload["questions"]
                        if q["answerability"] == "unanswerable"]
        self.assertGreaterEqual(len(unanswerable), 3)

    def test_verify_commands_run_and_spend_nothing(self):
        code, text = self._run(["--capability-verify"])
        self.assertEqual(code, 0)
        self.assertIn("docs.kalshi.com", text)
        self.assertNotIn("apiKey=", text)

    def test_no_arguments_is_a_usage_error_not_a_silent_success(self):
        code, _ = self._run([])
        self.assertEqual(code, 2)

    # --- the CLI's own claims, checked against what shipped ---------------
    #
    # A README can be correct while the command contradicts it, because
    # nothing compared them. That is exactly what happened: the README was
    # rewritten when the screen, ledger and replay shipped, and
    # `run_reaction.py` went on saying "only the capability audit", listing
    # those three as the unbuilt next increment, and describing the BOOK's
    # move instant as answerable -- a semantic the code had already
    # corrected. Three stale claims in the file a person reads first.
    #
    # So these assertions derive what is true from the modules and the
    # capability verdicts, rather than from a phrase typed next to them.

    #: the reaction layers that exist as importable modules
    SHIPPED_LAYERS = ("clocks", "detector", "measure", "screen", "episodes",
                      "replay")

    #: how the CLI's prose names each of them
    LAYER_PROSE = {
        "detector": "move detector",
        "measure": "reaction measurement",
        "screen": "opportunity screen",
        "episodes": "episode ledger",
        "replay": "replay",
    }

    @staticmethod
    def _unbuilt_section(text: str) -> str:
        """Everything the CLI presents as NOT built, however it is worded."""
        import re
        marker = re.search(r"not built[^.]{0,20}not faked", text, re.I)
        return text[marker.start():] if marker else ""

    def test_every_shipped_layer_really_is_importable(self):
        """The premise of the claim tests below, asserted rather than assumed."""
        import importlib
        for layer in self.SHIPPED_LAYERS:
            with self.subTest(layer=layer):
                self.assertTrue(importlib.import_module(f"reaction.{layer}"))

    def test_no_shipped_layer_is_presented_as_unbuilt(self):
        """A line saying LESS exists than does is read exactly as one saying
        more does. Both are claims, and this one was wrong for three layers."""
        _, text = self._run(["--capability"])
        unbuilt = self._unbuilt_section(text)
        self.assertTrue(unbuilt, "the CLI no longer says what is NOT built")
        for layer, prose in self.LAYER_PROSE.items():
            with self.subTest(layer=layer):
                self.assertNotIn(prose.lower(), unbuilt.lower(),
                                 f"{layer} shipped but is listed as unbuilt")

    def test_orders_are_still_declared_unbuilt(self):
        """The one thing that genuinely is not built, and must stay said.

        The footer read "NOT BUILT: collection" until the collector shipped,
        then "NOT BUILT: live capture" until the shadow monitor did. Each was
        true when written and false the day its module landed -- so both are
        asserted BUILT here from the modules themselves, and what is still
        unbuilt is asserted by what the code cannot do.
        """
        import collect_reaction
        import shadow_monitor
        self.assertTrue(callable(collect_reaction.main))
        self.assertTrue(callable(shadow_monitor.main))
        _, text = self._run(["--capability"])
        unbuilt = self._unbuilt_section(text).lower()
        built = text[:len(text) - len(unbuilt)].lower()
        self.assertIn("orders", unbuilt)
        self.assertNotIn("collection", unbuilt)
        self.assertNotIn("live capture", unbuilt)
        for entry_point in ("collect_reaction.py", "shadow_monitor.py"):
            self.assertIn(entry_point, built)
        self.assertIn("--spend", built)
        self.assertIn("none can be placed", unbuilt)
        self.assertIn("makes no paid request", unbuilt)

    def test_the_cli_does_not_claim_the_books_move_instant_is_answerable(self):
        """`last_update` is the PROVIDER's observation, not the book's change.

        The docstring listed "when the BOOK moved" as answerable long after
        `capability.question_verdicts()` had been corrected to UNANSWERABLE.
        Both halves are asserted here so they cannot drift apart again.
        """
        import run_reaction
        from reaction.capability import Answerability, question_verdicts
        book_change = [v for v in question_verdicts()
                       if "BOOK actually change" in v.question]
        self.assertEqual(len(book_change), 1)
        self.assertIs(book_change[0].answerability,
                      Answerability.UNANSWERABLE)

        doc = run_reaction.__doc__.lower()
        self.assertNotIn("when the book moved", doc)
        self.assertIn("not when the bookmaker changed", doc)

        _, text = self._run(["--capability"])
        self.assertIn("[ NO     ] when did the BOOK actually change", text)

    def test_the_docstring_restates_no_verdict_count(self):
        """A copied count drifts the same way a copied verdict does.

        It said "two of them turn out to be unsupportable" while the audit
        graded five. The number belongs in one place; the CLI prints it.
        """
        import re
        import run_reaction
        from reaction.capability import unanswerable_questions
        doc = run_reaction.__doc__.lower()
        for word in ("one", "two", "three", "four", "five", "six"):
            self.assertNotIn(f"{word} of them", doc)
        self.assertFalse(re.search(r"\b\d+ of (them|the study)", doc))
        _, text = self._run(["--capability"])
        self.assertIn(f"{len(unanswerable_questions())} of the study's "
                      f"questions are UNANSWERABLE", text)

    def test_the_usage_line_names_every_mode_the_parser_accepts(self):
        """Adding a mode and leaving it out of the usage line hides it.

        The line still read "pass --capability or --capability-verify" after
        --policy and --replay shipped, so the two most useful free commands
        were invisible to anyone who ran the tool with no arguments.
        """
        import run_reaction
        # Every option the parser accepts is either a MODE or a MODIFIER.
        # A new option that is neither fails here, which is the only way a
        # usage line stays complete without someone remembering.
        declared = set(run_reaction.MODES) | set(run_reaction.MODIFIERS)
        for action in run_reaction.build_parser()._actions:
            for option in action.option_strings:
                if option.startswith("--"):
                    with self.subTest(option=option):
                        self.assertIn(option, declared,
                                      f"{option} is neither a MODE nor a "
                                      f"MODIFIER; classify it")
        code, text = self._run([])
        self.assertEqual(code, 2)
        usage = [line for line in text.splitlines()
                 if "nothing to do" in line]
        self.assertEqual(len(usage), 1, "no single usage line")
        for mode in run_reaction.MODES:
            with self.subTest(mode=mode):
                self.assertIn(mode, usage[0])

    def test_policy_prints_every_declared_threshold_and_spends_nothing(self):
        """`--policy` is the audit trail for "declared, not fitted"."""
        code, text = self._run(["--policy"])
        self.assertEqual(code, 0)
        for threshold in ("min_move", "max_age_at_decision_seconds",
                          "devig_method", "min_response", "max_wait_seconds",
                          "lookback_seconds", "candle_period_seconds"):
            self.assertIn(threshold, text)
        self.assertIn("shin", text)
        self.assertIn("never serve as a holdout", text)
        self.assertNotIn("apiKey=", text)

    def test_policy_publishes_the_decision_rule_it_judges_by(self):
        """`--policy` exists so a declared number can be committed before any
        data. The stop rule was declared only in prose, the one place
        nothing checks, while `--policy` printed every threshold but it."""
        import json as _json
        from unittest import mock
        import run_reaction
        from reaction.episodes import FeasibilityRule
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(["--policy", "--json"])
        self.assertEqual(code, 0)
        rule = _json.loads("\n".join(chunks))["feasibility_rule"]
        self.assertEqual(rule, FeasibilityRule().as_dict())
        self.assertFalse(rule["tuned_on_outcomes"])
        _, text = self._run(["--policy"])
        self.assertIn("feasibility_rule", text)
        self.assertIn("min_determinate_reactions", text)

    def test_policy_json_carries_every_declaration_and_the_not_tuned_flag(self):
        import json as _json
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(["--policy", "--json"])
        self.assertEqual(code, 0)
        payload = _json.loads("\n".join(chunks))
        self.assertEqual(set(payload),
                         {"move_detection", "reaction_measurement",
                          "feasibility_rule"})
        for section in payload.values():
            self.assertFalse(section["tuned_on_outcomes"])
        self.assertEqual(payload["move_detection"]["devig_method"], "shin")
        self.assertEqual(
            payload["move_detection"]["min_move"],
            payload["reaction_measurement"]["min_response"],
            "a reaction threshold looser than the trigger threshold would "
            "count noise as a follow")


if __name__ == "__main__":
    unittest.main(verbosity=2)
