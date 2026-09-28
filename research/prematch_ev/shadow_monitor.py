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
   follows is known, with its assessment (`reaction.assessment`): the named
   reason, both sides' prices and costs, the books' ages and depth, the
   fee's provenance, and whether a missing book was never due, a one-sided
   market or a failed collection.
4. The hourly slate rejoin, if one is due -- AFTER the moves, so its dozen
   requests never sit between a move and its execution read.
5. A moved game is then followed every few seconds, free, for the declared
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
  credit cap             the session spent what it was allowed -- or has too
                         little left for a recovery probe, which is checked
                         before each one is scheduled
  refused key            HTTP 401/403 on any poll: the key was refused or the
                         account is out, and no wait mends that
                         (`auth_refused`, on the first one)
  rejected request       any other 4xx: the request itself is wrong -- the
                         sport key, a parameter, the endpoint
                         (`request_rejected`)
  untrusted certificate  this machine does not trust the certificate it was
                         shown (`tls_certificate_refused`)
  unexpected shape       a 200 whose JSON is not the documented list
  unrecovered outage     an outage outlasted its bounds (`outage_unrecovered`,
                         below)
  rate limit             the provider asked for a wait longer than an outage
                         may last (`rate_limited`)
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

OUTAGES: BOUNDED RECOVERY, NOT A STOP
-------------------------------------
Three polls failing in a row used to END the session. At a 30s cadence that
is a minute or two of lost network ending a 24-hour session, and the
2026-09-25 validation stopped at 10h49m on exactly that. Now three
consecutive TRANSIENT failures -- a timeout, a dropped connection, DNS, a
5xx, a 429 (`data.failures` says which is which) -- begin an OUTAGE:

  * paid polling PAUSES. Nothing is bought while the provider is
    unreachable; the free Kalshi follow reads carry on.
  * one recovery PROBE -- an ordinary tick: its decision reads, then one
    paid poll -- is made after a backoff of 60s, then 120s, 240s, 480s and
    600s from then on: never less than the cadence, and never before a
    Retry-After the provider sent. Each probe therefore stands in for at
    least one poll the session's grid skipped, and polling resumes on that
    same grid afterwards, so an outage can never add a poll the session was
    not priced for.
  * the outage is BOUNDED: at most RECOVERY_MAX_PROBES (6) probes, and none
    later than RECOVERY_MAX_OUTAGE (45 minutes) after its first failure.
    Past either the session stops, `outage_unrecovered`, with the
    chronology in its records. It never loops, and the failure threshold is
    not raised.
  * BOTH HARD LIMITS HOLD. Every probe is a paid attempt, reserved against
    the session's one cap before it is sent -- a probe whose answer is lost
    to a timeout included. The remaining budget and the account's quota
    floor are checked before a probe is scheduled and again before it is
    sent. Recovery never moves the session's end and never creates a
    budget, and nothing starts a second session.
  * THE BOUNDS ARE CHECKED WHERE A REQUEST LEAVES. A probe's outage bound
    is checked when it is scheduled, again when it runs -- a machine that
    slept through its due time wakes past the bound -- and again after its
    book reads, immediately before the paid request, since reads can run
    across it. The bound is inclusive: a probe may leave at exactly 45
    minutes after the first failure, and not after.

THE END IS A HARD STOP
----------------------
The authorized end is exclusive, and checked at dispatch, not only at the
top of the loop: a tick's book reads block before its paid poll, and a
review's slow-read reproduction sent a poll 51 seconds after the end that
way. So:

  * no new work starts at or after the end -- no book read, no paid poll or
    probe, no follow, no rejoin -- and the paid request is refused BEFORE
    anything is reserved (`dispatch_refused`);
  * underneath every fetcher, the dispatch gate refuses any request at or
    after the end (`AuthorizedEndReached`, raised before anything is sent)
    and clips each request's timeout to the authorized time left, so one
    sent just before the end cannot run on long after it;
  * an answer to a request that left before the end but arrived after it is
    IN FLIGHT, not late: it is recorded (`after_authorized_end`) and never
    acted on -- no quote of it reaches the detector;
  * the status file's `deadline` block shows the end, the last paid
    request's send time, the paid requests sent at or after the end (zero)
    and how long in-flight work ran past it, and `--report` counts late
    dispatches from any session's own clocks.
  * a TERMINAL failure stops at once, in or out of an outage: waiting cannot
    mend a refused key, a rejected request, an untrusted certificate or an
    unexpected shape, and every further attempt costs a credit.

AN OUTAGE IS MISSING OBSERVATIONS, NOT A LONGER INTERVAL
--------------------------------------------------------
  * every failed poll declares a gap on every stream (`note_gap`), so the
    first answer after recovery re-establishes each baseline instead of
    closing a move across the blind interval.
  * so does a poll the monitor itself did not make. Consecutive polls more
    than MAX_UNPOLLED_CADENCES (three) cadences apart -- the machine asleep,
    the process suspended, the loop held up behind reads -- are a gap
    declared exactly like a failed poll's. The detector's own 35-minute
    `max_gap` was set for a 5-minute archive grid; left to it, a sleeping
    laptop's first poll on waking would be compared with the price from
    before it slept.
  * the LAST stretch has no poll after it to declare it, so it is checked
    when the session stops: a machine asleep through the end, or reads that
    ran to it, leave a `not_polled` gap to the stop, unrecovered -- and a
    session that did not see its last twenty minutes is not `complete`.
  * after an outage or such a gap, every Kalshi book read before it leaves
    the decision memory (`books_invalidated`): the next decision is
    screened on books read since, and if those reads failed there is no
    decision book -- the screen says so -- rather than a stale one.
  * the slate is rejoined on the first answer after an outage or a gap.
  * each blind interval is recorded as a `gap` -- its cause, its bounds, the
    polls and probes inside it -- and summarised at the end.

RECORDS
-------
One append-only JSONL file per session under `study_output/shadow/`: every
raw response with the clocks around it, every move, every decision, every
gap, every failure described (`failure`: category, phase, elapsed, the
attempt and the last answer before it), the detector's refusals for each
poll by reason, and the reason the session ended. `--report` reads it, and
prints "0 moves" beside those refusals, so a detector that could not see is
never mistaken for a quiet market, and lists every failed poll in order, so
an outage's chronology is read from the file rather than remembered. The API
key is in no record: a recorded URL is its path, never its query.

Beside it, `<session>.status.json`: whether the session was COMPLETE,
RECOVERED WITH GAPS, ENDED IN AN OUTAGE or STOPPED EARLY, why, how long it
actually ran, what it attempted and reserved, its gaps, and where its
records are. Written however the session ends -- an unrecovered outage, an
interrupt, a crash -- and local only: nothing is sent anywhere.

`--report` then rebuilds every assessment from those raw records, offline
and unable to open a connection (`shadow_diagnostics`), so a session recorded
before assessments existed gets them, and one recorded after gets them
checked.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import statistics
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from analysis.scoring import Eligibility                          # noqa: E402
from core.fees import DEFAULT_ROUTE                                # noqa: E402
from collect import Ledger, join_markets, yes_side                 # noqa: E402
from collect_reaction import (                                     # noqa: E402
    OUTPUT_DIR, Slate, assert_nothing_here_can_trade,
    only_declared_endpoints, resolve_days,
)
from data import espn_schedule, kalshi_history                     # noqa: E402
from data.cache import CreditCapReached, redact                    # noqa: E402
from data.failures import scrub, status_category                   # noqa: E402
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
    Bracket, ReactionOutcome, ReactionPolicy, inside_window, measure_response,
    request_span,
)
from reaction.assessment import (                                  # noqa: E402
    SCHEMA as ASSESSMENT_SCHEMA, TickContext, describe, read_status,
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
#: Consecutive transient failures that begin an outage. Not raised to ride
#: out a longer one: an outage is handled by pausing and probing, below.
OUTAGE_AFTER_FAILED_POLLS = 3
#: The recovery backoff: the first probe this long after an outage begins,
#: doubling after each failed probe up to RECOVERY_MAX_BACKOFF -- 60, 120,
#: 240, 480, 600, 600s. Never less than the cadence (`recovery_backoff`).
RECOVERY_FIRST_BACKOFF = timedelta(seconds=60)
RECOVERY_MAX_BACKOFF = timedelta(minutes=10)
#: Probes per outage. Six fit the backoff inside RECOVERY_MAX_OUTAGE with
#: room for slow attempts, so this bounds the credits one outage can spend
#: and the duration bounds its wall time -- a probe that is slow, or a
#: machine that slept, reaches that one first.
RECOVERY_MAX_PROBES = 6
#: The longest an outage may last, from its first failed poll to the last
#: probe it may make. Past it the session stops, recorded, however many
#: probes remain.
RECOVERY_MAX_OUTAGE = timedelta(minutes=45)
#: Consecutive polls further apart than this many cadences leave a GAP the
#: detector is told about, as a failed poll's is: at least two planned polls
#: were never made. The line MAX_UNOBSERVED draws for followed contracts --
#: one lost read inside it, two in a row not -- drawn for the sharp feed.
MAX_UNPOLLED_CADENCES = 3
MAX_CLOCK_SKEW = timedelta(seconds=5)
QUOTA_FLOOR = 50
#: How far back polled books are kept in memory for decisions. The records
#: keep everything; this only bounds what a long session holds.
BOOK_MEMORY = timedelta(hours=2)
#: How far past the measured entry delay the execution read may land and
#: still be the execution book: the read itself IS the delay, so the only
#: slack needed is clock rounding.
ENTRY_TOLERANCE = timedelta(seconds=1)

EXIT_OK, EXIT_STOPPED, EXIT_USAGE = 0, 1, 2

SHADOW_DIR = OUTPUT_DIR / "shadow"


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


def code_version() -> dict:
    """The commit this study's code is at, and whether it has local edits.

    Recorded on every session and every analysis, so a validation session
    can SHOW which frozen policies it was collected and analysed under
    rather than assert it. Unknown -- no git, not a checkout -- is None,
    never a guess.
    """
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(HERE), *args],
                                  capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    head, status = git("rev-parse", "HEAD"), git("status", "--porcelain",
                                                 "--", ".")
    return {"commit": head.strip() or None if head is not None else None,
            "dirty": None if status is None else bool(status.strip())}


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


class AuthorizedEndReached(RuntimeError):
    """A request tried to leave at or after the session's authorized end.

    Raised by the dispatch gate BEFORE anything is sent. Not an OSError or a
    URLError, so no fetcher can mistake it for a transport failure and retry
    or record it as one: it ends the session.
    """


#: The stop each TERMINAL failure category ends the session with.
TERMINAL_STOPS = {
    "auth": "auth_refused",
    "request_rejected": "request_rejected",
    "tls_certificate": "tls_certificate_refused",
    "unexpected_shape": "unexpected_shape",
}


def recovery_backoff(probes_made: int, cadence: timedelta) -> timedelta:
    """How long an outage waits before its next probe: RECOVERY_FIRST_BACKOFF,
    doubling per failed probe up to RECOVERY_MAX_BACKOFF -- and never less
    than the cadence, so recovery never polls faster than the session was
    priced at."""
    wait = RECOVERY_FIRST_BACKOFF * (2 ** min(probes_made, 16))
    return max(cadence, min(wait, RECOVERY_MAX_BACKOFF))


def recovery_policy() -> dict:
    """The recovery bounds, as every session records them."""
    return {"outage_after_failed_polls": OUTAGE_AFTER_FAILED_POLLS,
            "first_backoff_seconds": RECOVERY_FIRST_BACKOFF.total_seconds(),
            "max_backoff_seconds": RECOVERY_MAX_BACKOFF.total_seconds(),
            "max_probes": RECOVERY_MAX_PROBES,
            "max_outage_seconds": RECOVERY_MAX_OUTAGE.total_seconds(),
            "max_unpolled_cadences": MAX_UNPOLLED_CADENCES,
            "terminal": sorted(TERMINAL_STOPS)}


@dataclass
class FailureRun:
    """Consecutive failed polls, from the first to the answer that ends them.

    Every run is a gap. One that reaches OUTAGE_AFTER_FAILED_POLLS becomes an
    OUTAGE: paid polling pauses and recovery probes take over.
    """

    started: datetime                   # the first failed poll's request
    last_answer: datetime | None        # the answered poll before it, if any
    failed_polls: int = 0
    categories: Counter = field(default_factory=Counter)
    last_failure: dict | None = None
    outage_at: datetime | None = None   # when it became an outage
    #: Probes begun -- each one's number -- and probes whose paid request
    #: actually left. They differ only by a probe refused at dispatch, which
    #: ends the session.
    probes: int = 0
    probes_sent: int = 0
    next_probe_at: datetime | None = None

    @property
    def is_outage(self) -> bool:
        return self.outage_at is not None


def blind_seconds(gaps: Iterable[dict]) -> float:
    """The time the gaps cover, overlaps counted once. A failed poll after a
    suspension is recorded under both causes; it was blind once."""
    spans = sorted((start, end) for start, end in (
        (_time(g.get("from")) if isinstance(g.get("from"), str)
         else g.get("from"),
         _time(g.get("to")) if isinstance(g.get("to"), str) else g.get("to"))
        for g in gaps) if start is not None and end is not None
        and end > start)
    total, reach = 0.0, None
    for start, end in spans:
        if reach is None or start > reach:
            total += (end - start).total_seconds()
            reach = end
        elif end > reach:
            total += (end - reach).total_seconds()
            reach = end
    return total


def session_status(*, reason: str, in_outage: bool, gaps: int) -> str:
    """The session's verdict on itself, in one word.

    `complete`: it ran to its authorized end and every interval was
    observed. `recovered_with_gaps`: it ran to its end, with the blind
    intervals its gaps list. `ended_in_outage`: its end arrived while an
    outage was unrecovered, so it ran the whole window without seeing the
    last of it. `stopped_early`: any stop before the authorized end.
    """
    if reason != "end_of_session":
        return "stopped_early"
    if in_outage:
        return "ended_in_outage"
    return "recovered_with_gaps" if gaps else "complete"


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


def _book_failure(coverage: Coverage, sent: datetime,
                  received: datetime) -> dict:
    """A failed book read described, for its record: the classified cause
    when the fetcher established one, and the read's own elapsed time."""
    failure = coverage.failure
    if hasattr(failure, "as_dict"):
        row = failure.as_dict()
    else:
        # An answer that arrived and did not parse: the transcribed shape.
        row = {"category": "unexpected_shape", "phase": "shape",
               "detail": "; ".join(coverage.reasons)}
    row["elapsed_seconds"] = (received - sent).total_seconds()
    return row


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
                 book_horizon: timedelta = BOOK_HORIZON,
                 status_path: Path | None = None):
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
        #: The run of consecutive failed polls under way, if one is: a gap,
        #: and an outage once it reaches OUTAGE_AFTER_FAILED_POLLS.
        self.failures: FailureRun | None = None
        #: Every blind interval closed so far, as recorded.
        self.gaps: list[dict] = []
        self.last_poll_ready: datetime | None = None
        self.last_answer: datetime | None = None
        #: The provider's Retry-After, as an instant: no paid poll before it.
        self.not_before: datetime | None = None
        self.max_poll_spacing = MAX_UNPOLLED_CADENCES * cadence
        #: True while the tick under way is a recovery probe.
        self.probing = False
        self.ends: datetime | None = None
        #: When the last paid request left, and how many left at or after
        #: the authorized end -- none, by construction; the status file
        #: carries the count so a session can SHOW it rather than claim it.
        self.last_paid_sent: datetime | None = None
        self.paid_after_end = 0
        #: Answers to requests sent before the end that arrived after it:
        #: in flight, recorded, never acted on.
        self.answers_after_end = 0
        self.status_path = status_path or recorder.path.with_suffix(
            ".status.json")
        self.status: dict | None = None
        self.cost_verified = False
        self.dates_verified = False
        self.next_rejoin: datetime | None = None
        self.last_read_status = None
        #: What this tick knew when its decision reads began: the horizon
        #: judged from its start, the join then, and each read's status.
        #: Every decision records it, so a missing book can be told apart
        #: from a book that was never due.
        self.tick_context: TickContext | None = None
        self.counts = {"polls": 0, "polls_failed": 0, "triggers": 0,
                       "decisions": 0, "entries": 0, "book_reads": 0,
                       "book_failures": 0, "in_play_skipped": 0,
                       "assessment_errors": 0, "outages": 0,
                       "recovery_probes": 0, "gaps": 0}

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
        failure = None
        if not coverage.complete:
            self.counts["book_failures"] += 1
            failure = _book_failure(coverage, sent, received)
        self.last_read_status = read_status(payload is not None, book)
        self.recorder.write(
            "book", ticker=watched.ticker, purpose=purpose, sent_at=sent,
            received_at=received, payload=payload,
            coverage=coverage.reasons, **(context or {}),
            **({"failure": failure} if failure else {}))
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

    # --- the time bounds, checked at dispatch ----------------------------------
    #
    # Checked where a request is about to LEAVE, not only where the loop
    # decided to start the work. A tick's decision reads block before its
    # paid poll, and a probe's before its paid probe; either can run long
    # enough to cross a bound the loop checked on the way in.

    def _past_end(self) -> bool:
        """The authorized end is EXCLUSIVE: at it, or after, nothing new
        starts -- no read, no poll, no probe, no rejoin."""
        return self.ends is not None and self.clock.now() >= self.ends

    def _paid_request_refused(self) -> Stop | None:
        """Immediately before a paid request is reserved and sent: after the
        reads that precede it. Nothing is reserved when this refuses."""
        now = self.clock.now()
        request = "probe" if self.probing else "poll"
        if self.ends is not None and now >= self.ends:
            self.recorder.write("dispatch_refused", at=now, request=request,
                                reason="authorized_end", ends=self.ends)
            return self._end_of_session()
        run = self.failures
        if (self.probing and run is not None
                and now - run.started > RECOVERY_MAX_OUTAGE):
            self.recorder.write("dispatch_refused", at=now, request=request,
                                reason="outage_bound",
                                first_failure_at=run.started)
            return self._outage_expired(now, "its reads ended")
        return None

    def _outage_expired(self, now: datetime, when: str) -> Stop:
        """The outage bound is INCLUSIVE: a probe may leave at exactly
        RECOVERY_MAX_OUTAGE after the first failure, and never after."""
        run = self.failures
        return self._unrecovered(
            f"the probe was not sent: {when} "
            f"{(now - run.started).total_seconds() / 60:.1f} minutes after "
            f"the first failure, beyond the "
            f"{RECOVERY_MAX_OUTAGE.total_seconds() / 60:g}-minute bound")

    @contextmanager
    def _dispatch_gate(self) -> Iterator[None]:
        """No request of ANY kind leaves at or after the authorized end.

        The loop's own checks stop the session before this is reached; this
        is the backstop for any path they miss, installed process-wide as
        the endpoint gate is, and raising before anything is sent. It also
        clips each request's timeout to the authorized time left, so one
        sent just before the end cannot run on long after it.
        """
        real = urllib.request.urlopen

        def gated(request: Any, *args: Any, **kwargs: Any) -> Any:
            if self.ends is not None:
                now = self.clock.now()
                left = (self.ends - now).total_seconds()
                if left <= 0:
                    raise AuthorizedEndReached(
                        f"a request at {_iso(now)} would leave at or after "
                        f"the authorized end {_iso(self.ends)}; it was not "
                        f"sent")
                timeout = kwargs.get("timeout")
                if not args and isinstance(timeout, (int, float)):
                    kwargs["timeout"] = min(timeout, left)
                elif not args and timeout is None:
                    kwargs["timeout"] = left
            return real(request, *args, **kwargs)

        urllib.request.urlopen = gated
        try:
            yield
        finally:
            urllib.request.urlopen = real

    # --- one tick ----------------------------------------------------------

    def tick(self) -> Stop | None:
        now = self.clock.now()
        joined = frozenset(w.ticker for contracts in self.watched.values()
                           for w in contracts)
        reads: dict[str, str] = {}
        abandoned = None
        for watched in self.contracts_in_horizon(now):
            if self._past_end():
                # NO NEW WORK AFTER THE END. The reads stop here, and the
                # paid request below is refused before it is reserved.
                break
            book = self.read_book(watched, "decision")
            reads[watched.ticker] = self.last_read_status.value
            if book is None:
                # ONE failed read abandons the rest of this tick's reads: a
                # stalled exchange must not hold the paid poll behind two
                # dozen timeouts. A move this tick then has no decision book
                # and the screen says so.
                self.recorder.write("decision_reads_abandoned",
                                    at=self.clock.now(),
                                    after=watched.ticker)
                abandoned = watched.ticker
                break
        self.tick_context = TickContext(at=now, horizon=self.book_horizon,
                                        joined=joined, reads=reads,
                                        abandoned_after=abandoned)

        # THE BOUNDS AT DISPATCH: the reads above may have run past the
        # authorized end, or past a probe's outage bound.
        refused = self._paid_request_refused()
        if refused is not None:
            return refused
        live = fetch_live_odds(self.sport, self.api_key, ledger=self.ledger,
                               now=self.clock.now)
        self.last_paid_sent = live.sent_at
        if self.probing and self.failures is not None:
            self.failures.probes_sent += 1
            self.counts["recovery_probes"] += 1
        if self.ends is not None and live.sent_at >= self.ends:
            self.paid_after_end += 1
        parsed = parse_snapshot(live.snapshot_body()) if live.ok else None
        # DECISION READINESS: the answer has arrived in full AND been read.
        # Moves are dated here, never earlier. The time between the body
        # arriving and this is recorded as processing, not credited to the
        # opportunity as though a bot could have traded during it.
        ready_at = self.clock.now()
        self.counts["polls"] += 1
        after_end = self.ends is not None and ready_at >= self.ends
        # CONTINUITY BEFORE CONTENT: a hole the monitor left by not polling
        # is declared before this answer's quotes reach the detector, and
        # written before this poll's record, so every reader meets it first.
        if not after_end:
            self._check_continuity(live, ready_at)
        failure = self._describe_failure(live, parsed)
        self.recorder.write(
            "odds", url=recorded_url(live_odds_url(self.sport, "")),
            sent_at=live.sent_at, headers_at=live.headers_at,
            received_at=live.received_at, ready_at=ready_at,
            processing_seconds=(
                (ready_at - live.received_at).total_seconds()
                if live.received_at else None),
            provider_date=live.provider_date, status=live.status,
            charged=live.charged, used=live.used, remaining=live.remaining,
            payload=live.payload, coverage=live.coverage.reasons,
            **({"failure": failure} if failure else {}),
            **({"after_authorized_end": True} if after_end else {}))
        if after_end:
            # IN-FLIGHT COMPLETION, NOT LATE DISPATCH: this request left
            # before the end and its answer arrived after it. Recorded -- it
            # was paid for -- and never acted on: no quote of it reaches the
            # detector, so no move, decision or read follows it.
            self.answers_after_end += 1
            if failure is not None:
                self._note_failed_poll(live, failure)
            return self._end_of_session()
        stop = self.absorb(live, parsed, ready_at, now, failure)
        # WHY NOTHING TRIGGERED, for THIS answer: recorded once its quotes
        # have been through the detector, so every count sits beside the
        # poll it belongs to -- and a detector that refuses everything shows
        # it on the poll where it started.
        self.recorder.write("detector", received_at=live.received_at,
                            rejections=self._drain_rejections())
        return stop

    def _describe_failure(self, live: LiveOdds, parsed: Any) -> dict | None:
        """A failed poll, described for its record -- None when it answered.

        The fetcher's classification (`data.failures`), with the monitor's
        own facts beside it: which attempt of the run of consecutive failures
        this was, which recovery probe if it was one, and when the feed last
        answered. Scrubbed at the source; nothing here carries the key.
        """
        if parsed is not None and parsed.coverage.complete:
            return None
        failure = live.failure
        if failure is not None:
            row = failure.as_dict()
        elif live.ok:
            # The body arrived as a list and the snapshot parser still
            # refused it: the shape is not the one transcribed.
            row = {"category": "unexpected_shape", "terminal": True,
                   "phase": "parse", "exception": None, "status": live.status,
                   "errno": None, "elapsed_seconds": None,
                   "retry_after_seconds": None,
                   "detail": "; ".join(parsed.coverage.reasons
                                       if parsed else [])}
        else:
            row = {"category": "network", "terminal": False, "phase": None,
                   "exception": None, "status": live.status, "errno": None,
                   "elapsed_seconds": None, "retry_after_seconds": None,
                   "detail": "; ".join(live.coverage.reasons)
                   or "no response"}
        run = self.failures
        row["attempt"] = (run.failed_polls if run else 0) + 1
        row["probe"] = run.probes if run is not None and self.probing else None
        row["last_answer_at"] = self.last_answer
        return row

    def _check_continuity(self, live: LiveOdds, ready_at: datetime) -> None:
        """Declare the hole if the sharp book went unpolled too long.

        Measured from the last poll's readiness to this one's receipt, so a
        machine that slept between polls and one that slept inside a poll
        are caught alike. A recovery probe is exempt: its outage is already
        the gap, declared by the failed polls that began it.
        """
        previous = self.last_poll_ready
        self.last_poll_ready = ready_at
        if previous is None or self.probing:
            return
        received = live.received_at or ready_at
        if received - previous > self.max_poll_spacing:
            self._unpolled(previous, ready_at)

    def _unpolled(self, since: datetime, until: datetime) -> None:
        span = (until - since).total_seconds()
        why = (f"the sharp book was not polled for {span:,.0f}s, beyond "
               f"{MAX_UNPOLLED_CADENCES} cadences "
               f"({self.max_poll_spacing.total_seconds():g}s)")
        for stream in sorted(self.seen):
            self.detector.note_gap(stream, until, why)
        self._record_gap({
            "cause": "not_polled", "from": since, "to": until,
            # The next poll was due one cadence after the last; from then
            # on the last one no longer stood for the book.
            "unobserved_from": since + self.cadence,
            "seconds": span,
            "limit_seconds": self.max_poll_spacing.total_seconds(),
            "failed_polls": 0, "probes": 0, "recovered": True})
        self._invalidate_books(until, why)
        self.next_rejoin = None

    def _unpolled_tail(self, ended: datetime) -> None:
        """The stretch after the last poll, when the session ended inside it.

        `_check_continuity` declares a hole when the poll that ends it
        arrives; the last stretch of a session has no such poll. A machine
        asleep through the end, or book reads that ran to it -- whose poll
        the end then refused -- left it unobserved all the same, and a
        session that did not see its last twenty minutes is not `complete`.
        Measured to when the session stopped, as a run of failures still
        open at the end is: the offline rebuild judges markouts up to that
        same stop, and one inside the stretch must be `not_polled`, never
        the last price carried across a machine asleep.
        """
        last = self.last_poll_ready
        if last is None or ended - last <= self.max_poll_spacing:
            return
        self._record_gap({
            "cause": "not_polled", "from": last, "to": ended,
            "unobserved_from": last + self.cadence,
            "seconds": (ended - last).total_seconds(),
            "limit_seconds": self.max_poll_spacing.total_seconds(),
            "failed_polls": 0, "probes": 0,
            # No poll ended it: the session ended inside it.
            "recovered": False})

    def _record_gap(self, gap: dict) -> None:
        self.gaps.append(gap)
        self.counts["gaps"] += 1
        self.recorder.write("gap", **gap)

    def _invalidate_books(self, at: datetime, why: str) -> None:
        """Nothing read before `at` may be a decision book after it.

        The screen takes the newest book at or before a move; after a blind
        interval that could be one read before it, however old. Dropping the
        memory makes the next decision's book one read since -- or none,
        which the screen reports -- never a stale one. Always recorded, even
        when there was nothing to drop, because the offline rebuild clears
        its own memory on this record (`shadow_diagnostics.replay`).
        """
        dropped = sum(len(history) for history in self.books.values())
        self.books.clear()
        self.recorder.write("books_invalidated", at=at, reason=why,
                            dropped=dropped)

    def _note_failed_poll(self, live: LiveOdds, failure: dict) -> FailureRun:
        """A poll that did not answer, counted into its run of failures."""
        self.counts["polls_failed"] += 1
        run = self.failures
        if run is None:
            run = self.failures = FailureRun(started=live.sent_at,
                                             last_answer=self.last_answer)
        run.failed_polls += 1
        run.categories[failure["category"]] += 1
        run.last_failure = failure
        # THE INTERVAL WAS NOT OBSERVED. Every stream re-anchors on its next
        # good quote rather than closing a move across the hole.
        for stream in sorted(self.seen):
            self.detector.note_gap(stream, live.received_at,
                                   "the live poll did not answer")
        return run

    def _failed_poll(self, live: LiveOdds, failure: dict,
                     ready_at: datetime) -> Stop | None:
        """A poll that did not answer: a gap, and perhaps a stop or an outage."""
        run = self._note_failed_poll(live, failure)
        if failure["terminal"]:
            return Stop(TERMINAL_STOPS.get(failure["category"],
                                           failure["category"]),
                        f"{failure['detail']} ({failure['category']}, "
                        f"phase {failure['phase']}): waiting cannot mend "
                        f"this, and every further poll would cost a credit")
        retry = failure.get("retry_after_seconds")
        if retry is not None:
            # RATE-LIMIT GUIDANCE IS OBEYED, not raced: no paid poll before
            # the instant the provider named -- and a wait beyond what an
            # outage may last is a stop, not a very long sleep.
            if retry > RECOVERY_MAX_OUTAGE.total_seconds():
                return Stop("rate_limited",
                            f"the provider asked for {retry:,.0f}s before the "
                            f"next request, beyond the "
                            f"{RECOVERY_MAX_OUTAGE.total_seconds() / 60:g}-"
                            f"minute outage bound")
            wait_until = (live.received_at or ready_at) + timedelta(
                seconds=retry)
            self.not_before = max(wait_until, self.not_before or wait_until)
        if run.is_outage:
            return self._schedule_probe(ready_at)
        if run.failed_polls >= OUTAGE_AFTER_FAILED_POLLS:
            run.outage_at = ready_at
            self.counts["outages"] += 1
            # The first answer after it rejoins the slate: an outage can
            # outlast the join the monitor was working from.
            self.next_rejoin = None
            self.recorder.write(
                "outage_start", at=ready_at, first_failure_at=run.started,
                last_answer_at=run.last_answer,
                failed_polls=run.failed_polls,
                categories=dict(run.categories), policy=recovery_policy(),
                credits_reserved=self.ledger.spent_this_run)
            return self._schedule_probe(ready_at)
        return None

    def _schedule_probe(self, at: datetime) -> Stop | None:
        """The outage's next probe -- or the stop its bounds call for."""
        run = self.failures
        if run.probes >= RECOVERY_MAX_PROBES:
            return self._unrecovered(f"all {RECOVERY_MAX_PROBES} recovery "
                                     f"probes failed")
        wait = recovery_backoff(run.probes, self.cadence)
        due = at + wait
        if self.not_before is not None and self.not_before > due:
            due = self.not_before
        if due - run.started > RECOVERY_MAX_OUTAGE:
            return self._unrecovered(
                f"a further probe would fall "
                f"{(due - run.started).total_seconds() / 60:.1f} minutes "
                f"after the first failure, beyond the "
                f"{RECOVERY_MAX_OUTAGE.total_seconds() / 60:g}-minute bound")
        stop = self._probe_affordable()
        if stop is not None:
            return stop
        run.next_probe_at = due
        self.recorder.write("recovery_scheduled", probe=run.probes + 1,
                            at=at, due=due, backoff_seconds=wait,
                            retry_after_until=(
                                self.not_before if self.not_before is not None
                                and self.not_before > at else None))
        return None

    def _probe_affordable(self) -> Stop | None:
        """A probe is a paid attempt: the budget and the account are checked
        before one is scheduled, and again before it is sent."""
        ledger = self.ledger
        if (ledger.cap is not None
                and ledger.spent_this_run + CREDITS_PER_LIVE_CALL > ledger.cap):
            return Stop("credit_cap",
                        f"no credit left for a recovery probe: "
                        f"{ledger.spent_this_run} reserved of a {ledger.cap} "
                        f"cap, and a probe reserves "
                        f"{CREDITS_PER_LIVE_CALL}")
        if ledger.remaining is not None and ledger.remaining <= QUOTA_FLOOR:
            return Stop("quota_floor",
                        f"the account reported {ledger.remaining} credits "
                        f"left, at or below the floor of {QUOTA_FLOOR}; no "
                        f"probe is made")
        return None

    def _unrecovered(self, why: str) -> Stop:
        run = self.failures
        last = run.last_failure or {}
        return Stop("outage_unrecovered",
                    f"{why}: {run.failed_polls} failed poll(s), "
                    f"{run.probes_sent} of them probes, since "
                    f"{_iso(run.started)}; last failure "
                    f"{last.get('category')} ({last.get('phase')}): "
                    f"{last.get('detail')}")

    def _close_failures(self, until: datetime, *, recovered: bool) -> None:
        """The run of failed polls is over: its blind interval, recorded."""
        run, self.failures = self.failures, None
        self._record_gap({
            "cause": "outage" if run.is_outage else "failed_polls",
            "from": run.last_answer or run.started, "to": until,
            "first_failure_at": run.started, "outage_at": run.outage_at,
            "seconds": (until - (run.last_answer or run.started))
            .total_seconds(),
            "failed_polls": run.failed_polls, "probes": run.probes_sent,
            "categories": dict(run.categories), "recovered": recovered,
            "last_failure": run.last_failure})

    def absorb(self, live: LiveOdds, parsed: Any, ready_at: datetime,
               now: datetime, failure: dict | None = None) -> Stop | None:
        """One answer through the stops, the join and the detector."""
        if failure is None and (parsed is None
                                or not parsed.coverage.complete):
            failure = self._describe_failure(live, parsed)
        if failure is not None:
            return self._failed_poll(live, failure, ready_at)
        if self.failures is not None:
            # THE FAILURES ARE OVER. The blind interval ends here, and every
            # stream was gapped by them, so this answer re-anchors rather
            # than moves.
            self._close_failures(ready_at, recovered=True)
        self.last_answer = ready_at
        stop = self.check_answer(live) or self.check_dates(parsed.quotes,
                                                           live.received_at)
        if stop:
            return stop

        quotes_by_event: dict[str, list] = {}
        for quote in parsed.quotes:
            quotes_by_event.setdefault(quote.provider_event_id, []).append(quote)
        rejoin_due = self.next_rejoin is None or now >= self.next_rejoin

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
        # THE HOURLY REJOIN COMES AFTER THIS ANSWER'S MOVES, never between a
        # move and its execution read. It is a dozen free requests; an
        # execution read that waited behind them recorded the monitor's
        # housekeeping as entry delay -- 2.4s of it in the synthetic session
        # that found this. A move on a rejoin tick is screened on the join
        # before it, at most an hour old; the first tick cannot move, since
        # every stream's first quote is only its baseline.
        if rejoin_due and not self._past_end():
            self.rejoin(quotes_by_event)
            self.next_rejoin = now + REJOIN_EVERY
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
                                decision_at=trigger.detected_at,
                                refusal="not_joined_to_an_open_contract",
                                admitted=False)
            return

        # An execution read is new work too: none leaves after the end. The
        # side it would have priced then has no execution book, exactly as a
        # failed read leaves it -- never a fill at the decision price.
        executions: dict[str, BookQuote | None] = {}
        unsent: set[str] = set()
        for w in contracts:
            if self._past_end():
                executions[w.ticker] = None
                unsent.add(w.ticker)
                continue
            executions[w.ticker] = self.read_book(
                w, "execution", {"stream_id": trigger.stream_id})
        decisions = []
        for watched in contracts:
            book = executions.get(watched.ticker)
            # A FAILED EXECUTION READ IS NOT A FILL AT THE DECISION PRICE. With
            # no book the delay is "until now", which no read satisfies, so
            # the screen drops the side exactly as the replay does -- rather
            # than a zero delay, which would price it at the decision book.
            delay = ((book.ts if book is not None else self.clock.now())
                     - trigger.detected_at)
            books = self.books.get(watched.ticker, [])
            decision = screen_live(
                trigger, books, market_ticker=watched.ticker,
                yes_is_home=watched.yes_is_home, start=watched.start,
                entry_delay=delay, entry_tolerance=ENTRY_TOLERANCE,
                series=self.series)
            decisions.append((watched, book, decision,
                              self._assessment(decision, trigger, books,
                                               watched, delay)))

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
        for watched, book, decision, assessment in decisions:
            self.counts["decisions"] += 1
            self.recorder.write(
                "decision", stream_id=trigger.stream_id,
                yes_is_home=watched.yes_is_home,
                book_move_for_yes=trigger.delta_for(watched.yes_is_home),
                entered=watched.ticker == entry,
                execution_depth=(None if book is None else {
                    "yes_bid_size": book.bid_size,
                    "no_bid_size": book.ask_size}),
                **decision.as_dict(),
                tick=(self.tick_context.as_dict()
                      if self.tick_context else None),
                assessment=assessment,
                **({"execution_read": "not sent: the authorized end had "
                                      "passed"}
                   if watched.ticker in unsent else {}))

        until = trigger.detected_at + self.follow_for
        current = self.follows.get(trigger.event_id)
        if current is None:
            self.follows[trigger.event_id] = Follow(
                until=until, next_at=self.clock.now() + self.follow_every)
        else:
            current.until = max(current.until, until)

    def _assessment(self, decision: Any, trigger: MoveTrigger, books: Sequence,
                    watched: Watched, delay: timedelta) -> dict:
        """Why the screen decided what it did, recorded with the decision.

        AFTER the decision and unable to change it: the screen ran first.
        A failure here is recorded and counted, never allowed to end a paid
        session -- every input is in the raw records, so `--report`
        rebuilds this assessment offline either way.
        """
        try:
            return describe(decision, trigger, books,
                            yes_is_home=watched.yes_is_home,
                            start=watched.start, entry_delay=delay,
                            entry_tolerance=ENTRY_TOLERANCE,
                            eligibility=Eligibility(), series=self.series,
                            tick=self.tick_context)
        except Exception as exc:                    # noqa: BLE001 -- recorded
            self.counts["assessment_errors"] += 1
            return {"schema": ASSESSMENT_SCHEMA,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "recomputed offline by --report from the raw "
                            "records"}

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
                if self._past_end():
                    return
                self.read_book(watched, "follow")
            while follow.next_at <= self.clock.now():
                follow.next_at += self.follow_every

    # --- the session ---------------------------------------------------------

    def run(self) -> Stop:
        started = self.clock.now()
        end = self.ends = started + timedelta(hours=self.hours)
        self.recorder.write(
            "session_start", at=started, ends=end, sport=self.sport,
            series=self.series, cadence_seconds=self.cadence.total_seconds(),
            follow_every_seconds=self.follow_every.total_seconds(),
            follow_for_seconds=self.follow_for.total_seconds(),
            book_horizon_seconds=self.book_horizon.total_seconds(),
            book_memory_seconds=BOOK_MEMORY.total_seconds(),
            entry_tolerance_seconds=ENTRY_TOLERANCE.total_seconds(),
            fee_route=DEFAULT_ROUTE,
            code=code_version(),
            credit_cap=self.ledger.cap,
            credits_per_call=CREDITS_PER_LIVE_CALL,
            move_policy=self.detector.policy.as_dict(),
            reaction_policy=REACTION_POLICY.as_dict(),
            eligibility=vars(Eligibility()),
            recovery=recovery_policy(),
            max_poll_spacing_seconds=self.max_poll_spacing.total_seconds(),
            status_file=self.status_path.name)
        crash: Exception | None = None
        try:
            with only_declared_endpoints(), self._dispatch_gate():
                stop = self.preflight()
                if stop is None:
                    stop = self._loop(end)
        except AuthorizedEndReached as exc:
            # The backstop refused a request the loop's own checks did not
            # stop. Nothing was sent; the session is over.
            self.recorder.write("dispatch_refused", at=self.clock.now(),
                                request="backstop", reason="authorized_end",
                                detail=str(exc))
            stop = self._end_of_session()
        except CreditCapReached as exc:
            stop = Stop("credit_cap", str(exc))
        except CaptureRefused as exc:
            # A request outside the allow-list was refused before it was
            # sent. That is a code change nobody declared, not a transient
            # failure, so the session ends -- and says why in its own record.
            stop = Stop("undeclared_request", str(exc))
        except KeyboardInterrupt:
            stop = Stop("interrupted", "stopped by the operator")
        except Exception as exc:                    # noqa: BLE001 -- re-raised
            # A CRASH STILL ENDS WITH A STATUS. The records and the status
            # file say it crashed, and how far it got; then the exception
            # goes on, unchanged, to whoever ran it.
            crash = exc
            stop = Stop("crashed", scrub(f"{type(exc).__name__}: {exc}",
                                         (self.api_key,)))
        ended = self.clock.now()
        in_outage = self.failures is not None and self.failures.is_outage
        if self.failures is not None:
            # A run of failures still open at the end: its blind interval
            # ran to the end, unrecovered.
            self._close_failures(ended, recovered=False)
        else:
            self._unpolled_tail(ended)
        self.status = self._final_status(stop, started, end, ended, in_outage)
        self.recorder.write("session_end", at=ended,
                            reason=stop.reason, detail=stop.detail,
                            ok=stop.ok, counts=self.counts,
                            detector=self._drain_rejections(),
                            ledger=str(self.ledger),
                            credits_reserved=self.ledger.spent_this_run,
                            account_remaining=self.ledger.remaining,
                            status=self.status["status"],
                            gaps=self.status["gaps"],
                            deadline=self.status["deadline"])
        self._write_status(self.status)
        if crash is not None:
            raise crash
        return stop

    def _loop(self, end: datetime) -> Stop:
        next_tick = self.clock.now()
        while True:
            now = self.clock.now()
            if now >= end:
                return self._end_of_session()
            run = self.failures
            if run is not None and run.is_outage:
                # PAID POLLING IS PAUSED. The one paid request an outage
                # makes is its probe, when the backoff says it is due.
                if now >= run.next_probe_at:
                    stop = self._probe()
                    if stop is not None:
                        return stop
                    while next_tick <= self.clock.now():
                        next_tick += self.cadence
            elif now >= self._due(next_tick):
                stop = self.tick()
                if stop is not None:
                    return stop
                while next_tick <= self.clock.now():
                    next_tick += self.cadence
            self.follow_due()
            run = self.failures
            due = (run.next_probe_at if run is not None and run.is_outage
                   else self._due(next_tick))
            wake = min([due, end]
                       + [f.next_at for f in self.follows.values()])
            self.clock.sleep((wake - self.clock.now()).total_seconds())

    def _due(self, next_tick: datetime) -> datetime:
        """The next poll's time: its tick, or the provider's Retry-After."""
        if self.not_before is not None and self.not_before > next_tick:
            return self.not_before
        return next_tick

    def _probe(self) -> Stop | None:
        """One recovery probe: an ordinary tick, once the checks that make
        it affordable have passed. Its decision reads are the fresh books
        the next decision needs, so the memory is dropped before them."""
        run = self.failures
        now = self.clock.now()
        if now - run.started > RECOVERY_MAX_OUTAGE:
            # AT EXECUTION, NOT ONLY WHEN SCHEDULED. A probe due inside the
            # bound can come to run long after it -- the machine slept
            # through its due time, or the loop was held up behind reads --
            # and an answer then is not a recovery the bound allows.
            self.recorder.write("dispatch_refused", at=now, request="probe",
                                reason="outage_bound",
                                first_failure_at=run.started)
            return self._outage_expired(now, "it came due and the clock read")
        stop = self._probe_affordable()
        if stop is not None:
            return stop
        run.probes += 1
        self.recorder.write("recovery_probe", probe=run.probes, at=now,
                            first_failure_at=run.started,
                            credits_reserved=self.ledger.spent_this_run)
        self._invalidate_books(now, f"recovery probe {run.probes}: every "
                                    f"book read before it predates the "
                                    f"outage's end")
        self.probing = True
        try:
            return self.tick()
        finally:
            self.probing = False

    def _end_of_session(self) -> Stop:
        """The authorized end. Never extended -- including by an outage."""
        run = self.failures
        if run is not None and run.is_outage:
            return Stop("end_of_session",
                        f"the authorized end arrived during an unrecovered "
                        f"outage ({run.failed_polls} failed poll(s), "
                        f"{run.probes_sent} probe(s) sent, since "
                        f"{_iso(run.started)}): "
                        f"the session ran its window without observing the "
                        f"end of it")
        return Stop("end_of_session", ok=True)

    def _final_status(self, stop: Stop, started: datetime, ends: datetime,
                      ended: datetime, in_outage: bool) -> dict:
        """The session's own summary of itself, for `<session>.status.json`."""
        polls, failed = self.counts["polls"], self.counts["polls_failed"]
        authorized = (ends - started).total_seconds()
        ran = (ended - started).total_seconds()
        outages = [g for g in self.gaps if g["cause"] == "outage"]
        return {
            "schema": STATUS_SCHEMA,
            "status": session_status(reason=stop.reason, in_outage=in_outage,
                                     gaps=len(self.gaps)),
            "reason": stop.reason, "detail": stop.detail, "ok": stop.ok,
            "authorized": {"start": _iso(started), "end": _iso(ends),
                           "hours": self.hours},
            "actual": {"start": _iso(started), "end": _iso(ended),
                       "duration_seconds": ran,
                       "share_of_authorized": (ran / authorized
                                               if authorized else None)},
            "polls": {"attempted": polls, "answered": polls - failed,
                      "failed": failed,
                      "recovery_probes": self.counts["recovery_probes"]},
            "credits": {"cap": self.ledger.cap,
                        "reserved": self.ledger.spent_this_run,
                        "account_remaining": self.ledger.remaining},
            "gaps": {"count": len(self.gaps),
                     "blind_seconds": blind_seconds(self.gaps),
                     "longest_seconds": max((g["seconds"] for g in self.gaps),
                                            default=0.0),
                     "by_cause": dict(Counter(g["cause"] for g in self.gaps)),
                     "outages": len(outages),
                     "outages_recovered": sum(1 for g in outages
                                              if g["recovered"])},
            "deadline": {
                "authorized_end": _iso(ends),
                "last_paid_request_sent": _iso(self.last_paid_sent),
                "paid_requests_sent_at_or_after_end": self.paid_after_end,
                "answers_completed_after_end": self.answers_after_end,
                "finished_after_end_seconds": max(
                    0.0, (ended - ends).total_seconds()),
                "note": "the end is exclusive: nothing is sent at or after "
                        "it. A request sent before it may complete after it, "
                        "in flight, its timeout clipped to the time that was "
                        "left; such an answer is recorded and never acted "
                        "on"},
            "observed": {"moves": self.counts["triggers"],
                         "decisions": self.counts["decisions"],
                         "entries": self.counts["entries"]},
            "paths": {"records": _shown(self.recorder.path),
                      "status": _shown(self.status_path)},
            "next": (f"python3 shadow_monitor.py --report "
                     f"{_shown(self.recorder.path)}"),
            "code": code_version(),
            "note": "local only: written for whoever ran the session; nothing "
                    "was sent anywhere",
        }

    def _write_status(self, status: dict) -> None:
        """Written whole or not at all: a reader never meets half a file."""
        tmp = self.status_path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(status, default=_jsonable, indent=1,
                                      sort_keys=True) + "\n",
                           encoding="utf-8")
            tmp.replace(self.status_path)
        except OSError as exc:
            # The JSONL's session_end already carries the verdict; say the
            # file is missing rather than let its absence pass unnoticed.
            self.recorder.write("status_unwritten",
                                error=scrub(f"{type(exc).__name__}: {exc}"))


STATUS_SCHEMA = "shadow-status/1"


def _shown(path: Path) -> str:
    """A path as a status file may show it: relative to this study when it
    is inside it, so the owner's home directory is not written into a file
    that may be shared; as given otherwise."""
    try:
        return str(path.resolve().relative_to(HERE))
    except ValueError:
        return str(path)


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


def parse_odds_row(row: dict) -> Any:
    """One recorded poll's answer, parsed as the monitor parsed it, or None
    when the poll did not answer. The one reading of an `odds` record: the
    report's sightings and the diagnostics' sharp path both come from it.
    """
    received, provider = _time(row.get("received_at")), _time(
        row.get("provider_date"))
    if row.get("payload") is None or received is None or row.get("coverage"):
        return None
    stamp = provider or received
    parsed = parse_snapshot({"timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "data": row["payload"]})
    return parsed if parsed.coverage.complete else None


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
                      if end else None,
                      **_recorded_status(rows, end)}

    odds = [r for r in rows if r["kind"] == "odds"]
    skews, ages, refresh, processing = [], [], [], []
    sightings: Counter = Counter()
    last_seen: dict[tuple[str, str], datetime] = {}
    for row in rows:
        if row["kind"] == "gap" and row.get("cause") == "not_polled":
            # A STRETCH THE MONITOR DID NOT POLL IS A HOLE, as a failed poll
            # is: it is recorded before the poll that ended it, so that
            # poll's sightings are first sightings again.
            last_seen.clear()
            sightings["unpolled_gaps"] += 1
            continue
        if row["kind"] != "odds":
            continue
        received, provider = _time(row.get("received_at")), _time(
            row.get("provider_date"))
        headers = _time(row.get("headers_at")) or received
        if headers and provider:
            skews.append((headers - provider).total_seconds())
        if isinstance(row.get("processing_seconds"), (int, float)):
            processing.append(float(row["processing_seconds"]))
        parsed = parse_odds_row(row)
        if parsed is None:
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
    # THE END, CHECKED FROM THE RECORDS: every paid request's own send time
    # against the end the session recorded when it began. A session from
    # before the dispatch gate is checked the same way.
    ends = _time(start.get("ends"))
    sent = [_time(r.get("sent_at")) for r in odds]
    out["session"].update(
        authorized_end=start.get("ends"),
        paid_after_end=(None if ends is None else
                        sum(1 for t in sent if t is not None and t >= ends)),
        answers_after_end=sum(1 for r in odds
                              if r.get("after_authorized_end")))
    out["polls"] = {"total": len(odds),
                    "answered": sum(1 for r in odds if r.get("payload")
                                    is not None and not r.get("coverage")),
                    "clock_skew": _quantiles(skews),
                    "processing": _quantiles(processing)}
    out["age_when_received"] = _quantiles(ages)
    out["provider_refresh"] = _quantiles(refresh)
    out["sightings"] = {k: sightings.get(k, 0) for k in (
        "first", "new", "repeat", "older_copy", "unanswered_polls",
        "unpolled_gaps")}
    out["cadence_seconds"] = start.get("cadence_seconds")
    out["failures"] = failure_chronology(rows)

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


def _recorded_status(rows: Sequence[dict], end: dict | None) -> dict:
    """The session's status as it recorded it -- or, for a session recorded
    before it did, derived by the same function from what it did record: an
    unanswered poll is a gap, and an outage is not something it could have
    been in, since it stopped on its third failure."""
    if end is not None and end.get("status"):
        return {"status": end["status"], "status_source": "recorded",
                "gaps": end.get("gaps")}
    if end is None:
        return {"status": None, "status_source": "no session_end record",
                "gaps": None}
    unanswered = sum(1 for r in rows if r["kind"] == "odds"
                     and (r.get("payload") is None or r.get("coverage")))
    return {"status": session_status(reason=end.get("reason") or "",
                                     in_outage=False, gaps=unanswered),
            "status_source": "derived: the session predates status records",
            "gaps": None}


#: Reading an older record's reason sentence, for a session recorded before
#: failures were classified. Each is labelled INFERRED where it is used.
_LEGACY_PATTERNS = (
    ("timed out", "timeout"), ("Name or service not known", "dns"),
    ("nodename nor servname", "dns"), ("Temporary failure in name", "dns"),
    ("Connection refused", "connection"), ("Connection reset", "connection"),
    ("Network is unreachable", "connection"),
    ("No route to host", "connection"), ("IncompleteRead", "incomplete_body"),
    ("CERTIFICATE_VERIFY_FAILED", "tls_certificate"),
)


def _legacy_failure(reason: str) -> dict:
    """What an unclassified failure's recorded message shows, labelled as an
    inference. `<urlopen error ...>` is the one phase the text proves:
    CPython wraps an OSError in URLError only while connecting and sending,
    before any response began."""
    out: dict[str, Any] = {"inferred_from": "the recorded message"}
    status = next((int(tok[:3]) for tok in reason.split("HTTP Error ")[1:]
                   if tok[:3].isdigit()), None)
    if status is not None:
        out.update(category=status_category(status), status=status,
                   phase="response")
        return out
    out["category"] = next((cat for text, cat in _LEGACY_PATTERNS
                            if text in reason), None)
    if "<urlopen error" in reason:
        out["phase"] = "request"
    if "[Errno " in reason:
        number = reason.split("[Errno ", 1)[1].split("]", 1)[0]
        out["errno"] = int(number) if number.isdigit() else None
    return out


def failure_chronology(rows: Sequence[dict]) -> dict:
    """Every failed poll, in order: the outage's chronology, from the file.

    Consecutive failures are grouped into RUNS. Each run gives the last
    answer before it, every attempt -- when it was sent, how long it ran,
    what it failed with, whether it was a recovery probe -- the Kalshi reads
    made while it lasted and how many of those failed, and how it ended.
    Built from the clocks and reasons every session has recorded, so a
    session from before failures were classified is described too; what is
    read from its message rather than recorded as a field says so. The
    failed Kalshi reads beside a run are evidence about the path, never a
    cause: nothing here names one.
    """
    books = [r for r in rows if r["kind"] == "book"]
    runs: list[dict] = []
    current: dict | None = None
    last_answer: datetime | None = None
    ended = None
    for row in rows:
        kind = row["kind"]
        if kind == "session_end":
            ended = _time(row.get("at"))
        if current is not None and kind == "outage_start":
            current["outage"] = True
        if kind != "odds":
            continue
        sent, received = _time(row.get("sent_at")), _time(row.get("received_at"))
        ready = _time(row.get("ready_at")) or received
        if row.get("payload") is not None and not row.get("coverage"):
            if current is not None:
                current.update(ended="answered", answered_at=_iso(ready))
                runs.append(current)
                current = None
            last_answer = ready
            continue
        reason = "; ".join(row.get("coverage") or []) or "no response"
        failure = row.get("failure") or _legacy_failure(reason)
        elapsed = failure.get("elapsed_seconds")
        if elapsed is None and sent is not None and received is not None:
            elapsed = (received - sent).total_seconds()
        if current is None:
            current = {"first_failure_at": row.get("sent_at"),
                       "last_answer_at": _iso(last_answer), "attempts": [],
                       "outage": False, "ended": None, "answered_at": None}
        current["attempts"].append({
            "sent_at": row.get("sent_at"), "elapsed_seconds": elapsed,
            "category": failure.get("category"),
            "phase": failure.get("phase"),
            "status": failure.get("status", row.get("status")),
            "errno": failure.get("errno"), "probe": failure.get("probe"),
            "inferred": "inferred_from" in failure, "reason": reason})
        current["last_failure_at"] = _iso(received or sent)
    if current is not None:
        current["ended"] = "unanswered_at_session_end"
        runs.append(current)
    sent_times = [(_time(b.get("sent_at")) or _time(b.get("received_at")), b)
                  for b in books]
    for run in runs:
        begin = _time(run["first_failure_at"])
        finish = (_time(run["answered_at"]) or ended
                  or _time(run["last_failure_at"]))
        inside = ([b for sent, b in sent_times
                   if sent is not None and begin <= sent <= finish]
                  if begin is not None and finish is not None else [])
        run["kalshi_reads"] = len(inside)
        run["kalshi_failed"] = sum(1 for b in inside
                                   if b.get("payload") is None
                                   or b.get("coverage"))
        since = _time(run["last_answer_at"])
        until = _time(run["answered_at"])
        run["blind_seconds"] = ((until - since).total_seconds()
                                if since is not None and until is not None
                                else None)
    return {"runs": runs,
            "failed_polls": sum(len(r["attempts"]) for r in runs),
            "book_reads": len(books),
            "book_failures": sum(1 for b in books if b.get("payload") is None
                                 or b.get("coverage")),
            "unpolled": [{"from": g.get("from"), "to": g.get("to"),
                          "seconds": g.get("seconds"),
                          "recovered": g.get("recovered", True)}
                         for g in rows if g["kind"] == "gap"
                         and g.get("cause") == "not_polled"]}


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


Span = tuple[datetime, datetime]


def _book_reads(books: Sequence[dict]
                ) -> dict[str, list[tuple[Span, BookQuote | None]]]:
    """Every read of every contract, in answer order: (the span it answered
    over -- request to receipt -- and the book, or None when the read gave
    nothing a price can be read from).

    Failed reads are KEPT, as None: they are not observations, but a window
    is only as watched as its reads were, and the count of the ones that
    failed is reported beside every outcome. The span comes from
    `request_span`, the function `reading_span` gives a successful read's,
    so both kinds are placed in a window by one rule.
    """
    out: dict[str, list[tuple[Span, BookQuote | None]]] = {}
    for row in books:
        ticker, received = row.get("ticker"), _time(row.get("received_at"))
        if not ticker or received is None:
            continue
        sent = _time(row.get("sent_at"))
        span = request_span(sent, received)
        book = None
        if row.get("payload") is not None and not row.get("coverage"):
            parsed, coverage = parse_orderbook(
                row["payload"], ticker=ticker, received_at=received,
                sent_at=sent)
            if (parsed is not None and coverage.complete
                    and parsed.mid is not None):
                book = parsed
        out.setdefault(ticker, []).append((span, book))
    for series in out.values():
        series.sort(key=lambda item: (item[0][1], item[0][0]))
    return out


def _response(decision: dict, trigger: dict | None,
              reads: Sequence[tuple[Span, BookQuote | None]],
              policy: ReactionPolicy, ended: datetime | None) -> dict:
    """What Kalshi did after ONE move, on ONE contract -- measured by the
    replay's own `measure_response`, over this session's reads.

    One rule for both paths (rule 19). Each read describes the book somewhere
    between its request and its answer, so a change is bracketed by
    `sent_at` as well as the receipt; a change located only across the
    trigger is `moved_around_trigger`, not a reaction; a move before it
    that was still in place at the trigger, with nothing further after, is
    `exchange_moved_before_trigger` -- one that came undone is not, and a
    further move on top of one is a response, the earlier move reported
    beside it; and a window the reads did not cover
    to its end -- reads that failed, stopped, or never came -- is
    `blind_interval`, never a quiet market. Coverage rides along: the reads
    inside the window, the ones that failed, and whether the session itself
    ended before the window did -- "inside" by the measurement's own
    `inside_window`, so the counts are of the reads the outcome used.
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
           "detail": reaction.detail,
           # Kalshi's move the book's way already in place at the trigger:
           # on `exchange_moved_before_trigger` it is the outcome; on a
           # response, the response came on top of it. Movement, not a
           # verdict on the opportunity -- that is the decision's screen.
           "prior_move": (reaction.prior.as_dict()
                          if reaction.prior else None)}
    if decided is not None:
        deadline = decided + policy.max_wait
        inside = [book for span, book in reads
                  if inside_window(span, decided, deadline)]
        row.update(
            window_ends=_iso(deadline),
            reads_in_window=sum(1 for book in inside if book is not None),
            unreadable_in_window=sum(1 for book in inside if book is None),
            session_ended_inside_window=(None if ended is None
                                         else ended < deadline))
    return row


def _stamp(value: Any) -> str:
    moment = _time(value)
    return (moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
            if moment is not None else "none")


def _render_status(s: dict) -> str:
    if not s.get("status"):
        return f"not recorded ({s.get('status_source')})"
    line = s["status"]
    gaps = s.get("gaps")
    if isinstance(gaps, dict):
        line += (f"  ({gaps.get('count', 0)} gap(s), "
                 f"{(gaps.get('blind_seconds') or 0) / 60:.1f} min blind, "
                 f"{gaps.get('outages', 0)} outage(s), "
                 f"{gaps.get('outages_recovered', 0)} recovered)")
    if s.get("status_source") != "recorded":
        line += f"  [{s.get('status_source')}]"
    return line


def _render_deadline(s: dict) -> str:
    if s.get("authorized_end") is None:
        return "not recorded: no session_start record gives the end"
    line = (f"{s['authorized_end']}: {s.get('paid_after_end')} paid "
            f"request(s) sent at or after it")
    if s.get("answers_after_end"):
        line += (f"; {s['answers_after_end']} answer(s) to earlier requests "
                 f"arrived after it, in flight, and were not acted on")
    return line


def _render_failures(chronology: dict, shown: int = 12) -> list[str]:
    """The chronology, in the order it happened."""
    runs = chronology["runs"]
    lines = ["", "  FAILED POLLS AND GAPS (in order, from the records)"]
    lines.append(f"    {chronology['failed_polls']} failed poll(s) in "
                 f"{len(runs)} run(s); {chronology['book_failures']:,} of "
                 f"{chronology['book_reads']:,} book read(s) failed")
    listed = (list(enumerate(runs, 1)) if len(runs) <= shown else
              list(enumerate(runs, 1))[:shown // 2]
              + list(enumerate(runs, 1))[-(shown // 2):])
    previous = 0
    for number, run in listed:
        if number != previous + 1:
            lines.append(f"    ... {number - previous - 1} run(s) not shown")
        previous = number
        attempts = run["attempts"]
        head = (f"    run {number}  {_stamp(run['first_failure_at'])}  "
                f"{len(attempts)} failed poll(s)"
                + (", an OUTAGE" if run["outage"] else ""))
        if run["ended"] == "answered":
            head += f"; answered again {_stamp(run['answered_at'])}"
            if run["blind_seconds"] is not None:
                head += (f", {run['blind_seconds']:,.0f}s after the last "
                         f"answer")
        else:
            head += "; still unanswered when the session ended"
        lines.append(head)
        lines.append(f"      last answer before it: "
                     f"{_stamp(run['last_answer_at'])}")
        for attempt in attempts:
            what = attempt["category"] or "unclassified"
            if attempt["inferred"]:
                what += " (inferred from the message)"
            if attempt["phase"]:
                what += f", phase {attempt['phase']}"
            took = ("?" if attempt["elapsed_seconds"] is None
                    else f"{attempt['elapsed_seconds']:.1f}s")
            probe = (f" (probe {attempt['probe']})" if attempt["probe"]
                     else "")
            lines.append(f"      {_stamp(attempt['sent_at'])}{probe}  {what}"
                         f" after {took}: {attempt['reason'][:110]}")
        lines.append(f"      Kalshi reads while it lasted: "
                     f"{run['kalshi_reads']}, {run['kalshi_failed']} failed")
    for gap in chronology["unpolled"]:
        lines.append(f"    NOT POLLED {_stamp(gap['from'])} to "
                     f"{_stamp(gap['to'])} ({gap['seconds'] or 0:,.0f}s)"
                     + ("" if gap["recovered"] else
                        ": the session ended inside it"))
    if runs or chronology["unpolled"]:
        lines.append("    (a category is how an attempt failed, not why; a "
                     "Kalshi read failing alongside is evidence about the "
                     "path, not a cause)")
    return lines


def render_report(figures: dict) -> str:
    s = figures["session"]
    lines = ["SHADOW SESSION", "",
             f"  started            {s['started']}",
             f"  ended              {s['ended']}   ({s['stop']})",
             f"  status             {_render_status(s)}",
             f"  authorized end     {_render_deadline(s)}",
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
              f"unanswered poll(s)"
              + (f", {seen['unpolled_gaps']} stretch(es) not polled"
                 if seen.get("unpolled_gaps") else "")]
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
    on_top = sum(1 for r in responses if r.get("prior_move")
                 and r["outcome"] != ReactionOutcome.ALREADY_PRICED.value)
    if on_top:
        lines.append(f"    {on_top} response(s) came on top of a move Kalshi "
                     f"had already made before the trigger (prior_move)")
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
    if figures.get("failures") is not None:
        lines += _render_failures(figures["failures"])
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
    parser.add_argument("--book-horizon-hours", type=float,
                        default=BOOK_HORIZON.total_seconds() / 3600,
                        help="read decision books for games this close to "
                             "kickoff (default: the screen's own lead-time "
                             "ceiling). Collection only: the screen is "
                             "unchanged, so a move beyond its ceiling is "
                             "still refused -- see the README before "
                             "changing it")
    parser.add_argument("--starts-at", default=None, metavar="UTC_ISO",
                        help="plan only: list which games a session starting "
                             "then would watch, and for how long")
    parser.add_argument("--report", default=None, metavar="SESSION_JSONL",
                        help="summarise a recorded session and rebuild every "
                             "assessment in it. Free, offline: no network "
                             "request is possible while it runs")
    parser.add_argument("--json", default=None, metavar="OUT_JSON",
                        help="with --report: also write the full diagnostics "
                             "as JSON")
    parser.add_argument("--settlements", default=None, metavar="JSON",
                        help="with --report: {market_ticker: 1|0} results, "
                             "so admitted entries can carry a realized "
                             "figure. Without it every realized figure is "
                             "withheld")
    return parser


def _report(args: argparse.Namespace) -> int:
    """The report and the diagnostics, offline. Imported here: the
    diagnostics module rebuilds the monitor's state from this module's own
    constants and parsers, so it depends on this one, not the reverse."""
    from shadow_diagnostics import (
        diagnose, jsonable, load_settlements, no_network, render_diagnostics,
        source_of,
    )
    path = Path(args.report)
    if not path.is_file():
        print(f"no such session file: {path}", file=sys.stderr)
        return EXIT_USAGE
    settlements = None
    if args.settlements:
        try:
            settlements = load_settlements(Path(args.settlements))
        except (OSError, ValueError) as exc:
            print(f"--settlements: {exc}", file=sys.stderr)
            return EXIT_USAGE
    with no_network():
        rows, bad = read_records(path)
        print(render_report(report(rows)))
        figures = diagnose(rows, settlements=settlements,
                           source=source_of(path, len(rows), bad))
        print(render_diagnostics(figures))
    if bad:
        print(f"\n  *** {bad} unreadable line(s) skipped (a session "
              f"killed mid-write leaves one)")
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(figures, default=jsonable, indent=1,
                                  sort_keys=True) + "\n", encoding="utf-8")
        print(f"\n  diagnostics written to {out}")
    return EXIT_OK


def _horizon_plan(kickoffs: dict, *, now: datetime, starts: datetime,
                  hours: float, horizon: timedelta) -> list[str]:
    """Which games a session over [starts, starts + hours] would read
    decision books for, and for how long: a game is watched from `horizon`
    before its kickoff until kickoff."""
    ends = starts + timedelta(hours=hours)
    lines = [f"  games on the slate (the exchange's listings with the "
             f"schedule's kickoffs; which join to a sharp event is known "
             f"only once a paid poll answers):"]
    upcoming = sorted((k, e) for e, k in kickoffs.items() if k > now)
    if not upcoming:
        lines.append("    none")
    for kickoff, event in upcoming:
        enters = kickoff - horizon
        watched = (min(ends, kickoff) - max(starts, enters)).total_seconds()
        lines.append(f"    {event:<34} kickoff {kickoff:%a %Y-%m-%d %H:%MZ}  "
                     f"in horizon from {enters:%a %m-%d %H:%MZ}  watched "
                     f"{max(0.0, watched) / 3600:4.1f}h of the session")
    return lines


def main(argv: Sequence[str] | None = None, *, clock: Any = None,
         slate_source: Callable[[], Slate] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.report:
        return _report(args)
    if args.json or args.settlements:
        print("--json and --settlements go with --report", file=sys.stderr)
        return EXIT_USAGE

    if args.hours is None or not args.hours > 0:
        print("--hours must be given and positive", file=sys.stderr)
        return EXIT_USAGE
    cadence = timedelta(seconds=args.cadence_seconds)
    if not cadence >= MIN_CADENCE:
        print(f"--cadence-seconds must be at least "
              f"{MIN_CADENCE.total_seconds():.0f}", file=sys.stderr)
        return EXIT_USAGE
    if not args.book_horizon_hours > 0:
        print("--book-horizon-hours must be positive", file=sys.stderr)
        return EXIT_USAGE
    horizon = timedelta(hours=args.book_horizon_hours)
    starts = None
    if args.starts_at is not None:
        starts = _time(args.starts_at)
        if starts is None:
            print("--starts-at must be an ISO time with a UTC offset, e.g. "
                  "2026-10-02T14:00:00Z", file=sys.stderr)
            return EXIT_USAGE
        if args.spend is not None:
            print("--starts-at is plan-only: a paid session starts when it "
                  "is run", file=sys.stderr)
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
              f"{sum(1 for k in upcoming if k <= now + horizon)} "
              f"within the {horizon.total_seconds() / 3600:g}h book horizon")
    if horizon != BOOK_HORIZON:
        print(f"  *** book horizon {horizon.total_seconds() / 3600:g}h is NOT "
              f"the screen's {BOOK_HORIZON.total_seconds() / 3600:g}h "
              f"lead-time ceiling. Collection only: the screen still refuses "
              f"a move beyond its ceiling, and each extra game in the horizon "
              f"adds two free book reads to every poll's read phase.")
    if not slate.coverage.complete:
        print(f"  *** slate retrieval incomplete: {slate.coverage}")
    print("\n".join(_horizon_plan(slate.kickoffs, now=now,
                                   starts=starts or now, hours=args.hours,
                                   horizon=horizon)))
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
    if args.spend is None and slate.coverage.complete and not slate.markets:
        # The preflight's own stop, in the plan. With nothing open there is
        # no book to check the shape on, so a plan that said "re-run with
        # --spend" here would pass a check it never made.
        shape_ok = False
        print(f"\n  *** no open {args.series} market in the next "
              f"{SLATE_DAYS} days: nothing to watch, and no book to check "
              f"the transcribed shape on. A paid session would stop here, "
              f"before its first poll.", file=sys.stderr)
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
            print("\n  Nothing was spent. A session is worth running only "
                  "once the book parser has read a real book.",
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
        slate_source=slate_source, book_horizon=horizon)
    print(f"\n  recording to {path}")
    try:
        stop = monitor.run()
    finally:
        recorder.close()
    print(f"\n  session ended: {stop.reason}"
          f"{': ' + stop.detail if stop.detail else ''}")
    status = monitor.status or {}
    gaps = status.get("gaps") or {}
    print(f"  status: {status.get('status')} -- "
          f"{(status.get('actual') or {}).get('duration_seconds', 0) / 3600:.2f}"
          f"h of {args.hours:g}h, {gaps.get('count', 0)} gap(s) "
          f"({gaps.get('blind_seconds', 0) / 60:.1f} min blind), "
          f"{gaps.get('outages', 0)} outage(s)")
    print(f"  {monitor.ledger}")
    print(f"  status file: {monitor.status_path}")
    print(f"  next: python3 shadow_monitor.py --report {path}")
    return EXIT_OK if stop.ok else EXIT_STOPPED


if __name__ == "__main__":
    raise SystemExit(main())
