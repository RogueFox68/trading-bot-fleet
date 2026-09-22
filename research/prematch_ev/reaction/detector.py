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

from .clocks import Origin, SourceEnvelope

# --- de-vigging (one quote at a time) --------------------------------------


def american_to_decimal(price: float) -> float | None:
    """American odds -> decimal. None for the one value that has no meaning."""
    if price is None:
        return None
    try:
        value = float(price)
    except (TypeError, ValueError):
        return None
    if value == 0 or value != value:          # 0 and NaN are not odds
        return None
    if value > 0:
        return 1.0 + value / 100.0
    return 1.0 + 100.0 / abs(value)


def implied(price: float) -> float | None:
    decimal = american_to_decimal(price)
    if decimal is None or decimal <= 0:
        return None
    return 1.0 / decimal


def devig_multiplicative(away_price: float, home_price: float
                         ) -> tuple[float, float] | None:
    """Fair (away, home) probabilities from ONE two-sided quote.

    Multiplicative (proportional) de-vig: normalise the two implied
    probabilities so they sum to one. Deliberately the same family the
    checkpoint study already uses, so a trigger's fair probability and the
    existing EV screen cannot disagree about what "fair" means (rule 19).

    Returns None when either side is unusable or the overround is not
    positive, because a fabricated probability here propagates into every lag
    figure downstream.
    """
    away, home = implied(away_price), implied(home_price)
    if away is None or home is None:
        return None
    total = away + home
    if total <= 0:
        return None
    return away / total, home / total


# --- outcomes ---------------------------------------------------------------

class Rejection(str, Enum):
    """Why a candidate pair produced no trigger. Every one is reported."""

    FIRST_OBSERVATION = "first_observation"        # baseline, nothing to move from
    UNCHANGED_CONTENT = "unchanged_content"        # re-served/reconnect, same value
    MISSING_SIDE = "missing_side"                  # one-sided quote
    UNDEVIGGABLE = "undeviggable"                  # prices will not de-vig
    STALE_CONTENT = "stale_content"                # fresh envelope, old book price
    UNKNOWN_CONTENT_AGE = "unknown_content_age"    # no last_update: not "fresh"
    GAP = "gap_in_input"                           # elapsed > max_gap; baseline reset
    OUT_OF_ORDER = "out_of_order"                  # earlier than the baseline
    BELOW_THRESHOLD = "below_threshold"            # real change, too small
    NO_AVAILABILITY = "no_availability_time"       # cannot date the decision
    VIG_ONLY = "vig_only_change"                   # margin moved, fair did not


@dataclass(frozen=True)
class MovePolicy:
    """Declared detection rules. Recorded on every episode.

    EXPLORATORY DEFAULTS, fixed before inspecting any outcome:

      min_move             0.01 of probability. One cent of fair value, the
                           same unit the EV screen already works in.
      max_gap              35 minutes. Wider than the 5-minute archive grid by
                           enough to tolerate a missed snapshot or two, and
                           narrow enough that a multi-hour hole resets rather
                           than being reported as one move.
      max_content_age      900s. A book price older than 15 minutes at capture
                           is not evidence of a move happening now.
      debounce             120s. Two triggers on one market inside this are one
                           episode; a book walking a line in three steps is one
                           event, not three independent bets.
      require_content_age  True. An absent `last_update` is UNKNOWN, and this
                           study will not treat unknown as fresh. Set False
                           only to measure how much data that rule costs.
    """

    min_move: float = 0.01
    max_gap: timedelta = timedelta(minutes=35)
    max_content_age: timedelta = timedelta(seconds=900)
    debounce: timedelta = timedelta(seconds=120)
    require_content_age: bool = True
    label: str = "exploratory-v1"

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "min_move": self.min_move,
            "max_gap_seconds": self.max_gap.total_seconds(),
            "max_content_age_seconds": self.max_content_age.total_seconds(),
            "debounce_seconds": self.debounce.total_seconds(),
            "require_content_age": self.require_content_age,
            "tuned_on_outcomes": False,
            "note": ("declared before inspecting outcomes; the 16 development "
                     "games must not be used to fit these"),
        }


@dataclass(frozen=True)
class BookState:
    """The last accepted two-sided quote for one market."""

    envelope: SourceEnvelope
    fair_away: float
    fair_home: float
    available_at: datetime
    overround: float

    def fair_for(self, participant_is_home: bool) -> float:
        return self.fair_home if participant_is_home else self.fair_away


@dataclass(frozen=True)
class MoveTrigger:
    """A detected, causally-dated move in the book's fair probability."""

    event_id: str
    market_id: str
    detected_at: datetime            # EXECUTABLE clock: when we could have known
    book_moved_at: datetime | None    # the book's own stamp; unobservable to us then
    detection_blindness_seconds: float | None
    fair_before_away: float
    fair_before_home: float
    fair_after_away: float
    fair_after_home: float
    delta_away: float
    delta_home: float
    overround_before: float
    overround_after: float
    policy: MovePolicy
    before_provenance: dict
    after_provenance: dict

    @property
    def magnitude(self) -> float:
        return abs(self.delta_home)

    def delta_for(self, participant_is_home: bool) -> float:
        """Signed move ORIENTED to one participant.

        A move is +4 points for one side and -4 for the other, and a study
        that reports only the home number will call half its triggers the
        wrong direction. Orientation is always explicit at the call site.
        """
        return self.delta_home if participant_is_home else self.delta_away

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "detected_at": self.detected_at.isoformat(),
            "book_moved_at": (self.book_moved_at.isoformat()
                              if self.book_moved_at else None),
            "detection_blindness_seconds": self.detection_blindness_seconds,
            "fair_before": {"away": self.fair_before_away,
                            "home": self.fair_before_home},
            "fair_after": {"away": self.fair_after_away,
                           "home": self.fair_after_home},
            "delta": {"away": self.delta_away, "home": self.delta_home},
            "overround": {"before": self.overround_before,
                          "after": self.overround_after},
            "policy": self.policy.as_dict(),
            "before": self.before_provenance,
            "after": self.after_provenance,
        }


@dataclass(frozen=True)
class Rejected:
    market_id: str
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

    def as_dict(self) -> dict:
        return {
            "triggers": [t.as_dict() for t in self.triggers],
            "trigger_count": len(self.triggers),
            "rejections": self.counts(),
            "rejection_total": len(self.rejections),
        }


# --- the detector -----------------------------------------------------------

class MoveDetector:
    """Feed it envelopes in availability order; it yields triggers.

    Stateful per market, deliberately: continuity, debounce and baseline reset
    are all properties of a SEQUENCE, and a stateless "compare these two rows"
    function cannot express any of them. Out-of-order input is rejected by
    name rather than sorted silently, because a detector that tolerates
    disorder cannot tell a late arrival from a reversal.
    """

    def __init__(self, policy: MovePolicy | None = None) -> None:
        self.policy = policy or MovePolicy()
        self._state: dict[str, BookState] = {}
        self._last_trigger_at: dict[str, datetime] = {}
        self._last_envelope: dict[str, SourceEnvelope] = {}
        self.result = DetectorResult()

    # --- helpers ---
    def _reject(self, market_id: str, reason: Rejection,
                at: datetime | None, detail: str = "") -> None:
        self.result.rejections.append(Rejected(market_id, reason, at, detail))

    def _fair(self, envelope: SourceEnvelope
              ) -> tuple[float, float, float] | None:
        """(fair_away, fair_home, overround) from ONE quote, or None."""
        quote = envelope.payload
        away_price = getattr(quote, "away_price", None)
        home_price = getattr(quote, "home_price", None)
        if away_price is None or home_price is None:
            return None
        pair = devig_multiplicative(away_price, home_price)
        if pair is None:
            return None
        away_implied = implied(away_price)
        home_implied = implied(home_price)
        if away_implied is None or home_implied is None:
            return None
        return pair[0], pair[1], away_implied + home_implied

    # --- the loop ---
    def observe(self, envelope: SourceEnvelope,
                snapshot_times: Sequence[datetime] | None = None
                ) -> MoveTrigger | None:
        """Take one book envelope. Returns a trigger, or None with a reason."""
        market_id = envelope.provenance.market_id
        available, _ = envelope.available_at(snapshot_times)
        if available is None:
            self._reject(market_id, Rejection.NO_AVAILABILITY, None,
                         "the decision instant cannot be established")
            return None

        previous_envelope = self._last_envelope.get(market_id)

        # UNCHANGED CONTENT FIRST. A re-served price and a reconnect both
        # arrive looking fresh; deciding on content before anything else is
        # what stops either becoming a move. This ordering is the guard, not
        # the threshold test further down.
        if not envelope.is_new_content_versus(previous_envelope):
            self._reject(market_id, Rejection.UNCHANGED_CONTENT, available,
                         "same source_update_time and payload hash as the "
                         "previous record: re-served or reconnect, not a move")
            self._last_envelope[market_id] = envelope
            return None

        # Out of order BEFORE state is touched: a late arrival must not be
        # allowed to rewrite a baseline that already advanced past it.
        previous = self._state.get(market_id)
        if previous is not None and available < previous.available_at:
            self._reject(market_id, Rejection.OUT_OF_ORDER, available,
                         f"available {available.isoformat()} precedes the "
                         f"baseline at {previous.available_at.isoformat()}")
            return None

        fair = self._fair(envelope)
        if fair is None:
            quote = envelope.payload
            missing = (getattr(quote, "away_price", None) is None
                       or getattr(quote, "home_price", None) is None)
            self._reject(
                market_id,
                Rejection.MISSING_SIDE if missing else Rejection.UNDEVIGGABLE,
                available,
                "a one-sided quote cannot be de-vigged" if missing else
                "prices present but will not de-vig")
            self._last_envelope[market_id] = envelope
            return None
        fair_away, fair_home, overround = fair

        # Freshness of the CONTENT, not of the envelope.
        age = envelope.content_age_seconds()
        if age is None:
            if self.policy.require_content_age:
                self._reject(market_id, Rejection.UNKNOWN_CONTENT_AGE, available,
                             "no source update time: the price's age is "
                             "unknown, which is not fresh")
                self._last_envelope[market_id] = envelope
                return None
        elif age > self.policy.max_content_age.total_seconds():
            self._reject(market_id, Rejection.STALE_CONTENT, available,
                         f"book price was {age:.0f}s old at capture "
                         f"(> {self.policy.max_content_age.total_seconds():.0f}s)")
            self._last_envelope[market_id] = envelope
            return None

        state = BookState(envelope, fair_away, fair_home, available, overround)

        if previous is None:
            self._reject(market_id, Rejection.FIRST_OBSERVATION, available,
                         "baseline established; a first observation has "
                         "nothing to have moved from")
            self._state[market_id] = state
            self._last_envelope[market_id] = envelope
            return None

        elapsed = available - previous.available_at
        if elapsed > self.policy.max_gap:
            # A hole is not continuity. Reset rather than call the difference
            # between the endpoints "a move" -- inside it the book may have
            # moved any number of times, in any direction.
            self._reject(market_id, Rejection.GAP, available,
                         f"{elapsed.total_seconds():.0f}s since the baseline "
                         f"(> {self.policy.max_gap.total_seconds():.0f}s); "
                         "baseline reset, no move claimed across the hole")
            self._state[market_id] = state
            self._last_envelope[market_id] = envelope
            return None

        delta_home = fair_home - previous.fair_home
        delta_away = fair_away - previous.fair_away
        self._last_envelope[market_id] = envelope

        if abs(delta_home) < self.policy.min_move:
            # Distinguish "the fair view held while the margin moved" from
            # "nothing happened at all": the first is a real, reportable
            # observation about the book and must not be filed as noise.
            if abs(overround - previous.overround) >= self.policy.min_move:
                self._reject(market_id, Rejection.VIG_ONLY, available,
                             f"overround moved "
                             f"{overround - previous.overround:+.4f} while fair "
                             f"probability moved {delta_home:+.4f}")
            else:
                self._reject(market_id, Rejection.BELOW_THRESHOLD, available,
                             f"fair move {delta_home:+.4f} < "
                             f"{self.policy.min_move}")
            self._state[market_id] = state
            return None

        last_trigger = self._last_trigger_at.get(market_id)
        if last_trigger is not None and \
                available - last_trigger < self.policy.debounce:
            self._reject(market_id, Rejection.BELOW_THRESHOLD, available,
                         f"debounced: {(available - last_trigger).total_seconds():.0f}s "
                         f"since the previous trigger on this market "
                         f"(< {self.policy.debounce.total_seconds():.0f}s)")
            self._state[market_id] = state
            return None

        blindness = None
        if envelope.source_update_time is not None:
            blindness = (available - envelope.source_update_time).total_seconds()

        trigger = MoveTrigger(
            event_id=envelope.provenance.event_id,
            market_id=market_id,
            detected_at=available,
            book_moved_at=envelope.source_update_time,
            detection_blindness_seconds=blindness,
            fair_before_away=previous.fair_away,
            fair_before_home=previous.fair_home,
            fair_after_away=fair_away,
            fair_after_home=fair_home,
            delta_away=delta_away,
            delta_home=delta_home,
            overround_before=previous.overround,
            overround_after=overround,
            policy=self.policy,
            before_provenance=previous.envelope.as_dict(snapshot_times),
            after_provenance=envelope.as_dict(snapshot_times),
        )
        self._state[market_id] = state
        self._last_trigger_at[market_id] = available
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
    """Convenience: run one detector over one market's envelopes."""
    return MoveDetector(policy).observe_all(envelopes, snapshot_times)
