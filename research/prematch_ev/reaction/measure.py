"""Did the exchange follow the book, and could we have got there first?

Two questions, deliberately answered separately, because they have different
answers and different evidence:

  ORDERING      did the book change before the exchange did?
  TRADEABLE LAG how long after WE could have seen the book move was the
                exchange still at its old price?

Collapsing them is the temptation. "The book led by 90 seconds" reads like
both at once, and it is neither: it is a point estimate of an interval, on a
clock this data does not carry.

WHY EVERY ANSWER HERE IS AN INTERVAL
------------------------------------
`capability.py` grades the sources, and the grades are binding on this module
rather than advisory:

* The book's own change instant is UNANSWERABLE. `MoveTrigger` reports it as
  a bracket between consecutive provider observations, and this module uses
  THAT bracket -- it does not re-derive one (rule 26).

* A Kalshi candle's `ts` is its period CLOSE. A price first seen in the
  candle closing at 12:03 changed somewhere in `(12:02, 12:03]`. That is a
  bracket too, and its width is the candle period, not zero.

* Ordering is INTERVAL_CENSORED. Two brackets that OVERLAP do not order, at
  any sample size. The audit puts the usual bound at 300s because the odds
  grid is five-minutely; this module tests the actual brackets instead, which
  is strictly sharper and cannot disagree with the audit -- when the brackets
  are 300s wide, overlap is exactly what the audit predicts.

So a reaction reports `lag_earliest_seconds` and `lag_latest_seconds`, and
there is no `lag_seconds`. A single number would be read, quoted, averaged and
plotted, and half of it would be an artifact of the grid (rule 24).

A MISSING CANDLE IS NOT A FLAT PRICE
------------------------------------
This is the hard one, and it is NOT resolved here, because it cannot be:
whether a minute with no candle means "nobody traded and the quote stood" or
"we have no data for that minute" is the same question the audit already
grades UNANSWERABLE -- *was the market suspended rather than merely absent*.

The two readings give opposite answers. Treating a hole as an unchanged price
manufactures a `NO_RESPONSE`; treating an unchanged price as a hole
manufactures a `BLIND_INTERVAL`. So the default is the conservative one --
a hole inside the search window yields `BLIND_INTERVAL`, an outcome that says
we could not see, not that nothing happened -- and the cadence that decides
what counts as a hole is a DECLARED policy value, measured against the data
by `measure_candle_cadence` so a wrong declaration surfaces as disagreement
rather than as a quietly different result.

`ReactionPolicy.treat_missing_candles_as_unchanged` exists for the day someone
verifies the cadence contract against the live API. It defaults to False and
the verification command is in `capability.free_verification_commands`.

DIRECTION IS PART OF THE DEFINITION
-----------------------------------
A reaction is the exchange moving the SAME WAY as the book. An exchange price
that moves the other way is a divergence, and calling it a reaction with a
negative sign would put it in the same average as the real ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Sequence

from .capability import KALSHI_CANDLE_FLOOR_SECONDS
from .detector import MoveTrigger

# A candle's timestamp is its period CLOSE, so the value it reports came into
# being somewhere in the period BEFORE it. One minute is the exchange's
# finest published interval (`period_interval` 1); it is transcribed from the
# candlesticks documentation, not measured here, which is why
# `measure_candle_cadence` exists.
CANDLE_PERIOD = timedelta(seconds=KALSHI_CANDLE_FLOOR_SECONDS)


class Ordering(str, Enum):
    """Which side moved first, WHEN THE BRACKETS ALLOW AN ANSWER."""

    BOOK_LED = "book_led"                  # book bracket closes before Kalshi's opens
    KALSHI_LED = "kalshi_led"              # Kalshi bracket closes before the book's opens
    INDETERMINATE = "indeterminate"        # the brackets overlap: no answer exists
    UNKNOWN = "unknown"                    # a bracket endpoint is missing


class ReactionOutcome(str, Enum):
    """What happened on the exchange after a detected book move.

    Every one is reported. A study that only counts `RESPONDED` is measuring
    its own filter, and the censored outcomes are where the interesting
    failures live -- `NO_RESPONSE` and `BLIND_INTERVAL` look identical in a
    hit rate and mean opposite things.
    """

    RESPONDED = "responded"                      # moved the same way, in window
    ALREADY_PRICED = "exchange_moved_before_trigger"   # nothing left to react to
    OPPOSITE_DIRECTION = "opposite_direction"    # moved AGAINST the book
    NO_RESPONSE = "no_response_in_window"        # right-censored at max_wait
    BLIND_INTERVAL = "blind_interval"            # a data hole covers the window
    NO_BASELINE = "no_exchange_baseline"         # nothing usable at/before T
    NO_TRIGGER_CLOCK = "no_trigger_clock"        # the trigger cannot be dated


@dataclass(frozen=True)
class ReactionPolicy:
    """Declared measurement rules, recorded on every reaction.

    EXPLORATORY DEFAULTS, fixed before inspecting any outcome:

      min_response    0.01 of probability, the same unit and size as the
                      detector's `min_move`. Deliberately equal: a reaction
                      threshold looser than the trigger threshold would count
                      noise as a follow, and tighter would discard real ones.
      max_wait        30 minutes. Past this the answer is right-censored, not
                      "no reaction" -- the distinction the whole outcome enum
                      exists for.
      lookback        30 minutes, symmetric with `max_wait`. WITHOUT IT THE
                      NULL HYPOTHESIS IS UNREACHABLE: a forward-only search
                      sees candles after the trigger, so an exchange that had
                      ALREADY priced the move reports `NO_RESPONSE` -- the
                      exchange leading and the exchange never following are
                      recorded as the same outcome, and they are opposite
                      findings. The lookback runs first, for that reason.
      candle_period   60s, the exchange's finest published interval. Used as
                      the width of the Kalshi bracket AND as the hole
                      threshold, so the two cannot drift.
      require_direction
                      True. A move against the book is a divergence.
      treat_missing_candles_as_unchanged
                      False. Whether a minute with no candle means the quote
                      stood or the data is absent is UNANSWERABLE from these
                      sources (see the module docstring). False is the
                      conservative reading; flip it only against a verified
                      cadence contract, and the flip is recorded on the
                      output.
    """

    min_response: float = 0.01
    max_wait: timedelta = timedelta(minutes=30)
    lookback: timedelta = timedelta(minutes=30)
    candle_period: timedelta = CANDLE_PERIOD
    require_direction: bool = True
    treat_missing_candles_as_unchanged: bool = False
    label: str = "exploratory-v1"

    def __post_init__(self) -> None:
        if not (self.min_response > 0) or self.min_response != self.min_response:
            raise ValueError(
                f"min_response must be positive, got {self.min_response!r}")
        for name in ("max_wait", "lookback", "candle_period"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta, "
                                 f"got {value!r}")

    @property
    def max_reportable_lag_seconds(self) -> float:
        """The largest `lag_earliest_seconds` this policy can EVER report.

        The response search ends at `decided_at + max_wait`, and a candle's
        bracket opens one period before its close, so the latest possible
        earliest-bound is `max_wait - candle_period`. With the defaults that
        is 1,740s, not 1,800s.

        This exists because a pilot decision rule was declared at "beyond
        1,800s" against exactly this policy, and no measured reaction could
        ever satisfy it. The rule was unfalsifiable in the direction that
        mattered and nothing said so: `--policy` printed 1,800s as the wait
        and the document quoted 1,800s as the threshold, and the two numbers
        looked like agreement. A ceiling nothing computes is a ceiling
        nothing can be checked against (rule 21).
        """
        return (self.max_wait - self.candle_period).total_seconds()

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "min_response": self.min_response,
            "max_wait_seconds": self.max_wait.total_seconds(),
            "max_reportable_lag_seconds": self.max_reportable_lag_seconds,
            "lookback_seconds": self.lookback.total_seconds(),
            "candle_period_seconds": self.candle_period.total_seconds(),
            "require_direction": self.require_direction,
            "treat_missing_candles_as_unchanged":
                self.treat_missing_candles_as_unchanged,
            "tuned_on_outcomes": False,
            "note": ("declared before inspecting outcomes; the 16 development "
                     "games must not be used to fit these"),
        }


@dataclass(frozen=True)
class Bracket:
    """A closed interval an instant is known to lie in. Never a point."""

    earliest: datetime | None
    latest: datetime | None

    @property
    def width_seconds(self) -> float | None:
        if self.earliest is None or self.latest is None:
            return None
        return (self.latest - self.earliest).total_seconds()

    @property
    def known(self) -> bool:
        return self.earliest is not None and self.latest is not None

    def overlaps(self, other: "Bracket") -> bool | None:
        """None when either bracket is unknown -- not False (rule 17)."""
        if not self.known or not other.known:
            return None
        return self.earliest <= other.latest and other.earliest <= self.latest

    def as_dict(self) -> dict:
        return {
            "earliest": _iso(self.earliest),
            "latest": _iso(self.latest),
            "width_seconds": self.width_seconds,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def candle_bracket(ts: datetime, period: timedelta) -> Bracket:
    """The interval a candle's closing value came into being in.

    `(ts - period, ts]`. Reported closed for arithmetic; the open lower end
    only matters at the boundary, and treating it as closed is the wider,
    more conservative bracket.
    """
    return Bracket(earliest=ts - period, latest=ts)


def order_brackets(book: Bracket, exchange: Bracket) -> Ordering:
    """Which moved first, from the brackets alone.

    Takes the detector's bracket as given rather than re-deriving one from
    `last_update` (rule 26): a second derivation of the same fact will drift,
    and then the two disagree in the incident where one of them had to be
    authoritative.
    """
    overlap = book.overlaps(exchange)
    if overlap is None:
        return Ordering.UNKNOWN
    if overlap:
        return Ordering.INDETERMINATE
    return Ordering.BOOK_LED if book.latest < exchange.earliest \
        else Ordering.KALSHI_LED


@dataclass(frozen=True)
class Reaction:
    """One measured response (or non-response) to one detected book move."""

    event_id: str
    stream_id: str
    market_ticker: str
    outcome: ReactionOutcome
    ordering: Ordering
    policy: ReactionPolicy

    detected_at: datetime | None               # the book's EXECUTABLE clock
    book_change: Bracket
    exchange_change: Bracket

    # THE LAG, AS AN INTERVAL. There is no `lag_seconds` on purpose.
    lag_earliest_seconds: float | None = None
    lag_latest_seconds: float | None = None

    book_delta: float | None = None            # oriented to the YES side
    exchange_before: float | None = None
    exchange_after: float | None = None
    exchange_delta: float | None = None

    censored_at_seconds: float | None = None   # for NO_RESPONSE
    blind_from: datetime | None = None         # for BLIND_INTERVAL
    blind_to: datetime | None = None
    detail: str = ""

    @property
    def lag_width_seconds(self) -> float | None:
        if self.lag_earliest_seconds is None or self.lag_latest_seconds is None:
            return None
        return self.lag_latest_seconds - self.lag_earliest_seconds

    def discrepancy_survives(self, delay: timedelta) -> bool | None:
        """Was the exchange still at its old price `delay` after the trigger?

        THE OWNER'S "delay misses the gap" CASE, decided honestly. Three
        answers, not two:

          True   the lag interval starts after the delay, so the exchange
                 demonstrably had not moved yet -- the discrepancy survived
                 long enough to act on
          False  the lag interval ENDS at or before the delay, so it
                 demonstrably had moved -- the opportunity was gone
          None   the delay falls INSIDE the lag interval. The censoring
                 bites: this data cannot say, and returning either boolean
                 would be a coin flip dressed as a measurement (rule 24).

        None for any outcome without a lag interval -- a non-response has no
        "still" to ask about.
        """
        if self.lag_earliest_seconds is None or self.lag_latest_seconds is None:
            return None
        seconds = delay.total_seconds()
        if self.lag_earliest_seconds > seconds:
            return True
        if self.lag_latest_seconds <= seconds:
            return False
        return None

    @property
    def is_measured(self) -> bool:
        """Did this produce a usable lag interval at all?"""
        return (self.outcome is ReactionOutcome.RESPONDED
                and self.lag_earliest_seconds is not None)

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "market_ticker": self.market_ticker,
            "outcome": self.outcome.value,
            "ordering": self.ordering.value,
            "detected_at": _iso(self.detected_at),
            "book_change_bracket": self.book_change.as_dict(),
            "exchange_change_bracket": self.exchange_change.as_dict(),
            "lag": {
                "earliest_seconds": self.lag_earliest_seconds,
                "latest_seconds": self.lag_latest_seconds,
                "width_seconds": self.lag_width_seconds,
                "note": ("interval-censored by the candle period and the odds "
                         "snapshot grid; there is no point estimate"),
            },
            "survives_delay": {
                "60s": self.discrepancy_survives(timedelta(seconds=60)),
                "300s": self.discrepancy_survives(timedelta(seconds=300)),
                "note": ("null means the delay falls inside the lag interval "
                         "and this data cannot decide"),
            },
            "book_delta": self.book_delta,
            "exchange": {
                "before": self.exchange_before,
                "after": self.exchange_after,
                "delta": self.exchange_delta,
            },
            "censored_at_seconds": self.censored_at_seconds,
            "blind_interval": {
                "from": _iso(self.blind_from),
                "to": _iso(self.blind_to),
            } if self.blind_from else None,
            "detail": self.detail,
            "policy": self.policy.as_dict(),
        }


@dataclass
class ReactionResult:
    reactions: list[Reaction] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for reaction in self.reactions:
            out[reaction.outcome.value] = out.get(reaction.outcome.value, 0) + 1
        return dict(sorted(out.items()))

    def ordering_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for reaction in self.reactions:
            out[reaction.ordering.value] = out.get(reaction.ordering.value, 0) + 1
        return dict(sorted(out.items()))

    @property
    def measured(self) -> list[Reaction]:
        return [r for r in self.reactions if r.is_measured]

    def as_dict(self) -> dict:
        return {
            "reactions": [r.as_dict() for r in self.reactions],
            "total": len(self.reactions),
            "outcomes": self.counts(),
            "ordering": self.ordering_counts(),
            "measured": len(self.measured),
            "note": ("`measured` counts only RESPONDED reactions with a lag "
                     "interval. The censored outcomes are NOT failures to "
                     "exclude -- NO_RESPONSE and BLIND_INTERVAL look the same "
                     "in a hit rate and mean opposite things"),
        }


# --- cadence, measured rather than assumed ----------------------------------

def measure_candle_cadence(timestamps: Sequence[datetime],
                           policy: ReactionPolicy | None = None) -> dict:
    """Check the DECLARED candle period against real timestamps.

    `CANDLE_PERIOD` is transcribed from documentation this session cannot
    reach. A transcribed constant that nothing checks is a number that can be
    wrong forever, and every lag derived from it would be off by the
    difference -- so the measurement is reported beside the declaration and
    disagreement is loud.

    A smaller observed gap than declared means the declaration is WRONG.
    Larger gaps are ordinary: they are the holes this module refuses to read
    as unchanged prices.
    """
    policy = policy or ReactionPolicy()
    declared = policy.candle_period.total_seconds()
    ordered = sorted(timestamps)
    gaps = [(b - a).total_seconds()
            for a, b in zip(ordered, ordered[1:]) if b > a]
    if not gaps:
        return {
            "declared_period_seconds": declared,
            "min_gap_seconds": None,
            "modal_gap_seconds": None,
            "samples": len(ordered),
            "agrees_with_declared": None,
            "note": "fewer than two distinct timestamps: a cadence needs two",
        }
    modal = max(set(gaps), key=gaps.count)
    smallest = min(gaps)
    return {
        "declared_period_seconds": declared,
        "min_gap_seconds": smallest,
        "modal_gap_seconds": modal,
        "samples": len(ordered),
        "agrees_with_declared": smallest >= declared,
        "note": ("a gap SMALLER than the declared period means the "
                 "declaration is wrong and every lag bracket is too wide; "
                 "larger gaps are holes, not disagreement"),
    }


# --- the measurement --------------------------------------------------------

def usable_candle(candle) -> bool:
    """A candle we may read a price from. PUBLIC, and shared on purpose.

    `mid` is None for a one-sided book -- which is the audit's
    suspension-versus-absence question again, so it is excluded rather than
    filled in from the last trade. `has_malformed_price` means the parser may
    be behind the wire format, which is a reason to distrust the value rather
    than to use it.

    `reaction.screen` reads the same filter, so the candle this module calls
    the pre-move baseline and the candle that module screens as the decision
    book are the SAME candle by construction. Two matching filters would be
    two filters, and one of them would eventually be changed (rule 19).
    """
    return (getattr(candle, "mid", None) is not None
            and not getattr(candle, "has_malformed_price", False))


def _first_hole(timestamps: Sequence[datetime], start: datetime,
                end: datetime, period: timedelta
                ) -> tuple[datetime, datetime] | None:
    """The first interval inside `(start, end]` with no usable candle.

    Includes the leading edge: if the first usable candle after `start` is
    more than one period later, the hole starts at `start`.
    """
    inside = [ts for ts in sorted(timestamps) if start < ts <= end]
    cursor = start
    for ts in inside:
        if ts - cursor > period:
            return cursor, ts
        cursor = ts
    if end - cursor > period:
        return cursor, end
    return None


def measure_reaction(trigger: MoveTrigger, candles: Iterable,
                     *, market_ticker: str, yes_is_home: bool,
                     policy: ReactionPolicy | None = None) -> Reaction:
    """Measure the exchange's response to one detected book move.

    `yes_is_home` orients the book's signed move onto the contract's YES
    participant. A move is +4 points for one side and -4 for the other, so a
    measurement that ignored the orientation would score half its reactions
    as divergences.
    """
    policy = policy or ReactionPolicy()
    book_bracket = Bracket(trigger.book_change_earliest,
                           trigger.book_change_latest)
    base = dict(event_id=trigger.event_id, stream_id=trigger.stream_id,
                market_ticker=market_ticker, policy=policy,
                detected_at=trigger.detected_at, book_change=book_bracket)

    decided_at = trigger.detected_at
    if decided_at is None:
        return Reaction(outcome=ReactionOutcome.NO_TRIGGER_CLOCK,
                        ordering=Ordering.UNKNOWN,
                        exchange_change=Bracket(None, None),
                        detail="the trigger carries no executable clock",
                        **base)

    book_delta = trigger.delta_for(participant_is_home=yes_is_home)
    usable = sorted((c for c in candles if usable_candle(c)),
                    key=lambda c: c.ts)

    # THE BASELINE. The exchange price we could have seen at the instant the
    # book move became known to us. Strictly at or before; a candle closing
    # after the trigger reports a period that includes post-trigger activity.
    before = [c for c in usable if c.ts <= decided_at]
    if not before:
        return Reaction(outcome=ReactionOutcome.NO_BASELINE,
                        ordering=Ordering.UNKNOWN,
                        exchange_change=Bracket(None, None),
                        book_delta=book_delta,
                        detail=("no usable exchange quote at or before the "
                                "trigger: nothing to measure a response from"),
                        **base)
    baseline = before[-1]

    # THE NULL HYPOTHESIS, CHECKED FIRST AND ON ITS OWN EVIDENCE.
    #
    # If the exchange had already moved the book's way before we could act on
    # the book, there was nothing left to react to. A forward-only search
    # cannot see that -- it looks at candles AFTER the trigger, finds the
    # exchange sitting at its already-adjusted price, and reports
    # `NO_RESPONSE`. So "the exchange led us" and "the exchange never
    # followed" would be filed under one name, and they are opposite
    # findings: the first kills the thesis, the second is the thesis.
    #
    # Note the two answers this can produce. The ORDERING may still be
    # BOOK_LED -- the book can change, the provider observe it, and the
    # exchange re-price before our snapshot ever carries it to us. The book
    # led and we had no opportunity anyway, because the delivery lag ate it.
    # That is not the same as the exchange leading the book, and reporting
    # outcome and ordering separately is what keeps them apart.
    lookback_start = decided_at - policy.lookback
    prior = [c for c in before if c.ts > lookback_start]
    if len(prior) >= 2:
        pre = prior[0]
        for candle in prior[1:]:
            delta = candle.mid - pre.mid
            if abs(delta) < policy.min_response:
                continue
            if (delta > 0) != (book_delta > 0):
                # A prior move the OTHER way is not pre-pricing. It leaves the
                # discrepancy wider, not narrower.
                continue
            exchange_bracket = candle_bracket(candle.ts, policy.candle_period)
            return Reaction(
                outcome=ReactionOutcome.ALREADY_PRICED,
                ordering=order_brackets(book_bracket, exchange_bracket),
                exchange_change=exchange_bracket,
                book_delta=book_delta, exchange_before=pre.mid,
                exchange_after=candle.mid, exchange_delta=delta,
                detail=(f"the exchange moved {delta:+.4f} the book's way "
                        f"{(decided_at - candle.ts).total_seconds():.0f}s "
                        f"BEFORE the book move became actionable to us: there "
                        f"was no discrepancy left to trade"),
                **base)

    deadline = decided_at + policy.max_wait

    # THE SEARCH. First candle inside the window whose mid has moved far
    # enough. Direction is checked against the book's oriented delta.
    window = [c for c in usable if decided_at < c.ts <= deadline]
    for candle in window:
        delta = candle.mid - baseline.mid
        if abs(delta) < policy.min_response:
            continue
        aligned = (delta > 0) == (book_delta > 0)
        if policy.require_direction and not aligned:
            return Reaction(
                outcome=ReactionOutcome.OPPOSITE_DIRECTION,
                ordering=order_brackets(
                    book_bracket, candle_bracket(candle.ts,
                                                 policy.candle_period)),
                exchange_change=candle_bracket(candle.ts, policy.candle_period),
                book_delta=book_delta, exchange_before=baseline.mid,
                exchange_after=candle.mid, exchange_delta=delta,
                detail=(f"exchange moved {delta:+.4f} against a book move of "
                        f"{book_delta:+.4f}: a divergence, not a reaction"),
                **base)

        # A HOLE BEFORE THE RESPONSE IS STILL A HOLE. The move may have
        # happened inside it, in which case the lag bracket below is wrong --
        # too late at its early end. So the hole is reported rather than the
        # lag.
        hole = None
        if not policy.treat_missing_candles_as_unchanged:
            hole = _first_hole([c.ts for c in usable], baseline.ts, candle.ts,
                               policy.candle_period)
        if hole:
            return Reaction(
                outcome=ReactionOutcome.BLIND_INTERVAL,
                ordering=Ordering.UNKNOWN,
                exchange_change=Bracket(None, None),
                book_delta=book_delta, exchange_before=baseline.mid,
                exchange_after=candle.mid, exchange_delta=delta,
                blind_from=hole[0], blind_to=hole[1],
                detail=(f"the exchange moved, but a "
                        f"{(hole[1] - hole[0]).total_seconds():.0f}s hole in "
                        f"the candle series precedes it: the response may have "
                        f"happened inside the hole, so the lag is not bounded"),
                **base)

        exchange_bracket = candle_bracket(candle.ts, policy.candle_period)
        # The lag runs from when WE could have known the book moved to when
        # the exchange's new price came into being -- an interval, because the
        # candle only locates that within its own period. Clamped at zero: a
        # candle period straddling the trigger cannot imply a negative wait.
        earliest = max(0.0,
                       (exchange_bracket.earliest - decided_at).total_seconds())
        latest = (exchange_bracket.latest - decided_at).total_seconds()
        return Reaction(
            outcome=ReactionOutcome.RESPONDED,
            ordering=order_brackets(book_bracket, exchange_bracket),
            exchange_change=exchange_bracket,
            lag_earliest_seconds=earliest, lag_latest_seconds=latest,
            book_delta=book_delta, exchange_before=baseline.mid,
            exchange_after=candle.mid, exchange_delta=delta,
            **base)

    # NOTHING CLEARED THE THRESHOLD. Before calling that a non-response, ask
    # whether we could actually see the whole window: a hole means we could
    # not, and "no response" would be a claim about data we do not have.
    hole = None
    if not policy.treat_missing_candles_as_unchanged:
        hole = _first_hole([c.ts for c in usable], baseline.ts, deadline,
                           policy.candle_period)
    if hole:
        return Reaction(
            outcome=ReactionOutcome.BLIND_INTERVAL,
            ordering=Ordering.UNKNOWN,
            exchange_change=Bracket(None, None),
            book_delta=book_delta, exchange_before=baseline.mid,
            blind_from=hole[0], blind_to=hole[1],
            detail=(f"no qualifying move found, but a "
                    f"{(hole[1] - hole[0]).total_seconds():.0f}s hole in the "
                    f"candle series falls inside the window: this is not "
                    f"evidence of no response"),
            **base)
    return Reaction(
        outcome=ReactionOutcome.NO_RESPONSE,
        ordering=Ordering.UNKNOWN,
        exchange_change=Bracket(None, None),
        book_delta=book_delta, exchange_before=baseline.mid,
        exchange_after=window[-1].mid if window else baseline.mid,
        censored_at_seconds=policy.max_wait.total_seconds(),
        detail=(f"no move of {policy.min_response} or more within "
                f"{policy.max_wait.total_seconds():.0f}s: RIGHT-CENSORED at "
                f"the window, not a measured non-reaction"),
        **base)


def measure_all(pairs: Iterable[tuple[MoveTrigger, Sequence, str, bool]],
                policy: ReactionPolicy | None = None) -> ReactionResult:
    """Measure a batch of `(trigger, candles, market_ticker, yes_is_home)`."""
    result = ReactionResult()
    for trigger, candles, ticker, yes_is_home in pairs:
        result.reactions.append(measure_reaction(
            trigger, candles, market_ticker=ticker,
            yes_is_home=yes_is_home, policy=policy))
    return result
