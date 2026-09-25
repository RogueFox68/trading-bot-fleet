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

from core.fees import (                                         # noqa: E402
    DEFAULT_ROUTE, FeeScheduleUnresolved, fee_for,
)
from .measure import usable_candle                              # noqa: E402
from .screen import execution_candle                            # noqa: E402

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
    "the fee provenance's unresolved inputs (route, series multiplier)",
    "exits are pre-match only: a markout at or after kickoff is censored",
)


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


@dataclass(frozen=True)
class CapturePolicy:
    """Declared before measuring -- see the module docstring for when."""

    markouts: tuple[timedelta, ...] = tuple(
        timedelta(seconds=s) for s in (30, 60, 120, 300, 600, 900, 1800))
    #: Entry is the first usable read at or after the move was actionable,
    #: within this: the monitor's own hole limit (three follow intervals).
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
                "measured_from": "the entry read's receipt",
                "entry_within_seconds": self.entry_within.total_seconds(),
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
    if error:
        leg["unusable_because"] = f"fee could not be charged: {error}"
    elif depth is not None and depth < policy.contracts:
        leg["unusable_because"] = (f"observed depth {depth:g} is below the "
                                   f"{policy.contracts} contract(s) priced: "
                                   f"no fill can be assumed")
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
            sharp: dict | None = None, why: str = "") -> dict:
    """One scenario's round trips: an entry, and an exit at every markout.

    `sharp`, when given, is the keyword arguments for `sharp_state` other
    than `at`, so each markout reports the signal as it stood then.
    """
    usable = [r.book for r in reads if r.book is not None
              and usable_candle(r.book)]
    out: dict[str, Any] = {"kind": kind, "side": side, "why": why,
                           "policy": policy.label}
    if session_end is not None and detected_at > session_end:
        out["entry_missing_because"] = "session_ended"
        return out
    entry_book = execution_candle(usable, detected_at, policy.entry_within)
    if entry_book is None:
        seen = _reads_between(reads, detected_at,
                              detected_at + policy.entry_within)
        out["entry_missing_because"] = (
            f"no usable read within {policy.entry_within.total_seconds():g}s "
            f"of the move (reads in that span by status: {seen or 'none'})")
        return out
    entry = _leg(entry_book, side, entering=True, policy=policy, venue=venue,
                 role=role, series=series, route=route)
    entry["delay_after_move_seconds"] = (entry_book.ts
                                         - detected_at).total_seconds()
    out["entry"] = entry
    if "unusable_because" in entry:
        out["entry_missing_because"] = entry["unusable_because"]
        return out
    cost = entry["price"] + entry["fee"]
    markouts = []
    for offset in policy.markouts:
        target = entry_book.ts + offset
        row: dict[str, Any] = {"seconds": offset.total_seconds(),
                               "target": _iso(target)}
        if sharp is not None:
            row["sharp"] = sharp_state(at=target, **sharp)
        censored = None
        if session_end is not None and target > session_end:
            censored = {"reason": "session_ended"}
        elif start is not None and target >= start:
            censored = {"reason": "game_started"}
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
                    gross = exit_leg["price"] - entry["price"]
                    row.update(exit=exit_leg, gross=gross,
                               fees=entry["fee"] + exit_leg["fee"],
                               net=gross - entry["fee"] - exit_leg["fee"],
                               return_on_cost=((gross - entry["fee"]
                                                - exit_leg["fee"]) / cost),
                               depth_observed=(entry["depth_observed"]
                                               and exit_leg["depth_observed"]))
        if censored is not None:
            row["censored"] = censored
        markouts.append(row)
    out["markouts"] = markouts
    out["hindsight"] = _hindsight(usable, entry_book, entry, side,
                                  policy=policy, start=start,
                                  session_end=session_end, venue=venue,
                                  role=role, series=series, route=route)
    return out


def _hindsight(usable: Sequence, entry_book: Any, entry: dict, side: str, *,
               policy: CapturePolicy, start: datetime | None,
               session_end: datetime | None, venue: str, role: str,
               series: str | None, route: str) -> dict:
    """The best and worst net exits the reads showed -- labelled, because
    choosing either needs the future."""
    horizon = entry_book.ts + policy.markouts[-1]
    for bound in (start, session_end):
        if bound is not None:
            horizon = min(horizon, bound)
    best = worst = None
    for book in usable:
        if not entry_book.ts < book.ts <= horizon:
            continue
        leg = _leg(book, side, entering=False, policy=policy, venue=venue,
                   role=role, series=series, route=route)
        if "unusable_because" in leg:
            continue
        net = leg["price"] - entry["price"] - entry["fee"] - leg["fee"]
        point = {"at": leg["at"], "exit_price": leg["price"], "net": net,
                 "after_entry_seconds": (book.ts
                                         - entry_book.ts).total_seconds()}
        if best is None or net > best["net"]:
            best = point
        if worst is None or net < worst["net"]:
            worst = point
    return {"label": HINDSIGHT_LABEL, "through": _iso(horizon),
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
