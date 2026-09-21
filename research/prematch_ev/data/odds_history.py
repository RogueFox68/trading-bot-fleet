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

from .kalshi_history import Coverage

BASE_URL = "https://api.the-odds-api.com/v4"
SHARP_BOOK = "pinnacle"
CREDITS_PER_HISTORICAL_CALL = 10          # per region per market
REQUEST_TIMEOUT = 30
RETRIES = 3
BACKOFF_SECONDS = (2, 4, 8)

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
    """Running quota state, read from the API's own response headers."""

    used: int = 0
    remaining: int | None = None
    calls: int = 0

    def observe(self, headers: Any) -> None:
        self.calls += 1
        try:
            self.used = int(headers.get("x-requests-used", self.used))
        except (TypeError, ValueError):
            pass
        try:
            self.remaining = int(headers.get("x-requests-remaining"))
        except (TypeError, ValueError):
            pass

    def exhausted(self, reserve: int = 50) -> bool:
        return self.remaining is not None and self.remaining <= reserve

    def __str__(self) -> str:
        left = "unknown" if self.remaining is None else str(self.remaining)
        return f"{self.calls} calls, {self.used} credits used, {left} remaining"


@dataclass(frozen=True)
class SharpQuote:
    """One two-way moneyline from the sharp book, at one snapshot time."""

    snapshot: datetime
    commence_time: datetime
    away_name: str
    home_name: str
    away_price: float          # American odds
    home_price: float
    book: str

    def minutes_to_start(self) -> float:
        return (self.commence_time - self.snapshot).total_seconds() / 60.0


@dataclass
class SnapshotResult:
    snapshot: datetime | None
    quotes: list[SharpQuote] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    events_seen: int = 0
    events_without_sharp_book: int = 0


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
    missing_book = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        commence = _parse_time(event.get("commence_time"))
        home_name = event.get("home_team")
        away_name = event.get("away_team")
        if commence is None or not home_name or not away_name:
            continue

        outcomes = _sharp_h2h_outcomes(event, book)
        if outcomes is None:
            missing_book += 1
            continue
        home_price, away_price = outcomes
        quotes.append(
            SharpQuote(
                snapshot=snapshot or commence,
                commence_time=commence,
                away_name=str(away_name),
                home_name=str(home_name),
                away_price=away_price,
                home_price=home_price,
                book=book,
            )
        )

    return SnapshotResult(snapshot, quotes, coverage, len(events), missing_book)


def _sharp_h2h_outcomes(event: dict, book: str) -> tuple[float, float] | None:
    """(home_price, away_price) from the sharp book's h2h market, or None.

    Returns None -- never a partial or a substituted book -- when the sharp
    book is absent, the market is not two-way, or a price is unreadable. A
    soft book's line silently standing in for Pinnacle would invalidate the
    entire thesis being tested.
    """
    home_name, away_name = event.get("home_team"), event.get("away_team")
    for bookmaker in event.get("bookmakers", []) or []:
        if not isinstance(bookmaker, dict) or bookmaker.get("key") != book:
            continue
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
                if not isinstance(price, (int, float)):
                    return None
                prices[str(name)] = float(price)
            if home_name in prices and away_name in prices:
                return prices[home_name], prices[away_name]
            return None
    return None


def fetch_snapshot(
    sport: str,
    at: datetime,
    api_key: str,
    regions: str = "us",
    ledger: CreditLedger | None = None,
    base_url: str = BASE_URL,
) -> SnapshotResult:
    """One historical snapshot for one sport at one instant."""
    sport_key = SPORT_KEYS.get(sport.upper(), sport)
    query = urllib.parse.urlencode(
        {
            "apiKey": api_key,
            "regions": regions,
            "markets": "h2h",
            "oddsFormat": "american",
            "date": _iso(at),
        }
    )
    url = f"{base_url}/historical/sports/{sport_key}/odds?{query}"

    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if ledger is not None:
                    ledger.observe(resp.headers)
                if resp.status != 200:
                    raise OddsFetchError(f"HTTP {resp.status}")
                return parse_snapshot(json.loads(resp.read().decode("utf-8")))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[attempt])

    return SnapshotResult(None, [], Coverage().fail(
        f"snapshot at {_iso(at)} failed after {RETRIES} attempts: {last}"))


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
