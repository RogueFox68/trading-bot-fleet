"""Stateful, causal detection of a sharp-book probability move.

WHAT A TRIGGER IS
-----------------
A signed change in the book's DE-VIGGED fair probability, between two valid
two-sided quotes, large enough to clear a declared threshold, where the
strategy could actually have known both quotes.

Every clause in that sentence is load-bearing, and each one corresponds to a
way this detector could report a move that never happened:

* **de-vigged, not raw.** Raw American prices move when the book widens its
  margin without changing its view. De-vigging inside ONE contemporaneous
  quote isolates the view. Mixing an away price from one update with a home
  price from another manufactures a move out of two honest quotes -- which is
  why `_fair` only ever sees one quote at a time.

* **two-sided.** One side alone cannot be de-vigged. A missing side is a
  named outcome (`MISSING_SIDE`), not a zero and not a carried-forward value.

* **between two VALID quotes.** The first observation of a market establishes
  a baseline and is never itself a move: there is nothing to have moved from.
  This is the single most tempting bug here, because a first observation
  arrives with a perfectly good probability and a threshold test will happily
  compare it against zero.

* **the strategy could have known both.** The trigger instant is the
  EXECUTABLE clock from `clocks.available_at`, never the book's own
  `last_update`. A move the book made at 11:20 that we first saw at 11:25 is
  detected at 11:25.

* **something actually changed.** The archive re-serves an unchanged price
  every five minutes and a live feed re-sends on reconnect. Both look like
  fresh arrivals. `is_new_content_versus` decides on CONTENT, so a reconnect
  cannot masquerade as a move -- the failure the owner named explicitly.

WHAT IS NOT A TRIGGER, BY NAME
------------------------------
`Rejection` enumerates them so a run can report why it saw fewer moves than
updates, instead of a bare count. Silence about why a candidate was dropped is
how a detector ends up measuring its own filter.

CONTINUITY AND GAPS
-------------------
A gap in the input is not continuity. If the elapsed time between two
observations exceeds `max_gap`, the pair is `GAP` -- the book may have moved
several times inside it, and the difference between the endpoints is not "a
move", it is an unknown number of moves aggregated. The baseline resets, which
deliberately costs a trigger rather than inventing one.

THRESHOLDS ARE DECLARED, NOT FITTED
-----------------------------------
`MovePolicy` carries its own values into the output. The default is marked
EXPLORATORY and was chosen before looking at any outcome; tuning it on the
16 development games would make every figure downstream a selection artifact,
which is why the owner asked for a holdout and why the policy is recorded on
every episode rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Sequence

from .clocks import (
    CLOCK_SKEW_TOLERANCE, Origin, SourceEnvelope, clock_order_problem,
)

# --- de-vigging: ONE implementation, shared with the rest of the study -----
#
# This module used to carry its OWN multiplicative de-vig. It agreed with
# `core.devig.devig_multiplicative` to the last digit -- and the study's frozen
# baseline is SHIN, which differs:
#
#     120/-140   mult 0.5620438   shin 0.5643939   (0.0024)
#     160/-190   mult 0.6301020   shin 0.6352785   (0.0052)
#
# Against a 0.01 move threshold that is up to HALF the threshold, so a
# near-threshold trigger could exist under one method and not the other. Two
# implementations of one fact is rule 19; two DIFFERENT methods silently
# chosen is worse, because detection and the downstream EV screen would then
# disagree about what "fair" means. So there is one call, into `core.devig`,
# and the method is named in the policy and recorded on every output.

from core.devig import DevigError, devig_american


def american_to_decimal(price: float) -> float | None:
    """American odds -> decimal. None for values that are not odds."""
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if value == 0 or value != value or value in (float("inf"), float("-inf")):
        return None
    if value > 0:
        return 1.0 + value / 100.0
    return 1.0 + 100.0 / abs(value)


def implied(price: float) -> float | None:
    decimal = american_to_decimal(price)
    if decimal is None or decimal <= 0:
        return None
    return 1.0 / decimal


def fair_probabilities(away_price: float, home_price: float,
                       method: str = "shin"
                       ) -> tuple[float, float, float, float] | None:
    """(fair_away, fair_home, overround, method_disagreement) from ONE quote.

    Delegates to `core.devig`, which computes BOTH methods and reports their
    gap. The gap is returned so a trigger sitting near the threshold under
    method disagreement is visible rather than implicit.

    Returns None when either price is unusable -- a fabricated probability
    here propagates into every lag figure downstream.
    """
    if away_price is None or home_price is None:
        return None
    for price in (away_price, home_price):
        if american_to_decimal(price) is None:
            return None
    try:
        result = devig_american([away_price, home_price])
    except (DevigError, ValueError, ZeroDivisionError):
        return None
    probabilities = (result.shin if method == "shin"
                     else result.multiplicative)
    if len(probabilities) != 2:
        return None
    away_implied = implied(away_price)
    home_implied = implied(home_price)
    if away_implied is None or home_implied is None:
        return None
    return (probabilities[0], probabilities[1],
            away_implied + home_implied, result.disagreement())


# --- outcomes ---------------------------------------------------------------

class Rejection(str, Enum):
    """Why a candidate pair produced no trigger. Every one is reported."""

    FIRST_OBSERVATION = "first_observation"        # baseline, nothing to move from
    UNCHANGED_CONTENT = "unchanged_content"        # re-served/reconnect, same value
    MISSING_SIDE = "missing_side"                  # one-sided quote
    UNDEVIGGABLE = "undeviggable"                  # prices will not de-vig
    UNKNOWN_CONTENT_AGE = "unknown_content_age"    # no last_update: not "fresh"
    GAP = "gap_in_input"                           # elapsed > max_gap; baseline reset
    OUT_OF_ORDER = "out_of_order"                  # earlier than the baseline
    BELOW_THRESHOLD = "below_threshold"            # real change, too small
    NO_AVAILABILITY = "no_availability_time"       # cannot date the decision
    VIG_ONLY = "vig_only_change"                   # margin moved, fair did not
    CLOCK_ORDER_INVALID = "clock_order_invalid"    # impossible clock ordering
    STALE_AT_DECISION = "stale_at_decision"        # old by the time we could act
    #   there is deliberately no separate "stale_content": the capture-age
    #   version of this rejection is what let a LIVE record 1200s old at
    #   receipt through a 900s bound. One name, on the decision clock.
    WRONG_BOOK = "wrong_book"                      # not the declared book
    BASELINE_INVALIDATED = "baseline_invalidated"  # an unusable interval intervened
    DECLARED_GAP = "declared_gap"                  # caller reported an absence


@dataclass(frozen=True)
class MovePolicy:
    """Declared detection rules. Recorded on every episode.

    EXPLORATORY DEFAULTS, fixed before inspecting any outcome:

      devig_method          "shin" -- the study's frozen baseline. Named here
                            because shin and multiplicative differ by up to
                            0.005 on real prices, half the move threshold, so
                            a silent choice could create or destroy a trigger.
      min_move              0.01 of probability, the unit the EV screen uses.
      max_gap               35 minutes. Wider than the 5-minute archive grid
                            by enough to tolerate a missed snapshot, narrow
                            enough that a multi-hour hole resets.
      max_age_at_decision   900s, applied at the DECISION clock -- when we
                            could act, not when the provider captured. A live
                            record captured at 12:05 and received at 12:25 is
                            1200s old when actionable, and a capture-age bound
                            calls it fresh.
      debounce              120s. A book walked in three steps is one episode.
      require_content_age   True. An absent `last_update` is UNKNOWN, and
                            unknown is not fresh.
      book                  None = accept any. Set it to enforce the declared
                            sharp book at the detector boundary, so a second
                            book's quote cannot be mistaken for the first one
                            moving.
      clock_skew_tolerance  2s between a provider's own clocks. Beyond it the
                            ordering is impossible and the record is refused.
    """

    devig_method: str = "shin"
    min_move: float = 0.01
    max_gap: timedelta = timedelta(minutes=35)
    max_age_at_decision: timedelta = timedelta(seconds=900)
    debounce: timedelta = timedelta(seconds=120)
    require_content_age: bool = True
    book: str | None = None
    clock_skew_tolerance: timedelta = CLOCK_SKEW_TOLERANCE
    label: str = "exploratory-v2"

    def __post_init__(self) -> None:
        # Validate at the boundary. A NaN threshold compares False against
        # everything and silently disables detection; a negative bound admits
        # everything. Both are quiet, so neither may be reachable.
        if self.devig_method not in ("shin", "multiplicative"):
            raise ValueError(f"unknown devig_method {self.devig_method!r}")
        if not (self.min_move > 0) or self.min_move != self.min_move:
            raise ValueError(f"min_move must be positive, got {self.min_move!r}")
        for name in ("max_gap", "max_age_at_decision", "debounce",
                     "clock_skew_tolerance"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value < timedelta(0):
                raise ValueError(f"{name} must be a non-negative timedelta, "
                                 f"got {value!r}")

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "devig_method": self.devig_method,
            "min_move": self.min_move,
            "max_gap_seconds": self.max_gap.total_seconds(),
            "max_age_at_decision_seconds":
                self.max_age_at_decision.total_seconds(),
            "debounce_seconds": self.debounce.total_seconds(),
            "require_content_age": self.require_content_age,
            "book": self.book,
            "clock_skew_tolerance_seconds":
                self.clock_skew_tolerance.total_seconds(),
            "tuned_on_outcomes": False,
            "note": ("declared before inspecting outcomes; the 16 development "
                     "games must not be used to fit these"),
        }


@dataclass(frozen=True)
class BookState:
    """The last accepted two-sided quote for one stream."""

    envelope: SourceEnvelope
    fair_away: float
    fair_home: float
    available_at: datetime
    overround: float
    devig_disagreement: float

    def fair_for(self, participant_is_home: bool) -> float:
        return self.fair_home if participant_is_home else self.fair_away


@dataclass(frozen=True)
class MoveTrigger:
    """A detected change in the provider-observed fair probability.

    NOTE THE FIELD NAMES. There is no `book_moved_at`, because this data does
    not contain one: `last_update` is when the PROVIDER saw the odds. The
    book's own change instant is reported as a BRACKET between consecutive
    provider observations, and as nothing else.
    """

    event_id: str
    stream_id: str
    detected_at: datetime                      # EXECUTABLE clock
    provider_observed_at: datetime | None       # when the PROVIDER saw it
    book_change_earliest: datetime | None       # bracket lower bound
    book_change_latest: datetime | None         # bracket upper bound
    provider_to_available_seconds: float | None
    age_at_decision_seconds: float | None
    capture_age_seconds: float | None           # diagnostic only
    fair_before_away: float
    fair_before_home: float
    fair_after_away: float
    fair_after_home: float
    delta_away: float
    delta_home: float
    overround_before: float
    overround_after: float
    devig_disagreement: float
    policy: MovePolicy
    before_provenance: dict
    after_provenance: dict

    @property
    def magnitude(self) -> float:
        return abs(self.delta_home)

    @property
    def book_change_bracket_seconds(self) -> float | None:
        """Width of the interval the true change is known to lie in."""
        if self.book_change_earliest is None or self.book_change_latest is None:
            return None
        return (self.book_change_latest
                - self.book_change_earliest).total_seconds()

    @property
    def near_threshold_under_devig_disagreement(self) -> bool:
        """Would the other de-vig method plausibly have decided differently?

        True when the move clears the threshold by less than the gap between
        the two de-vig methods. Not a rejection -- a flag, so a marginal
        trigger is never read as robust.
        """
        return (self.magnitude - self.policy.min_move
                < self.devig_disagreement)

    def delta_for(self, participant_is_home: bool) -> float:
        """Signed move ORIENTED to one participant.

        A move is +4 points for one side and -4 for the other; a study that
        reports only the home number calls half its triggers the wrong way.
        """
        return self.delta_home if participant_is_home else self.delta_away

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "detected_at": self.detected_at.isoformat(),
            "provider_observed_at": (self.provider_observed_at.isoformat()
                                     if self.provider_observed_at else None),
            "book_change_bracket": {
                "earliest": (self.book_change_earliest.isoformat()
                             if self.book_change_earliest else None),
                "latest": (self.book_change_latest.isoformat()
                           if self.book_change_latest else None),
                "width_seconds": self.book_change_bracket_seconds,
                "note": ("the book's own change instant is NOT in this data; "
                         "it lies somewhere in this interval"),
            },
            "provider_to_available_seconds": self.provider_to_available_seconds,
            "age_at_decision_seconds": self.age_at_decision_seconds,
            "capture_age_seconds": self.capture_age_seconds,
            "fair_before": {"away": self.fair_before_away,
                            "home": self.fair_before_home},
            "fair_after": {"away": self.fair_after_away,
                           "home": self.fair_after_home},
            "delta": {"away": self.delta_away, "home": self.delta_home},
            "overround": {"before": self.overround_before,
                          "after": self.overround_after},
            "devig_disagreement": self.devig_disagreement,
            "near_threshold_under_devig_disagreement":
                self.near_threshold_under_devig_disagreement,
            "policy": self.policy.as_dict(),
            "before": self.before_provenance,
            "after": self.after_provenance,
        }


@dataclass(frozen=True)
class Rejected:
    stream_id: str
    reason: Rejection
    at: datetime | None
    detail: str = ""


@dataclass
class DetectorResult:
    triggers: list[MoveTrigger] = field(default_factory=list)
    rejections: list[Rejected] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rejected in self.rejections:
            out[rejected.reason.value] = out.get(rejected.reason.value, 0) + 1
        return dict(sorted(out.items()))

    @property
    def rejection_total(self) -> int:
        return len(self.rejections)

    def as_dict(self) -> dict:
        return {
            "triggers": [t.as_dict() for t in self.triggers],
            "trigger_count": len(self.triggers),
            "rejections": self.counts(),
            "rejection_total": self.rejection_total,
        }


# --- the detector -----------------------------------------------------------

class MoveDetector:
    """Feed it envelopes in availability order; it yields triggers.

    FOUR PIECES OF STATE PER STREAM, and the reason each is separate:

      _baseline       the last quote a move may be measured FROM
      _baseline_ok    False once an unusable interval intervened, so the next
                      valid quote re-establishes a baseline instead of being
                      attributed as a move across the hole
      _last_seen_at   the last VALID observation, changed or not. Continuity.
      _last_content   the last record of any kind. Deduplication.

    `_last_seen_at` and `_baseline` were one field, and that was wrong in both
    directions. An unchanged price polled every five minutes advanced neither,
    so a change forty minutes later read as `gap_in_input` although the feed
    had never stopped; and an unusable observation between two good ones left
    the baseline standing, so a move was attributed across an interval the
    detector could not see into. Continuity and comparability are different
    facts about a stream and need different clocks.
    """

    def __init__(self, policy: MovePolicy | None = None) -> None:
        self.policy = policy or MovePolicy()
        self._baseline: dict[str, BookState] = {}
        self._baseline_ok: dict[str, bool] = {}
        self._last_seen_at: dict[str, datetime] = {}
        self._last_content: dict[str, SourceEnvelope] = {}
        self._last_trigger_at: dict[str, datetime] = {}
        self.result = DetectorResult()

    # --- helpers ---
    def _reject(self, stream: str, reason: Rejection,
                at: datetime | None, detail: str = "") -> None:
        self.result.rejections.append(Rejected(stream, reason, at, detail))

    def _invalidate(self, stream: str, why: str, at: datetime | None) -> None:
        """Break comparability without destroying continuity.

        The next valid quote becomes a BASELINE, not a move. The baseline
        object is kept so its provenance is still reportable, but it may no
        longer anchor a delta.
        """
        self._baseline_ok[stream] = False

    def _book_of(self, envelope: SourceEnvelope) -> str:
        return str(getattr(envelope.payload, "book", "") or "").lower()

    # --- explicit absence, which observe() cannot discover ---
    def note_gap(self, stream_id: str, at: datetime,
                 reason: str = "market absent from the provider response"
                 ) -> None:
        """Declare that a stream had NO record for an interval.

        `observe()` sees only what arrived. A market omitted entirely from a
        provider response -- suspended, delisted, or simply not carried --
        produces no envelope at all, so nothing calls into the detector and
        the absence is invisible to it. The collector knows, and has to say
        so: this marks the baseline unusable and advances continuity to `at`,
        so the next valid quote anchors a fresh baseline rather than closing a
        delta across the hole.
        """
        self._reject(stream_id, Rejection.DECLARED_GAP, at, reason)
        self._invalidate(stream_id, reason, at)
        previous = self._last_seen_at.get(stream_id)
        if previous is None or at > previous:
            self._last_seen_at[stream_id] = at

    # --- the loop ---
    def observe(self, envelope: SourceEnvelope,
                snapshot_times: Sequence[datetime] | None = None
                ) -> MoveTrigger | None:
        """Take one book envelope. Returns a trigger, or None with a reason."""
        stream = envelope.provenance.market_id
        available, _ = envelope.available_at(snapshot_times)
        if available is None:
            self._reject(stream, Rejection.NO_AVAILABILITY, None,
                         "the decision instant cannot be established")
            return None

        # DECLARED BOOK, before anything touches state. A quote from another
        # bookmaker is not this stream at all; letting it through would read
        # the second book's different price as the first book moving.
        if self.policy.book is not None:
            book = self._book_of(envelope)
            if book and book != self.policy.book.lower():
                self._reject(stream, Rejection.WRONG_BOOK, available,
                             f"quote is from {book!r}, not the declared "
                             f"{self.policy.book!r}")
                return None

        # CLOCK ORDERING, before any freshness bound. An observation stamped
        # after its own capture yields a NEGATIVE age, which sails through
        # every upper-bound freshness test. A malformed record must not become
        # a baseline, and must not leave the previous one comparable either.
        problem = clock_order_problem(envelope, self.policy.clock_skew_tolerance)
        if problem:
            self._reject(stream, Rejection.CLOCK_ORDER_INVALID, available,
                         problem)
            self._invalidate(stream, problem, available)
            return None

        # CHRONOLOGY, before deduplication, so a late duplicate cannot slip
        # past the ordering check by being recognised as unchanged.
        previous_seen = self._last_seen_at.get(stream)
        if previous_seen is not None and available < previous_seen:
            self._reject(stream, Rejection.OUT_OF_ORDER, available,
                         f"available {available.isoformat()} precedes the last "
                         f"observation at {previous_seen.isoformat()}")
            return None

        # UNCHANGED CONTENT. A re-served price and a reconnect both arrive
        # looking fresh. This ADVANCES CONTINUITY -- the feed did not stop --
        # while leaving the baseline where it is, because nothing moved.
        last_content = self._last_content.get(stream)
        if not envelope.is_new_content_versus(last_content):
            self._reject(stream, Rejection.UNCHANGED_CONTENT, available,
                         "same provider observation stamp and payload hash as "
                         "the previous record: re-served or reconnect, not a "
                         "move. Continuity advances; the baseline does not")
            self._last_content[stream] = envelope
            self._last_seen_at[stream] = available
            return None
        self._last_content[stream] = envelope

        fair = fair_probabilities(
            getattr(envelope.payload, "away_price", None),
            getattr(envelope.payload, "home_price", None),
            self.policy.devig_method)
        if fair is None:
            quote = envelope.payload
            missing = (getattr(quote, "away_price", None) is None
                       or getattr(quote, "home_price", None) is None)
            reason = (Rejection.MISSING_SIDE if missing
                      else Rejection.UNDEVIGGABLE)
            detail = ("a one-sided quote cannot be de-vigged" if missing else
                      "prices present but will not de-vig")
            self._reject(stream, reason, available, detail)
            # AN UNUSABLE OBSERVATION BREAKS COMPARABILITY. Leaving the
            # baseline standing let a move be attributed across an interval
            # the detector could not see into -- exactly the reopening
            # uncertainty this study is trying to avoid.
            self._invalidate(stream, detail, available)
            self._last_seen_at[stream] = available
            return None
        fair_away, fair_home, overround, disagreement = fair

        capture_age = envelope.content_age_seconds()
        decision_age = envelope.age_at_decision_seconds(snapshot_times)
        if decision_age is None:
            if self.policy.require_content_age:
                self._reject(stream, Rejection.UNKNOWN_CONTENT_AGE, available,
                             "no provider observation time: the price's age is "
                             "unknown, which is not fresh")
                self._invalidate(stream, "unknown age", available)
                self._last_seen_at[stream] = available
                return None
        elif decision_age < 0:
            # Defence in depth: clock_order_problem should already have caught
            # this, but a negative age must never reach a threshold test.
            self._reject(stream, Rejection.CLOCK_ORDER_INVALID, available,
                         f"age at decision is {decision_age:.0f}s -- negative, "
                         "so the observation post-dates the decision")
            self._invalidate(stream, "negative age", available)
            self._last_seen_at[stream] = available
            return None
        elif decision_age > self.policy.max_age_at_decision.total_seconds():
            self._reject(
                stream, Rejection.STALE_AT_DECISION, available,
                f"the price was {decision_age:.0f}s old when we could act on "
                f"it (> {self.policy.max_age_at_decision.total_seconds():.0f}s)"
                + (f"; capture age was only {capture_age:.0f}s, which is why a "
                   "capture-age bound would have called this fresh"
                   if capture_age is not None
                   and capture_age <= self.policy.max_age_at_decision
                   .total_seconds() else ""))
            self._invalidate(stream, "stale at decision", available)
            self._last_seen_at[stream] = available
            return None

        state = BookState(envelope, fair_away, fair_home, available, overround,
                          disagreement)
        baseline = self._baseline.get(stream)
        baseline_ok = self._baseline_ok.get(stream, False)
        self._last_seen_at[stream] = available

        if baseline is None:
            self._reject(stream, Rejection.FIRST_OBSERVATION, available,
                         "baseline established; a first observation has "
                         "nothing to have moved from")
            self._baseline[stream] = state
            self._baseline_ok[stream] = True
            return None

        if not baseline_ok:
            self._reject(stream, Rejection.BASELINE_INVALIDATED, available,
                         "an unusable or absent interval intervened, so this "
                         "quote re-establishes a baseline rather than closing "
                         "a move across the hole")
            self._baseline[stream] = state
            self._baseline_ok[stream] = True
            return None

        # GAP against CONTINUITY, not against the last change. An unchanged
        # price polled steadily is not a gap.
        elapsed = available - (previous_seen or baseline.available_at)
        if elapsed > self.policy.max_gap:
            self._reject(stream, Rejection.GAP, available,
                         f"{elapsed.total_seconds():.0f}s since the last "
                         f"observation (> "
                         f"{self.policy.max_gap.total_seconds():.0f}s); "
                         "baseline reset, no move claimed across the hole")
            self._baseline[stream] = state
            self._baseline_ok[stream] = True
            return None

        delta_home = fair_home - baseline.fair_home
        delta_away = fair_away - baseline.fair_away

        if abs(delta_home) < self.policy.min_move:
            if abs(overround - baseline.overround) >= self.policy.min_move:
                self._reject(stream, Rejection.VIG_ONLY, available,
                             f"overround moved "
                             f"{overround - baseline.overround:+.4f} while fair "
                             f"probability moved {delta_home:+.4f}")
            else:
                self._reject(stream, Rejection.BELOW_THRESHOLD, available,
                             f"fair move {delta_home:+.4f} < "
                             f"{self.policy.min_move}")
            self._baseline[stream] = state
            self._baseline_ok[stream] = True
            return None

        last_trigger = self._last_trigger_at.get(stream)
        if last_trigger is not None and \
                available - last_trigger < self.policy.debounce:
            self._reject(stream, Rejection.BELOW_THRESHOLD, available,
                         f"debounced: "
                         f"{(available - last_trigger).total_seconds():.0f}s "
                         f"since the previous trigger on this stream "
                         f"(< {self.policy.debounce.total_seconds():.0f}s)")
            self._baseline[stream] = state
            self._baseline_ok[stream] = True
            return None

        observed = envelope.provider_observed_at
        provider_to_available = (
            (available - observed).total_seconds() if observed else None)

        trigger = MoveTrigger(
            event_id=envelope.provenance.event_id,
            stream_id=stream,
            detected_at=available,
            provider_observed_at=observed,
            # THE BRACKET. The book changed its price somewhere after the
            # provider last observed the OLD value and at or before it
            # observed the NEW one. No point estimate exists in this data.
            book_change_earliest=baseline.envelope.provider_observed_at,
            book_change_latest=observed,
            provider_to_available_seconds=provider_to_available,
            age_at_decision_seconds=decision_age,
            capture_age_seconds=capture_age,
            fair_before_away=baseline.fair_away,
            fair_before_home=baseline.fair_home,
            fair_after_away=fair_away,
            fair_after_home=fair_home,
            delta_away=delta_away,
            delta_home=delta_home,
            overround_before=baseline.overround,
            overround_after=overround,
            devig_disagreement=disagreement,
            policy=self.policy,
            before_provenance=baseline.envelope.as_dict(snapshot_times),
            after_provenance=envelope.as_dict(snapshot_times),
        )
        self._baseline[stream] = state
        self._baseline_ok[stream] = True
        self._last_trigger_at[stream] = available
        self.result.triggers.append(trigger)
        return trigger

    def observe_all(self, envelopes: Iterable[SourceEnvelope],
                    snapshot_times: Sequence[datetime] | None = None
                    ) -> DetectorResult:
        for envelope in envelopes:
            self.observe(envelope, snapshot_times)
        return self.result


def detect_moves(envelopes: Iterable[SourceEnvelope],
                 policy: MovePolicy | None = None,
                 snapshot_times: Sequence[datetime] | None = None
                 ) -> DetectorResult:
    """Convenience: run one detector over one stream's envelopes."""
    return MoveDetector(policy).observe_all(envelopes, snapshot_times)
