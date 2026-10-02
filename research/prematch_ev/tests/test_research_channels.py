"""The research channels' causal rules, on scripted quotes.

SYNTHETIC: every quote here is invented. The two shapes the channels exist
for are modelled on the owner's 2026-10-01 audit (PR #27, comment
5953266882) and use its reported prices, nothing else from it:

  * PIT-CLE: PIT -143 / CLE +129 drifting to PIT -150 / CLE +135, Shin CLE
    0.4241019282 -> 0.4127659574, 1.1336pp, in steps of under a point;
  * LAR-PHI: LAR -176 / PHI +151, then one poll whose event carried no
    sharp quote, then LAR -189 / PHI +161: PHI 1.5781pp lower.

Kickoffs, every timestamp, every other price, and the event ids are
invented. Nothing here is evidence about any book, exchange or strategy;
these are regression tests of the rules declared in `reaction.research`.
"""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.odds_history import SHARP_BOOK, SharpQuote               # noqa: E402
from reaction.detector import (                                    # noqa: E402
    MoveDetector, MovePolicy, Rejection, fair_probabilities,
)
from reaction.clocks import envelope_for_live_sharp_quote           # noqa: E402
from reaction.research import (                                    # noqa: E402
    BELOW_THRESHOLD, CENSORED_IN_PLAY, CENSORED_SESSION_END, DRIFT,
    GAP_TOO_LONG, RE_SERVED, RETURN, RETURN_NOT_NEWER, RETURN_STALE, SIGNAL,
    DriftPolicy, ResearchChannels, ReturnPolicy, repeat_freshness,
    research_coverage,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 10, 2, 0, 15, tzinfo=UTC)        # invented
CADENCE = timedelta(seconds=30)
LAG = timedelta(seconds=14)          # the provider's stamp, this much older
PIT, CLE = "Pittsburgh Steelers", "Cleveland Browns"

#: PIT-CLE's reported endpoints, and invented sub-point steps between them.
DRIFT_PATH = [(-143, 129), (-144, 130), (-145, 130), (-146, 131), (-147, 132),
              (-148, 133), (-149, 134), (-150, 135)]


def cle(away: float, home: float) -> float:
    return fair_probabilities(away, home)[1]


def quote(at: datetime, away: float, home: float, *, observed=None,
          event: str = "evt-pit-cle", kickoff: datetime = KICKOFF,
          names: tuple = (PIT, CLE)) -> SharpQuote:
    return SharpQuote(snapshot=at.replace(microsecond=0), commence_time=kickoff,
                      away_name=names[0], home_name=names[1],
                      away_price=away, home_price=home, book=SHARP_BOOK,
                      provider_event_id=event,
                      last_update=observed if observed is not None
                      else at - LAG)


class Feed:
    """The monitor's calls, one poll at a time, on a 30s grid."""

    def __init__(self, drift: DriftPolicy | None = None,
                 ret: ReturnPolicy | None = None,
                 move: MovePolicy | None = None):
        self.channels = ResearchChannels(
            move or MovePolicy(book=SHARP_BOOK),
            drift=drift or DriftPolicy(max_spacing=3 * CADENCE),
            ret=ret or ReturnPolicy())
        self.opened: list = []
        self.t = T0

    def answer(self, *quotes: SharpQuote) -> None:
        at = self.t
        self.opened += self.channels.on_answer(
            list(quotes), received_at=at,
            sent_at=at - timedelta(milliseconds=300),
            ready_at=at + timedelta(milliseconds=5))
        self.t = at + CADENCE

    def price(self, away: float, home: float, **kwargs) -> None:
        self.answer(quote(self.t, away, home, **kwargs))

    def hold(self, away: float, home: float, polls: int) -> None:
        for _ in range(polls):
            self.price(away, home)

    def absent(self) -> None:
        """An answered poll the game is missing from: HTTP 200, no quote."""
        self.answer()

    def failed(self) -> None:
        self.channels.on_failed_poll(self.t)
        self.t += CADENCE

    def signals(self, channel: str) -> list:
        return [s for s in self.opened if s.channel == channel]

    def returns(self) -> list:
        return self.channels.returns


class DriftTest(unittest.TestCase):
    """Channel 1: cumulative movement across the trailing window."""

    def test_the_synthetic_path_is_what_it_claims(self):
        fairs = [cle(*p) for p in DRIFT_PATH]
        steps = [abs(b - a) for a, b in zip(fairs, fairs[1:])]
        self.assertLess(max(steps), 0.01)
        self.assertAlmostEqual(fairs[0], 0.4241019282, places=9)
        self.assertAlmostEqual(fairs[-1], 0.4127659574, places=9)
        self.assertAlmostEqual(fairs[0] - fairs[-1], 0.011336, places=6)

    def test_accumulation_without_a_single_qualifying_jump(self):
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=12)          # 6 minutes on each price
        # THE ADJACENT DETECTOR SAW NOTHING: no step reached a point.
        self.assertEqual(feed.channels.adjacent, [])
        drift = feed.signals(DRIFT)
        self.assertEqual(len(drift), 1)
        signal = drift[0]
        self.assertEqual(signal.direction, -1)
        self.assertAlmostEqual(signal.delta_home, -0.011336, places=6)
        window = signal.detail["window"]
        self.assertLess(window["largest_single_step"], 0.01)
        self.assertFalse(window["contains_an_adjacent_size_step"])
        self.assertFalse(signal.adjacent_trigger_here)
        # The anchor is the LAST time the price stood at the high: the end of
        # the first six minutes, not the first poll.
        self.assertEqual(signal.reference.at,
                         T0 + 11 * CADENCE + timedelta(milliseconds=5))
        # Dated when it was READY, on the executable clock.
        self.assertEqual(signal.detected_at,
                         T0 + 84 * CADENCE + timedelta(milliseconds=5))
        self.assertEqual(feed.signals(RETURN), [])

    def test_an_anchor_older_than_the_window_has_expired(self):
        """The same 1.13pp, taken over 84 minutes: no 60-minute window holds
        a point both ends of it."""
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=24)          # 12 minutes each
        self.assertEqual(feed.signals(DRIFT), [])
        self.assertGreater(feed.channels.counts[DRIFT]["below_threshold"], 0)
        largest = max(s.max_excursion for s in feed.channels.streams.values())
        self.assertLess(largest, 0.01)

    def test_a_first_observation_has_no_anchor(self):
        feed = Feed()
        feed.price(-143, 129)
        self.assertEqual(
            feed.channels.counts[DRIFT]["no_anchor_in_window"], 1)
        self.assertEqual(feed.opened, [])

    def test_overlapping_windows_are_one_episode(self):
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=4)
        feed.hold(-150, 135, polls=60)          # held: qualifies on and on
        self.assertEqual(len(feed.signals(DRIFT)), 1)
        episode = feed.channels.episodes[DRIFT][0]
        self.assertGreater(episode.qualifying, 20)
        self.assertGreater(
            feed.channels.counts[DRIFT]["qualifying_extended"], 20)

    def test_a_further_drift_inside_the_merge_window_is_the_same_episode(self):
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=4)
        for pair in [(-152, 137), (-155, 140), (-160, 144)]:
            feed.hold(*pair, polls=8)
        self.assertEqual(len(feed.signals(DRIFT)), 1)
        self.assertGreater(feed.channels.episodes[DRIFT][0].peak, 0.02)

    def test_a_drift_after_the_merge_window_is_a_new_episode(self):
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=4)
        feed.hold(-150, 135, polls=250)         # two hours, flat
        for pair in [(-152, 137), (-155, 140), (-160, 144)]:
            feed.hold(*pair, polls=4)
        drift = feed.signals(DRIFT)
        self.assertEqual(len(drift), 2)
        self.assertEqual([s.episode_id for s in drift], ["drift-1", "drift-2"])
        self.assertIsNone(drift[1].reverses)

    def test_a_reversal_opens_its_own_episode_and_names_the_one_it_reverses(
            self):
        feed = Feed()
        for pair in DRIFT_PATH:
            feed.hold(*pair, polls=4)
        for pair in reversed(DRIFT_PATH):
            feed.hold(*pair, polls=4)
        drift = feed.signals(DRIFT)
        self.assertEqual([s.direction for s in drift], [-1, 1])
        self.assertEqual(drift[1].reverses, drift[0].episode_id)
        self.assertEqual(feed.channels.adjacent, [])

    def test_a_single_jump_is_both_and_says_so(self):
        """A one-poll jump of a point is the adjacent detector's; the drift
        channel sees it too, and the overlap is flagged on the signal."""
        feed = Feed()
        feed.hold(-143, 129, polls=4)
        feed.price(-155, 140)
        self.assertEqual(len(feed.channels.adjacent), 1)
        drift = feed.signals(DRIFT)
        self.assertEqual(len(drift), 1)
        self.assertTrue(drift[0].adjacent_trigger_here)
        self.assertTrue(drift[0].detail["window"]
                        ["contains_an_adjacent_size_step"])

    def test_a_hole_resets_the_window_it_is_never_bridged(self):
        """0.5pp, a missing quote, then 0.6pp more: 1.1pp in all, but no
        window holds both sides of the hole."""
        feed = Feed()
        feed.hold(-143, 129, polls=4)
        for pair in DRIFT_PATH[1:4]:
            feed.hold(*pair, polls=2)
        feed.absent()
        for pair in DRIFT_PATH[4:]:
            feed.hold(*pair, polls=2)
        self.assertGreater(cle(-143, 129) - cle(-150, 135), 0.01)
        self.assertEqual(feed.signals(DRIFT), [])
        self.assertEqual(feed.channels.resets["declared:absent_from_answer"],
                         1)
        # The return across the hole was judged -- and was small.
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [BELOW_THRESHOLD])

    def test_a_failed_poll_resets_every_stream(self):
        feed = Feed()
        other = (("Denver Broncos", "Kansas City Chiefs"), "evt-den-kc")
        for _ in range(3):
            feed.answer(quote(feed.t, -143, 129),
                        quote(feed.t, 150, -170, event=other[1],
                              names=other[0]))
        feed.failed()
        self.assertEqual(feed.channels.resets["declared:failed_poll"], 2)
        self.assertEqual(
            {s.gap.order[0] for s in feed.channels.streams.values()},
            {"declared:failed_poll"})

    def test_an_older_copy_resets_the_window_though_not_the_adjacent(self):
        """Stricter than the adjacent detector, as declared: at the poll
        that served an older copy, the current price was not observed."""
        feed = Feed()
        feed.hold(-143, 129, polls=4)
        for pair in DRIFT_PATH[1:4]:
            feed.hold(*pair, polls=2)
        feed.price(-143, 129, observed=T0)                  # an older copy
        for pair in DRIFT_PATH[4:]:
            feed.hold(*pair, polls=2)
        self.assertEqual(feed.signals(DRIFT), [])
        self.assertEqual(feed.channels.resets["regressed_content"], 1)
        self.assertEqual(
            feed.channels.counts["observations"]["regressed_content"], 1)

    def test_more_than_the_spacing_between_sightings_is_a_hole(self):
        feed = Feed()
        feed.hold(-143, 129, polls=4)
        feed.t += 4 * CADENCE                   # undeclared: the backstop
        for pair in DRIFT_PATH[1:]:
            feed.hold(*pair, polls=2)
        self.assertEqual(feed.signals(DRIFT), [])
        self.assertEqual(feed.channels.resets["spacing_exceeded"], 1)


class ReturnTest(unittest.TestCase):
    """Channel 2: a price changed on return after a quote gap."""

    def lar_phi(self, feed: Feed, *, returning=(-189, 161), **kwargs) -> None:
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        feed.absent()
        feed.answer(quote(feed.t, *returning, event="evt-lar-phi",
                          names=names, **kwargs))

    def test_a_changed_quote_on_return_is_a_signal(self):
        feed = Feed()
        self.lar_phi(feed)
        self.assertEqual(feed.channels.adjacent, [])
        self.assertEqual(feed.signals(DRIFT), [])
        [signal] = feed.signals(RETURN)
        self.assertAlmostEqual(signal.delta_home, -0.015781, places=6)
        gap = signal.detail["gap"]
        self.assertEqual(gap["first_cause"], "declared:absent_from_answer")
        self.assertAlmostEqual(gap["seconds"], 60.0, places=3)
        record = signal.as_dict()
        # Everything the owner asked to keep, by name.
        self.assertAlmostEqual(record["reference"]["fair"]["home"],
                               0.3803626075, places=9)
        self.assertEqual(record["reference"]["ready_at"],
                         gap["pre_gap_last_seen"])
        self.assertAlmostEqual(record["current"]["fair"]["home"],
                               0.3645812618, places=9)
        self.assertIsNotNone(record["current"]["provider_observed_at"])
        self.assertIsNotNone(record["current"]["ready_at"])
        bracket = record["change_bracket"]
        self.assertIn("include the unobserved gap", bracket["note"])
        self.assertEqual(bracket["our_sightings"]["from"],
                         gap["pre_gap_last_seen"])
        self.assertNotIn("book_moved_at", str(record))

    def test_the_adjacent_detector_still_re_anchors(self):
        feed = Feed()
        self.lar_phi(feed)
        verdicts = feed.channels.counts["observations"]
        self.assertEqual(verdicts[Rejection.BASELINE_INVALIDATED.value], 1)
        self.assertEqual(verdicts["trigger"], 0)

    def test_a_small_change_is_below_threshold(self):
        feed = Feed()
        self.lar_phi(feed, returning=(-178, 152))
        self.assertEqual(feed.signals(RETURN), [])
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [BELOW_THRESHOLD])

    def test_a_stale_return_is_refused_and_closes_the_gap(self):
        """Valid by the detector's 900s rule, older than the channel's 120s:
        the provider was lagging -- by 250s before the gap, 200s after it --
        so the return is newer than the pre-gap quote and still stale."""
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names,
                              observed=feed.t - timedelta(seconds=250)))
        feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names,
                          observed=feed.t - timedelta(seconds=200)))
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi",
                          names=names))
        self.assertEqual(feed.signals(RETURN), [])
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [RETURN_STALE])
        self.assertEqual(feed.returns()[0]["failed"], [RETURN_STALE])
        # Closed: the fresh quote after it is continuous, not a return.
        self.assertIsNone(next(iter(feed.channels.streams.values())).gap)

    def test_a_return_too_old_to_be_valid_extends_the_gap(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names,
                          observed=feed.t - timedelta(seconds=1000)))
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        [signal] = feed.signals(RETURN)
        gap = signal.detail["gap"]
        self.assertEqual([r["reason"] for r in gap["refused_returns"]],
                         ["stale_at_decision"])
        self.assertAlmostEqual(gap["seconds"], 90.0, places=3)

    def test_an_older_copy_is_never_the_return(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names,
                          observed=T0))                     # older copy
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        [signal] = feed.signals(RETURN)
        self.assertEqual(
            [r["reason"] for r in signal.detail["gap"]["refused_returns"]],
            ["regressed_content"])

    def test_a_return_the_provider_did_not_observe_afresh_is_refused(self):
        """New prices under the provider's OLD observation stamp: not news
        the provider vouches for, so not a return."""
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        stamp = T0 - timedelta(seconds=5)
        for _ in range(2):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names, observed=stamp))
        feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names,
                          observed=stamp))
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [RETURN_NOT_NEWER])
        self.assertEqual(feed.signals(RETURN), [])

    def test_a_re_served_copy_does_not_end_the_gap(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        stamp = T0 + timedelta(seconds=80)
        feed.t = T0 + timedelta(seconds=90)
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names, observed=stamp))
        feed.absent()
        feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                          names=names, observed=stamp))     # re-served
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        [signal] = feed.signals(RETURN)
        self.assertEqual(signal.detail["gap"]["re_served_unchanged"], 1)
        self.assertAlmostEqual(signal.detail["gap"]["seconds"], 90.0,
                               places=3)

    def test_a_long_gap_is_too_long_whatever_the_change(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        for _ in range(10):
            feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        self.assertEqual(feed.signals(RETURN), [])
        [judged] = feed.returns()
        self.assertEqual(judged["verdict"], GAP_TOO_LONG)
        self.assertGreater(judged["gap"]["seconds"], 300)
        self.assertEqual(judged["gap"]["causes"],
                         [{"cause": "declared:absent_from_answer",
                           "count": 10}])

    def test_an_outage_is_a_named_cause_and_bounded_by_the_gap_limit(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        for _ in range(3):
            feed.failed()
        feed.channels.on_outage(feed.t)
        feed.t += timedelta(seconds=60)                      # the probe
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        [signal] = feed.signals(RETURN)
        causes = [c["cause"] for c in signal.detail["gap"]["causes"]]
        self.assertEqual(causes, ["declared:failed_poll", "declared:outage"])
        # A longer outage is too long for this channel.
        feed = Feed()
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        for _ in range(3):
            feed.failed()
        feed.channels.on_outage(feed.t)
        feed.t += timedelta(minutes=8)
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [GAP_TOO_LONG])

    def test_a_stretch_not_polled_is_a_gap(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        feed.t += timedelta(seconds=150)
        feed.channels.on_unpolled(feed.t)
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        [signal] = feed.signals(RETURN)
        self.assertEqual(signal.detail["gap"]["first_cause"],
                         "declared:not_polled")

    def test_in_play_censors_an_open_gap(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        kickoff = T0 + 3 * CADENCE
        for _ in range(2):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names, kickoff=kickoff))
        feed.absent()
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names,
                          kickoff=kickoff))                  # in play now
        feed.answer(quote(feed.t, -260, 210, event="evt-lar-phi", names=names,
                          kickoff=kickoff))
        self.assertEqual(feed.opened, [])
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [CENSORED_IN_PLAY])

    def test_the_end_censors_an_open_gap(self):
        feed = Feed()
        self.lar_phi(feed)
        feed.absent()
        feed.channels.finish(feed.t)
        self.assertEqual([r["verdict"] for r in feed.returns()],
                         [SIGNAL, CENSORED_SESSION_END])

    def test_returns_debounce_into_episodes(self):
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        path = [(-176, 151), (-189, 161), (-176, 151), (-189, 161)]
        for pair in path:
            feed.answer(quote(feed.t, *pair, event="evt-lar-phi", names=names))
            feed.absent()
        signals = feed.signals(RETURN)
        # Down, up (reversing it), down again: inside the merge window the
        # second down joins the first episode rather than opening a third.
        self.assertEqual([s.direction for s in signals], [-1, 1])
        self.assertEqual(signals[1].reverses, signals[0].episode_id)
        self.assertEqual(len(feed.channels.episodes[RETURN]), 2)
        self.assertEqual(feed.channels.counts[RETURN]["qualifying_extended"],
                         1)

    def test_one_older_copy_can_put_a_jump_on_both_and_it_is_flagged(self):
        """The adjacent detector keeps its baseline across an older copy;
        this channel calls that copy a gap. A point's jump across it is
        then both a trigger and a return, flagged as such."""
        feed = Feed()
        names = ("Los Angeles Rams", "Philadelphia Eagles")
        for _ in range(4):
            feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi",
                              names=names))
        feed.answer(quote(feed.t, -176, 151, event="evt-lar-phi", names=names,
                          observed=T0))
        feed.answer(quote(feed.t, -189, 161, event="evt-lar-phi", names=names))
        self.assertEqual(len(feed.channels.adjacent), 1)
        [signal] = feed.signals(RETURN)
        self.assertTrue(signal.adjacent_trigger_here)


READY = timedelta(milliseconds=5)          # Feed: ready this long after receipt
STALE_REPEAT = f"{RE_SERVED}:{Rejection.STALE_AT_DECISION.value}"


class StaleRepeatTest(unittest.TestCase):
    """A re-served copy is a sighting only while it is fresh (review
    5959548662). The adjacent detector recognises a repeat before it ages
    anything -- to it a re-served copy means the feed did not stop -- so it
    never calls one stale. These channels must: a provider re-serving one
    observation for twenty minutes would otherwise carry a segment, and a
    drift, across minutes in which nobody had seen the price."""

    @staticmethod
    def stream(feed: Feed):
        [state] = feed.channels.streams.values()
        return state

    @staticmethod
    def frozen(feed: Feed, copies: int, stamp: datetime = T0) -> None:
        """One observation, then `copies` re-served copies of it, one a
        poll: copy k is 30k seconds (and the Feed's 5ms) old when ready."""
        for _ in range(copies + 1):
            feed.price(-143, 129, observed=stamp)

    def test_the_reviewed_reproduction(self):
        """The owner's probe, verbatim, and what it now finds."""
        f = Feed()
        f.price(-143, 129, observed=T0)
        for _ in range(40):
            f.price(-143, 129, observed=T0)
        f.price(-150, 135)
        assert not f.signals("drift")
        self.assertEqual(dict(f.channels.counts["observations"]),
                         {"first_observation": 1, "unchanged_content": 40,
                          "trigger": 1})
        # THE FIRST COPY PAST 900s INTERRUPTED THE STREAM, once.
        self.assertEqual(dict(f.channels.resets), {STALE_REPEAT: 1})
        # The changed quote is judged once, as a return across the stale
        # stretch, measured from the last copy that was still fresh.
        [judged] = f.returns()
        self.assertEqual(judged["verdict"], GAP_TOO_LONG)
        self.assertEqual(judged["failed"], [GAP_TOO_LONG])
        self.assertEqual(judged["gap"]["causes"],
                         [{"cause": STALE_REPEAT, "count": 1}])
        self.assertEqual(judged["pre_gap_last_seen"],
                         (T0 + 29 * CADENCE + READY).isoformat())
        self.assertEqual(judged["gap"]["opened_at"],
                         (T0 + 30 * CADENCE + READY).isoformat())
        self.assertAlmostEqual(judged["gap"]["seconds"], 360.0, places=6)
        self.assertEqual(judged["gap"]["re_served_unchanged"], 10)
        last = judged["pre_gap_last_sighting"]
        self.assertTrue(last["re_served"])
        self.assertAlmostEqual(last["age_at_decision_seconds"], 870.005,
                               places=6)
        self.assertEqual(last["provider_observed_at"], T0.isoformat())
        self.assertEqual(judged["pre_gap"]["ready_at"],
                         (T0 + READY).isoformat())
        # THE ADJACENT DETECTOR IS UNCHANGED: it never aged the copies, kept
        # its baseline, and fires on the same quote. The record says so.
        self.assertEqual(len(f.channels.adjacent), 1)
        self.assertTrue(judged["adjacent_trigger_on_this_quote"])

    def test_copies_inside_the_bound_are_sightings_with_their_own_clocks(self):
        """A fresh copy keeps the segment alive and can anchor a drift --
        and an anchor that is a copy says so, with ITS receipt and ITS age
        beside the provider stamp it shares with the first sighting."""
        f = Feed()
        self.frozen(f, 10)                          # the last is 300.005s old
        for pair in DRIFT_PATH[1:]:
            f.price(*pair)
        self.assertEqual(dict(f.channels.resets), {})
        self.assertEqual(f.channels.adjacent, [])
        [signal] = f.signals(DRIFT)
        anchor = signal.reference.as_dict()
        self.assertTrue(anchor["re_served"])
        self.assertEqual(anchor["ready_at"],
                         (T0 + 10 * CADENCE + READY).isoformat())
        self.assertEqual(anchor["received_at"],
                         (T0 + 10 * CADENCE).isoformat())
        self.assertEqual(anchor["provider_observed_at"], T0.isoformat())
        self.assertAlmostEqual(anchor["age_at_decision_seconds"], 300.005,
                               places=6)
        self.assertEqual(signal.detail["window"]["anchor_age_seconds"],
                         7 * CADENCE.total_seconds())
        # A new value is still recorded as one.
        self.assertFalse(signal.current.as_dict()["re_served"])

    def test_a_copy_exactly_at_the_bound_is_still_a_sighting(self):
        """The detector's bound is inclusive -- stale means `age > 900` --
        and so is this one."""
        f = Feed()
        stamp = T0 + READY                          # copy k: 30k seconds old
        self.frozen(f, 30, stamp)
        state = self.stream(f)
        self.assertEqual(dict(f.channels.resets), {})
        self.assertIsNone(state.gap)
        self.assertEqual(state.seen.age_at_decision, 900.0)
        f.price(-143, 129, observed=stamp)          # 930s
        self.assertEqual(dict(f.channels.resets), {STALE_REPEAT: 1})
        self.assertEqual(state.gap.pre_seen.age_at_decision, 900.0)
        self.assertEqual(state.gap.pre_last_seen, T0 + 30 * CADENCE + READY)

    def test_stale_copies_after_the_bound_neither_restart_nor_close_the_gap(
            self):
        f = Feed()
        self.frozen(f, 60)                          # half an hour of one stamp
        state = self.stream(f)
        self.assertEqual(dict(f.channels.resets), {STALE_REPEAT: 1})
        self.assertEqual(state.segment, [])
        self.assertIsNotNone(state.gap)
        self.assertEqual(state.gap.re_served, 30)
        self.assertEqual(f.channels.counts[RETURN]["re_served_inside_gap"], 30)
        # NO STALE COPY IS A SIGHTING: the last valid one is still the last
        # copy that was fresh, so the gap is not shortened by them.
        self.assertEqual(state.last_seen, T0 + 29 * CADENCE + READY)
        self.assertEqual(state.gap.pre_last_seen, T0 + 29 * CADENCE + READY)
        self.assertEqual(f.channels.evaluations, 30)
        self.assertEqual(f.returns(), [])
        f.channels.finish(f.t)
        [censored] = f.returns()
        self.assertEqual(censored["verdict"], CENSORED_SESSION_END)
        self.assertEqual(censored["gap"]["re_served_unchanged"], 30)

    def test_a_fresh_changed_return_after_a_short_stale_stretch_is_a_signal(
            self):
        f = Feed()
        self.frozen(f, 30)                          # the 30th: 900.005s, stale
        f.price(-150, 135)                          # observed 14s before receipt
        self.assertEqual(f.signals(DRIFT), [])      # nothing bridges the copy
        [signal] = f.signals(RETURN)
        self.assertAlmostEqual(signal.delta_home,
                               cle(-150, 135) - cle(-143, 129), places=12)
        gap = signal.detail["gap"]
        self.assertEqual(gap["first_cause"], STALE_REPEAT)
        self.assertAlmostEqual(gap["seconds"], 60.0, places=6)
        # The reference is the last copy that was still fresh, with its own
        # clocks; the value's first sighting is recorded beside it.
        reference = signal.reference
        self.assertFalse(reference.new)
        self.assertEqual(reference.at, T0 + 29 * CADENCE + READY)
        self.assertEqual(reference.received_at, T0 + 29 * CADENCE)
        self.assertAlmostEqual(reference.age_at_decision, 870.005, places=6)
        self.assertEqual(reference.observed_at, T0)
        self.assertEqual(gap["pre_gap_value_first_seen"],
                         (T0 + READY).isoformat())
        self.assertTrue(signal.adjacent_trigger_here)
        # DRIFT RE-ANCHORS ON THE RETURN, and only there.
        self.assertEqual([p.at for p in self.stream(f).segment],
                         [signal.current.at])

    def test_after_a_stale_stretch_the_return_takes_every_declared_check(self):
        """The first new observation is judged as any return is, and the
        gap closes on it whatever the verdict."""
        for copies, quote_age, pair, verdict in (
                (40, 14, (-150, 135), GAP_TOO_LONG),        # 360s > 300s
                (30, 200, (-150, 135), RETURN_STALE),       # 200s > 120s
                (30, 14, (-144, 130), BELOW_THRESHOLD)):    # under a point
            with self.subTest(verdict=verdict):
                f = Feed()
                self.frozen(f, copies)
                f.price(*pair, observed=f.t - timedelta(seconds=quote_age))
                [judged] = f.returns()
                self.assertEqual(judged["verdict"], verdict)
                self.assertEqual(judged["gap"]["first_cause"], STALE_REPEAT)
                self.assertEqual(f.signals(RETURN), [])
                self.assertEqual(f.signals(DRIFT), [])
                self.assertIsNone(self.stream(f).gap)
                self.assertEqual(len(self.stream(f).segment), 1)

    def test_the_bound_is_the_sessions_own(self):
        f = Feed(move=MovePolicy(book=SHARP_BOOK,
                                 max_age_at_decision=timedelta(seconds=300)))
        self.frozen(f, 10)                          # the 10th: 300.005s
        self.assertEqual(dict(f.channels.resets), {STALE_REPEAT: 1})
        self.assertAlmostEqual(self.stream(f).gap.pre_seen.age_at_decision,
                               270.005, places=6)


class RepeatFreshnessTest(unittest.TestCase):
    """`repeat_freshness` gives a re-served copy the answer the detector
    gives NEW content of the same age: one rule, applied in two places, held
    to one answer (rule 19)."""

    def test_the_detectors_answer_for_new_content_of_the_same_age(self):
        at = T0 + timedelta(hours=1)
        for require in (True, False):
            policy = MovePolicy(book=SHARP_BOOK, require_content_age=require)
            for age in (0.0, 14.0, 899.999, 900.0, 900.001, 5000.0, -1.0,
                        None):
                with self.subTest(require_content_age=require, age=age):
                    stamp = (None if age is None else
                             at + READY - timedelta(seconds=age))
                    q = dataclasses.replace(quote(at, -143, 129),
                                            last_update=stamp)
                    envelope = envelope_for_live_sharp_quote(
                        q, received_at=at,
                        sent_at=at - timedelta(milliseconds=300),
                        ready_at=at + READY)
                    self.assertEqual(envelope.age_at_decision_seconds(), age)
                    detector = MoveDetector(policy)
                    self.assertIsNone(detector.observe(envelope))
                    [rejection] = detector.result.rejections
                    verdict = rejection.reason.value
                    expected = (None if verdict
                                == Rejection.FIRST_OBSERVATION.value
                                else verdict)
                    self.assertEqual(
                        repeat_freshness(policy,
                                         envelope.age_at_decision_seconds()),
                        expected)


class SameInputsTest(unittest.TestCase):

    def test_the_private_detector_is_the_adjacent_detector(self):
        """Fed the same quotes and holes, the channels' private detector
        triggers exactly where a standalone one does: they share the rule
        and never alter it."""
        from reaction.clocks import envelope_for_live_sharp_quote
        feed = Feed()
        standalone = MoveDetector(MovePolicy(book=SHARP_BOOK))
        script = ([(-143, 129)] * 3 + [(-155, 140)] * 3 + [None]
                  + [(-140, 126)] * 2 + [(-160, 144)] * 2)
        triggers = []
        for step in script:
            at = feed.t
            if step is None:
                feed.absent()
                stream = next(iter(feed.channels.seen))
                standalone.note_gap(stream, at, "absent")
                continue
            q = quote(at, *step)
            feed.answer(q)
            envelope = envelope_for_live_sharp_quote(
                q, received_at=at, sent_at=at - timedelta(milliseconds=300),
                ready_at=at + timedelta(milliseconds=5))
            trigger = standalone.observe(envelope)
            if trigger is not None:
                triggers.append((trigger.stream_id, trigger.detected_at,
                                 trigger.delta_home))
        self.assertEqual(len(triggers), 2)
        self.assertEqual(feed.channels.adjacent, triggers)


class PolicyTest(unittest.TestCase):

    def test_policies_refuse_values_that_would_silence_them(self):
        for bad in (dict(min_move=float("nan")), dict(min_move=0),
                    dict(window=timedelta(0)),
                    dict(episode_merge=timedelta(seconds=-1)),
                    dict(max_spacing=timedelta(0))):
            with self.assertRaises(ValueError, msg=bad):
                DriftPolicy(**bad)
        for bad in (dict(min_move=-0.01), dict(max_gap=timedelta(0)),
                    dict(max_return_age=timedelta(0))):
            with self.assertRaises(ValueError, msg=bad):
                ReturnPolicy(**bad)

    def test_policies_say_what_they_are(self):
        for policy in (DriftPolicy(), ReturnPolicy()):
            row = policy.as_dict()
            self.assertTrue(row["research_only"])
            self.assertFalse(row["tuned_on_outcomes"])
            self.assertFalse(row["validated"])
            self.assertIn("development-informed", row["declared"])
        self.assertEqual(DriftPolicy().as_dict()["window_seconds"], 3600)
        self.assertEqual(ReturnPolicy().as_dict()["max_gap_seconds"], 300)

    def test_the_horizon_rule(self):
        horizon = timedelta(hours=73)
        self.assertEqual(research_coverage(T0 + timedelta(hours=10), T0,
                                           horizon), "in_horizon")
        self.assertEqual(research_coverage(T0 + timedelta(hours=73.74), T0,
                                           horizon),
                         "outside_observation_horizon")
        self.assertEqual(research_coverage(T0, T0, horizon),
                         "at_or_after_start")
        self.assertEqual(research_coverage(None, T0, horizon),
                         "no_kickoff_time")


class ReadOutTest(unittest.TestCase):

    def test_a_run_with_no_signal_still_reports_its_distribution(self):
        feed = Feed()
        for pair in DRIFT_PATH[:5]:
            feed.hold(*pair, polls=4)
        summary = feed.channels.summary()
        self.assertEqual(summary[DRIFT]["episodes"], 0)
        reached = summary[DRIFT]["games_with_max_excursion_at_least"]
        self.assertEqual(reached["0.1pp"], 1)
        self.assertEqual(reached["0.5pp"], 1)
        self.assertEqual(reached["1pp"], 0)
        self.assertTrue(math.isclose(
            summary[DRIFT]["largest_excursions"][0]["excursion"],
            cle(-143, 129) - cle(-147, 132)))


if __name__ == "__main__":
    unittest.main()
