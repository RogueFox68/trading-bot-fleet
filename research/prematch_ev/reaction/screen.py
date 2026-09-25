"""Turn a detected book move into the checkpoint study's own Observation.

WHAT THIS LAYER ACTUALLY CONTRIBUTES, AND WHAT IT MUST NOT
----------------------------------------------------------
The event-driven study's whole difference from the checkpoint study is WHICH
INSTANTS GET SCREENED: a fixed lead grid versus the moments a sharp-book move
became visible. That is the contribution. It is not a different idea of what
a tradeable opportunity is.

So there is NO second screen here. `analysis.scoring` owns that, and it owns
it with lessons this module must not re-learn:

* **The screen is predicted net EV at the EXECUTABLE price.** Round 2 of this
  study screened on midpoint disagreement, and bid .40 / ask .60 / sharp .53
  cleared it -- buying YES at .60 plus fee, a predicted **-0.09 per
  contract**. A fresh screen written here would be one refactor away from
  that again.
* **YES pays the ask, NO pays (1 - bid)**, computed explicitly rather than by
  negating a YES figure, because getting it wrong inverts half the sample.
* **The fee resolves at execution**, on a dated per-series schedule.
* **An unexecutable delayed side is DROPPED, never priced at the decision
  book.**

`observation_for` therefore builds an `Observation` and hands it to
`Eligibility` / `as_trade` / `screen_diagnostics` unchanged. Rule 19: two
implementations of "is this tradeable" would eventually disagree, and the
disagreement would surface as a difference between the two studies that
looked like a finding.

WHERE THE DECISION BOOK COMES FROM
----------------------------------
The candle at or before the trigger's EXECUTABLE clock -- the same candle
`measure.measure_reaction` uses as its reaction baseline, taken from the same
helper, so the screened price and the measured pre-move price cannot differ.
The execution book is the candle at `detected_at + delay`, and at zero delay
it is the same candle by definition.

"DELAY MISSES THE GAP" NEEDS NO EXTRA GATE
------------------------------------------
It is tempting to refuse a trade when the exchange has already re-priced. It
is also wrong, because `side_quotes` already prices the delayed entry at
whatever the book has become -- a moved book simply collapses the EV, which
is the honest result rather than a filtered one. Adding a gate would be a
second implementation of the same fact, and it would hide the magnitude.

`Reaction.discrepancy_survives` is carried alongside as the DIAGNOSTIC that
explains a collapse, including its third answer: the delay can fall inside
the lag interval, where this data cannot say whether the price was still
there.

SETTLEMENT IS WITHHELD, NOT DEFAULTED
-------------------------------------
`Observation.outcome` is required, and a wrong one poisons every realized
figure while leaving every predicted one intact and plausible. When
settlement is not supplied, this module builds the observation with a
declared placeholder and reports NO realized field at all -- predicted EV
does not read `outcome`, so the screen still runs. `ScreenedReaction`
withholds `realized_*` rather than annotating it (rule 24), and a test
asserts the serialised output is IDENTICAL for both placeholder values, which
is what proves nothing settlement-derived escapes.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import (                                  # noqa: E402
    Eligibility, Observation, ScreenRejection, ScreenVerdict, Trade, as_trade,
    side_quotes,
)
from .detector import MoveTrigger                               # noqa: E402
from .measure import Reaction, ReactionOutcome, usable_candle    # noqa: E402

# The placeholder `Observation.outcome` carries when settlement is unknown.
# ZERO, deliberately: `side_quotes` maps the YES payout as `float(outcome)`
# and the NO payout as `float(1 - outcome)`, so any truthy sentinel would
# manufacture a winning YES leg. Nothing downstream may read it -- which is
# enforced by withholding the fields that would, and asserted by comparing
# the serialised output across both values.
UNKNOWN_SETTLEMENT = 0


class ScreenRefusal(str, Enum):
    """Why a detected move produced no screenable observation.

    Each is reported. A count of trades with no count of refusals measures
    the filter, and these refusals are where this layer's own failures live.
    """

    NOT_A_MOVE_WE_COULD_TRADE = "reaction_not_screenable"
    NO_DECISION_BOOK = "no_exchange_book_at_the_trigger"
    CROSSED_DECISION_BOOK = "crossed_exchange_book_at_the_trigger"
    NO_START_TIME = "no_kickoff_time"
    TRIGGER_AT_OR_AFTER_START = "trigger_at_or_after_start"


# Reaction outcomes that can be screened at all. `ALREADY_PRICED` is
# deliberately screenable: the exchange having moved first does not make the
# observation unreal, and dropping it would remove exactly the rows that
# falsify the thesis from the denominator. Whether its move left anything to
# trade is decided HERE, at the decision book -- a partial move can leave a
# discrepancy -- never inferred from the movement. The same goes for
# `AROUND_TRIGGER`, a response that may have come first.
SCREENABLE = frozenset({
    ReactionOutcome.RESPONDED,
    ReactionOutcome.AROUND_TRIGGER,
    ReactionOutcome.NO_RESPONSE,
    ReactionOutcome.BLIND_INTERVAL,
    ReactionOutcome.OPPOSITE_DIRECTION,
    ReactionOutcome.ALREADY_PRICED,
})


@dataclass(frozen=True)
class ScreenedReaction:
    """One detected move, screened by the checkpoint study's own policy."""

    event_id: str
    market_ticker: str
    decision_at: datetime | None
    refusal: ScreenRefusal | None
    observation: Observation | None
    admitted: bool
    trade: Trade | None
    settlement_known: bool
    reaction_outcome: str
    ordering: str
    survives_delay: bool | None
    entry_delay_seconds: float
    detail: str = ""
    # The exchange's move the book's way already in place at the trigger, as
    # the measurement saw it: reported beside this row's opportunity, which
    # is judged here at the decision book -- never inferred from that move.
    prior_move: dict | None = None
    # The screen's own verdict: `admitted` is its `admitted`, and a refused
    # row carries the gate that refused it. None when nothing was screened.
    verdict: ScreenVerdict | None = None

    @property
    def rejection(self) -> ScreenRejection | None:
        return self.verdict.rejection if self.verdict else None

    @property
    def predicted_ev(self) -> float | None:
        """Per contract, after fee, at the DECISION book. No settlement."""
        return self.trade.predicted_ev if self.trade else None

    @property
    def realized_profit(self) -> float | None:
        """WITHHELD unless settlement was supplied.

        Not annotated -- absent. A realized figure computed from a
        placeholder outcome reads exactly like a real one.
        """
        if not self.settlement_known or self.trade is None:
            return None
        return self.trade.profit

    def as_dict(self) -> dict:
        row = {
            "event_id": self.event_id,
            "market_ticker": self.market_ticker,
            "decision_at": self.decision_at.isoformat()
            if self.decision_at else None,
            "refusal": self.refusal.value if self.refusal else None,
            "admitted": self.admitted,
            "rejection": self.rejection.value if self.rejection else None,
            "reaction_outcome": self.reaction_outcome,
            "ordering": self.ordering,
            "survives_delay": self.survives_delay,
            "entry_delay_seconds": self.entry_delay_seconds,
            "detail": self.detail,
            "prior_move": self.prior_move,
            "predicted": None,
            "realized": None,
            "settlement_known": self.settlement_known,
        }
        if self.trade is not None:
            row["predicted"] = {
                "side": self.trade.side,
                "entry_price": self.trade.entry_price,
                "fee": self.trade.fee,
                "predicted_ev_per_contract": self.trade.predicted_ev,
                "note": ("predicted EV reads the decision book and the fair "
                         "probability only; it never reads settlement"),
            }
            if self.settlement_known:
                row["realized"] = {
                    "paid": self.trade.paid,
                    "payout": self.trade.payout,
                    "profit": self.trade.profit,
                    "return_on_stake": self.trade.return_on_stake,
                }
            else:
                row["realized_withheld_because"] = (
                    "settlement was not supplied for this contract; a "
                    "realized figure from a placeholder outcome is "
                    "indistinguishable from a real one")
        return row


@dataclass
class ScreenResult:
    screened: list[ScreenedReaction] = field(default_factory=list)

    @property
    def admitted(self) -> list[ScreenedReaction]:
        return [s for s in self.screened if s.admitted]

    @property
    def observations(self) -> list[Observation]:
        return [s.observation for s in self.screened
                if s.observation is not None]

    def refusal_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.screened:
            if item.refusal is not None:
                out[item.refusal.value] = out.get(item.refusal.value, 0) + 1
        return dict(sorted(out.items()))

    def settlement_coverage(self) -> dict:
        """How much of this run can carry a realized figure at all."""
        known = sum(1 for s in self.screened if s.settlement_known)
        return {
            "screened": len(self.screened),
            "settlement_known": known,
            "settlement_unknown": len(self.screened) - known,
            "realized_reportable": known > 0,
            "note": ("predicted EV needs no settlement; every realized "
                     "figure does, and is withheld without it"),
        }

    def as_dict(self) -> dict:
        return {
            "screened": [s.as_dict() for s in self.screened],
            "total": len(self.screened),
            "admitted": len(self.admitted),
            "refusals": self.refusal_counts(),
            "settlement": self.settlement_coverage(),
        }


def decision_book(candles: Iterable, at: datetime):
    """The exchange book as seen at `at`: the newest usable candle at/before.

    Shares `measure.usable_candle`, so the price this module screens and the
    price `measure_reaction` calls the pre-move baseline are the same candle
    by construction rather than by two matching filters (rule 19).
    """
    before = sorted((c for c in candles if usable_candle(c) and c.ts <= at),
                    key=lambda c: c.ts)
    return before[-1] if before else None


def execution_candle(candles: Iterable, target: datetime,
                     tolerance: timedelta):
    """The first usable candle at or after `target`, within `tolerance`.

    No fallback and no reaching backwards: a delay means waiting for the next
    quote. Returning an earlier one would convert a missed opportunity into a
    taken one at a price that was already gone.
    """
    after = sorted((c for c in candles
                    if usable_candle(c) and target <= c.ts <= target + tolerance),
                   key=lambda c: c.ts)
    return after[0] if after else None


def observation_for(reaction: Reaction, trigger: MoveTrigger, candles: Sequence,
                    *, market_ticker: str, yes_is_home: bool,
                    start: datetime | None,
                    settled_yes: int | None = None,
                    entry_delay: timedelta = timedelta(0),
                    entry_tolerance: timedelta = timedelta(minutes=5),
                    checkpoint_label: float | None = None
                    ) -> tuple[Observation | None, ScreenRefusal | None, str]:
    """Build the checkpoint study's `Observation` from one detected move.

    Returns `(observation, refusal, detail)`. Exactly one of the first two is
    set.
    """
    if reaction.outcome not in SCREENABLE:
        return None, ScreenRefusal.NOT_A_MOVE_WE_COULD_TRADE, (
            f"reaction outcome {reaction.outcome.value} carries no decision "
            f"instant to screen")
    return observation_at(trigger, candles, market_ticker=market_ticker,
                          yes_is_home=yes_is_home, start=start,
                          settled_yes=settled_yes, entry_delay=entry_delay,
                          entry_tolerance=entry_tolerance,
                          checkpoint_label=checkpoint_label)


def observation_at(trigger: MoveTrigger, candles: Sequence, *,
                   market_ticker: str, yes_is_home: bool,
                   start: datetime | None, settled_yes: int | None = None,
                   entry_delay: timedelta = timedelta(0),
                   entry_tolerance: timedelta = timedelta(minutes=5),
                   checkpoint_label: float | None = None
                   ) -> tuple[Observation | None, ScreenRefusal | None, str]:
    """`observation_for` without the reaction gate.

    A LIVE decision is made the moment a move is seen, before any reaction can
    exist, so it cannot pass a measured reaction in. It enters here instead,
    and from here on the path is the replay's own: the same decision book,
    the same execution quote, the same Observation. `observation_for` is this
    plus its gate, not a copy of it (rule 19).
    """
    decided_at = trigger.detected_at
    if start is None:
        return None, ScreenRefusal.NO_START_TIME, (
            "no kickoff time, so lead time and the pre-game bound are both "
            "undefined -- never inferred from a settlement or expiry stamp")
    if decided_at >= start:
        return None, ScreenRefusal.TRIGGER_AT_OR_AFTER_START, (
            "the move became visible at or after kickoff: not a pre-match "
            "opportunity")

    book = decision_book(candles, decided_at)
    if book is None:
        return None, ScreenRefusal.NO_DECISION_BOOK, (
            "no usable exchange candle at or before the trigger")
    # NO one-sided check here: `usable_candle` requires `mid`, and `Candle.mid`
    # is None whenever either side is, so a candle that reached this line has
    # both. A second check would be unreachable, and unreachable code that
    # looks like a guard is worse than no code -- it reads as covering a case
    # nothing can produce. The one-sided case is real; it is caught upstream,
    # by the same filter, and surfaces as the reaction's NO_BASELINE.
    if book.bid_close > book.ask_close:
        return None, ScreenRefusal.CROSSED_DECISION_BOOK, (
            f"crossed book at the trigger: bid {book.bid_close} > ask "
            f"{book.ask_close}")

    entry = book
    entry_at = book.ts
    delay_minutes = entry_delay.total_seconds() / 60.0
    if entry_delay > timedelta(0):
        found = execution_candle(candles, decided_at + entry_delay,
                                 entry_tolerance)
        # A MISSING DELAYED QUOTE IS NOT A FILL AT THE DECISION PRICE. Left
        # as None, `Observation.execution_book` returns None under a delay
        # and `side_quotes` drops the side, which is the correct outcome:
        # there was no fill. That is why this does not fall back.
        entry = found
        entry_at = found.ts if found else None

    # p_sharp is the fair probability AFTER the move, oriented to this
    # contract's YES participant. An unoriented probability is a silently
    # inverted signal, not a smaller effect.
    p_sharp = (trigger.fair_after_home if yes_is_home
               else trigger.fair_after_away)
    return Observation(
        game_id=trigger.event_id,
        market_id=market_ticker,
        decision_at=decided_at,
        minutes_to_start=(start - decided_at).total_seconds() / 60.0,
        p_sharp=p_sharp,
        p_exchange=book.mid,
        outcome=UNKNOWN_SETTLEMENT if settled_yes is None else int(settled_yes),
        yes_participant=market_ticker.rsplit("-", 1)[-1],
        exchange_bid=book.bid_close,
        exchange_ask=book.ask_close,
        sharp_at=trigger.provider_observed_at,
        sharp_snapshot_at=trigger.detected_at,
        exchange_at=book.ts,
        devig_method=trigger.policy.devig_method,
        checkpoint_minutes=checkpoint_label,
        entry_at=entry_at,
        entry_delay_minutes=delay_minutes,
        entry_bid=entry.bid_close if entry is not None else None,
        entry_ask=entry.ask_close if entry is not None else None,
    ), None, ""


def screen_reaction(reaction: Reaction, trigger: MoveTrigger,
                    candles: Sequence, *, market_ticker: str,
                    yes_is_home: bool, start: datetime | None,
                    settled_yes: int | None = None,
                    entry_delay: timedelta = timedelta(0),
                    entry_tolerance: timedelta = timedelta(minutes=5),
                    eligibility: Eligibility | None = None,
                    series: str | None = None,
                    venue: str = "kalshi", role: str = "taker",
                    route: str | None = None) -> ScreenedReaction:
    """Screen one detected move through the checkpoint study's own policy."""
    eligibility = eligibility or Eligibility()
    observation, refusal, detail = observation_for(
        reaction, trigger, candles, market_ticker=market_ticker,
        yes_is_home=yes_is_home, start=start, settled_yes=settled_yes,
        entry_delay=entry_delay, entry_tolerance=entry_tolerance)
    common = dict(
        event_id=trigger.event_id, market_ticker=market_ticker,
        decision_at=trigger.detected_at,
        settlement_known=settled_yes is not None,
        reaction_outcome=reaction.outcome.value,
        ordering=reaction.ordering.value,
        survives_delay=reaction.discrepancy_survives(entry_delay),
        entry_delay_seconds=entry_delay.total_seconds(),
        prior_move=reaction.prior.as_dict() if reaction.prior else None)
    if observation is None:
        return ScreenedReaction(refusal=refusal, observation=None,
                                admitted=False, trade=None, detail=detail,
                                **common)

    verdict, trade = _admit(observation, eligibility, venue=venue, role=role,
                            series=series, route=route)
    return ScreenedReaction(refusal=None, observation=observation,
                            admitted=verdict.admitted, trade=trade,
                            detail=detail, verdict=verdict, **common)


def _admit(observation: Observation, eligibility: Eligibility, *, venue: str,
           role: str, series: str | None, route: str | None
           ) -> tuple[ScreenVerdict, Trade | None]:
    """The checkpoint study's own admission and pricing, called one way.

    The VERDICT, not a bare boolean: `Eligibility.admits` is its
    `admitted`, so the named reason recorded beside a refusal is the
    decision's own and cannot disagree with it.
    """
    kwargs = dict(venue=venue, role=role, series=series)
    if route is not None:
        kwargs["route"] = route
    return (eligibility.verdict(observation, **kwargs),
            as_trade(observation, eligibility=eligibility, **kwargs))


@dataclass
class LiveDecision:
    """What a bot would have done at the instant a live move was seen.

    Made BEFORE anything that follows is known, and recorded then, so it
    cannot be improved by hindsight. It is SCREENED at the decision book --
    the last one read before the move -- exactly as the replay screens, so
    `entry_price` and the predicted EV are what the bot knew. `paid` is what
    the EXECUTION book, read after the move was seen, would have cost, and
    `entry_delay_seconds` is how long that read took: a measured delay where
    the replay can only assume one. The difference is `latency_cost`.
    """

    event_id: str
    market_ticker: str
    decision_at: datetime | None
    refusal: ScreenRefusal | None
    observation: Observation | None
    admitted: bool
    trade: Trade | None
    entry_delay_seconds: float
    detail: str = ""
    verdict: ScreenVerdict | None = None

    @property
    def rejection(self) -> ScreenRejection | None:
        """The gate that refused a SCREENED move; None when admitted, and
        None when nothing could be screened (that is `refusal`)."""
        return self.verdict.rejection if self.verdict else None

    def as_dict(self) -> dict:
        row = {
            "event_id": self.event_id,
            "market_ticker": self.market_ticker,
            "decision_at": (self.decision_at.isoformat()
                            if self.decision_at else None),
            "refusal": self.refusal.value if self.refusal else None,
            "admitted": self.admitted,
            "rejection": self.rejection.value if self.rejection else None,
            "entry_delay_seconds": self.entry_delay_seconds,
            "detail": self.detail,
            "predicted": None,
            "realized_withheld_because": (
                "a live decision is recorded before settlement exists"),
        }
        if self.observation is not None:
            o = self.observation
            row["book"] = {"decision_bid": o.exchange_bid,
                           "decision_ask": o.exchange_ask,
                           "entry_bid": o.entry_bid, "entry_ask": o.entry_ask,
                           "p_sharp": o.p_sharp,
                           "minutes_to_start": o.minutes_to_start}
        if self.trade is not None:
            decided_cost = self.trade.entry_price + self.trade.fee
            row["predicted"] = {
                "side": self.trade.side,
                "entry_price": self.trade.entry_price,
                "fee": self.trade.fee,
                "predicted_ev_per_contract": self.trade.predicted_ev,
                "paid_at_execution": self.trade.paid,
                # What the delay between seeing the move and reading the book
                # cost per contract: the trade's own two prices, subtracted.
                "latency_cost": (None if self.trade.paid is None
                                 else self.trade.paid - decided_cost),
            }
        return row


def screen_live(trigger: MoveTrigger, books: Sequence, *, market_ticker: str,
                yes_is_home: bool, start: datetime | None,
                entry_delay: timedelta,
                entry_tolerance: timedelta = timedelta(minutes=5),
                eligibility: Eligibility | None = None,
                series: str | None = None, venue: str = "kalshi",
                role: str = "taker", route: str | None = None
                ) -> LiveDecision:
    """Screen a live move through the checkpoint study's own policy.

    `books` are polled books (`data.kalshi_history.BookQuote`), which carry
    the candle field names so they pass through `observation_at` unchanged:
    the newest at or before the move is the decision book, and the first at
    or after `detected_at + entry_delay` is the execution book.
    """
    eligibility = eligibility or Eligibility()
    observation, refusal, detail = observation_at(
        trigger, books, market_ticker=market_ticker, yes_is_home=yes_is_home,
        start=start, entry_delay=entry_delay,
        entry_tolerance=entry_tolerance)
    common = dict(event_id=trigger.event_id, market_ticker=market_ticker,
                  decision_at=trigger.detected_at,
                  entry_delay_seconds=entry_delay.total_seconds())
    if observation is None:
        return LiveDecision(refusal=refusal, observation=None, admitted=False,
                            trade=None, detail=detail, **common)
    verdict, trade = _admit(observation, eligibility, venue=venue, role=role,
                            series=series, route=route)
    return LiveDecision(refusal=None, observation=observation,
                        admitted=verdict.admitted, trade=trade, detail=detail,
                        verdict=verdict, **common)


def screen_all(items: Iterable[tuple], **kwargs) -> ScreenResult:
    """Screen a batch of `(reaction, trigger, candles, per-item kwargs)`."""
    result = ScreenResult()
    for reaction, trigger, candles, per_item in items:
        merged = dict(kwargs)
        merged.update(per_item)
        result.screened.append(
            screen_reaction(reaction, trigger, candles, **merged))
    return result


def executable_sides(observation: Observation, *, series: str | None = None,
                     venue: str = "kalshi", role: str = "taker") -> list:
    """Both sides, priced, for reporting WHY a screen refused.

    Exposed so a run can show the EV distribution underneath a zero-trade
    result. A run that finds nothing and a run whose collection broke both
    print "no eligible trades"; this is what tells them apart, and it is the
    honest alternative to loosening a frozen threshold until trades appear.
    """
    return side_quotes(observation, venue, role, series)
