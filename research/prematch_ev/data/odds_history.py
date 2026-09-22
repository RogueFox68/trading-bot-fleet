"""Historical sharp odds snapshots (The Odds API).

    GET /v4/historical/sports/{sport}/odds
        ?apiKey=&regions=&markets=h2h&date=<ISO8601>&oddsFormat=american

Snapshots are 5-minutely; the endpoint returns the closest snapshot at or
BEFORE `date`. Quota cost is 10 credits per region per market, paid tiers only.

Pull BY TIMESTAMP, not per game. One request at time T returns every event live
at T, so a day of a sport costs one request per snapshot you want, not one per
game. That is the difference between a study that fits a 20k-credit tier and
one that does not.

The response carries its own quota accounting in headers; `CreditLedger`
surfaces it so a run can stop before exhausting the plan rather than
discovering it as a wall of 401s halfway through a season.

COVERAGE DEPTH IS THE FIRST THING TO CHECK. If the archive starts later than
the season you want, the study is not possible on that sport and no amount of
careful scoring fixes it -- `probe_earliest_snapshot` answers that in one
request before you spend the rest.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .cache import (
    CreditCapReached, ResponseCache, key_for, redact, resolve_key,
)
from .kalshi_history import Coverage

BASE_URL = "https://api.the-odds-api.com/v4"
SHARP_BOOK = "pinnacle"
CREDITS_PER_HISTORICAL_CALL = 10          # per region per market
REQUEST_TIMEOUT = 30
RETRIES = 3
BACKOFF_SECONDS = (2, 4, 8)

# REQUEST THE BOOK, NOT A REGION THAT MIGHT CONTAIN IT. An earlier version sent
# `regions=us` while the parser accepted only Pinnacle -- which the provider
# classifies under EU. Every response came back without the book the study is
# built on, every quote was discarded, and each call still cost 10 credits. The
# `bookmakers` parameter names the book directly and is immune to a book moving
# between regions, so it is what this uses; `regions` remains only as a
# fallback for a caller that genuinely wants a whole region.
DEFAULT_BOOKMAKERS = SHARP_BOOK

# The provider notes that suspended or closed markets can linger in a response
# for roughly fifteen minutes, and that its Pinnacle prices come from the
# public site and may themselves be delayed. A fresh ENVELOPE therefore does
# not imply a fresh QUOTE: without this bound, a stale sharp line sitting
# beside a moving exchange price reads as edge, which is precisely the
# measurement error this study exists to avoid making.
MAX_QUOTE_AGE_SECONDS = 900

SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NFL": "americanfootball_nfl",
    "NBA": "basketball_nba",
    "NHL": "icehockey_nhl",
}


class OddsFetchError(RuntimeError):
    """A snapshot that did not answer. Not the same as a snapshot with no games."""


@dataclass
class CreditLedger:
    """Running quota state, read from the API's own response headers.

    `cap` is a HARD budget for this run, counted in credits actually spent. A
    cap that only warns is not a cap: `spend_or_raise` refuses the call so the
    caller persists partial diagnostics instead of discovering an overspend
    afterwards.
    """

    # None means NOT READ, which is not the same as zero. A cache-only replay
    # makes no request, so no header is seen and the account's cumulative
    # usage is unknown -- printing "0 cumulative" claimed a fact the run never
    # established.
    used: int | None = None
    remaining: int | None = None
    calls: int = 0
    cap: int | None = None
    # RESERVED, not billed. Debited before each attempt, including attempts
    # that then fail, because a provider can charge a request whose response
    # never arrives. It is the figure the cap acts on, and an upper bound on
    # what was actually billed.
    spent_this_run: int = 0

    def spend_or_raise(self, credits: int = CREDITS_PER_HISTORICAL_CALL) -> None:
        if self.cap is not None and self.spent_this_run + credits > self.cap:
            # "Reserved", not "spent": the debit is taken before each attempt,
            # so a run whose every request was REFUSED (a bad key) reaches the
            # cap too, and saying "spent" there reads as money gone.
            raise CreditCapReached(
                f"paid-request budget reached: {self.spent_this_run} credits "
                f"reserved of a {self.cap} cap; refusing the next call"
            )
        self.spent_this_run += credits

    def observe(self, headers: Any) -> None:
        self.calls += 1
        try:
            raw = headers.get("x-requests-used")
            if raw is not None:
                self.used = int(raw)
        except (TypeError, ValueError):
            pass
        try:
            self.remaining = int(headers.get("x-requests-remaining"))
        except (TypeError, ValueError):
            pass

    def exhausted(self, reserve: int = 50) -> bool:
        return self.remaining is not None and self.remaining <= reserve

    def __str__(self) -> str:
        """Three different numbers, named as three different things.

        `used` is the ACCOUNT's cumulative usage, read from
        `x-requests-used`; it includes every earlier run. Presenting it as
        "credits used" made a 140-credit run report 340. `spent_this_run` is
        what THIS run reserved, and is the figure a `--max-credits` cap acts on.
        """
        left = "unknown" if self.remaining is None else f"{self.remaining:,}"
        cumulative = "unknown" if self.used is None else f"{self.used:,}"
        return (f"{self.calls} calls, {self.spent_this_run:,} credits reserved "
                f"this run, {cumulative} cumulative on the account, "
                f"{left} remaining")


@dataclass(frozen=True)
class SharpQuote:
    """One two-way moneyline from the sharp book, at one snapshot time.

    Carries THREE times, not one, because they answer different questions:

      snapshot        when the PROVIDER captured the archive row
      last_update     the last time the PROVIDER'S SYSTEM saw odds for this
                      market from the bookmaker
      commence_time   scheduled start

    `last_update` IS NOT WHEN THE BOOKMAKER CHANGED ITS PRICE. This docstring
    said it was, and that error propagated: `reaction/clocks.py` inherited the
    wording verbatim, `reaction/capability.py` graded "when did the BOOK move"
    as ANSWERABLE on the strength of it, and the reaction detector reported a
    `book_moved_at` point estimate the source cannot support. Bookmaker-level
    `last_update` is deprecated upstream; what this field reports is the
    provider's own observation, so the book's change instant can only be
    BRACKETED between two consecutive observations.

    Do not measure a lag FROM this field. `reaction/clocks.py` keeps the
    clocks apart (`provider_observed_at`, `provider_snapshot_time`,
    `local_receipt_time`) and `reaction/capability.py` grades what each one
    can and cannot answer.
    """

    snapshot: datetime
    commence_time: datetime
    away_name: str
    home_name: str
    away_price: float          # American odds
    home_price: float
    book: str
    provider_event_id: str     # the provider's stable id for this game
    last_update: datetime | None = None

    def minutes_to_start(self) -> float:
        return (self.commence_time - self.snapshot).total_seconds() / 60.0

    def age_seconds(self) -> float | None:
        """How long before the capture the PROVIDER last observed this price.

        Not "how long since the book moved" -- see the class docstring. The
        arithmetic is the same either way; what it licenses is not. This is a
        usable staleness bound on the provider's observation, and it is NOT a
        measurement of the bookmaker's behaviour.

        None when no update time was published -- unknown, which is not the
        same as fresh and must not be treated as it.
        """
        if self.last_update is None:
            return None
        return (self.snapshot - self.last_update).total_seconds()

    def is_fresh(self, max_age: float = MAX_QUOTE_AGE_SECONDS) -> bool:
        age = self.age_seconds()
        return age is not None and 0 <= age <= max_age


@dataclass
class SnapshotResult:
    snapshot: datetime | None
    quotes: list[SharpQuote] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    events_seen: int = 0
    events_without_sharp_book: int = 0
    quotes_without_update_time: int = 0
    quotes_stale: int = 0
    events_without_id: int = 0
    #: The response body exactly as received, or None when nothing arrived.
    #: A replay bundle stores RAW payloads so a parser fix reaches it (see
    #: reaction/replay.py), and the cache keeps only COMPLETE parses -- so an
    #: unreadable body, which a bundle must still carry to count it as a loss,
    #: would otherwise be unrecoverable. Nothing in the checkpoint study reads
    #: it; it exists for the reaction collector.
    raw: Any = None

    def fresh_quotes(self, max_age: float = MAX_QUOTE_AGE_SECONDS) -> list[SharpQuote]:
        return [q for q in self.quotes if q.is_fresh(max_age)]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_snapshot(payload: Any, book: str = SHARP_BOOK) -> SnapshotResult:
    """Parse one historical snapshot body. Pure -- no network.

    An event missing the sharp book is COUNTED, not dropped silently: a sport
    where Pinnacle covers a third of the slate is a different study from one
    where it covers all of it, and the difference must be visible in the report.
    """
    coverage = Coverage()
    if not isinstance(payload, dict):
        return SnapshotResult(None, [], coverage.fail(
            f"response was {type(payload).__name__}, expected object"))

    snapshot = _parse_time(payload.get("timestamp"))
    if snapshot is None:
        coverage.fail("snapshot has no usable 'timestamp'")

    events = payload.get("data")
    if not isinstance(events, list):
        return SnapshotResult(snapshot, [], coverage.fail(
            "'data' missing or not a list"))

    quotes: list[SharpQuote] = []
    missing_book = no_update_time = stale = missing_id = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        commence = _parse_time(event.get("commence_time"))
        home_name = event.get("home_team")
        away_name = event.get("away_team")
        event_id = event.get("id")
        if commence is None or not home_name or not away_name:
            continue
        if not event_id:
            # Without the provider's own id there is no reliable game identity:
            # a date plus two team names collapses doubleheaders and cannot
            # separate a rematch from a reschedule. Drop rather than invent one
            # -- but COUNT the drop, or the loss is invisible downstream.
            missing_id += 1
            continue

        parsed = _sharp_h2h_outcomes(event, book)
        if parsed is None:
            missing_book += 1
            continue
        home_price, away_price, last_update = parsed

        quote = SharpQuote(
            snapshot=snapshot or commence,
            commence_time=commence,
            away_name=str(away_name),
            home_name=str(home_name),
            away_price=away_price,
            home_price=home_price,
            book=book,
            provider_event_id=str(event_id),
            last_update=last_update,
        )
        if last_update is None:
            no_update_time += 1
        elif not quote.is_fresh():
            stale += 1
        quotes.append(quote)

    return SnapshotResult(
        snapshot, quotes, coverage, len(events), missing_book,
        no_update_time, stale, missing_id,
    )


def _sharp_h2h_outcomes(
    event: dict, book: str
) -> tuple[float, float, datetime | None] | None:
    """(home_price, away_price, last_update) from the sharp book, or None.

    Returns None -- never a partial or a substituted book -- when the sharp
    book is absent, the market is not two-way, or a price is unreadable. A
    soft book's line silently standing in for Pinnacle would invalidate the
    entire thesis being tested.

    `last_update` is taken from the MARKET when the provider supplies one and
    falls back to the bookmaker envelope, because the market-level stamp is the
    one that says when this particular price moved.
    """
    home_name, away_name = event.get("home_team"), event.get("away_team")
    for bookmaker in event.get("bookmakers", []) or []:
        if not isinstance(bookmaker, dict) or bookmaker.get("key") != book:
            continue
        book_update = _parse_time(bookmaker.get("last_update"))
        for market in bookmaker.get("markets", []) or []:
            if not isinstance(market, dict) or market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) != 2:
                return None       # a draw leg makes this not a two-way book
            prices: dict[str, float] = {}
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    return None
                name, price = outcome.get("name"), outcome.get("price")
                if isinstance(price, bool) or not isinstance(price, (int, float)):
                    return None
                prices[str(name)] = float(price)
            if home_name in prices and away_name in prices:
                market_update = _parse_time(market.get("last_update"))
                return (
                    prices[home_name],
                    prices[away_name],
                    market_update or book_update,
                )
            return None
    return None


def build_snapshot_url(
    sport: str,
    at: datetime,
    api_key: str,
    bookmakers: str | None = DEFAULT_BOOKMAKERS,
    regions: str | None = None,
    base_url: str = BASE_URL,
) -> str:
    """The exact URL a snapshot fetch will request.

    Split out from `fetch_snapshot` so a test can assert the QUERY, not just
    the response parsing. The book-selection defect was invisible to every
    parser test precisely because no test looked at what was requested.
    """
    if not bookmakers and not regions:
        raise ValueError("a snapshot needs either `bookmakers` or `regions`")
    sport_key = SPORT_KEYS.get(sport.upper(), sport)
    params = {
        "apiKey": api_key,
        "markets": "h2h",
        "oddsFormat": "american",
        "date": _iso(at),
    }
    # `bookmakers` names the book directly and takes precedence; `regions` is
    # only sent when no book was named.
    if bookmakers:
        params["bookmakers"] = bookmakers
    else:
        params["regions"] = regions
    return f"{base_url}/historical/sports/{sport_key}/odds?{urllib.parse.urlencode(params)}"


def fetch_snapshot(
    sport: str,
    at: datetime,
    api_key: str,
    bookmakers: str | None = DEFAULT_BOOKMAKERS,
    regions: str | None = None,
    ledger: CreditLedger | None = None,
    base_url: str = BASE_URL,
    cache: ResponseCache | None = None,
) -> SnapshotResult:
    """One historical snapshot for one sport at one instant.

    A cache hit costs no credits and is not counted against the budget -- that
    is the point: debugging a local join must not cost money twice.
    """
    if cache is not None and cache.enabled:
        hit = cache.get(resolve_key(cache, sport, at, bookmakers or "", "h2h"))
        if hit is not None:
            result = parse_snapshot(hit)
            result.raw = hit
            return result

    url = build_snapshot_url(sport, at, api_key, bookmakers, regions, base_url)

    last: Exception | None = None
    for attempt in range(RETRIES):
        # RESERVE BEFORE EACH ATTEMPT, not once before the loop. A provider can
        # process and charge a request whose response never reaches us, so a
        # retry is not free: reserving once let three network attempts run
        # against a single debit, and a 10-credit cap permit three chargeable
        # requests. CreditCapReached is a RuntimeError and is deliberately not
        # in the except clause below, so it propagates to the caller's
        # partial-artifact handler rather than being retried away.
        if ledger is not None:
            ledger.spend_or_raise()

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if ledger is not None:
                    ledger.observe(resp.headers)
                if resp.status != 200:
                    raise OddsFetchError(f"HTTP {resp.status}")
                payload = json.loads(resp.read().decode("utf-8"))
                result = parse_snapshot(payload)
                result.raw = payload
                # Only a successful, parseable response is stored; caching a
                # failure would make a transient outage permanent on replay.
                if (cache is not None and cache.enabled
                        and result.coverage.complete):
                    cache.put(key_for(sport, at, bookmakers or "", "h2h"), payload)
                return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[attempt])

    return SnapshotResult(None, [], Coverage().fail(
        redact(f"snapshot at {_iso(at)} failed after {RETRIES} attempts: {last}")))


def probe_earliest_snapshot(
    sport: str, api_key: str, candidates: list[datetime], base_url: str = BASE_URL
) -> tuple[datetime | None, str]:
    """Find the earliest candidate date that returns games. Run this FIRST.

    Archive depth decides which sports the study is even possible on. Costs one
    call per candidate -- a handful of credits to avoid budgeting a season that
    is not there.
    """
    for candidate in sorted(candidates):
        result = fetch_snapshot(sport, candidate, api_key, base_url=base_url)
        if result.coverage.complete and result.quotes:
            return candidate, f"{len(result.quotes)} sharp quotes at {_iso(candidate)}"
    return None, "no candidate date returned sharp quotes"


def estimate_credits(days: int, snapshots_per_day: int, regions: int = 1, markets: int = 1) -> int:
    return days * snapshots_per_day * CREDITS_PER_HISTORICAL_CALL * regions * markets
