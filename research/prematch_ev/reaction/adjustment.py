"""Adjustment capture: what the exchange's late adjustment left to take.

A DIFFERENT QUESTION FROM THE SCREEN'S
--------------------------------------
The frozen screen asks a SETTLEMENT question: is the sharp probability far
enough above the price, after the fee, to be worth holding to the result? Its
realized answer needs settlement, and is withheld without it.

This asks an EXECUTION question that needs no settlement: after a sharp move,
had you taken the exchange's quote and later sold into its book, what would
the round trip have paid, after both fees? That is answerable from quotes
alone, which is why a live session can measure it -- and why it never borrows
the screen's EV language or its admission. Every result carries its scenario:
`screen_admitted` for an entry the frozen screen took, and
`hypothetical_follow_move` for the question "was there room behind the move",
asked of every assessment whether or not the screen would have entered.

THE TWO SCENARIOS ENTER DIFFERENTLY, ON PURPOSE
-----------------------------------------------
* A `hypothetical_follow_move` finds its own entry: the first usable read at
  or after the move, within `entry_within`. It is a question about the
  book, not a trade anyone took.
* A `screen_admitted` capture NEVER finds its own entry. It is handed the
  screen's (`screen_entry`): the execution book the screen priced, at the
  price and fee the admitted trade paid, so its entry cost IS the trade's
  `paid`. Re-selecting it -- the first read after the move, as the
  hypothetical does -- entered one review's example at a +0.2s read still
  quoting 0.60 when the screen's one-second-delayed execution read quoted
  0.65: 0.62 paid against the trade's 0.67, and every markout measured from
  the wrong instant. `capture` refuses a `screen_admitted` call without the
  screen's entry, and a hypothetical with one.

THE RULES THAT KEEP IT AN EXECUTION MEASURE
-------------------------------------------
* Entry pays the ASK for YES and (1 - bid) for NO; exit receives the BID for
  YES and (1 - ask) for NO. The spread is paid on the way in AND out: the
  2026-09-24 Seattle case -- the 0.75 ask paid, the 0.75 bid later received --
  is ZERO gross and a loss after fees, however far the book moved.
* Each leg pays a taker fee at its own price and its own instant.
* The exit at each markout is the FIRST usable read at or after entry plus
  the markout, within a tolerance: `screen.execution_candle`, the rule the
  screen waits for a delayed quote by. Never the best quote in a window.
  Picking the most favourable later quote is picking with hindsight.
* A markout with no usable read is CENSORED, with its reason -- never filled
  from an earlier read, never silently dropped.
* PRE-MATCH ONLY, judged on the READ, not just the target. Every quote a
  round trip uses -- the entry and each exit -- must have been RECEIVED
  before kickoff and no later than the session's end (`_outside`, one rule
  for targets, entries, exits and hindsight). A markout whose target
  precedes kickoff but whose first read arrives after it is censored
  `game_started`, never filled: checking only the target once priced an
  exit read 5s after kickoff at +0.16. A read received exactly AT kickoff
  is not pre-match.
* The best and worst exits seen are reported only as a HINDSIGHT DIAGNOSTIC,
  and labelled so.
* A fill is ASSUMED: one contract at the top of the book as read. The depth
  recorded is what rested at the read, not what an order arriving later
  would have found; the time between our read and an order's arrival, queue
  position and partial fills are not modelled. Observed depth below the size
  censors the leg; unobserved depth is flagged, not treated as sufficient.
* The sharp signal's state at each markout is judged only from polls that
  had been READ by then -- what a bot could have known, not what the file
  knows now.

THE MARKOUTS ARE DECLARED, AND THE DEVELOPMENT SESSION HAS BEEN SEEN
-------------------------------------------------------------------
30s to 30min, spanning the monitor's follow window, declared 2026-09-25 --
AFTER the owner's summary of the 2026-09-24 session was read, including its
one response bracket (~610-620s). That session is therefore development data
for these markouts as well as for everything else, and nothing measured on
it is evidence. A later session is a test of them only if they are used
unchanged.
"""

from __future__ import annotations

import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import side_quotes                        # noqa: E402
from core.fees import (                                         # noqa: E402
    DEFAULT_ROUTE, FeeScheduleUnresolved, fee_for,
)
from .measure import usable_candle                              # noqa: E402
from .screen import execution_candle, screen_books              # noqa: E402

HYPOTHETICAL = "hypothetical_follow_move"
SCREEN_ADMITTED = "screen_admitted"

HINDSIGHT_LABEL = ("HINDSIGHT DIAGNOSTIC: the best and worst exits the reads "
                   "showed, chosen AFTER the fact. Not an achievable exit, "
                   "not a strategy result")

FILL_LIMITS = (
    "a fill is assumed: one contract, taker, at the top of the book as read",
    "depth is what rested at the read instant, not what a later order would "
    "have found; queue position and partial fills are not modelled",
    "the time between our read and an order reaching the exchange is not "
    "modelled; the read's own span (request to receipt) is recorded",
    "each leg's fee is the model's taker fee at its price and instant, with "
    "the fee provenance's unresolved inputs (route, series multiplier); a "
    "screen_admitted entry carries the screen's own fee, unrecomputed",
    "pre-match only: a markout target, entry read or exit read at or after "
    "kickoff, or after the session ended, is censored",
)


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


@dataclass(frozen=True)
class CapturePolicy:
    """Declared before measuring -- see the module docstring for when."""

    markouts: tuple[timedelta, ...] = tuple(
        timedelta(seconds=s) for s in (30, 60, 120, 300, 600, 900, 1800))
    #: A HYPOTHETICAL entry is the first usable read at or after the move
    #: was actionable, within this: the monitor's own hole limit (three
    #: follow intervals). An admitted entry is the screen's, whatever its
    #: delay: this bounds nothing about it.
    entry_within: timedelta = timedelta(seconds=30)
    #: An exit is the first usable read at or after its markout, within this.
    exit_tolerance: timedelta = timedelta(seconds=30)
    contracts: int = 1
    label: str = "adjustment-capture-v1"

    def __post_init__(self) -> None:
        if not self.markouts or any(m <= timedelta(0) for m in self.markouts):
            raise ValueError("markouts must be positive")
        if list(self.markouts) != sorted(set(self.markouts)):
            raise ValueError("markouts must be strictly increasing")
        for name in ("entry_within", "exit_tolerance"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if not isinstance(self.contracts, int) or self.contracts <= 0:
            raise ValueError("contracts must be a positive int")

    def as_dict(self) -> dict:
        return {"label": self.label,
                "markout_seconds": [m.total_seconds() for m in self.markouts],
                "measured_from": ("the entry: its read's receipt, or the "
                                  "move's detection when that is later (a "
                                  "zero-delay screen entry prices the "
                                  "decision book, read before the move)"),
                "entry_within_seconds": self.entry_within.total_seconds(),
                "entry_within_applies_to": (
                    f"{HYPOTHETICAL} only: a {SCREEN_ADMITTED} entry is the "
                    f"screen's own execution quote, at its own delay"),
                "exit_tolerance_seconds": self.exit_tolerance.total_seconds(),
                "contracts": self.contracts,
                "declared": ("2026-09-25, after the 2026-09-24 session's "
                             "summary was read: that session is development "
                             "data for these markouts")}


@dataclass(frozen=True)
class Read:
    """One read of one contract: the span it answered over, its status, and
    the book (None when the read gave nothing a price can come from)."""

    sent: datetime
    received: datetime
    status: str
    book: Any = None


@dataclass(frozen=True)
class SharpReading:
    """The sharp book's state for one game, as one poll left it.

    `status` is `answered` when a fair price could be read, else why not:
    the poll failed, the game was absent from it, it was already in play,
    or its prices would not de-vig.
    """

    ready_at: datetime
    status: str
    fair_home: float | None = None
    fair_away: float | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class ScreenEntry:
    """The frozen screen's own entry, as the screen made it.

    `book` is the execution book the screen priced (`screen.screen_books`);
    `price` and `fee` are the screen's execution price and fee for `side`
    (`side_quotes`, the function the screen prices with), and `paid` is
    their sum -- the admitted trade's `paid`, checked, not assumed. A
    `screen_admitted` capture enters here and nowhere else.
    """

    side: str
    book: Any
    price: float
    fee: float
    paid: float
    entry_delay: timedelta
    fee_basis: str


class ScreenEntryMismatch(ValueError):
    """The entry rebuilt from the screen's inputs is not the trade it
    admitted: an admitted capture must never be reported off it."""


def screen_entry(decision: Any, books: Sequence, *, entry_delay: timedelta,
                 entry_tolerance: timedelta, series: str | None,
                 venue: str = "kalshi", role: str = "taker",
                 route: str = DEFAULT_ROUTE) -> ScreenEntry | None:
    """The admitted trade's entry; None when the screen admitted nothing.

    `decision` is `screen.screen_live`'s, and every other argument must be
    what that call was given. The book comes from `screen_books` on those
    inputs, the price and fee from `side_quotes` on the screen's own
    Observation -- the quotes `as_trade` chose the side from. Both are
    checked against what the screen recorded (the execution read's time,
    the trade's `paid`), and a disagreement is REFUSED, loudly: it would
    mean these are not the screen's inputs, and a capture priced off them
    would be a round trip the policy did not take.
    """
    trade, o = decision.trade, decision.observation
    if not decision.admitted or trade is None or o is None:
        return None
    _, book = screen_books(books, o.decision_at, entry_delay, entry_tolerance)
    quote = next((q for q in side_quotes(o, venue, role, series, route)
                  if q.side == trade.side), None)
    if book is None or book.ts != o.entry_at:
        raise ScreenEntryMismatch(
            f"{decision.market_ticker}: the execution book found from these "
            f"inputs ({None if book is None else _iso(book.ts)}) is not the "
            f"one the screen priced ({_iso(o.entry_at)})")
    if quote is None or quote.paid != trade.paid:
        raise ScreenEntryMismatch(
            f"{decision.market_ticker}: {trade.side} re-priced from the "
            f"screen's observation pays "
            f"{None if quote is None else quote.paid}, the admitted trade "
            f"paid {trade.paid}")
    if quote.delayed:
        price, fee = quote.exec_price, quote.exec_fee
        basis = ("the screen's execution fee (side_quotes exec_fee), on the "
                 "execution price")
    else:
        price, fee = quote.entry_price, quote.fee
        basis = ("the screen's decision fee (side_quotes fee): at zero delay "
                 "the execution book is the decision book")
    return ScreenEntry(side=trade.side, book=book, price=price, fee=fee,
                       paid=quote.paid, entry_delay=entry_delay,
                       fee_basis=basis)


def _outside(moment: datetime, *, start: datetime | None,
             session_end: datetime | None) -> str | None:
    """Why an instant is outside the pre-match session, or None.

    One rule for every instant a round trip depends on -- the move, the
    entry read, each markout target, each exit read, each hindsight read.
    Kickoff is EXCLUSIVE (a quote received at kickoff is not pre-match);
    the session's end is inclusive (the last read it recorded counts).
    """
    if session_end is not None and moment > session_end:
        return "session_ended"
    if start is not None and moment >= start:
        return "game_started"
    return None


def entry_price(book: Any, side: str) -> float | None:
    """What entering costs: YES pays the ask, NO pays (1 - bid)."""
    if side == "YES":
        return book.ask_close
    return None if book.bid_close is None else 1.0 - book.bid_close


def exit_price(book: Any, side: str) -> float | None:
    """What exiting receives: YES sells at the bid, NO at (1 - ask) -- the
    best NO bid, since the YES ask is 1 - best NO bid."""
    if side == "YES":
        return book.bid_close
    return None if book.ask_close is None else 1.0 - book.ask_close


def _depth(book: Any, side: str, *, entering: bool) -> float | None:
    takes_no_bids = (side == "YES") == entering
    return getattr(book, "ask_size" if takes_no_bids else "bid_size", None)


def _fee(price: float, at: datetime, *, contracts: int, venue: str, role: str,
         series: str | None, route: str) -> tuple[float | None, str | None]:
    try:
        return fee_for(venue, contracts, price, role, series, at,
                       route).dollars / contracts, None
    except (FeeScheduleUnresolved, ValueError) as exc:
        return None, str(exc)


def _leg(book: Any, side: str, *, entering: bool, policy: CapturePolicy,
         venue: str, role: str, series: str | None, route: str) -> dict:
    price = (entry_price if entering else exit_price)(book, side)
    depth = _depth(book, side, entering=entering)
    leg: dict[str, Any] = {
        "at": _iso(book.ts), "sent_at": _iso(getattr(book, "sent_at", None)),
        "price": price, "depth": depth,
        "depth_observed": depth is not None,
        "bid": book.bid_close, "ask": book.ask_close}
    if price is None or not 0.0 < price < 1.0:
        leg["unusable_because"] = (f"{'entry' if entering else 'exit'} price "
                                   f"{price} outside (0, 1)")
        return leg
    fee, error = _fee(price, book.ts, contracts=policy.contracts, venue=venue,
                      role=role, series=series, route=route)
    leg["fee"] = fee
    short = _depth_short(depth, policy)
    if error:
        leg["unusable_because"] = f"fee could not be charged: {error}"
    elif short:
        leg["unusable_because"] = short
    return leg


def _depth_short(depth: float | None, policy: CapturePolicy) -> str | None:
    if depth is not None and depth < policy.contracts:
        return (f"observed depth {depth:g} is below the {policy.contracts} "
                f"contract(s) priced: no fill can be assumed")
    return None


def _screen_leg(entry: ScreenEntry, policy: CapturePolicy) -> dict:
    """The admitted entry as a leg: the screen's book, price and fee,
    carried over -- never re-priced here."""
    book = entry.book
    depth = _depth(book, entry.side, entering=True)
    leg: dict[str, Any] = {
        "at": _iso(book.ts), "sent_at": _iso(getattr(book, "sent_at", None)),
        "price": entry.price, "fee": entry.fee, "paid": entry.paid,
        "depth": depth, "depth_observed": depth is not None,
        "bid": book.bid_close, "ask": book.ask_close,
        "source": ("the frozen screen's own entry: the execution quote it "
                   "priced, at the price and fee the admitted trade paid"),
        "fee_basis": entry.fee_basis,
        "screen_entry_delay_seconds": entry.entry_delay.total_seconds()}
    short = _depth_short(depth, policy)
    if short:
        leg["unusable_because"] = short
    return leg


def _reads_between(reads: Sequence[Read], lo: datetime, hi: datetime) -> dict:
    inside = [r for r in reads if lo <= r.received <= hi]
    counts: dict[str, int] = {}
    for read in inside:
        counts[read.status] = counts.get(read.status, 0) + 1
    return counts


def sharp_state(readings: Sequence[SharpReading], at: datetime, *,
                detected_at: datetime, yes_is_home: bool,
                fair_before_yes: float, fair_after_yes: float,
                min_move: float, session_end: datetime | None) -> dict:
    """Whether the move still stood at `at`, from the polls READ by then.

    `persisted`: the sharp price for this contract's side has given back
    less than the detector's own threshold since the move. `reversed`: it
    has given back at least that much (`full_reversal` when it is back at or
    past the pre-move level). `unobservable`: the latest poll read by then
    could not say, and why.
    """
    if session_end is not None and at > session_end:
        return {"status": "unobservable", "because": "session_ended"}
    known = [r for r in readings if detected_at <= r.ready_at <= at]
    if not known:
        return {"status": "unobservable",
                "because": "no_poll_read_since_the_move"}
    latest = max(known, key=lambda r: r.ready_at)
    base = {"poll_ready_at": _iso(latest.ready_at),
            "poll_age_seconds": (at - latest.ready_at).total_seconds()}
    if latest.status != "answered" or latest.fair_home is None:
        return {**base, "status": "unobservable", "because": latest.status}
    now_yes = latest.fair_home if yes_is_home else latest.fair_away
    direction = 1.0 if fair_after_yes >= fair_before_yes else -1.0
    retrace = direction * (fair_after_yes - now_yes)
    return {**base,
            "status": "reversed" if retrace >= min_move else "persisted",
            "fair_yes": now_yes, "retraced": retrace,
            "full_reversal": direction * (now_yes - fair_before_yes) <= 0.0,
            "provider_observed_at": _iso(latest.observed_at)}


def capture(*, kind: str, side: str, reads: Sequence[Read],
            detected_at: datetime, start: datetime | None,
            session_end: datetime | None, series: str | None,
            policy: CapturePolicy = CapturePolicy(), venue: str = "kalshi",
            role: str = "taker", route: str = DEFAULT_ROUTE,
            sharp: dict | None = None, why: str = "",
            entry: ScreenEntry | None = None) -> dict:
    """One scenario's round trips: an entry, and an exit at every markout.

    A `hypothetical_follow_move` finds its own entry read; a
    `screen_admitted` capture is given the screen's (`entry`, from
    `screen_entry`) and is refused without it -- see the module docstring.
    `sharp`, when given, is the keyword arguments for `sharp_state` other
    than `at`, so each markout reports the signal as it stood then.
    """
    if kind not in (HYPOTHETICAL, SCREEN_ADMITTED):
        raise ValueError(f"unknown capture scenario {kind!r}")
    if (kind == SCREEN_ADMITTED) != (entry is not None):
        raise ValueError(
            f"a {SCREEN_ADMITTED} capture enters at the screen's own entry "
            f"and only it does: a {HYPOTHETICAL} finds its own read, and an "
            f"admitted one never may")
    if entry is not None and entry.side != side:
        raise ValueError(f"the screen admitted {entry.side}, not {side}")
    if entry is not None and policy.contracts != 1:
        raise ValueError(f"the screen prices one contract; a "
                         f"{policy.contracts}-contract capture would not be "
                         f"the admitted trade")
    usable = [r.book for r in reads if r.book is not None
              and usable_candle(r.book)]
    out: dict[str, Any] = {"kind": kind, "side": side, "why": why,
                           "policy": policy.label}
    bounds = dict(start=start, session_end=session_end)
    outside = _outside(detected_at, **bounds)
    if outside:
        out["entry_missing_because"] = (
            f"{outside} (the move was detected at {_iso(detected_at)}; "
            f"kickoff {_iso(start)}, session end {_iso(session_end)})")
        return out
    if entry is not None:
        entry_book = entry.book
        leg = _screen_leg(entry, policy)
    else:
        entry_book = execution_candle(usable, detected_at, policy.entry_within)
        if entry_book is None:
            seen = _reads_between(reads, detected_at,
                                  detected_at + policy.entry_within)
            out["entry_missing_because"] = (
                f"no usable read within "
                f"{policy.entry_within.total_seconds():g}s of the move (reads "
                f"in that span by status: {seen or 'none'})")
            return out
        leg = _leg(entry_book, side, entering=True, policy=policy,
                   venue=venue, role=role, series=series, route=route)
        leg["source"] = (f"the first usable read at or after the move, "
                         f"within {policy.entry_within.total_seconds():g}s: "
                         f"a hypothetical entry, not the screen's")
    leg["delay_after_move_seconds"] = (entry_book.ts
                                       - detected_at).total_seconds()
    out["entry"] = leg
    outside = _outside(entry_book.ts, **bounds)
    if outside:
        out["entry_missing_because"] = (
            f"{outside} (the entry read was received at "
            f"{_iso(entry_book.ts)}; kickoff {_iso(start)}, session end "
            f"{_iso(session_end)}): not a pre-match entry"
            + (" -- the SCREEN admitted it" if entry is not None else ""))
        return out
    if "unusable_because" in leg:
        out["entry_missing_because"] = leg["unusable_because"]
        return out
    # A zero-delay screen entry prices the decision book, read BEFORE the
    # move: nothing can be held from before it was decided.
    entered_at = max(entry_book.ts, detected_at)
    out["entered_at"] = _iso(entered_at)
    cost = leg["price"] + leg["fee"]
    markouts = []
    for offset in policy.markouts:
        target = entered_at + offset
        row: dict[str, Any] = {"seconds": offset.total_seconds(),
                               "target": _iso(target)}
        if sharp is not None:
            row["sharp"] = sharp_state(at=target, **sharp)
        censored = None
        outside = _outside(target, **bounds)
        if outside:
            censored = {"reason": outside}
        else:
            found = execution_candle(usable, target, policy.exit_tolerance)
            if found is None:
                window_end = target + policy.exit_tolerance
                censored = {
                    "reason": ("session_ended" if session_end is not None
                               and window_end > session_end else
                               "no_usable_read_within_tolerance"),
                    "reads_in_tolerance": _reads_between(reads, target,
                                                         window_end),
                    "note": ("an earlier read is never used as the exit, and "
                             "neither is a later one")}
            elif _outside(found.ts, **bounds):
                censored = {
                    "reason": _outside(found.ts, **bounds),
                    "read_received_at": _iso(found.ts),
                    "note": ("the first usable read at or after the markout "
                             "was received outside the pre-match session: "
                             "not a pre-match exit, and no other read is used "
                             "in its place")}
            else:
                exit_leg = _leg(found, side, entering=False, policy=policy,
                                venue=venue, role=role, series=series,
                                route=route)
                exit_leg["late_by_seconds"] = (found.ts - target).total_seconds()
                if "unusable_because" in exit_leg:
                    censored = {"reason": "exit_unusable",
                                "detail": exit_leg["unusable_because"],
                                "exit": exit_leg}
                else:
                    gross = exit_leg["price"] - leg["price"]
                    net = gross - leg["fee"] - exit_leg["fee"]
                    row.update(exit=exit_leg, gross=gross,
                               fees=leg["fee"] + exit_leg["fee"], net=net,
                               return_on_cost=net / cost,
                               depth_observed=(leg["depth_observed"]
                                               and exit_leg["depth_observed"]))
        if censored is not None:
            row["censored"] = censored
        markouts.append(row)
    out["markouts"] = markouts
    out["hindsight"] = _hindsight(usable, entered_at, leg, side,
                                  policy=policy, start=start,
                                  session_end=session_end, venue=venue,
                                  role=role, series=series, route=route)
    return out


def _hindsight(usable: Sequence, entered_at: datetime, entry: dict, side: str,
               *, policy: CapturePolicy, start: datetime | None,
               session_end: datetime | None, venue: str, role: str,
               series: str | None, route: str) -> dict:
    """The best and worst net exits the reads showed -- labelled, because
    choosing either needs the future. Only reads a markout could have used:
    after the entry, within the last markout, and inside the pre-match
    session by `_outside`'s rule -- a read at kickoff is not one."""
    through = entered_at + policy.markouts[-1]
    if session_end is not None:
        through = min(through, session_end)
    best = worst = None
    for book in usable:
        if (not entered_at < book.ts <= through
                or _outside(book.ts, start=start, session_end=session_end)):
            continue
        leg = _leg(book, side, entering=False, policy=policy, venue=venue,
                   role=role, series=series, route=route)
        if "unusable_because" in leg:
            continue
        net = leg["price"] - entry["price"] - entry["fee"] - leg["fee"]
        point = {"at": leg["at"], "exit_price": leg["price"], "net": net,
                 "after_entry_seconds": (book.ts
                                         - entered_at).total_seconds()}
        if best is None or net > best["net"]:
            best = point
        if worst is None or net < worst["net"]:
            worst = point
    return {"label": HINDSIGHT_LABEL, "after": _iso(entered_at),
            "through": _iso(through), "before_kickoff": _iso(start),
            "max_favourable": best, "max_adverse": worst}


def summarize(scenarios: Iterable[tuple[str, dict]],
              policy: CapturePolicy = CapturePolicy()) -> dict:
    """Per scenario kind and markout: how many round trips were priced, why
    the rest were censored, and what the priced ones paid.

    `scenarios` is `(game_id, scenario)`: the two contracts of one game are
    one outcome seen through two books, so games are counted beside rows.
    Figures from a handful of moves describe those moves; they are not an
    estimate of anything.
    """
    by_kind: dict[str, list[tuple[str, dict]]] = {}
    for game, scenario in scenarios:
        by_kind.setdefault(scenario["kind"], []).append((game, scenario))
    out: dict[str, Any] = {}
    for kind, rows in sorted(by_kind.items()):
        entered = [(g, s) for g, s in rows if "markouts" in s]
        missing: dict[str, int] = {}
        for _, s in rows:
            if "markouts" not in s:
                reason = str(s.get("entry_missing_because", "unknown"))
                key = reason.split(" (")[0]
                missing[key] = missing.get(key, 0) + 1
        per_markout = []
        for index, offset in enumerate(policy.markouts):
            priced = [(g, s["markouts"][index]) for g, s in entered
                      if "net" in s["markouts"][index]]
            censored: dict[str, int] = {}
            for _, s in entered:
                reason = (s["markouts"][index].get("censored") or {}).get(
                    "reason")
                if reason:
                    censored[reason] = censored.get(reason, 0) + 1
            nets = [m["net"] for _, m in priced]
            grosses = [m["gross"] for _, m in priced]
            per_markout.append({
                "seconds": offset.total_seconds(),
                "priced": len(priced),
                "games": len({g for g, _ in priced}),
                "censored": censored,
                "gross": _stats(grosses),
                "net": _stats(nets),
                "net_positive": sum(1 for n in nets if n > 0),
                "sharp": _count(m.get("sharp", {}).get("status")
                                for _, m in priced),
            })
        out[kind] = {"assessments": len(rows), "entered": len(entered),
                     "games": len({g for g, _ in rows}),
                     "entry_missing": missing, "markouts": per_markout}
    return out


def _stats(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    return {"mean": statistics.fmean(values),
            "median": statistics.median(values),
            "min": min(values), "max": max(values)}


def _count(values: Iterable[Any]) -> dict:
    out: dict[str, int] = {}
    for value in values:
        if value is not None:
            out[value] = out.get(value, 0) + 1
    return out
