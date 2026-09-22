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
revealed the move, never at the book's own `last_update`. Measuring from
`last_update` would credit the strategy with information it did not have, and
it is the single easiest way to turn this study into a lookahead engine that
reports a beautiful result.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.odds_history import SharpQuote                        # noqa: E402
from reaction import capability as cap                          # noqa: E402
from reaction.clocks import (                                   # noqa: E402
    FutureDataError, Origin, Provenance, ReceiptTimeUnknown, SourceEnvelope,
    assert_no_future_data, detection_blindness_seconds, earliest_availability,
    envelope_for_candle, envelope_for_sharp_quote,
    first_usable_at_or_after, latest_usable, payload_hash, usable_at,
)
from reaction.detector import (                                 # noqa: E402
    MoveDetector, MovePolicy, Rejection, devig_multiplicative, implied,
)

UTC = timezone.utc
START = datetime(2026, 9, 14, 0, 20, tzinfo=UTC)      # a real NFL kickoff
EVENT = "odds-evt-401872930"


def at(minutes: float) -> datetime:
    """An instant, `minutes` before kickoff. Synthetic, for timing control."""
    return START - timedelta(minutes=minutes)


def quote(snapshot: datetime, away: float, home: float,
          last_update: datetime | None = None) -> SharpQuote:
    """A real `SharpQuote` with SYNTHETIC prices.

    The type and field semantics are the adapter's; the numbers are chosen to
    make a specific probability move.
    """
    return SharpQuote(
        snapshot=snapshot, commence_time=START,
        away_name="Dallas Cowboys", home_name="New York Giants",
        away_price=away, home_price=home, book="pinnacle",
        provider_event_id=EVENT,
        last_update=last_update if last_update is not None
        else snapshot - timedelta(seconds=30),
    )


def env(snapshot: datetime, away: float, home: float,
        last_update: datetime | None = None) -> SourceEnvelope:
    return envelope_for_sharp_quote(quote(snapshot, away, home, last_update))


class DevigTest(unittest.TestCase):
    """De-vig within ONE quote, and refuse rather than invent."""

    def test_a_balanced_pair_is_fifty_fifty(self):
        fair = devig_multiplicative(100, -100)
        self.assertIsNotNone(fair)
        self.assertAlmostEqual(fair[0], 0.5, places=6)
        self.assertAlmostEqual(fair[1], 0.5, places=6)

    def test_probabilities_sum_to_one_after_devig(self):
        for away, home in ((-140, 120), (250, -300), (-110, -110)):
            with self.subTest(prices=(away, home)):
                fair = devig_multiplicative(away, home)
                self.assertAlmostEqual(sum(fair), 1.0, places=9)

    def test_the_favourite_keeps_the_larger_probability(self):
        away_fair, home_fair = devig_multiplicative(200, -250)
        self.assertGreater(home_fair, away_fair)

    def test_unusable_prices_refuse(self):
        for away, home in ((0, -110), (None, -110), (-110, 0), ("x", -110)):
            with self.subTest(prices=(away, home)):
                self.assertIsNone(devig_multiplicative(away, home))

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

    def test_the_book_move_instant_is_answerable_but_detection_is_not(self):
        """The distinction the whole module exists for, asserted."""
        self.assertIs(cap.verdict_for("when did the BOOK move").answerability,
                      cap.Answerability.ANSWERABLE)
        self.assertIs(cap.verdict_for("OUR SYSTEM").answerability,
                      cap.Answerability.INTERVAL_CENSORED)

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

    def test_blindness_is_unknown_without_a_source_update_time(self):
        raw = SharpQuote(
            snapshot=at(120), commence_time=START, away_name="a", home_name="b",
            away_price=120, home_price=-140, book="pinnacle",
            provider_event_id=EVENT, last_update=None)
        self.assertIsNone(detection_blindness_seconds(
            envelope_for_sharp_quote(raw)))

    def test_candles_carry_no_source_update_time(self):
        """A candle cannot say when inside its period the quote moved."""
        @dataclass
        class Candle:
            ts: datetime
            bid_close: float
            ask_close: float

        envelope = envelope_for_candle(
            Candle(at(100), 0.44, 0.47), "KXNFLGAME-26SEP13DALNYG-DAL")
        self.assertIsNone(envelope.source_update_time)
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
        self.assertLess(envelope.source_update_time, at(118))
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

    def test_the_trigger_is_dated_at_the_snapshot_not_the_book_update(self):
        result = self.feed(env(at(200), 120, -140, last_update=at(201)),
                           env(at(195), 160, -190, last_update=at(199)))
        trigger = result.triggers[0]
        self.assertEqual(trigger.detected_at, at(195))
        self.assertEqual(trigger.book_moved_at, at(199))
        self.assertEqual(trigger.detection_blindness_seconds, 240.0)

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
        self.assertIn(Rejection.STALE_CONTENT.value, result.counts())

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
        """The book widened its margin without changing its view."""
        result = self.feed(env(at(200), 100, -100),      # overround ~1.0
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
        self.assertEqual(policy["label"], "exploratory-v1")
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

    def test_the_cli_declares_what_is_not_built(self):
        """An increment has to say what it does not cover."""
        _, text = self._run(["--capability"])
        self.assertIn("not built, not faked", text)
        self.assertIn("No orders", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
