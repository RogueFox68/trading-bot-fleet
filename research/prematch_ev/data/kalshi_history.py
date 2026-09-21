"""Kalshi historical market data.

Kalshi publishes more than most exchanges: the candlestick endpoint returns
OHLC for the BID and the ASK separately, at 1-minute resolution, alongside
price OHLC, volume and open interest. That is what makes a taker backtest
honest -- you are reading the quote you would have traded against, not the
last print.

    yes_bid   open/high/low/close_dollars
    yes_ask   open/high/low/close_dollars
    price     open/high/low/close + mean/previous/min/max_dollars
    volume_fp, open_interest_fp
    period_interval   1 (minute) | 60 (hour) | 1440 (day)

Kalshi splits live from archived data at a cutoff timestamp; settled markets
older than roughly three months come from the historical archive, which starts
July 2021. `fetch_candlesticks` targets whichever endpoint fits the window.

WHAT THIS MODULE WILL NOT DO: return a short list quietly. Every fetch reports
whether its coverage is COMPLETE. A truncated page walk, an HTTP error, or an
unparseable body all surface as `complete=False` with a reason attached -- they
never present as "no data", because a study that cannot tell those apart will
happily conclude there was no edge on a window it failed to read.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

# Public market-data host. No authentication is required for market data,
# candlesticks or settlements; only trading needs RSA-signed headers.
BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
HISTORICAL_PATH = "/historical/markets/{ticker}/candlesticks"
LIVE_PATH = "/series/{series}/markets/{ticker}/candlesticks"

PAGE_LIMIT = 1000
MAX_PAGES = 200            # coverage cap; hitting it sets complete=False
REQUEST_TIMEOUT = 30
RETRIES = 3
BACKOFF_SECONDS = (2, 4, 8)

PERIOD_MINUTE, PERIOD_HOUR, PERIOD_DAY = 1, 60, 1440


class KalshiFetchError(RuntimeError):
    """A fetch that did not answer. Distinct from a fetch that answered 'none'.

    The fleet's `OrderFetchError` rule, one layer out: only an ANSWER may be
    treated as data. A timeout is silence, and silence is not an empty market.
    """


@dataclass
class Coverage:
    """Whether a result can be concluded from, and why not if it cannot."""

    complete: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> "Coverage":
        self.complete = False
        self.reasons.append(reason)
        return self

    def merge(self, other: "Coverage") -> "Coverage":
        if not other.complete:
            self.complete = False
            self.reasons.extend(other.reasons)
        return self

    def __str__(self) -> str:
        return "complete" if self.complete else f"INCOMPLETE: {'; '.join(self.reasons)}"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    bid_close: float | None
    ask_close: float | None
    price_close: float | None
    price_mean: float | None
    volume: float
    open_interest: float

    @property
    def mid(self) -> float | None:
        """Mid of the quote. None when either side is absent -- a one-sided
        book has no mid, and inventing one from the last trade would be a
        proxy for a number we do not have."""
        if self.bid_close is None or self.ask_close is None:
            return None
        return (self.bid_close + self.ask_close) / 2.0

    @property
    def spread(self) -> float | None:
        if self.bid_close is None or self.ask_close is None:
            return None
        return self.ask_close - self.bid_close


def _get(url: str) -> Any:
    """One GET with retry/backoff. Raises rather than returning a short read."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    raise KalshiFetchError(f"HTTP {resp.status} from {url}")
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_SECONDS[attempt])
    raise KalshiFetchError(f"{url} failed after {RETRIES} attempts: {last}")


def _dollars(node: Any, key: str) -> float | None:
    """Read `<key>_dollars` out of an OHLC sub-object, tolerating absence.

    Returns None for a missing value rather than 0.0. A zero price is a real,
    tradeable price on this venue; defaulting to it would manufacture data.
    """
    if not isinstance(node, dict):
        return None
    value = node.get(f"{key}_dollars")
    return float(value) if isinstance(value, (int, float)) else None


def parse_candles(payload: Any) -> tuple[list[Candle], Coverage]:
    """Parse a candlesticks response body. Pure -- no network, so it is testable.

    A body that is not the documented shape yields an INCOMPLETE coverage, not
    an empty list. Those two look identical downstream and mean opposite things.
    """
    coverage = Coverage()
    if not isinstance(payload, dict):
        return [], coverage.fail(f"response was {type(payload).__name__}, expected object")
    raw = payload.get("candlesticks")
    if raw is None:
        return [], coverage.fail("response has no 'candlesticks' key")
    if not isinstance(raw, list):
        return [], coverage.fail(f"'candlesticks' was {type(raw).__name__}, expected list")

    out: list[Candle] = []
    skipped = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        ts = item.get("end_period_ts")
        if not isinstance(ts, (int, float)):
            skipped += 1
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}
        out.append(
            Candle(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                bid_close=_dollars(item.get("yes_bid"), "close"),
                ask_close=_dollars(item.get("yes_ask"), "close"),
                price_close=_dollars(price, "close"),
                price_mean=_dollars(price, "mean"),
                volume=float(item.get("volume_fp") or item.get("volume") or 0.0),
                open_interest=float(
                    item.get("open_interest_fp") or item.get("open_interest") or 0.0
                ),
            )
        )
    if skipped:
        coverage.fail(f"{skipped}/{len(raw)} candlesticks were unparseable")
    out.sort(key=lambda c: c.ts)
    return out, coverage


def fetch_candlesticks(
    ticker: str,
    series: str,
    start: datetime,
    end: datetime,
    period_interval: int = PERIOD_MINUTE,
    base_url: str = BASE_URL,
    use_archive: bool | None = None,
) -> tuple[list[Candle], Coverage]:
    """Candles for one market over [start, end].

    `use_archive=None` picks the archive for windows older than ~90 days, which
    is roughly where Kalshi's live/historical split sits. Pass it explicitly if
    the cutoff has moved.
    """
    if use_archive is None:
        age_days = (datetime.now(timezone.utc) - end).days
        use_archive = age_days > 90

    path = (
        HISTORICAL_PATH.format(ticker=ticker)
        if use_archive
        else LIVE_PATH.format(series=series, ticker=ticker)
    )
    query = urllib.parse.urlencode(
        {
            "start_ts": int(start.timestamp()),
            "end_ts": int(end.timestamp()),
            "period_interval": period_interval,
        }
    )
    try:
        payload = _get(f"{base_url}{path}?{query}")
    except KalshiFetchError as exc:
        return [], Coverage().fail(str(exc))
    return parse_candles(payload)


def iter_settled_markets(
    series_ticker: str,
    base_url: str = BASE_URL,
    max_pages: int = MAX_PAGES,
) -> Iterator[tuple[list[dict], Coverage]]:
    """Page settled markets for a series, yielding (page, coverage).

    Stops at `max_pages` and marks coverage INCOMPLETE if the cursor had not
    run out -- truncation is a fact about the result, not a log line.
    """
    cursor = ""
    for page_no in range(max_pages):
        query = urllib.parse.urlencode(
            {k: v for k, v in
             {"series_ticker": series_ticker, "status": "settled",
              "limit": PAGE_LIMIT, "cursor": cursor}.items() if v}
        )
        try:
            payload = _get(f"{base_url}/markets?{query}")
        except KalshiFetchError as exc:
            yield [], Coverage().fail(f"page {page_no}: {exc}")
            return
        if not isinstance(payload, dict):
            yield [], Coverage().fail(f"page {page_no}: response was not an object")
            return
        markets = payload.get("markets")
        if not isinstance(markets, list):
            yield [], Coverage().fail(f"page {page_no}: 'markets' missing or not a list")
            return

        yield markets, Coverage()
        cursor = payload.get("cursor") or ""
        if not cursor:
            return

    yield [], Coverage().fail(
        f"stopped at max_pages={max_pages} with a cursor still outstanding; "
        "series coverage is truncated"
    )


def settlement_outcome(market: dict) -> int | None:
    """1 if YES settled true, 0 if false, None if not determinable.

    None rather than a default. A market whose result cannot be read must drop
    out of the scoring set; scoring it as a loss would bias every predictor
    downward by exactly the unreadable fraction.
    """
    result = str(market.get("result", "")).strip().lower()
    if result in ("yes", "y", "true", "1"):
        return 1
    if result in ("no", "n", "false", "0"):
        return 0
    return None


def audit_abbreviations(series_ticker: str, league: str, base_url: str = BASE_URL) -> int:
    """Report roster abbreviations that never appear in a series' tickers.

    An abbreviation the exchange spells differently does not raise -- it simply
    never matches, and the study reads as thin coverage on that team. That is
    the failure this command exists to make visible before a run, not after.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.matcher import ROSTERS, parse_kalshi_game_ticker

    roster = ROSTERS.get(league.upper())
    if roster is None:
        print(f"unknown league {league!r}; known: {sorted(ROSTERS)}")
        return 2

    seen: set[str] = set()
    tickers = malformed = 0
    coverage = Coverage()
    for page, page_cov in iter_settled_markets(series_ticker, base_url=base_url):
        coverage.merge(page_cov)
        for market in page:
            tickers += 1
            parsed = parse_kalshi_game_ticker(str(market.get("ticker", "")))
            if parsed:
                seen.update((parsed["a"], parsed["b"]))
            else:
                malformed += 1

    known = set(roster)
    never_seen = sorted(known - seen)
    unknown_to_us = sorted(seen - known)

    print(f"series {series_ticker} / league {league}: {tickers:,} settled markets "
          f"({coverage})")
    if malformed:
        print(f"  {malformed:,} tickers did not fit the expected shape")
    print(f"  roster abbreviations never seen in a ticker ({len(never_seen)}): "
          f"{', '.join(never_seen) or 'none'}")
    print(f"  ticker abbreviations absent from our roster ({len(unknown_to_us)}): "
          f"{', '.join(unknown_to_us) or 'none'}")
    if never_seen or unknown_to_us:
        print("  -> fix these in core/matcher.ROSTERS before running the study;")
        print("     each one is a team that will silently contribute no data.")
        return 1
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi historical data utilities")
    parser.add_argument("--audit-abbreviations", action="store_true")
    parser.add_argument("--series", default="KXMLBGAME")
    parser.add_argument("--league", default="MLB")
    ns = parser.parse_args()
    if ns.audit_abbreviations:
        raise SystemExit(audit_abbreviations(ns.series, ns.league))
    parser.print_help()
