"""Offline re-analysis of a recorded shadow session: every assessment, rebuilt.

    python3 shadow_monitor.py --report SESSION.jsonl                 # text
    python3 shadow_monitor.py --report SESSION.jsonl --json OUT.json # + JSON

WHAT IT DOES
------------
Walks a session file in the order the monitor wrote it and rebuilds, for
every decision, exactly what the monitor held at that instant: the books in
its memory (the same parser, the same two-hour memory), the join in force,
the trigger, the measured entry delay, and the tick the move arrived on. It
then runs the monitor's own screen and `reaction.assessment` on those inputs
and checks the result against the decision the monitor RECORDED. A session
recorded before assessments existed gets them this way, and a session
recorded after gets them checked -- a disagreement is printed, never
resolved in favour of either side.

On top of each assessment, and kept apart from it:

* SETTLEMENT VALUE -- the frozen screen's predicted EV, per assessment and
  in aggregate. Realized figures only for admitted entries, and only with
  `--settlements`; otherwise withheld, never defaulted.
* ADJUSTMENT CAPTURE -- `reaction.adjustment`: executable round trips at the
  declared markouts, as a hypothetical scenario for every assessment and as
  `screen_admitted` for entries the screen took.
* THE SHARP PATH -- whether each move persisted, reversed or became
  unobservable, from the polls that had been read at each markout.
* HORIZON AND COVERAGE -- whether a missing book was never due (outside the
  observation horizon), a market state, or a collection failure.

WHAT IT MAY NOT DO
------------------
Touch the network. The whole analysis runs inside `no_network()`, which
replaces every way this process could open a connection with one that
raises. An analysis that quietly re-fetched a book would no longer be an
analysis of the session.
"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import statistics
import sys
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from analysis.scoring import Eligibility                          # noqa: E402
from core.fees import DEFAULT_ROUTE, fee_provenance                # noqa: E402
from data.kalshi_history import parse_orderbook                    # noqa: E402
from reaction.adjustment import (                                  # noqa: E402
    FILL_LIMITS, HYPOTHETICAL, SCREEN_ADMITTED, CapturePolicy, Read,
    SharpReading, capture, summarize,
)
from reaction.assessment import (                                  # noqa: E402
    TickContext, TickRead, assess, read_status,
)
from reaction.detector import (                                    # noqa: E402
    MovePolicy, MoveTrigger, fair_probabilities,
)
from reaction.measure import request_span                          # noqa: E402
from shadow_monitor import (                                       # noqa: E402
    BOOK_HORIZON, BOOK_MEMORY, ENTRY_TOLERANCE, _time, code_version,
    parse_odds_row,
)

SCHEMA = "shadow-diagnostics/1"


class OfflineViolation(RuntimeError):
    """The offline analysis tried to open a connection."""


@contextmanager
def no_network() -> Iterator[None]:
    """Every way this process could reach a network, refused while inside.

    Replaced rather than trusted: the analysis imports the same modules the
    live monitor does, and one convenience re-fetch anywhere in them would
    turn a re-analysis into a new, unrecorded collection.
    """
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OfflineViolation("the offline re-analysis attempted a network "
                               "connection; it must read only the session "
                               "file")

    targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"),
               (socket, "create_connection"), (socket, "getaddrinfo"),
               (urllib.request, "urlopen")]
    saved = [(obj, name, getattr(obj, name)) for obj, name in targets]
    try:
        for obj, name, _ in saved:
            setattr(obj, name, refuse)
        yield
    finally:
        for obj, name, original in saved:
            setattr(obj, name, original)


# --- rebuilding the monitor's objects from its records ---------------------

def move_policy_from_record(row: dict | None) -> MovePolicy:
    row = row or {}
    base = MovePolicy()

    def span(key: str, fallback: timedelta) -> timedelta:
        value = row.get(key)
        return (timedelta(seconds=value)
                if isinstance(value, (int, float)) else fallback)

    return MovePolicy(
        devig_method=row.get("devig_method", base.devig_method),
        min_move=row.get("min_move", base.min_move),
        max_gap=span("max_gap_seconds", base.max_gap),
        max_age_at_decision=span("max_age_at_decision_seconds",
                                 base.max_age_at_decision),
        debounce=span("debounce_seconds", base.debounce),
        require_content_age=row.get("require_content_age",
                                    base.require_content_age),
        book=row.get("book", base.book),
        clock_skew_tolerance=span("clock_skew_tolerance_seconds",
                                  base.clock_skew_tolerance),
        label=row.get("label", base.label))


def trigger_from_record(row: dict) -> MoveTrigger:
    """A `trigger` record back into the detector's object, field for field."""
    bracket = row.get("book_change_bracket") or {}
    before, after = row.get("fair_before") or {}, row.get("fair_after") or {}
    delta, overround = row.get("delta") or {}, row.get("overround") or {}
    return MoveTrigger(
        event_id=str(row.get("event_id")), stream_id=str(row.get("stream_id")),
        detected_at=_time(row.get("detected_at")),
        provider_observed_at=_time(row.get("provider_observed_at")),
        book_change_earliest=_time(bracket.get("earliest")),
        book_change_latest=_time(bracket.get("latest")),
        provider_to_available_seconds=row.get("provider_to_available_seconds"),
        age_at_decision_seconds=row.get("age_at_decision_seconds"),
        capture_age_seconds=row.get("capture_age_seconds"),
        fair_before_away=before.get("away"), fair_before_home=before.get("home"),
        fair_after_away=after.get("away"), fair_after_home=after.get("home"),
        delta_away=delta.get("away"), delta_home=delta.get("home"),
        overround_before=overround.get("before"),
        overround_after=overround.get("after"),
        devig_disagreement=row.get("devig_disagreement"),
        policy=move_policy_from_record(row.get("policy")),
        before_provenance=row.get("before") or {},
        after_provenance=row.get("after") or {})


def eligibility_from_record(row: dict | None) -> tuple[Eligibility, str]:
    """The screen the SESSION ran, not today's defaults, when it said."""
    if not isinstance(row, dict) or not row:
        return Eligibility(), "not recorded: today's defaults"
    fields = dict(row)
    if isinstance(fields.get("price_band"), list):
        fields["price_band"] = tuple(fields["price_band"])
    known = set(vars(Eligibility()))
    return (Eligibility(**{k: v for k, v in fields.items() if k in known}),
            "recorded by the session")


@dataclass
class _Tick:
    at: datetime | None
    joined: frozenset
    reads: dict = field(default_factory=dict)
    abandoned_after: str | None = None
    odds_seen: bool = False

    def context(self, horizon: timedelta) -> TickContext | None:
        if self.at is None:
            return None
        return TickContext(at=self.at, horizon=horizon, joined=self.joined,
                           reads=dict(self.reads),
                           abandoned_after=self.abandoned_after,
                           source="reconstructed_from_file_order")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        if a is None or b is None:
            return a is b
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def _agreement(recorded: dict, decision: Any) -> dict:
    """The recomputed decision against the one the monitor wrote down."""
    now = decision.as_dict()
    diffs = []

    def check(name: str, was: Any, is_: Any) -> None:
        if not _same(was, is_):
            diffs.append({"field": name, "recorded": was, "recomputed": is_})

    check("refusal", recorded.get("refusal"), now["refusal"])
    check("admitted", bool(recorded.get("admitted")), now["admitted"])
    if "rejection" in recorded:
        check("rejection", recorded.get("rejection"), now["rejection"])
    was_book, is_book = recorded.get("book") or {}, now.get("book") or {}
    for key in ("decision_bid", "decision_ask", "entry_bid", "entry_ask",
                "p_sharp", "minutes_to_start"):
        check(f"book.{key}", was_book.get(key), is_book.get(key))
    was_pred = recorded.get("predicted") or {}
    is_pred = now.get("predicted") or {}
    for key in ("side", "entry_price", "fee", "predicted_ev_per_contract",
                "paid_at_execution"):
        check(f"predicted.{key}", was_pred.get(key), is_pred.get(key))
    return {"agrees": not diffs, "differences": diffs}


def _tick_agreement(recorded: TickContext | None,
                    rebuilt: TickContext | None) -> dict | None:
    if recorded is None or rebuilt is None:
        return None
    diffs = []
    for name in ("joined", "abandoned_after"):
        if getattr(recorded, name) != getattr(rebuilt, name):
            diffs.append(name)
    if dict(recorded.reads) != dict(rebuilt.reads):
        diffs.append("reads")
    if abs((recorded.at - rebuilt.at).total_seconds()) > 1.0:
        diffs.append("at")
    return {"agrees": not diffs, "differences": diffs}


# --- the walk ----------------------------------------------------------------

@dataclass
class Replay:
    """Everything the walk rebuilt, before any summary is drawn from it."""

    start: dict
    end: dict | None
    series: str
    eligibility: Eligibility
    eligibility_source: str
    route: str
    route_source: str
    horizon: timedelta
    tolerance: timedelta
    triggers: list = field(default_factory=list)
    assessments: list = field(default_factory=list)
    not_joined: list = field(default_factory=list)
    reads: dict = field(default_factory=dict)
    odds: list = field(default_factory=list)
    last_seen: datetime | None = None
    #: The monitor's book memory as the walk left it -- exposed so a test
    #: can hold it against the live monitor's own.
    memory: dict = field(default_factory=dict)


def replay(rows: Sequence[dict]) -> Replay:
    """Walk the records in the order the monitor wrote them."""
    start = next((r for r in rows if r.get("kind") == "session_start"), {})
    end = next((r for r in reversed(rows) if r.get("kind") == "session_end"),
               None)
    eligibility, eligibility_source = eligibility_from_record(
        start.get("eligibility"))
    recorded_route = start.get("fee_route")
    out = Replay(
        start=start, end=end, series=start.get("series") or "KXNFLGAME",
        eligibility=eligibility, eligibility_source=eligibility_source,
        route=recorded_route or DEFAULT_ROUTE,
        route_source=("recorded by the session" if recorded_route else
                      "not recorded: the monitor's default route, which "
                      "every decision it made was priced on"),
        horizon=timedelta(seconds=start.get("book_horizon_seconds")
                          or BOOK_HORIZON.total_seconds()),
        tolerance=timedelta(seconds=start.get("entry_tolerance_seconds")
                            or ENTRY_TOLERANCE.total_seconds()))
    memory_span = timedelta(seconds=start.get("book_memory_seconds")
                            or BOOK_MEMORY.total_seconds())
    memory: dict[str, list] = {}
    join: dict[str, dict] = {}
    tick: _Tick | None = None
    trigger: MoveTrigger | None = None
    trigger_row: dict | None = None
    for index, row in enumerate(rows):
        kind = row.get("kind")
        for key in ("received_at", "at"):
            moment = _time(row.get(key))
            if moment is not None and (out.last_seen is None
                                       or moment > out.last_seen):
                out.last_seen = moment
        if kind == "book":
            ticker, received = row.get("ticker"), _time(row.get("received_at"))
            if not ticker or received is None:
                continue
            sent = _time(row.get("sent_at"))
            payload = row.get("payload")
            book = None
            if payload is not None:
                book, _ = parse_orderbook(payload, ticker=ticker,
                                          received_at=received, sent_at=sent)
            status = read_status(payload is not None, book)
            span = request_span(sent, received)
            out.reads.setdefault(ticker, []).append(
                Read(span[0], span[1], status.value, book))
            if book is not None:
                # `read_book`'s memory, exactly: appended, then pruned
                # against the newest receipt.
                history = memory.setdefault(ticker, [])
                history.append(book)
                memory[ticker] = [b for b in history
                                  if b.ts >= received - memory_span]
            if row.get("purpose") == "decision":
                if tick is None or tick.odds_seen:
                    tick = _Tick(at=sent or received, joined=frozenset(join))
                tick.reads[ticker] = status.value
        elif kind == "decision_reads_abandoned":
            if tick is not None:
                tick.abandoned_after = row.get("after")
        elif kind == "odds":
            if tick is None or tick.odds_seen:
                tick = _Tick(at=_time(row.get("sent_at"))
                             or _time(row.get("received_at")),
                             joined=frozenset(join))
            tick.odds_seen = True
            out.odds.append(row)
        elif kind == "join":
            join = dict(row.get("contracts") or {})
        elif kind == "trigger":
            trigger, trigger_row = trigger_from_record(row), row
            out.triggers.append({"index": index, "trigger": trigger,
                                 "row": row})
        elif kind == "decision":
            ticker = row.get("market_ticker")
            if not ticker:
                out.not_joined.append({
                    "event_id": row.get("event_id"),
                    "stream_id": row.get("stream_id"),
                    "decision_at": row.get("decision_at")
                    or (trigger_row or {}).get("detected_at"),
                    "refusal": row.get("refusal")})
                continue
            out.assessments.append(_reassess(
                row, index, trigger, trigger_row, join, memory, tick, out))
    out.memory = memory
    return out


def _reassess(row: dict, index: int, trigger: MoveTrigger | None,
              trigger_row: dict | None, join: dict, memory: dict,
              tick: _Tick | None, out: Replay) -> dict:
    ticker = row["market_ticker"]
    item: dict[str, Any] = {"index": index, "ticker": ticker,
                            "event_id": row.get("event_id"),
                            "entered": bool(row.get("entered")),
                            "recorded": row}
    if (trigger is None or trigger_row is None
            or trigger_row.get("stream_id") != row.get("stream_id")
            or _time(trigger_row.get("detected_at"))
            != _time(row.get("decision_at"))):
        item["error"] = ("no trigger record precedes this decision; it "
                         "cannot be recomputed")
        return item
    contract = join.get(ticker) or {}
    start = _time(contract.get("start"))
    yes_is_home = row.get("yes_is_home")
    if not isinstance(yes_is_home, bool):
        yes_is_home = bool(contract.get("yes_is_home"))
    delay = timedelta(seconds=float(row.get("entry_delay_seconds") or 0.0))
    rebuilt = tick.context(out.horizon) if tick is not None else None
    recorded_tick = (TickContext.from_dict(row["tick"])
                     if isinstance(row.get("tick"), dict) else None)
    decision, assessment = assess(
        trigger, list(memory.get(ticker, [])), market_ticker=ticker,
        yes_is_home=yes_is_home, start=start, entry_delay=delay,
        entry_tolerance=out.tolerance, series=out.series,
        eligibility=out.eligibility, route=out.route,
        tick=recorded_tick or rebuilt)
    item.update(trigger=trigger, start=start, yes_is_home=yes_is_home,
                decision=decision, assessment=assessment,
                agreement=_agreement(row, decision),
                tick_agreement=_tick_agreement(recorded_tick, rebuilt))
    return item


# --- the sharp path ---------------------------------------------------------

def parsed_polls(odds_rows: Sequence[dict]) -> list[tuple]:
    """Every recorded poll as (ready, received, parsed answer or None),
    parsed once: a 24-hour session has thousands, and every moved game
    reads all of them."""
    out = []
    for row in odds_rows:
        ready = _time(row.get("ready_at")) or _time(row.get("received_at"))
        if ready is not None:
            out.append((ready, _time(row.get("received_at")),
                        parse_odds_row(row)))
    return out


def sharp_readings(polls: Sequence[tuple], event_id: str,
                   method: str) -> list[SharpReading]:
    """The sharp book's state for one game after every recorded poll."""
    out = []
    for ready, received, parsed in polls:
        if parsed is None:
            out.append(SharpReading(ready, "poll_unanswered"))
            continue
        quote = next((q for q in parsed.quotes
                      if q.provider_event_id == event_id), None)
        if quote is None:
            out.append(SharpReading(ready, "absent_from_answer"))
        elif received is not None and quote.commence_time <= received:
            out.append(SharpReading(ready, "in_play"))
        else:
            fair = fair_probabilities(quote.away_price, quote.home_price,
                                      method)
            out.append(SharpReading(ready, "undeviggable") if fair is None
                       else SharpReading(ready, "answered", fair_home=fair[1],
                                         fair_away=fair[0],
                                         observed_at=quote.last_update))
    return out


def _opposite_moves(replayed: Replay, item: dict,
                    within: timedelta) -> list[dict]:
    trigger: MoveTrigger = item["trigger"]
    mine = trigger.delta_for(item["yes_is_home"])
    out = []
    for other in replayed.triggers:
        later: MoveTrigger = other["trigger"]
        if (later.event_id != trigger.event_id
                or not trigger.detected_at < later.detected_at
                <= trigger.detected_at + within):
            continue
        theirs = later.delta_for(item["yes_is_home"])
        if theirs * mine < 0:
            out.append({"detected_at": later.detected_at.isoformat(),
                        "after_seconds": (later.detected_at
                                          - trigger.detected_at).total_seconds(),
                        "move_for_yes": theirs})
    return out


# --- the analysis -------------------------------------------------------------

def diagnose(rows: Sequence[dict], *,
             capture_policy: CapturePolicy | None = None,
             settlements: dict[str, int] | None = None,
             source: dict | None = None) -> dict:
    """Every assessment in a session, rebuilt, checked and summarised. Pure."""
    policy = capture_policy or CapturePolicy()
    replayed = replay(rows)
    session_end = (_time(replayed.end.get("at")) if replayed.end
                   else replayed.last_seen)
    polls = parsed_polls(replayed.odds)
    readings: dict[str, list[SharpReading]] = {}
    items = []
    capture_rows = []
    for item in replayed.assessments:
        if "assessment" not in item:
            items.append(_public(item))
            continue
        trigger: MoveTrigger = item["trigger"]
        method = trigger.policy.devig_method
        key = f"{trigger.event_id}|{method}"
        if key not in readings:
            readings[key] = sharp_readings(polls, trigger.event_id, method)
        assessment = item["assessment"]
        move = assessment["move"]
        sharp = dict(readings=readings[key], detected_at=trigger.detected_at,
                     yes_is_home=item["yes_is_home"],
                     fair_before_yes=move["fair_before_yes"],
                     fair_after_yes=move["fair_after_yes"],
                     min_move=trigger.policy.min_move, session_end=session_end)
        reads = replayed.reads.get(item["ticker"], [])
        scenarios = [capture(
            kind=HYPOTHETICAL, side=move["favoured_side"], reads=reads,
            detected_at=trigger.detected_at, start=item["start"],
            session_end=session_end, series=replayed.series, policy=policy,
            route=replayed.route, sharp=sharp,
            why=("the side the sharp move favoured, entered at the first "
                 "usable read after the move whether or not the screen "
                 "admitted it: was there room behind the move?"))]
        decision = item["decision"]
        if decision.admitted and decision.trade is not None:
            scenarios.append(capture(
                kind=SCREEN_ADMITTED, side=decision.trade.side, reads=reads,
                detected_at=trigger.detected_at, start=item["start"],
                session_end=session_end, series=replayed.series,
                policy=policy, route=replayed.route, sharp=sharp,
                why="the side the frozen screen admitted"))
        for scenario in scenarios:
            capture_rows.append((item["event_id"] or trigger.event_id,
                                 scenario))
        public = _public(item)
        public["adjustment_capture"] = scenarios
        public["opposite_moves"] = _opposite_moves(replayed, item,
                                                   policy.markouts[-1])
        public["settlement"] = _settlement(item, settlements)
        items.append(public)
    items.sort(key=lambda a: (a.get("assessment", {}).get("move", {})
                              .get("detected_at") or "", a["ticker"]))
    return {
        "schema": SCHEMA,
        "source": source or {},
        "network": "none: the analysis ran inside no_network()",
        "session": _session(replayed, session_end),
        "coverage": _coverage(replayed, items),
        "settlement_value": _settlement_value(replayed, items, settlements),
        "adjustment_capture": {
            "policy": policy.as_dict(),
            "limits": list(FILL_LIMITS),
            "summary": summarize(capture_rows, policy),
            "note": ("hypothetical rows are NOT entries: they ask whether "
                     "the move left executable room, on every assessment. "
                     "Only screen_admitted rows are the frozen policy's "
                     "own entries"),
        },
        "fees": _fees(replayed),
        "assessments": items,
    }


def _public(item: dict) -> dict:
    """An assessment item without the in-memory objects."""
    out = {k: v for k, v in item.items()
           if k not in ("trigger", "decision", "recorded", "start")}
    if "decision" in item:
        out["decision"] = item["decision"].as_dict()
    out["recorded_decision_had_assessment"] = isinstance(
        item["recorded"].get("assessment"), dict)
    return out


def _settlement(item: dict, settlements: dict[str, int] | None) -> dict:
    decision = item["decision"]
    if not decision.admitted or decision.trade is None:
        return {"status": "not_applicable",
                "because": "the frozen screen did not admit this assessment"}
    settled = (settlements or {}).get(item["ticker"])
    if settled not in (0, 1):
        return {"status": "withheld",
                "because": "settlement was not supplied (--settlements); a "
                           "realized figure from a placeholder is "
                           "indistinguishable from a real one"}
    trade = decision.trade
    payout = float(settled if trade.side == "YES" else 1 - settled)
    return {"status": "realized", "settled_yes": settled,
            "side": trade.side, "paid": trade.paid, "payout": payout,
            "profit": payout - trade.paid}


def _session(replayed: Replay, session_end: datetime | None) -> dict:
    start, end = replayed.start, replayed.end
    return {
        "recorded_by": start.get("code") or {"commit": None, "dirty": None,
                                             "note": "not recorded"},
        "analysed_by": code_version(),
        "started": start.get("at"),
        "ended": end.get("at") if end else None,
        "analysed_through": session_end.isoformat() if session_end else None,
        "stop": (end.get("reason") if end else
                 "no session_end record: the analysis runs to the last record"),
        "series": replayed.series,
        "cadence_seconds": start.get("cadence_seconds"),
        "book_horizon_hours": replayed.horizon.total_seconds() / 3600.0,
        "entry_tolerance_seconds": replayed.tolerance.total_seconds(),
        "eligibility": {**vars(replayed.eligibility),
                        "price_band": list(replayed.eligibility.price_band)},
        "eligibility_source": replayed.eligibility_source,
        "fee_route": replayed.route,
        "fee_route_source": replayed.route_source,
        "assessment_errors_live": ((end or {}).get("counts") or {}).get(
            "assessment_errors"),
    }


def _coverage(replayed: Replay, items: Sequence[dict]) -> dict:
    assessed = [a for a in items if "assessment" in a]
    classes = Counter(a["assessment"]["coverage"]["class"] for a in assessed)
    screened = [a for a in assessed if a["decision"]["refusal"] is None]
    rejections = Counter(a["decision"]["rejection"] or "admitted"
                         for a in screened)
    refusals = Counter(a["decision"]["refusal"] for a in assessed
                       if a["decision"]["refusal"] is not None)
    disagreements = [{"ticker": a["ticker"], **a["agreement"]}
                     for a in assessed if not a["agreement"]["agrees"]]
    tick_disagreements = [{"ticker": a["ticker"], **a["tick_agreement"]}
                          for a in assessed if a.get("tick_agreement")
                          and not a["tick_agreement"]["agrees"]]
    within = sum(1 for a in assessed
                 if a["assessment"]["coverage"].get("within_horizon_at_tick"))
    return {
        "moves": len(replayed.triggers),
        "games": len({t["trigger"].event_id for t in replayed.triggers}),
        "moves_on_no_joined_contract": len(replayed.not_joined),
        "contract_assessments": len(items),
        "within_horizon": within,
        "outside_horizon": classes.get("outside_observation_horizon", 0)
        + classes.get("observed_outside_horizon", 0),
        "collection_failures": classes.get("collection_failure", 0),
        "by_class": dict(sorted(classes.items())),
        "usable": len(screened),
        "screen": dict(sorted(rejections.items())),
        "refusals": dict(sorted(refusals.items())),
        "admitted": sum(1 for a in screened if a["decision"]["admitted"]),
        "entries": sum(1 for a in items if a.get("entered")),
        "not_recomputable": sum(1 for a in items if "error" in a),
        "reproduced": len(assessed) - len(disagreements),
        "disagreements": disagreements,
        "tick_disagreements": tick_disagreements,
    }


def _settlement_value(replayed: Replay, items: Sequence[dict],
                      settlements: dict[str, int] | None) -> dict:
    screened = [a for a in items if "assessment" in a
                and a["assessment"]["screen"]["best_net_ev"] is not None]
    evs = [a["assessment"]["screen"]["best_net_ev"] for a in screened]
    margins = [a["assessment"]["screen"]["best_margin_to_floor"]
               for a in screened]
    realized = [a["settlement"] for a in items
                if a.get("settlement", {}).get("status") == "realized"]
    closest = max(screened, key=lambda a: a["assessment"]["screen"]
                  ["best_margin_to_floor"], default=None)
    return {
        "question": ("the frozen checkpoint screen: is the sharp probability "
                     "far enough above the executable price, after the fee, "
                     "to hold to settlement?"),
        "floor": replayed.eligibility.min_net_ev,
        "priced": len(screened),
        "admitted": sum(1 for a in screened if a["decision"]["admitted"]),
        "best_net_ev": _stats(evs),
        "best_margin_to_floor": _stats(margins),
        "closest_to_floor": None if closest is None else {
            "ticker": closest["ticker"],
            "side": closest["assessment"]["screen"]["best_side"],
            "margin": closest["assessment"]["screen"]["best_margin_to_floor"]},
        "realized": ({"status": "realized", "entries": realized,
                      "profit_total": sum(r["profit"] for r in realized)}
                     if realized else
                     {"status": "withheld" if not settlements else "none",
                      "because": ("no admitted entry" if settlements else
                                  "settlement was not supplied; pass "
                                  "--settlements to attach it")}),
    }


def _fees(replayed: Replay) -> dict:
    at = _time(replayed.start.get("at"))
    provenance = fee_provenance(replayed.series, at, replayed.route)
    return {"provenance": provenance, "unresolved": provenance["unresolved"],
            "verify": ("python3 verify_fees.py --series "
                       f"{replayed.series} --window "
                       f"{replayed.start.get('at')} "
                       f"{(replayed.end or {}).get('at') or ''}").strip()}


def _stats(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    return {"n": len(values), "mean": statistics.fmean(values),
            "median": statistics.median(values), "min": min(values),
            "max": max(values)}


def source_of(path: Path, rows: int, bad: int) -> dict:
    """What was analysed: the file by name and content hash -- never its
    directory, which on the owner's machine is a private path."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"file": path.name, "sha256": digest, "records": rows,
            "unreadable_lines": bad}


# --- the text ---------------------------------------------------------------

def _f(value: Any, digits: int = 4, signed: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"


def _hhmm(value: str | None) -> str:
    moment = _time(value)
    return moment.strftime("%Y-%m-%d %H:%M:%SZ") if moment else "?"


def render_diagnostics(figures: dict) -> str:
    s, c = figures["session"], figures["coverage"]
    e = s["eligibility"]
    lines = ["", "SHADOW DIAGNOSTICS  (offline: no network request was made)"]
    source = figures.get("source") or {}
    if source:
        lines.append(f"  source             {source.get('file')}  "
                     f"({source.get('records'):,} records, "
                     f"{source.get('unreadable_lines')} unreadable; "
                     f"sha256 {str(source.get('sha256'))[:12]}...)")
    def commit(code: dict) -> str:
        if not code.get("commit"):
            return "unknown"
        return (f"{code['commit'][:12]}"
                + (" (with local edits)" if code.get("dirty") else ""))

    lines += [
        f"  code               recorded by {commit(s['recorded_by'])}; "
        f"analysed by {commit(s['analysed_by'])}",
        f"  screen (frozen)    min net EV {e['min_net_ev']:+.4f}, price band "
        f"{e['price_band'][0]:g}-{e['price_band'][1]:g}, lead "
        f"{e['min_minutes_to_start']:g}-{e['max_minutes_to_start']:g} min, "
        f"spread <= {e['max_spread']:g}  ({s['eligibility_source']})",
        f"  book horizon       {s['book_horizon_hours']:g}h before kickoff",
        f"  fee route          {s['fee_route']}  ({s['fee_route_source']})",
        "", "COVERAGE",
        f"  moves detected                  {c['moves']:>4}   across "
        f"{c['games']} game(s)",
        f"  moves on no joined contract     {c['moves_on_no_joined_contract']:>4}",
        f"  contract assessments            {c['contract_assessments']:>4}",
        f"    within the book horizon       {c['within_horizon']:>4}",
        f"    outside the book horizon      {c['outside_horizon']:>4}   "
        f"(by design, not a collection failure)",
        f"    collection failures           {c['collection_failures']:>4}"]
    for klass, n in c["by_class"].items():
        lines.append(f"      {klass:<32}{n:>4}")
    lines.append(f"  usable (screened)               {c['usable']:>4}")
    for key, n in c["screen"].items():
        lines.append(f"    {key:<34}{n:>4}")
    for key, n in c["refusals"].items():
        lines.append(f"  not screenable: {key:<22}{n:>4}")
    lines.append(f"  shadow entries                  {c['entries']:>4}")
    lines.append(f"  recorded decisions reproduced   {c['reproduced']:>4} of "
                 f"{c['contract_assessments'] - c['not_recomputable']}")
    for diff in c["disagreements"]:
        lines.append(f"  *** {diff['ticker']}: the recomputed decision "
                     f"DISAGREES with the recorded one: "
                     f"{[d['field'] for d in diff['differences']]}")
    for diff in c["tick_disagreements"]:
        lines.append(f"  *** {diff['ticker']}: the tick rebuilt from the "
                     f"file disagrees with the recorded one: "
                     f"{diff['differences']}")
    if c["not_recomputable"]:
        lines.append(f"  *** {c['not_recomputable']} decision(s) could not "
                     f"be recomputed (no trigger record before them)")

    lines += ["", "ASSESSMENTS  (one per move x contract; prices per 1 contract)"]
    for number, item in enumerate(figures["assessments"], 1):
        lines += _render_assessment(number, item)

    lines += _render_settlement(figures["settlement_value"])
    lines += _render_capture(figures["adjustment_capture"])
    lines += _render_fees(figures["fees"])
    return "\n".join(lines)


def _px(value: Any) -> str:
    """A book price: cents as cents, anything finer in full."""
    if value is None:
        return "n/a"
    return f"{value:.2f}" if abs(value * 100 - round(value * 100)) < 1e-9 \
        else f"{value:.4f}"


def _range(low: Any, high: Any) -> str:
    if low is None:
        return "?"
    return f"{low:.1f}s" if high is None else f"{low:.1f}-{high:.1f}s"


def _render_assessment(number: int, item: dict) -> list[str]:
    if "assessment" not in item:
        return [f"  [{number}] {item['ticker']}: {item.get('error')}"]
    a = item["assessment"]
    move, cov, screen = a["move"], a["coverage"], a["screen"]
    lines = [f"  [{number}] {_hhmm(move['detected_at'])}  {item['ticker']}  "
             f"sharp {move['fair_before_yes']:.4f} -> "
             f"{move['fair_after_yes']:.4f} for YES "
             f"({move['move_for_yes']:+.4f}: {move['favoured_side']} favoured)"]
    read = cov.get("tick_read")
    lines.append(f"      coverage: {cov['class']}"
                 + (f" (this tick's decision read: {read})"
                    if cov.get("within_horizon_at_tick") and read != "ok"
                    else ""))
    if cov["class"] != "observed":
        lines.append(f"        {cov.get('note')}")
    book = a["decision_book"]
    if book:
        age = book["age_at_decision_seconds"]
        lines.append(f"      decision book {_px(book['bid'])} bid / "
                     f"{_px(book['ask'])} ask (resting: {book['yes_bid_size']:g} "
                     f"YES-bid, {book['no_bid_size']:g} NO-bid), read "
                     f"{_range(age['since_received'], age['since_sent'])} "
                     f"before the move was actionable")
    execution = a["execution_book"]
    if execution:
        after = execution["after_decision_seconds"]
        lines.append(f"      execution book {_px(execution['bid'])} / "
                     f"{_px(execution['ask'])}, read "
                     f"{_range(after['sent'], after['received'])} after it")
    reason = screen["rejection"] or screen["refusal"] or ""
    lines.append(f"      screen: {screen['outcome']}"
                 f"{': ' + reason if reason else ''}")
    for side, row in (a.get("sides") or {}).items():
        if not row.get("priced"):
            lines.append(f"        {side:<3} not priced: "
                         f"{row.get('not_priced_because')}")
            continue
        lines.append(
            f"        {side:<3} pays {row['decision_price']:.4f}  wins "
            f"{row['win_probability']:.4f}  gross {_f(row['gross_edge'])}  "
            f"fee {row['fee']:.4f} (raw {row['fee_raw']:.6f})  net "
            f"{_f(row['net_ev'])}  vs floor {_f(row['margin_to_floor'])}")
    sensitivity = (a.get("fees") or {}).get("sensitivity")
    if sensitivity:
        cells = []
        for r in sensitivity["break_even_multiplier"]:
            if r["contracts"] not in (1, 100):
                continue
            ceiling = r["multiplier_at_most"]
            cells.append(f"{r['route']}/{r['contracts']} "
                         + ("never" if ceiling is None else
                            "only at zero fee" if ceiling == 0 else
                            f"<= {ceiling:.3f}"))
        lines.append(f"      fee sensitivity ({sensitivity['side']}, series "
                     f"multiplier that still clears the floor, by route/"
                     f"contracts): {'; '.join(cells)}")
    for scenario in item.get("adjustment_capture") or []:
        lines += _render_scenario(scenario)
    for opposite in item.get("opposite_moves") or []:
        lines.append(f"      opposite sharp move {opposite['after_seconds']:.0f}s "
                     f"later ({opposite['move_for_yes']:+.4f} for YES)")
    settlement = item.get("settlement") or {}
    if settlement.get("status") == "realized":
        lines.append(f"      realized: {settlement['profit']:+.4f}")
    return lines


def _render_scenario(scenario: dict) -> list[str]:
    label = ("hypothetical" if scenario["kind"] == HYPOTHETICAL
             else "SCREEN-ADMITTED")
    if "markouts" not in scenario:
        return [f"      capture ({label}, {scenario['side']}): no entry -- "
                f"{scenario.get('entry_missing_because')}"]
    entry = scenario["entry"]
    paid = "ask" if scenario["side"] == "YES" else "1 - bid"
    got = "bid" if scenario["side"] == "YES" else "1 - ask"
    lines = [f"      capture ({label}): {scenario['side']} bought at "
             f"{entry['price']:.4f} ({paid}) + fee {entry['fee']:.4f}, "
             f"{entry['delay_after_move_seconds']:.1f}s after the move",
             f"        {'markout':>8}  {'exit (' + got + ')':>13}  "
             f"{'gross':>8}  {'fees':>7}  {'net':>8}  sharp"]
    for m in scenario["markouts"]:
        sharp = m.get("sharp") or {}
        state = sharp.get("status", "")
        if state == "unobservable":
            state += f" ({sharp.get('because')})"
        elif state == "reversed" and sharp.get("full_reversal"):
            state += " (fully)"
        if "net" in m:
            lines.append(f"        {m['seconds']:>7g}s  "
                         f"{m['exit']['price']:>13.4f}  {m['gross']:>+8.4f}  "
                         f"{m['fees']:>7.4f}  {m['net']:>+8.4f}  {state}")
        else:
            lines.append(f"        {m['seconds']:>7g}s  censored: "
                         f"{m['censored']['reason']:<34}  {state}")
    best = (scenario.get("hindsight") or {}).get("max_favourable")
    if best:
        lines.append(f"        hindsight diagnostic only, not an achievable "
                     f"exit: best {best['exit_price']:.4f} (net "
                     f"{best['net']:+.4f}) at +{best['after_entry_seconds']:.0f}s")
    return lines


def _render_settlement(value: dict) -> list[str]:
    lines = ["", "SETTLEMENT VALUE  (the frozen screen; predicted, per contract)",
             f"  priced {value['priced']}, admitted {value['admitted']} at a "
             f"floor of {value['floor']:+.4f}"]
    ev, margin = value["best_net_ev"], value["best_margin_to_floor"]
    if ev:
        lines.append(f"  best-side net EV       min {_f(ev['min'])}  median "
                     f"{_f(ev['median'])}  max {_f(ev['max'])}")
        lines.append(f"  margin to the floor    min {_f(margin['min'])}  median "
                     f"{_f(margin['median'])}  max {_f(margin['max'])}")
    closest = value.get("closest_to_floor")
    if closest:
        lines.append(f"  closest: {closest['ticker']} {closest['side']} "
                     f"{_f(closest['margin'])} from the floor")
    realized = value["realized"]
    lines.append(f"  realized: {realized['status']}"
                 + (f" -- {realized['because']}" if realized.get("because")
                    else ""))
    return lines


def _render_capture(capture_figures: dict) -> list[str]:
    lines = ["", "ADJUSTMENT CAPTURE  (executable round trips after both fees; "
             "per 1 contract)",
             f"  policy {capture_figures['policy']['label']}: exit at the first "
             f"usable read at/after each markout, within "
             f"{capture_figures['policy']['exit_tolerance_seconds']:g}s"]
    for kind, figures in capture_figures["summary"].items():
        lines.append(f"  {kind}: {figures['assessments']} assessment(s), "
                     f"{figures['entered']} entered, {figures['games']} game(s)")
        for key, n in figures["entry_missing"].items():
            lines.append(f"    no entry: {key} ({n})")
        for m in figures["markouts"]:
            net, gross = m["net"], m["gross"]
            censored = ", ".join(f"{k} {v}" for k, v in m["censored"].items())
            lines.append(
                f"    {m['seconds']:>6g}s  priced {m['priced']:>2} "
                f"({m['games']} game(s))"
                + (f"  gross median {_f(gross['median'])}  net median "
                   f"{_f(net['median'])} (min {_f(net['min'])}, max "
                   f"{_f(net['max'])}), {m['net_positive']} positive"
                   if net else "")
                + (f"  censored: {censored}" if censored else ""))
    lines.append("  " + capture_figures["note"])
    for limit in capture_figures["limits"]:
        lines.append(f"  limit: {limit}")
    return lines


def _render_fees(fees: dict) -> list[str]:
    p = fees["provenance"]
    lines = ["", "FEES",
             f"  basis              {p['basis']}: taker coefficient "
             f"{p['generic_taker_coefficient']} x multiplier {p['multiplier']}",
             f"  rounding           {p['rounding']['route_label']} "
             f"(route resolved: "
             f"{'yes' if p['rounding']['route_resolved'] else 'NO'})",
             "  UNRESOLVED -- every net figure above inherits these:"]
    for item in fees["unresolved"]:
        lines.append(f"    - {item}")
    lines.append(f"  verify (free, on a machine that can reach Kalshi): "
                 f"{fees['verify']}")
    return lines


def load_settlements(path: Path) -> dict[str, int]:
    """`{market_ticker: 1 | 0}` -- YES settled true or false. Anything else
    is refused rather than read as a result."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("settlements must be a JSON object of ticker -> 0|1")
    out = {}
    for ticker, value in data.items():
        if isinstance(value, bool) or value not in (0, 1):
            raise ValueError(f"settlement for {ticker!r} is {value!r}; "
                             f"expected 0 or 1")
        out[str(ticker)] = int(value)
    return out


def jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"not serialisable: {type(value).__name__}")
