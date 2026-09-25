"""One assessment -- a detected move on one joined contract -- recorded in full.

WHY THIS EXISTS
---------------
The 2026-09-24 development session detected five moves and assessed ten
contracts. Eight were screened and all eight were refused, and the record of
each refusal was `admitted: false` beside a null `predicted` block: the two
sides' prices, the fee, how far below the floor, how old the quote was and
what depth stood behind it -- everything that says WHY -- was computed and
then thrown away, because only an admitted trade kept its numbers. A refused
trade is the commonest outcome by far, so it is the one whose diagnostics
matter most, and the one whose absence cost the most to reconstruct by hand.

WHAT THIS MAY NOT DO
--------------------
Decide. The screen is `screen.screen_live`, unchanged, and the named reason
is that screen's own verdict (`Eligibility.verdict`, of which `admits` is the
boolean). Both sides are priced by `side_quotes`, the function the screen
prices with, and every gate value is read through `Eligibility`'s own gate
methods. Nothing here re-states a comparison the screen makes (rule 19): a
second screen would agree with the first only until one of them changed, and
the disagreement would then read as a finding.

Pure: a trigger and the books in memory in, a record out. The live monitor
calls it the instant it decides; the offline re-analysis calls it on the same
inputs rebuilt from the session's raw records. One function, two callers, so
a recorded assessment and a recomputed one cannot mean different things.

WHERE A MISSING QUOTE COMES FROM
--------------------------------
"No decision book" has three unrelated causes, and they were one refusal:

* the contract was OUTSIDE THE OBSERVATION HORIZON -- the monitor reads books
  only for games within the screen's own lead-time ceiling of kickoff, so
  there was never going to be a baseline, and the screen's lead-time gate
  would refuse the move anyway (the Houston-Indianapolis move, ~76h out);
* the market was ONE-SIDED -- the exchange answered, with no resting bids on
  a side: a market state, not a failure;
* COLLECTION FAILED -- a watched, in-horizon contract whose read failed, was
  abandoned behind another failed read, or was joined after the tick's reads
  began. Only this one is a defect in the data.

`classify_coverage` names which, from the tick the move was detected on.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import (                                  # noqa: E402
    Eligibility, Observation, side_quotes,
)
from core.fees import (                                         # noqa: E402
    DEFAULT_ROUTE, FeeScheduleUnresolved, fee_provenance, fee_sensitivity,
)
from .detector import MoveTrigger                               # noqa: E402
from .screen import (                                           # noqa: E402
    LiveDecision, screen_books, screen_live,
)

SCHEMA = "shadow-assessment/1"


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


def _seconds(delta: timedelta | None) -> float | None:
    return None if delta is None else delta.total_seconds()


# --- the tick a move was detected on ------------------------------------------

class TickRead(str, Enum):
    """What this tick's decision read of one contract returned."""

    OK = "ok"                              # a usable two-sided book
    ONE_SIDED = "one_sided"                # answered; a side had no bids
    MALFORMED = "malformed"                # answered; the parser distrusted it
    FAILED = "failed"                      # no answer: timeout, HTTP error, cut
    ABANDONED = "abandoned"                # not attempted: an earlier read
    #                                        this tick failed and stopped them
    NOT_JOINED = "not_joined_at_tick"      # joined after this tick's reads
    NOT_ATTEMPTED = "not_read_outside_horizon"
    NOT_RECORDED = "no_read_recorded"      # in horizon, joined, no read, no
    #                                        abandonment: an inconsistency


def read_status(payload_present: bool, book: Any) -> TickRead:
    """One read's status, from what `read_book` had: the payload and the
    parsed book (None when the shape was refused)."""
    if not payload_present:
        return TickRead.FAILED
    if book is None or getattr(book, "has_malformed_price", False):
        return TickRead.MALFORMED
    if getattr(book, "mid", None) is None:
        return TickRead.ONE_SIDED
    return TickRead.OK


@dataclass(frozen=True)
class TickContext:
    """What the monitor knew when a tick's decision reads began.

    `at` is the instant the horizon was judged from; `joined` the contracts
    joined at that instant; `reads` each attempted decision read's status.
    `source` says whether this was recorded live or rebuilt from a session
    file's order -- the rebuilt `at` is the first request of the tick, which
    trails the monitor's own clock read by the time it took to issue it.
    """

    at: datetime
    horizon: timedelta
    joined: frozenset = frozenset()
    reads: Mapping[str, str] = field(default_factory=dict)
    abandoned_after: str | None = None
    source: str = "recorded"

    def within_horizon(self, start: datetime) -> bool:
        """The monitor's own rule: `now < start <= now + horizon`."""
        return self.at < start <= self.at + self.horizon

    def as_dict(self) -> dict:
        return {"at": _iso(self.at),
                "horizon_seconds": self.horizon.total_seconds(),
                "joined": sorted(self.joined),
                "reads": dict(sorted(self.reads.items())),
                "abandoned_after": self.abandoned_after,
                "source": self.source}

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "TickContext | None":
        at = _parse_time(row.get("at"))
        horizon = row.get("horizon_seconds")
        if at is None or not isinstance(horizon, (int, float)):
            return None
        return cls(at=at, horizon=timedelta(seconds=horizon),
                   joined=frozenset(row.get("joined") or ()),
                   reads=dict(row.get("reads") or {}),
                   abandoned_after=row.get("abandoned_after"),
                   source=str(row.get("source") or "recorded"))


class CoverageClass(str, Enum):
    OBSERVED = "observed"
    OBSERVED_OUTSIDE_HORIZON = "observed_outside_horizon"
    OUTSIDE_HORIZON = "outside_observation_horizon"
    NO_TWO_SIDED_BOOK = "no_two_sided_book"
    COLLECTION_FAILURE = "collection_failure"
    AT_OR_AFTER_START = "at_or_after_start_at_tick"
    UNKNOWN = "tick_context_unknown"


def classify_coverage(*, ticker: str, start: datetime | None,
                      tick: TickContext | None, decision_book_present: bool,
                      detected_at: datetime,
                      eligibility: Eligibility) -> dict:
    """Why this contract did or did not have a decision book at the trigger.

    `outside_observation_horizon` is a DESIGN boundary, never a failure:
    the monitor reads books only for games within its horizon of kickoff.
    Only a watched, in-horizon contract without a usable book is a
    `collection_failure`. The screen's own lead-time gate is evaluated at
    the trigger beside it -- through `Eligibility.lead_in_window`, not a copy
    -- so a move the horizon left unobserved says whether the frozen policy
    could have entered it at all.
    """
    minutes = (None if start is None
               else (start - detected_at).total_seconds() / 60.0)
    lead = (None if minutes is None else
            {"minutes_to_start": minutes,
             "bounds": [eligibility.min_minutes_to_start,
                        eligibility.max_minutes_to_start],
             "admits": eligibility.lead_in_window(minutes)})
    out: dict[str, Any] = {
        "decision_book_present": decision_book_present,
        "lead_time_gate_at_trigger": lead,
        "tick": None if tick is None else tick.as_dict()["at"],
        "tick_context": None if tick is None else tick.source,
    }
    if tick is None or start is None:
        out.update(**{"class": CoverageClass.UNKNOWN.value,
                      "collection_failure": None,
                      "note": ("no tick context was recorded or rebuilt, so "
                               "whether a book was due cannot be said")
                      if tick is None else "no kickoff time for this contract"})
        return out
    horizon_hours = tick.horizon.total_seconds() / 3600.0
    to_start = (start - tick.at).total_seconds() / 3600.0
    out.update(horizon_hours=horizon_hours, hours_to_start_at_tick=to_start,
               hours_beyond_horizon=max(0.0, to_start - horizon_hours))
    if start <= tick.at:
        read = TickRead.NOT_ATTEMPTED
        klass = CoverageClass.AT_OR_AFTER_START
        note = "kickoff had passed when the tick began; nothing was due"
    elif not tick.within_horizon(start):
        read = TickRead(tick.reads.get(ticker, TickRead.NOT_ATTEMPTED.value))
        klass = (CoverageClass.OBSERVED_OUTSIDE_HORIZON
                 if decision_book_present else CoverageClass.OUTSIDE_HORIZON)
        gate = ("the screen's lead-time gate refuses it at the trigger as "
                "well, so the horizon cost this move no opportunity"
                if lead and not lead["admits"] else
                "the screen's lead-time gate WOULD have admitted it: the "
                "horizon is narrower than the screen here")
        beyond = to_start - horizon_hours
        note = (f"kickoff was {to_start:.1f}h away, {beyond:.1f}h "
                f"beyond the {horizon_hours:g}h observation horizon: no "
                f"decision read was due, by design, not by failure; {gate}")
    else:
        if ticker not in tick.joined:
            read = TickRead.NOT_JOINED
        elif ticker in tick.reads:
            read = TickRead(tick.reads[ticker])
        elif tick.abandoned_after is not None:
            read = TickRead.ABANDONED
        else:
            read = TickRead.NOT_RECORDED
        if decision_book_present:
            klass = CoverageClass.OBSERVED
            note = ("within the horizon, with a usable decision book"
                    + ("" if read is TickRead.OK else
                       f"; this tick's read was {read.value}, so the book "
                       f"used is an earlier one -- its age says how much"))
        elif read is TickRead.ONE_SIDED:
            klass = CoverageClass.NO_TWO_SIDED_BOOK
            note = ("within the horizon and read, but a side had no resting "
                    "bids: a market state, not a collection failure")
        else:
            klass = CoverageClass.COLLECTION_FAILURE
            note = (f"within the horizon and watched, with no usable decision "
                    f"book: this tick's read was {read.value}. A COLLECTION "
                    f"failure, not a quiet market")
    out.update(**{"class": klass.value, "tick_read": read.value,
                  "within_horizon_at_tick": klass not in (
                      CoverageClass.OUTSIDE_HORIZON,
                      CoverageClass.OBSERVED_OUTSIDE_HORIZON,
                      CoverageClass.AT_OR_AFTER_START),
                  "collection_failure":
                      klass is CoverageClass.COLLECTION_FAILURE,
                  "note": note})
    return out


# --- the record -----------------------------------------------------------

def book_record(book: Any, decided_at: datetime,
                execution: bool = False) -> dict | None:
    """A polled book as a record: prices, depth, and when it was read.

    Timing is a RANGE, because a read describes the book somewhere between
    our request leaving and its answer arriving. A decision book is at
    least `since_received` and at most `since_sent` old at the decision; an
    execution book was read between `sent` and `received` after it.
    """
    if book is None:
        return None
    sent = getattr(book, "sent_at", None)
    row = {
        "bid": book.bid_close, "ask": book.ask_close, "mid": book.mid,
        "spread": book.spread,
        "yes_bid_size": getattr(book, "bid_size", None),
        "no_bid_size": getattr(book, "ask_size", None),
        "yes_levels": getattr(book, "yes_levels", None),
        "no_levels": getattr(book, "no_levels", None),
        "sent_at": _iso(sent), "received_at": _iso(book.ts),
        "read_seconds": _seconds(book.ts - sent) if sent else None,
    }
    if execution:
        row["after_decision_seconds"] = {
            "sent": _seconds(sent - decided_at) if sent else None,
            "received": _seconds(book.ts - decided_at)}
    else:
        row["age_at_decision_seconds"] = {
            "since_received": _seconds(decided_at - book.ts),
            "since_sent": _seconds(decided_at - sent) if sent else None}
    return row


def _depth(book: Any, side: str, *, entering: bool) -> float | None:
    """Contracts resting where this side trades. Entering YES takes the NO
    bids (the YES ask is 1 - best NO bid); entering NO takes the YES bids.
    Exiting reverses both."""
    if book is None:
        return None
    takes_no_bids = (side == "YES") == entering
    return getattr(book, "ask_size" if takes_no_bids else "bid_size", None)


def _unpriced_because(o: Observation, side: str) -> str:
    bid, ask = o.exchange_bid, o.exchange_ask
    if bid is None or ask is None:
        return "no two-sided decision book"
    if bid > ask:
        return f"decision book crossed: bid {bid} > ask {ask}"
    price = ask if side == "YES" else 1.0 - bid
    if not 0.0 < price < 1.0:
        return f"decision price {price} outside (0, 1)"
    return "not priced"


def _execution_missing_because(o: Observation, side: str, book: Any,
                               tolerance: timedelta) -> str:
    if book is None:
        return (f"no usable book read within {tolerance.total_seconds():g}s "
                f"at or after the trigger plus the entry delay: no fill, "
                f"never a fill at the decision price")
    bid, ask = o.entry_bid, o.entry_ask
    if bid is not None and ask is not None and bid > ask:
        return f"execution book crossed: bid {bid} > ask {ask}"
    price = ask if side == "YES" else (None if bid is None else 1.0 - bid)
    return f"execution price {price} outside (0, 1)"


def side_records(o: Observation, eligibility: Eligibility, *, venue: str,
                 role: str, series: str | None, route: str,
                 decision: Any, execution: Any,
                 entry_tolerance: timedelta) -> tuple[dict, list[str]]:
    """Both sides, at the decision book and at execution. Through
    `side_quotes`: once with the delay removed -- which is the decision book
    by `Observation.execution_book`'s own definition -- and once as the
    screen saw it."""
    problems: list[str] = []
    at_decision = replace(o, entry_delay_minutes=0.0, entry_at=None,
                          entry_bid=None, entry_ask=None)
    try:
        decided = {q.side: q for q in side_quotes(at_decision, venue, role,
                                                  series, route)}
    except (FeeScheduleUnresolved, ValueError) as exc:
        decided = {}
        problems.append(f"fee: the decision price could not be charged: {exc}")
    delayed = o.entry_delay_minutes > 0.0
    executed = decided
    if delayed:
        try:
            executed = {q.side: q for q in side_quotes(o, venue, role, series,
                                                       route)}
        except (FeeScheduleUnresolved, ValueError) as exc:
            executed = {}
            problems.append(f"fee: the execution price could not be charged: "
                            f"{exc}")
    out: dict[str, dict] = {}
    for side in ("YES", "NO"):
        price = (o.exchange_ask if side == "YES" else
                 None if o.exchange_bid is None else 1.0 - o.exchange_bid)
        win = o.p_sharp if side == "YES" else 1.0 - o.p_sharp
        row: dict[str, Any] = {
            "decision_price": price, "win_probability": win,
            "gross_edge": None if price is None else win - price,
            "depth_at_decision": _depth(decision, side, entering=True)}
        quote = decided.get(side)
        if quote is None:
            row.update(priced=False,
                       not_priced_because=_unpriced_because(o, side))
        else:
            margin = quote.predicted_ev - eligibility.min_net_ev
            row.update(priced=True, fee=quote.fee, fee_raw=quote.fee_raw,
                       fee_route=quote.fee_route, net_ev=quote.predicted_ev,
                       margin_to_floor=margin,
                       clears_floor=eligibility.clears_ev_floor(
                           quote.predicted_ev))
        filled = executed.get(side)
        if quote is not None and not delayed:
            row["execution"] = {
                "price": quote.entry_price, "fee": quote.fee,
                "paid": quote.paid, "latency_cost": 0.0,
                "depth": row["depth_at_decision"],
                "note": "zero delay: the execution book IS the decision book"}
        elif quote is not None and filled is not None:
            row["execution"] = {
                "price": filled.exec_price, "fee": filled.exec_fee,
                "paid": filled.paid, "latency_cost": filled.paid - quote.cost,
                "depth": _depth(execution, side, entering=True)}
        else:
            row["execution"] = None
            row["execution_missing_because"] = (
                "the side was not priced at the decision"
                if quote is None else
                _execution_missing_because(o, side, execution, entry_tolerance))
        out[side] = row
    return out, problems


def _gates(o: Observation | None, eligibility: Eligibility,
           minutes: float | None, best_ev: float | None) -> dict:
    """Every gate's value and whether it passes, each through the gate's own
    method. The REJECTION is the verdict's -- the first gate in its order --
    and this is only what the others would have said."""
    gates: dict[str, Any] = {}
    if o is not None:
        gates["price_band"] = {"value": o.p_exchange,
                               "bounds": list(eligibility.price_band),
                               "passes": eligibility.price_in_band(o.p_exchange)}
    if minutes is not None:
        gates["lead_time"] = {"minutes_to_start": minutes,
                              "bounds": [eligibility.min_minutes_to_start,
                                         eligibility.max_minutes_to_start],
                              "passes": eligibility.lead_in_window(minutes)}
    if o is not None:
        present = eligibility.quotes_present(o.exchange_bid, o.exchange_ask)
        gates["decision_quotes"] = {"bid": o.exchange_bid,
                                    "ask": o.exchange_ask, "passes": present}
        if present:
            gates["spread"] = {"value": o.exchange_ask - o.exchange_bid,
                               "max": eligibility.max_spread,
                               "passes": eligibility.spread_ok(o.exchange_bid,
                                                               o.exchange_ask)}
    if best_ev is not None:
        gates["net_ev"] = {"best": best_ev, "floor": eligibility.min_net_ev,
                           "passes": eligibility.clears_ev_floor(best_ev)}
    return gates


def describe(decision: LiveDecision, trigger: MoveTrigger, books: Sequence, *,
             yes_is_home: bool, start: datetime | None, entry_delay: timedelta,
             entry_tolerance: timedelta, eligibility: Eligibility,
             series: str | None, venue: str = "kalshi", role: str = "taker",
             route: str | None = None, tick: TickContext | None = None) -> dict:
    """The assessment record for one screened (or unscreenable) contract."""
    route = route or DEFAULT_ROUTE
    decided_at = trigger.detected_at
    # The screen's own rule, on the screen's own inputs: the same two books
    # `observation_at` priced.
    dbook, ebook = screen_books(books, decided_at, entry_delay,
                                entry_tolerance)
    move_for_yes = trigger.delta_for(yes_is_home)
    minutes = (None if start is None
               else (start - decided_at).total_seconds() / 60.0)
    o = decision.observation
    missing: list[str] = []
    sides, best = None, None
    if o is not None:
        sides, problems = side_records(
            o, eligibility, venue=venue, role=role, series=series, route=route,
            decision=dbook, execution=ebook, entry_tolerance=entry_tolerance)
        missing += problems
        priced = [(s, r) for s, r in sides.items() if r.get("priced")]
        if priced:
            best = max(priced, key=lambda item: item[1]["net_ev"])
    else:
        missing.append(f"screen: nothing to screen -- {decision.detail}")
    if dbook is None:
        missing.append("decision_book: no usable book at or before the trigger "
                       "(the coverage class says why)")
    if entry_delay > timedelta(0) and ebook is None:
        missing.append("execution_book: no usable book within the entry "
                       "tolerance of the execution read")
    if start is None:
        missing.append("kickoff: unknown, so lead time is undefined")

    provenance = fee_provenance(series, decided_at, route)
    sensitivity = None
    if best is not None:
        side, row = best
        sensitivity = fee_sensitivity(
            row["decision_price"], row["gross_edge"], eligibility.min_net_ev,
            in_force=provenance["multiplier"],
            in_force_label=("as priced: generic coefficient, ASSUMED"
                            if provenance["basis"] == "generic_coefficient"
                            else "as priced: " + provenance["basis"]))
        sensitivity["side"] = side
    verdict = decision.verdict
    return {
        "schema": SCHEMA,
        "move": {
            "event_id": trigger.event_id, "stream_id": trigger.stream_id,
            "detected_at": _iso(decided_at),
            "provider_observed_at": _iso(trigger.provider_observed_at),
            "fair_before_yes": (trigger.fair_before_home if yes_is_home
                                else trigger.fair_before_away),
            "fair_after_yes": (trigger.fair_after_home if yes_is_home
                               else trigger.fair_after_away),
            "move_for_yes": move_for_yes,
            "favoured_side": "YES" if move_for_yes > 0 else "NO",
            "devig_method": trigger.policy.devig_method,
            "age_at_decision_seconds": trigger.age_at_decision_seconds,
            "near_threshold_under_devig_disagreement":
                trigger.near_threshold_under_devig_disagreement,
        },
        "contract": {"market_ticker": decision.market_ticker,
                     "yes_is_home": yes_is_home, "start": _iso(start),
                     "minutes_to_start": minutes},
        "coverage": classify_coverage(
            ticker=decision.market_ticker, start=start, tick=tick,
            decision_book_present=dbook is not None, detected_at=decided_at,
            eligibility=eligibility),
        "decision_book": book_record(dbook, decided_at),
        "execution_book": None if ebook is None else {
            **book_record(ebook, decided_at, execution=True),
            "entry_delay_seconds": entry_delay.total_seconds(),
            "tolerance_seconds": entry_tolerance.total_seconds()},
        "screen": {
            "outcome": ("admitted" if decision.admitted else
                        "rejected" if o is not None else "not_screened"),
            "refusal": decision.refusal.value if decision.refusal else None,
            "rejection": (verdict.rejection.value
                          if verdict and verdict.rejection else None),
            "admitted": decision.admitted,
            "best_side": best[0] if best else None,
            "best_net_ev": best[1]["net_ev"] if best else None,
            "best_margin_to_floor": best[1]["margin_to_floor"] if best else None,
            "gates": _gates(o, eligibility, minutes,
                            best[1]["net_ev"] if best else None),
            "eligibility": {**vars(eligibility),
                            "price_band": list(eligibility.price_band)},
        },
        "sides": sides,
        "fees": {"provenance": provenance, "sensitivity": sensitivity},
        "missing": missing,
    }


def assess(trigger: MoveTrigger, books: Sequence, *, market_ticker: str,
           yes_is_home: bool, start: datetime | None, entry_delay: timedelta,
           entry_tolerance: timedelta, series: str | None,
           eligibility: Eligibility | None = None, venue: str = "kalshi",
           role: str = "taker", route: str | None = None,
           tick: TickContext | None = None) -> tuple[LiveDecision, dict]:
    """`screen_live`'s decision, unchanged, and the record of why."""
    eligibility = eligibility or Eligibility()
    decision = screen_live(
        trigger, books, market_ticker=market_ticker, yes_is_home=yes_is_home,
        start=start, entry_delay=entry_delay, entry_tolerance=entry_tolerance,
        eligibility=eligibility, series=series, venue=venue, role=role,
        route=route)
    return decision, describe(
        decision, trigger, books, yes_is_home=yes_is_home, start=start,
        entry_delay=entry_delay, entry_tolerance=entry_tolerance,
        eligibility=eligibility, series=series, venue=venue, role=role,
        route=route, tick=tick)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None
