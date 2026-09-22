"""Shadow monitor: watch the sharp book live, decide as a bot would, place nothing.

    python3 shadow_monitor.py --hours 72                       # plan, price, one real book: free
    python3 shadow_monitor.py --hours 72 --spend 4321          # run
    python3 shadow_monitor.py --report study_output/shadow/<session>.jsonl

WHAT IT IS
----------
The no-order version of the bot the owner described -- poll the sharp book,
notice when it moves, look at Kalshi at once -- and the forward half of the
reaction study. It records what a bot WOULD have done the instant a move was
seen, then keeps watching Kalshi to learn whether, and how fast, it followed.
Nothing is sent to Kalshi except reads of its public book.

It measures what no archive can:

  * how old the provider's observation of a price already is when it
    reaches us -- the delivery delay every replay has to assume is zero;
  * how often the provider actually re-observes a game, so a poll cadence
    can be set from evidence instead of bought on a guess. That is only
    measurable by polling faster than it: when every poll finds something
    new, the report says the figure is a ceiling set by the poll spacing;
  * how fast Kalshi follows, at the resolution of this machine's polls
    rather than the exchange's one-minute candles;
  * and what the executable book was at the moment of a move, with depth.

EACH TICK, IN THIS ORDER
------------------------
1. Kalshi's book for every watched contract (free). These are the DECISION
   books: fetched BEFORE the odds poll, they are what a bot held when the
   move arrived.
2. The sharp book (paid, one attempt). Every quote goes through the study's
   own detector, on the clock the decision runs on: when the WHOLE answer
   had arrived and been read. Headers, body and readiness are recorded
   apart, and the reading time is a delay, not time to trade.
3. For each move on a watched game, that game's books again (free). These are
   the EXECUTION books, and how long they took is the entry delay, measured.
   Then the study's own screen -- the checkpoint study's eligibility and fee
   model, unchanged -- and the decision is written down before anything that
   follows is known.
4. A moved game is then followed every few seconds, free, for the declared
   response window. `--report` measures Kalshi's response from those reads
   with the replay's own `measure_response`: only a change located after
   the move was actionable is a response, and a window the reads did not
   cover to its end is blind, not quiet.

WHAT IT CANNOT DO
-----------------
Trade. It holds no Kalshi credential, `reaction.capture.assert_read_only`
scans every module it can reach before the first paid request, and every
request in the process has to match `ALLOWED_ENDPOINTS` or the run stops.

SPENDING
--------
Plan first, like the collector: without `--spend` it prices the session and
stops, having made only free requests -- including one real order-book read,
so the book's transcribed shape is checked before anything is paid for. The
price per call is transcribed (1 credit: one market from one book), so the
provider's own `x-requests-last` is checked against it on the first answer
and a disagreement stops the run -- a cap priced at the wrong unit cost is
not a cap. The cap enforced is the quoted price.

STOPS, AND WHY EACH IS ONE
--------------------------
  credit cap             the session spent what it was allowed
  3 failed polls         an outage or a refused key; the rest would buy the
                         same answer
  cost mismatch          see SPENDING
  clock skew             this machine and the provider disagree about the
                         time by more than MAX_CLOCK_SKEW, and every lag this
                         records would inherit the disagreement. Run it on a
                         machine whose clock is synchronised (NTP).
  quota floor            the account is nearly out, whatever this session
                         was allowed
  unreadable book        Kalshi's book shape is transcribed, not observed, so
                         one real book is read -- free -- before the first
                         paid poll; a shape the parser refuses stops the run
                         before anything is bought
  undated quotes         the provider's observation stamp is transcribed too.
                         The detector refuses a price with no stamp, so a
                         first answer in which no pre-match quote carries one
                         stops the run after one credit rather than paying
                         for a session that could never trigger
  undeclared request     a request outside ALLOWED_ENDPOINTS, refused before
                         it was sent

RECORDS
-------
One append-only JSONL file per session under `study_output/shadow/`: every
raw response with the clocks around it, every move, every decision, every gap,
the detector's refusals for each poll by reason, and the reason the session
ended. `--report` reads it, and prints "0 moves" beside those refusals, so a
detector that could not see is never mistaken for a quiet market. The API key
is in no record: a recorded URL is its path, never its query.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from analysis.scoring import Eligibility                          # noqa: E402
from collect import Ledger, join_markets, yes_side                 # noqa: E402
from collect_reaction import (                                     # noqa: E402
    OUTPUT_DIR, Slate, assert_nothing_here_can_trade,
    only_declared_endpoints, resolve_days,
)
from data import espn_schedule, kalshi_history                     # noqa: E402
from data.cache import CreditCapReached, redact                    # noqa: E402
from data.kalshi_history import BookQuote, Coverage, parse_orderbook  # noqa: E402
from data.odds_history import (                                    # noqa: E402
    SHARP_BOOK, CreditLedger, parse_snapshot,
)
from data.odds_live import (                                       # noqa: E402
    CREDITS_PER_LIVE_CALL, LiveOdds, fetch_live_odds, live_odds_url,
    recorded_url,
)
from reaction.capture import CaptureRefused                        # noqa: E402
from reaction.clocks import envelope_for_live_sharp_quote          # noqa: E402
from reaction.detector import MoveDetector, MovePolicy, MoveTrigger  # noqa: E402
from reaction.measure import (                                     # noqa: E402
    Bracket, ReactionOutcome, ReactionPolicy, measure_response,
)
from reaction.screen import screen_live                            # noqa: E402

# --- declared operating values --------------------------------------------
#
# Operating values, not thresholds: none of them decides whether a move is a
# move or a trade is a trade. Those are the detector's and the screen's
# declared policies, used unchanged.

DEFAULT_CADENCE = timedelta(seconds=60)
#: Below this a poll is not monitoring, it is load; and it is faster than any
#: refresh the provider documents, so it would buy the same answer again.
MIN_CADENCE = timedelta(seconds=10)
#: How often a moved game's books are re-read while it is being followed.
FOLLOW_EVERY = timedelta(seconds=10)
#: The longest a followed contract may go unseen before its response window
#: has a HOLE in it: three follow intervals, so one lost read is inside it
#: and two in a row are not. Recorded on every session.
MAX_UNOBSERVED = 3 * FOLLOW_EVERY
#: The study's declared reaction measurement -- the replay's thresholds,
#: unchanged -- with the hole limit set by this monitor's own reads rather
#: than by a candle period it does not have.
REACTION_POLICY = ReactionPolicy(max_unobserved=MAX_UNOBSERVED)
#: How long a moved game is followed: the study's declared response window.
FOLLOW_FOR = REACTION_POLICY.max_wait
#: Decision books are read only for games this close to kickoff: beyond the
#: screen's own ceiling no entry can be admitted, so a book there could only
#: ever produce a refusal.
BOOK_HORIZON = timedelta(minutes=Eligibility().max_minutes_to_start)
#: The game days the slate covers, from today.
SLATE_DAYS = 7
#: How often the slate and the join are rebuilt (free requests): new listings
#: appear during the week, and a flexed kickoff moves.
REJOIN_EVERY = timedelta(hours=1)
STOP_AFTER_FAILED_POLLS = 3
MAX_CLOCK_SKEW = timedelta(seconds=5)
QUOTA_FLOOR = 50
#: How far back polled books are kept in memory for decisions. The records
#: keep everything; this only bounds what a long session holds.
BOOK_MEMORY = timedelta(hours=2)

EXIT_OK, EXIT_STOPPED, EXIT_USAGE = 0, 1, 2

SHADOW_DIR = OUTPUT_DIR / "shadow"


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"not serialisable: {type(value).__name__}")


class SystemClock:
    """The real clock. Tests pass one that only advances when told to."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class Recorder:
    """Append-only JSONL: each fact written, and flushed, when it is known.

    A session killed at any point leaves every line it wrote readable, so a
    report of a crashed session is a report of what happened up to the
    crash, not an empty file.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._handle = path.open("a", encoding="utf-8")

    def write(self, kind: str, **fields: Any) -> None:
        row = {"kind": kind, **fields}
        self._handle.write(json.dumps(row, default=_jsonable,
                                      separators=(",", ":")) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


@dataclass(frozen=True)
class Watched:
    """One open contract joined to one sharp event."""

    ticker: str
    event_ticker: str
    provider_event_id: str
    yes_is_home: bool
    start: datetime


@dataclass
class Follow:
    until: datetime
    next_at: datetime


@dataclass(frozen=True)
class Stop:
    reason: str
    detail: str = ""
    ok: bool = False


def read_one_book(ticker: str, now: Callable[[], datetime]
                  ) -> tuple[BookQuote | None, Coverage, Any]:
    """Read one real order book and parse it: whether the book's TRANSCRIBED
    shape is the real one. Free -- public market data -- and the one check
    both the plan and a paid session's preflight run, so they cannot
    disagree about what "readable" means."""
    sent = now()
    payload, coverage = kalshi_history.fetch_orderbook_payload(ticker)
    book = None
    if payload is not None:
        book, parsed = parse_orderbook(payload, ticker=ticker,
                                       received_at=now(), sent_at=sent)
        coverage.merge(parsed)
    return book, coverage, payload


def live_slate(today: date, *, league: str, series: str,
               enumerate_markets: Callable = kalshi_history
               .enumerate_open_markets,
               fetch_schedule: Callable = espn_schedule.fetch_schedule
               ) -> Slate:
    """This week's OPEN contracts and their kickoffs. Free.

    The collector's own slate resolution over a range of game days, with
    open markets instead of settled ones and no schedule cache: a cached
    future kickoff is exactly the value a flex would have moved.
    """
    days = [today + timedelta(days=offset) for offset in range(-1, SLATE_DAYS + 1)]
    return resolve_days(days, league=league, series=series,
                        schedule_cache=None,
                        enumerate_markets=enumerate_markets,
                        fetch_schedule=fetch_schedule)


def join_live(slate: Slate, quotes_by_event: dict[str, list], league: str
              ) -> tuple[dict[str, list[Watched]], Ledger]:
    """Open contracts to sharp events, through the study's own join.

    `require_settlement=False` is the only difference from every other join
    in the study: an open market has no result yet. Orientation is
    `collect.yes_side`, and an unorientable contract is left out, never
    defaulted -- an inverted signal is not a smaller one.
    """
    ledger = Ledger()
    joined = join_markets(slate.markets, quotes_by_event, league, ledger,
                          slate.resolver, require_settlement=False)
    watched: dict[str, list[Watched]] = {}
    for contract in joined:
        reference = quotes_by_event[contract.provider_event_id][0]
        side = yes_side(reference, contract.yes_participant, league)
        if side is None:
            ledger.reject("orientation_unresolved", contract.market_ticker,
                          stage="contracts")
            continue
        watched.setdefault(contract.provider_event_id, []).append(Watched(
            ticker=contract.market_ticker, event_ticker=contract.event_ticker,
            provider_event_id=contract.provider_event_id,
            yes_is_home=side == "home", start=contract.start))
    return watched, ledger


def session_polls(hours: float, cadence: timedelta) -> int:
    """How many polls a session makes: one at the start and one per cadence."""
    return math.floor(hours * 3600 / cadence.total_seconds()) + 1


def session_price(hours: float, cadence: timedelta) -> int:
    return session_polls(hours, cadence) * CREDITS_PER_LIVE_CALL


class ShadowMonitor:
    """The loop. Everything it touches is injected, so a test can drive it."""

    def __init__(self, *, sport: str, series: str, api_key: str,
                 cadence: timedelta, hours: float, ledger: CreditLedger,
                 recorder: Recorder, clock: Any = None,
                 slate_source: Callable[[], Slate] | None = None,
                 follow_every: timedelta = FOLLOW_EVERY,
                 follow_for: timedelta = FOLLOW_FOR,
                 book_horizon: timedelta = BOOK_HORIZON):
        self.sport = sport.upper()
        self.series = series
        self.api_key = api_key
        self.cadence = cadence
        self.hours = hours
        self.ledger = ledger
        self.recorder = recorder
        self.clock = clock or SystemClock()
        self.slate_source = slate_source or (lambda: live_slate(
            self.clock.now().date(), league=self.sport, series=self.series))
        self.follow_every = follow_every
        self.follow_for = follow_for
        self.book_horizon = book_horizon
        self.detector = MoveDetector(MovePolicy(book=SHARP_BOOK))
        self.watched: dict[str, list[Watched]] = {}
        self.books: dict[str, list[BookQuote]] = {}
        self.follows: dict[str, Follow] = {}
        self.entered: set[str] = set()
        self.seen: set[str] = set()
        self.started: set[str] = set()
        self.failed_polls = 0
        self.cost_verified = False
        self.dates_verified = False
        self.next_rejoin: datetime | None = None
        self.counts = {"polls": 0, "polls_failed": 0, "triggers": 0,
                       "decisions": 0, "entries": 0, "book_reads": 0,
                       "book_failures": 0, "in_play_skipped": 0}

    # --- the Kalshi side (free) -------------------------------------------

    def read_book(self, watched: Watched, purpose: str,
                  context: dict | None = None) -> BookQuote | None:
        sent = self.clock.now()
        payload, coverage = kalshi_history.fetch_orderbook_payload(
            watched.ticker)
        received = self.clock.now()
        book = None
        if payload is not None:
            book, parsed = parse_orderbook(payload, ticker=watched.ticker,
                                           received_at=received, sent_at=sent)
            coverage.merge(parsed)
        self.counts["book_reads"] += 1
        if not coverage.complete:
            self.counts["book_failures"] += 1
        self.recorder.write(
            "book", ticker=watched.ticker, purpose=purpose, sent_at=sent,
            received_at=received, payload=payload,
            coverage=coverage.reasons, **(context or {}))
        if book is not None:
            history = self.books.setdefault(watched.ticker, [])
            history.append(book)
            horizon = received - BOOK_MEMORY
            self.books[watched.ticker] = [b for b in history if b.ts >= horizon]
        return book

    def contracts_in_horizon(self, now: datetime) -> list[Watched]:
        return [w for contracts in self.watched.values() for w in contracts
                if now < w.start <= now + self.book_horizon]

    # --- the stops --------------------------------------------------------

    def preflight(self) -> Stop | None:
        """Free checks before the first paid request."""
        slate = self.slate_source()
        self.recorder.write("slate", markets=len(slate.markets),
                            kickoffs=slate.kickoffs,
                            unresolved=slate.unresolved,
                            coverage=slate.coverage.reasons)
        if not slate.coverage.complete:
            return Stop("slate_incomplete",
                        f"the slate did not fully answer: {slate.coverage}")
        if not slate.markets:
            return Stop("no_open_markets",
                        f"no open {self.series} market in the next "
                        f"{SLATE_DAYS} days; nothing to watch")
        # THE BOOK SHAPE IS TRANSCRIBED, so read one real book before paying
        # for anything. A shape the parser refuses stops the run here, free.
        ticker = sorted(slate.markets)[0]
        book, coverage, payload = read_one_book(ticker, self.clock.now)
        self.recorder.write("shape_check", ticker=ticker,
                            payload=payload, coverage=coverage.reasons,
                            levels=(None if book is None else
                                    book.yes_levels + book.no_levels))
        if book is None or not coverage.complete:
            return Stop("book_unreadable",
                        f"Kalshi's order book for {ticker} could not be read "
                        f"({coverage}); the parser's shape is transcribed "
                        f"from documentation, and nothing was bought")
        return None

    def check_answer(self, live: LiveOdds) -> Stop | None:
        """The per-answer stops: cost, clock, quota."""
        if not self.cost_verified:
            if live.charged is None:
                return Stop("cost_unverifiable",
                            "the provider sent no x-requests-last, so the "
                            "price per call cannot be checked and the cap "
                            "cannot be trusted")
            if live.charged != CREDITS_PER_LIVE_CALL:
                return Stop("cost_mismatch",
                            f"the provider charged {live.charged} per call; "
                            f"this session was priced at "
                            f"{CREDITS_PER_LIVE_CALL}")
            self.cost_verified = True
        # Against the HEADERS' arrival: `Date` marks the response starting,
        # so a slow body is not clock disagreement.
        arrived = live.headers_at or live.received_at
        if live.provider_date is not None and arrived is not None:
            skew = arrived - live.provider_date
            # The Date header is truncated to the second, so a synchronised
            # clock reads up to ~1s AHEAD of it; the bound is on the size.
            if abs(skew) > MAX_CLOCK_SKEW:
                return Stop("clock_skew",
                            f"this machine's clock is "
                            f"{skew.total_seconds():+.1f}s from the "
                            f"provider's; every lag recorded would carry "
                            f"that error. Synchronise the clock (NTP)")
        if live.remaining is not None and live.remaining <= QUOTA_FLOOR:
            return Stop("quota_floor",
                        f"the account reports {live.remaining} credits "
                        f"left, at or below the floor of {QUOTA_FLOOR}")
        return None

    def check_dates(self, quotes: Sequence[Any],
                    received_at: datetime) -> Stop | None:
        """The provider's observation stamp is transcribed too.

        The detector refuses a price with no observation time -- unknown age
        is not fresh -- so a feed that sends none would be watched, and paid
        for, with nothing able to trigger. Checked on the first answer that
        carries a pre-match quote; after that, a missing stamp is counted by
        the detector like any other refusal.
        """
        if self.dates_verified:
            return None
        upcoming = [q for q in quotes if q.commence_time > received_at]
        if not upcoming:
            return None
        if all(q.last_update is None for q in upcoming):
            return Stop("undated_quotes",
                        f"none of the {len(upcoming)} pre-match quote(s) in "
                        f"the first answer carries the provider's observation "
                        f"time; the detector refuses an undated price, so "
                        f"nothing this session watched could trigger")
        self.dates_verified = True
        return None

    # --- one tick ----------------------------------------------------------

    def tick(self) -> Stop | None:
        now = self.clock.now()
        for watched in self.contracts_in_horizon(now):
            if self.read_book(watched, "decision") is None:
                # ONE failed read abandons the rest of this tick's reads: a
                # stalled exchange must not hold the paid poll behind two
                # dozen timeouts. A move this tick then has no decision book
                # and the screen says so.
                self.recorder.write("decision_reads_abandoned",
                                    at=self.clock.now(),
                                    after=watched.ticker)
                break

        live = fetch_live_odds(self.sport, self.api_key, ledger=self.ledger,
                               now=self.clock.now)
        parsed = parse_snapshot(live.snapshot_body()) if live.ok else None
        # DECISION READINESS: the answer has arrived in full AND been read.
        # Moves are dated here, never earlier. The time between the body
        # arriving and this is recorded as processing, not credited to the
        # opportunity as though a bot could have traded during it.
        ready_at = self.clock.now()
        self.counts["polls"] += 1
        self.recorder.write(
            "odds", url=recorded_url(live_odds_url(self.sport, "")),
            sent_at=live.sent_at, headers_at=live.headers_at,
            received_at=live.received_at, ready_at=ready_at,
            processing_seconds=(
                (ready_at - live.received_at).total_seconds()
                if live.received_at else None),
            provider_date=live.provider_date, status=live.status,
            charged=live.charged, used=live.used, remaining=live.remaining,
            payload=live.payload, coverage=live.coverage.reasons)
        stop = self.absorb(live, parsed, ready_at, now)
        # WHY NOTHING TRIGGERED, for THIS answer: recorded once its quotes
        # have been through the detector, so every count sits beside the
        # poll it belongs to -- and a detector that refuses everything shows
        # it on the poll where it started.
        self.recorder.write("detector", received_at=live.received_at,
                            rejections=self._drain_rejections())
        return stop

    def absorb(self, live: LiveOdds, parsed: Any, ready_at: datetime,
               now: datetime) -> Stop | None:
        """One answer through the stops, the join and the detector."""
        if parsed is None or not parsed.coverage.complete:
            self.counts["polls_failed"] += 1
            self.failed_polls += 1
            # THE INTERVAL WAS NOT OBSERVED. Every stream re-anchors on its
            # next good quote rather than closing a move across the hole.
            for stream in sorted(self.seen):
                self.detector.note_gap(stream, live.received_at,
                                       "the live poll did not answer")
            if self.failed_polls >= STOP_AFTER_FAILED_POLLS:
                reasons = live.coverage.reasons or (
                    parsed.coverage.reasons if parsed else [])
                return Stop("failed_polls",
                            f"{self.failed_polls} polls in a row failed: "
                            f"{'; '.join(reasons) or 'no response'}")
            return None
        self.failed_polls = 0
        stop = self.check_answer(live) or self.check_dates(parsed.quotes,
                                                           live.received_at)
        if stop:
            return stop

        quotes_by_event: dict[str, list] = {}
        for quote in parsed.quotes:
            quotes_by_event.setdefault(quote.provider_event_id, []).append(quote)
        if self.next_rejoin is None or now >= self.next_rejoin:
            self.rejoin(quotes_by_event)
            self.next_rejoin = now + REJOIN_EVERY

        present: set[str] = set()
        for quote in parsed.quotes:
            envelope = envelope_for_live_sharp_quote(
                quote, received_at=live.received_at, sent_at=live.sent_at,
                ready_at=ready_at,
                request_url=recorded_url(live_odds_url(self.sport, "")),
                resolution_seconds=self.cadence.total_seconds())
            stream = envelope.provenance.market_id
            # IN PLAY IS NOT PRE-MATCH. A game under way re-prices on every
            # score, and the study is about moves before kickoff. Its stream
            # is retired, not gapped: it will not come back.
            if quote.commence_time <= live.received_at:
                self.counts["in_play_skipped"] += 1
                self.started.add(stream)
                continue
            present.add(stream)
            trigger = self.detector.observe(envelope)
            if trigger is not None:
                self.on_trigger(trigger)
        # A MARKET MISSING FROM A LIVE ANSWER IS A HOLE, as in the replay.
        for stream in sorted(self.seen - present - self.started):
            self.detector.note_gap(stream, live.received_at,
                                   "absent from the live response")
        self.seen = (self.seen | present) - self.started
        return None

    def _drain_rejections(self) -> dict[str, int]:
        """Why nothing triggered since the last poll, counted -- then dropped.

        The detector keeps every rejection it ever made, one per stream per
        poll: a few hundred thousand objects over a multi-day session. The
        counts go into the record the moment they are known, and the list is
        cleared, so a long session holds only one poll's worth.
        """
        rejections = self.detector.result.rejections
        counts = dict(Counter(r.reason.value for r in rejections))
        rejections.clear()
        return counts

    def rejoin(self, quotes_by_event: dict[str, list]) -> None:
        slate = self.slate_source()
        if not slate.coverage.complete:
            # A failed read is not an empty slate: keep watching what was
            # joined last time, and say so.
            self.recorder.write("rejoin_skipped", coverage=slate.coverage.reasons)
            return
        watched, ledger = join_live(slate, quotes_by_event, self.sport)
        self.watched = watched
        self.recorder.write(
            "join", contracts={w.ticker: {"event": event,
                                          "yes_is_home": w.yes_is_home,
                                          "start": w.start}
                               for event, ws in watched.items() for w in ws},
            rejections=ledger.as_dict())

    def on_trigger(self, trigger: MoveTrigger) -> None:
        self.counts["triggers"] += 1
        self.recorder.write("trigger", **trigger.as_dict())
        contracts = self.watched.get(trigger.event_id, [])
        if not contracts:
            self.recorder.write("decision", event_id=trigger.event_id,
                                stream_id=trigger.stream_id,
                                refusal="not_joined_to_an_open_contract",
                                admitted=False)
            return

        executions = {w.ticker: self.read_book(
            w, "execution", {"stream_id": trigger.stream_id})
            for w in contracts}
        decisions = []
        for watched in contracts:
            book = executions.get(watched.ticker)
            # A FAILED EXECUTION READ IS NOT A FILL AT THE DECISION PRICE. With
            # no book the delay is "until now", which no read satisfies, so
            # the screen drops the side exactly as the replay does -- rather
            # than a zero delay, which would price it at the decision book.
            delay = ((book.ts if book is not None else self.clock.now())
                     - trigger.detected_at)
            decision = screen_live(
                trigger, self.books.get(watched.ticker, []),
                market_ticker=watched.ticker,
                yes_is_home=watched.yes_is_home, start=watched.start,
                entry_delay=delay, entry_tolerance=timedelta(seconds=1),
                series=self.series)
            decisions.append((watched, book, decision))

        # ONE ENTRY PER GAME, as in the checkpoint study: the two contracts of
        # a game are one outcome seen from two sides. The admitted contract
        # with the higher predicted EV is the entry; a game already entered
        # takes no second one.
        admitted = sorted(
            (d for d in decisions if d[2].admitted and d[2].trade),
            key=lambda d: (-d[2].trade.predicted_ev, d[0].ticker))
        entry = (admitted[0][0].ticker
                 if admitted and trigger.event_id not in self.entered else None)
        if entry is not None:
            self.entered.add(trigger.event_id)
            self.counts["entries"] += 1
        for watched, book, decision in decisions:
            self.counts["decisions"] += 1
            self.recorder.write(
                "decision", stream_id=trigger.stream_id,
                yes_is_home=watched.yes_is_home,
                book_move_for_yes=trigger.delta_for(watched.yes_is_home),
                entered=watched.ticker == entry,
                execution_depth=(None if book is None else {
                    "yes_bid_size": book.bid_size,
                    "no_bid_size": book.ask_size}),
                **decision.as_dict())

        until = trigger.detected_at + self.follow_for
        current = self.follows.get(trigger.event_id)
        if current is None:
            self.follows[trigger.event_id] = Follow(
                until=until, next_at=self.clock.now() + self.follow_every)
        else:
            current.until = max(current.until, until)

    def follow_due(self) -> None:
        now = self.clock.now()
        for event_id in list(self.follows):
            follow = self.follows[event_id]
            if now > follow.until:
                del self.follows[event_id]
                continue
            if now < follow.next_at:
                continue
            for watched in self.watched.get(event_id, []):
                self.read_book(watched, "follow")
            while follow.next_at <= self.clock.now():
                follow.next_at += self.follow_every

    # --- the session ---------------------------------------------------------

    def run(self) -> Stop:
        started = self.clock.now()
        end = started + timedelta(hours=self.hours)
        self.recorder.write(
            "session_start", at=started, ends=end, sport=self.sport,
            series=self.series, cadence_seconds=self.cadence.total_seconds(),
            follow_every_seconds=self.follow_every.total_seconds(),
            follow_for_seconds=self.follow_for.total_seconds(),
            book_horizon_seconds=self.book_horizon.total_seconds(),
            credit_cap=self.ledger.cap,
            credits_per_call=CREDITS_PER_LIVE_CALL,
            move_policy=self.detector.policy.as_dict(),
            reaction_policy=REACTION_POLICY.as_dict(),
            eligibility=vars(Eligibility()))
        try:
            with only_declared_endpoints():
                stop = self.preflight()
                if stop is None:
                    stop = self._loop(end)
        except CreditCapReached as exc:
            stop = Stop("credit_cap", str(exc))
        except CaptureRefused as exc:
            # A request outside the allow-list was refused before it was
            # sent. That is a code change nobody declared, not a transient
            # failure, so the session ends -- and says why in its own record.
            stop = Stop("undeclared_request", str(exc))
        except KeyboardInterrupt:
            stop = Stop("interrupted", "stopped by the operator")
        self.recorder.write("session_end", at=self.clock.now(),
                            reason=stop.reason, detail=stop.detail,
                            ok=stop.ok, counts=self.counts,
                            detector=self._drain_rejections(),
                            ledger=str(self.ledger),
                            credits_reserved=self.ledger.spent_this_run,
                            account_remaining=self.ledger.remaining)
        return stop

    def _loop(self, end: datetime) -> Stop:
        next_tick = self.clock.now()
        while True:
            now = self.clock.now()
            if now >= end:
                return Stop("end_of_session", ok=True)
            if now >= next_tick:
                stop = self.tick()
                if stop is not None:
                    return stop
                while next_tick <= self.clock.now():
                    next_tick += self.cadence
            self.follow_due()
            wake = min([next_tick, end]
                       + [f.next_at for f in self.follows.values()])
            self.clock.sleep((wake - self.clock.now()).total_seconds())


# --- the report ------------------------------------------------------------------

def _quantiles(values: Sequence[float]) -> str:
    if not values:
        return "none"
    ordered = sorted(values)
    p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
    return (f"n={len(ordered)}  median {statistics.median(ordered):,.1f}s  "
            f"p90 {p90:,.1f}s  max {ordered[-1]:,.1f}s")


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def read_records(path: Path) -> tuple[list[dict], int]:
    """Every readable line, and how many were not. A torn last line is what
    a killed session leaves, and it is counted, not fatal."""
    rows, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def report(rows: Sequence[dict], *, min_response: float | None = None,
           window: timedelta | None = None) -> dict:
    """What the session measured. Pure: records in, figures out.

    Each figure is computed from the raw records the session wrote, through
    the same parsers the monitor used, so a report can be re-run after a
    parser fix without re-collecting anything.
    """
    out: dict[str, Any] = {}
    start = next((r for r in rows if r["kind"] == "session_start"), {})
    policy = _session_policy(start, min_response=min_response, window=window)
    end = next((r for r in reversed(rows) if r["kind"] == "session_end"), None)
    out["session"] = {"started": start.get("at"),
                      "ended": end.get("at") if end else None,
                      "stop": end.get("reason") if end else "no session_end "
                      "record: the process did not finish writing",
                      "detail": end.get("detail") if end else None,
                      "credits_reserved": end.get("credits_reserved")
                      if end else None}

    odds = [r for r in rows if r["kind"] == "odds"]
    skews, ages, refresh, processing = [], [], [], []
    sightings: Counter = Counter()
    last_seen: dict[tuple[str, str], datetime] = {}
    for row in odds:
        received, provider = _time(row.get("received_at")), _time(
            row.get("provider_date"))
        headers = _time(row.get("headers_at")) or received
        if headers and provider:
            skews.append((headers - provider).total_seconds())
        if isinstance(row.get("processing_seconds"), (int, float)):
            processing.append(float(row["processing_seconds"]))
        parsed = None
        if row.get("payload") is not None and received and not row.get(
                "coverage"):
            stamp = provider or received
            parsed = parse_snapshot({"timestamp": stamp.strftime(
                "%Y-%m-%dT%H:%M:%SZ"), "data": row["payload"]})
        if parsed is None or not parsed.coverage.complete:
            # A POLL THAT DID NOT ANSWER IS A HOLE, here as in the monitor.
            # Observations may have come and gone inside it, so the next one
            # of any game is a first sighting again: it measures neither how
            # stale a new observation is nor how long the last one took.
            last_seen.clear()
            sightings["unanswered_polls"] += 1
            continue
        present: set[tuple[str, str]] = set()
        for quote in parsed.quotes:
            if quote.last_update is None or quote.commence_time <= received:
                continue          # undated, or in play: not a sighting
            key = (quote.provider_event_id, quote.book)
            present.add(key)
            previous = last_seen.get(key)
            if previous is None:
                # Its first appearance may predate our watching, so its age
                # is not a delivery delay and there is no interval to time.
                sightings["first"] += 1
                last_seen[key] = quote.last_update
            elif quote.last_update == previous:
                sightings["repeat"] += 1
            elif quote.last_update < previous:
                # An older copy, as the detector judges it: not news, and
                # not allowed to lower the newest stamp seen.
                sightings["older_copy"] += 1
            else:
                # A NEW observation, first seen on this poll: how old it
                # already was when it reached us, and how long after the
                # previous one the provider made it.
                sightings["new"] += 1
                ages.append((received - quote.last_update).total_seconds())
                refresh.append((quote.last_update - previous).total_seconds())
                last_seen[key] = quote.last_update
        # A GAME MISSING FROM AN ANSWER IS A HOLE for that game.
        for key in [k for k in last_seen if k not in present]:
            del last_seen[key]
    out["polls"] = {"total": len(odds),
                    "answered": sum(1 for r in odds if r.get("payload")
                                    is not None and not r.get("coverage")),
                    "clock_skew": _quantiles(skews),
                    "processing": _quantiles(processing)}
    out["age_when_received"] = _quantiles(ages)
    out["provider_refresh"] = _quantiles(refresh)
    out["sightings"] = {k: sightings.get(k, 0) for k in (
        "first", "new", "repeat", "older_copy", "unanswered_polls")}
    out["cadence_seconds"] = start.get("cadence_seconds")

    # Every refusal the detector made, summed from the per-poll records and
    # the remainder the session_end carries. "0 moves" means nothing until
    # this says whether the detector could see.
    refused: Counter = Counter()
    for row in rows:
        if row["kind"] == "detector":
            refused.update(row.get("rejections") or {})
        elif row["kind"] == "session_end":
            refused.update(row.get("detector") or {})
    out["detector"] = dict(refused)

    triggers = [r for r in rows if r["kind"] == "trigger"]
    decisions = [r for r in rows if r["kind"] == "decision"]
    books = [r for r in rows if r["kind"] == "book"]
    out["moves"] = len(triggers)
    refusals: dict[str, int] = {}
    for d in decisions:
        key = ("admitted" if d.get("admitted") else
               (d.get("refusal") or "not admitted by the screen"))
        refusals[key] = refusals.get(key, 0) + 1
    out["decisions"] = refusals
    out["entries"] = [{"ticker": d.get("market_ticker"),
                       "decision_at": d.get("decision_at"),
                       "predicted": d.get("predicted"),
                       "entry_delay_seconds": d.get("entry_delay_seconds")}
                      for d in decisions if d.get("entered")]
    reads = _book_reads(books)
    by_move = {(t.get("stream_id"), _time(t.get("detected_at"))): t
               for t in triggers}
    ended = _time(end.get("at")) if end else None
    out["responses"] = [
        _response(d, by_move.get((d.get("stream_id"),
                                  _time(d.get("decision_at")))),
                  reads.get(d["market_ticker"], []), policy, ended)
        for d in decisions if d.get("market_ticker")
        and d.get("book_move_for_yes") is not None]
    return out


def _refresh_verdict(seen: dict, cadence: float | None) -> list[str]:
    """Whether the refresh figure measured the provider or this session.

    A poll can only see an observation that exists when it lands. If every
    poll found a new one, the provider re-observed at least once between
    every pair of polls, and the intervals timed are this session's spacing
    with the provider's hidden inside it: a ceiling on its interval, not a
    measurement of it. Only polls that found nothing new show the provider
    being slower than the session.
    """
    compared = seen["new"] + seen["repeat"]
    if not compared:
        return ["                     (no game was seen twice without a "
                "hole between: nothing to time)"]
    spacing = f"{cadence:g}s" if cadence else "session's"
    if not seen["repeat"]:
        return ["  *** no poll found a game unchanged, so the provider "
                "re-observed between",
                f"      every pair of polls and the refresh figure is set by "
                f"the {spacing} poll spacing:",
                "      a ceiling on the provider's interval, not a "
                "measurement of it. Only a",
                "      faster session can measure it."]
    return [f"                     ({seen['repeat']} of {compared} later "
            f"sightings of a game found it unchanged;",
            "                      polling faster than the provider "
            "re-observes buys more of those)"]


def _session_policy(start: dict, *, min_response: float | None,
                    window: timedelta | None) -> ReactionPolicy:
    """The reaction measurement as the SESSION recorded it.

    Not today's defaults: a report re-run after a default changes must still
    judge the session by the thresholds and the window it actually used.
    `min_response` and `window` override for a what-if.
    """
    declared = start.get("reaction_policy") or {}

    def span(value: Any, fallback: timedelta) -> timedelta:
        return (timedelta(seconds=value)
                if isinstance(value, (int, float)) and value > 0 else fallback)

    followed = span(start.get("follow_for_seconds"),
                    span(declared.get("max_wait_seconds"),
                         REACTION_POLICY.max_wait))
    return ReactionPolicy(
        min_response=(min_response if min_response is not None
                      else declared.get("min_response",
                                        REACTION_POLICY.min_response)),
        max_wait=window or followed,
        lookback=span(declared.get("lookback_seconds"),
                      REACTION_POLICY.lookback),
        max_unobserved=span(declared.get("max_unobserved_seconds"),
                            MAX_UNOBSERVED))


def _book_reads(books: Sequence[dict]
                ) -> dict[str, list[tuple[datetime, BookQuote | None]]]:
    """Every read of every contract, in answer order: (when it answered, the
    book -- or None when the read gave nothing a price can be read from).

    Failed reads are KEPT, as None: they are not observations, but a window
    is only as watched as its reads were, and the count of the ones that
    failed is reported beside every outcome.
    """
    out: dict[str, list[tuple[datetime, BookQuote | None]]] = {}
    for row in books:
        ticker, received = row.get("ticker"), _time(row.get("received_at"))
        if not ticker or received is None:
            continue
        book = None
        if row.get("payload") is not None and not row.get("coverage"):
            parsed, coverage = parse_orderbook(
                row["payload"], ticker=ticker, received_at=received,
                sent_at=_time(row.get("sent_at")))
            if (parsed is not None and coverage.complete
                    and parsed.mid is not None):
                book = parsed
        out.setdefault(ticker, []).append((received, book))
    for series in out.values():
        series.sort(key=lambda item: item[0])
    return out


def _response(decision: dict, trigger: dict | None,
              reads: Sequence[tuple[datetime, BookQuote | None]],
              policy: ReactionPolicy, ended: datetime | None) -> dict:
    """What Kalshi did after ONE move, on ONE contract -- measured by the
    replay's own `measure_response`, over this session's reads.

    One rule for both paths (rule 19). Each read describes the book somewhere
    between its request and its answer, so a change is bracketed by
    `sent_at` as well as the receipt; a change located only across the
    trigger is `moved_around_trigger`, not a reaction; a move before it is
    `exchange_moved_before_trigger`; and a window the reads did not cover
    to its end -- reads that failed, stopped, or never came -- is
    `blind_interval`, never a quiet market. Coverage rides along: the reads
    inside the window, the ones that failed, and whether the session itself
    ended before the window did.
    """
    ticker = decision["market_ticker"]
    decided = _time(decision.get("decision_at"))
    bracket = (trigger or {}).get("book_change_bracket") or {}
    reaction = measure_response(
        event_id=str(decision.get("event_id") or ""),
        stream_id=str(decision.get("stream_id") or ""),
        market_ticker=ticker, detected_at=decided,
        book_delta=decision["book_move_for_yes"],
        book_change=Bracket(_time(bracket.get("earliest")),
                            _time(bracket.get("latest"))),
        readings=[book for _, book in reads if book is not None],
        policy=policy)
    row = {"ticker": ticker, "decision_at": decision.get("decision_at"),
           "outcome": reaction.outcome.value,
           "ordering": reaction.ordering.value,
           "lag_seconds": ([reaction.lag_earliest_seconds,
                            reaction.lag_latest_seconds]
                           if reaction.lag_earliest_seconds is not None
                           else None),
           "blind": ({"from": _iso(reaction.blind_from),
                      "to": _iso(reaction.blind_to)}
                     if reaction.blind_from else None),
           "detail": reaction.detail}
    if decided is not None:
        deadline = decided + policy.max_wait
        inside = [book for at, book in reads if decided < at <= deadline]
        row.update(
            window_ends=_iso(deadline),
            reads_in_window=sum(1 for book in inside if book is not None),
            unreadable_in_window=sum(1 for book in inside if book is None),
            session_ended_inside_window=(None if ended is None
                                         else ended < deadline))
    return row


def render_report(figures: dict) -> str:
    s = figures["session"]
    lines = ["SHADOW SESSION", "",
             f"  started            {s['started']}",
             f"  ended              {s['ended']}   ({s['stop']})",
             f"  credits reserved   {s['credits_reserved']}", ""]
    polls = figures["polls"]
    seen = figures["sightings"]
    lines += [f"  polls              {polls['total']} "
              f"({polls['answered']} answered)",
              f"  clock skew         {polls['clock_skew']}",
              f"  processing         {polls['processing']}",
              "                     (body in hand to decision-ready: a delay "
              "every move carries,",
              "                      never counted as time to trade)",
              f"  age when received  {figures['age_when_received']}",
              "                     (how old a NEW provider observation "
              "already was when it reached us:",
              "                      the provider's own delay plus up to one "
              "poll of waiting for ours)",
              f"  provider refresh   {figures['provider_refresh']}",
              "                     (time between successive provider "
              "observations of one game)",
              f"  sightings          {seen['new']} new, {seen['repeat']} "
              f"repeated, {seen['older_copy']} older copies, "
              f"{seen['first']} first, {seen['unanswered_polls']} "
              f"unanswered poll(s)"]
    lines += _refresh_verdict(seen, figures.get("cadence_seconds"))
    lines += ["", f"  moves detected     {figures['moves']}"]
    for key, count in sorted(figures["decisions"].items()):
        lines.append(f"    {key:<34} {count}")
    lines.append(f"  shadow entries     {len(figures['entries'])}")
    for entry in figures["entries"]:
        predicted = entry.get("predicted") or {}
        paid = predicted.get("paid_at_execution")
        lines.append(
            f"    {entry['ticker']}  {predicted.get('side')}: decided at "
            f"{predicted.get('entry_price')} (predicted EV "
            f"{predicted.get('predicted_ev_per_contract'):+.4f}), would have "
            f"paid {'n/a' if paid is None else f'{paid:.4f}'} after "
            f"{entry['entry_delay_seconds']:.1f}s")
    responses = figures["responses"]
    outcomes = Counter(r["outcome"] for r in responses)
    lines.append("  Kalshi after each move, per contract:")
    for key, count in sorted(outcomes.items()):
        lines.append(f"    {key:<34} {count}")
    lines.append("    (only `responded` came demonstrably after we could act; "
                 "the others are why not)")
    lags = [r["lag_seconds"] for r in responses
            if r["outcome"] == ReactionOutcome.RESPONDED.value]
    if lags:
        lines.append(f"    responded within   "
                     f"{', '.join(f'{a:.0f}-{b:.0f}s' for a, b in lags[:12])}")
    cut = sum(1 for r in responses if r.get("session_ended_inside_window"))
    if cut:
        lines.append(f"    {cut} window(s) outlived the session: censored "
                     f"there, not answered")
    unreadable = sum(r.get("unreadable_in_window", 0) for r in responses)
    if unreadable:
        lines.append(f"    {unreadable} read(s) inside the windows gave no "
                     f"usable book")
    lines.append("  quotes the detector did not count as a move, by reason:")
    refused = figures["detector"]
    for key, count in sorted(refused.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"    {key:<34} {count:,}")
    if not refused:
        lines.append("    none recorded")
    lines += ["", "  No orders were placed. Predicted figures read the book "
              "at the decision; realised figures need settlement, which a "
              "live session does not have."]
    return "\n".join(lines)


# --- the command ----------------------------------------------------------------

def _interrupt(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sport", default="NFL")
    parser.add_argument("--series", default="KXNFLGAME")
    parser.add_argument("--hours", type=float, default=None,
                        help="session length. The session stops at the end "
                             "of it, or earlier on a stop rule")
    parser.add_argument("--cadence-seconds", type=float,
                        default=DEFAULT_CADENCE.total_seconds(),
                        help="seconds between sharp-book polls; each costs "
                             f"{CREDITS_PER_LIVE_CALL} credit")
    parser.add_argument("--spend", type=int, default=None, metavar="CREDITS",
                        help="confirm the quoted price. Without it nothing "
                             "is bought")
    parser.add_argument("--api-key", default=None,
                        help="Odds API key (or set ODDS_API_KEY)")
    parser.add_argument("--out-dir", default=str(SHADOW_DIR))
    parser.add_argument("--report", default=None, metavar="SESSION_JSONL",
                        help="summarise a recorded session. Free, offline")
    return parser


def main(argv: Sequence[str] | None = None, *, clock: Any = None,
         slate_source: Callable[[], Slate] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report:
        path = Path(args.report)
        if not path.is_file():
            print(f"no such session file: {path}", file=sys.stderr)
            return EXIT_USAGE
        rows, bad = read_records(path)
        print(render_report(report(rows)))
        if bad:
            print(f"\n  *** {bad} unreadable line(s) skipped (a session "
                  f"killed mid-write leaves one)")
        return EXIT_OK

    if args.hours is None or not args.hours > 0:
        print("--hours must be given and positive", file=sys.stderr)
        return EXIT_USAGE
    cadence = timedelta(seconds=args.cadence_seconds)
    if not cadence >= MIN_CADENCE:
        print(f"--cadence-seconds must be at least "
              f"{MIN_CADENCE.total_seconds():.0f}", file=sys.stderr)
        return EXIT_USAGE
    clock = clock or SystemClock()
    price = session_price(args.hours, cadence)

    print(f"SHADOW MONITOR  {args.sport} {args.series}")
    with only_declared_endpoints():
        slate = (slate_source or (lambda: live_slate(
            clock.now().date(), league=args.sport.upper(),
            series=args.series)))()
    now = clock.now()
    upcoming = sorted(k for k in slate.kickoffs.values() if k > now)
    print(f"  slate: {len(slate.markets)} open contract(s), "
          f"{len(upcoming)} upcoming game(s) with a kickoff")
    if upcoming:
        print(f"  next kickoff {_iso(upcoming[0])}; "
              f"{sum(1 for k in upcoming if k <= now + BOOK_HORIZON)} "
              f"within the screen's {BOOK_HORIZON.total_seconds() / 3600:.0f}h "
              f"ceiling")
    if not slate.coverage.complete:
        print(f"  *** slate retrieval incomplete: {slate.coverage}")
    print(f"\n  session {args.hours:g}h at one poll every "
          f"{cadence.total_seconds():g}s: {session_polls(args.hours, cadence):,}"
          f" poll(s), {price:,} credit(s) at {CREDITS_PER_LIVE_CALL} per call")
    print("  the same session at other cadences:")
    for seconds in (15, 30, 60, 120, 300):
        print(f"    every {seconds:>3}s  "
              f"{session_price(args.hours, timedelta(seconds=seconds)):>7,} "
              f"credits")
    print("  Kalshi reads are free: one book per watched contract per poll, "
          f"then every {FOLLOW_EVERY.total_seconds():g}s for "
          f"{FOLLOW_FOR.total_seconds() / 60:g} minutes after a move.")

    # THE FREE HALF OF THE PREFLIGHT, in the plan. The book's shape is
    # transcribed from documentation; a plan reads one real book so that can
    # be checked before anything is paid for. A paid session reads it again
    # in its own preflight, through the same function, and stops there.
    shape_ok = True
    if args.spend is None and slate.coverage.complete and slate.markets:
        ticker = sorted(slate.markets)[0]
        with only_declared_endpoints():
            book, coverage, _ = read_one_book(ticker, clock.now)
        if book is None or not coverage.complete:
            shape_ok = False
            print(f"\n  *** Kalshi's order book for {ticker} could not be "
                  f"read ({coverage}). The parser's shape is transcribed "
                  f"from documentation; a paid session would stop here, "
                  f"before its first poll.", file=sys.stderr)
        else:
            print(f"\n  Kalshi order book, read free for {ticker}: "
                  f"{book.yes_levels} YES / {book.no_levels} NO level(s), "
                  f"YES {book.bid_close} bid / {book.ask_close} ask -- the "
                  f"transcribed shape parses.")

    if not slate.coverage.complete:
        print("\n  The slate did not fully answer; nothing was bought. Run "
              "again.", file=sys.stderr)
        return EXIT_STOPPED
    if args.spend is None:
        if not shape_ok:
            print("\n  Nothing was spent. The book parser has to read the "
                  "real shape before a session is worth running.",
                  file=sys.stderr)
            return EXIT_STOPPED
        print(f"\n  Nothing was spent. To run, re-run with --spend {price}.")
        return EXIT_OK
    if args.spend < price:
        print(f"\n  --spend {args.spend} is below the {price} this session "
              f"may need; refusing rather than stopping partway.",
              file=sys.stderr)
        return EXIT_USAGE
    api_key = args.api_key or os.environ.get("ODDS_API_KEY", "")
    if not api_key:
        print("  need --api-key or ODDS_API_KEY", file=sys.stderr)
        return EXIT_USAGE

    assert_nothing_here_can_trade()
    # A stop from the service manager (SIGTERM) ends the session like Ctrl-C
    # does -- with a session_end record -- rather than cutting it off
    # mid-write with no word of why it ended.
    signal.signal(signal.SIGTERM, _interrupt)
    started = clock.now()
    path = Path(args.out_dir) / f"shadow_{started:%Y%m%dT%H%M%SZ}.jsonl"
    recorder = Recorder(path)
    monitor = ShadowMonitor(
        sport=args.sport, series=args.series, api_key=api_key,
        cadence=cadence, hours=args.hours,
        ledger=CreditLedger(cap=price), recorder=recorder, clock=clock,
        slate_source=slate_source)
    print(f"\n  recording to {path}")
    try:
        stop = monitor.run()
    finally:
        recorder.close()
    print(f"\n  session ended: {stop.reason}"
          f"{': ' + stop.detail if stop.detail else ''}")
    print(f"  {monitor.ledger}")
    print(f"  next: python3 shadow_monitor.py --report {path}")
    return EXIT_OK if stop.ok else EXIT_STOPPED


if __name__ == "__main__":
    raise SystemExit(main())
