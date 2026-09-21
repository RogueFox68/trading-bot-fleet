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
    has_malformed_price: bool = False

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


# Kalshi serialises `*_dollars` as a fixed-point STRING ("0.5600"), not a JSON
# number. An earlier version accepted only int/float, so every real bid and ask
# decoded to None, `mid` went with them, and the collector dropped the candle --
# a total data loss that presented as thin coverage. The fixtures did not catch
# it because they were written from an assumption about the wire format rather
# than copied from a response (rule 30, one layer out: the shape under test was
# invented, so the test could only ever confirm the invention).
PRICE_MIN, PRICE_MAX = 0.0, 1.0

ABSENT, OK, MALFORMED = "absent", "ok", "malformed"


def _dollars(node: Any, key: str) -> tuple[float | None, str]:
    """Read `<key>_dollars` from an OHLC sub-object.

    Returns (value, status). The status distinguishes the three cases that a
    bare None conflates:

      absent     the field is not there, or is explicitly null -- no quote
      ok         a finite decimal inside [0, 1]
      malformed  present but unreadable, or outside the tradeable range

    `malformed` must never be silently treated as `absent`: one is a market
    with no quote on that side, the other is a parser that has fallen behind
    the wire format, and only the second means the numbers cannot be trusted.
    """
    if not isinstance(node, dict):
        return None, ABSENT
    if f"{key}_dollars" not in node:
        return None, ABSENT
    raw = node[f"{key}_dollars"]
    if raw is None:
        return None, ABSENT

    if isinstance(raw, bool):            # bool is an int subclass; not a price
        return None, MALFORMED
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None, MALFORMED
    else:
        return None, MALFORMED

    if value != value or value in (float("inf"), float("-inf")):
        return None, MALFORMED
    if not PRICE_MIN <= value <= PRICE_MAX:
        return None, MALFORMED
    return value, OK


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
    skipped = malformed_prices = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        ts = item.get("end_period_ts")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            skipped += 1
            continue
        price = item.get("price") if isinstance(item.get("price"), dict) else {}

        bid, bid_status = _dollars(item.get("yes_bid"), "close")
        ask, ask_status = _dollars(item.get("yes_ask"), "close")
        close, close_status = _dollars(price, "close")
        mean, mean_status = _dollars(price, "mean")
        statuses = (bid_status, ask_status, close_status, mean_status)
        if MALFORMED in statuses:
            malformed_prices += 1

        out.append(
            Candle(
                ts=datetime.fromtimestamp(ts, tz=timezone.utc),
                bid_close=bid,
                ask_close=ask,
                price_close=close,
                price_mean=mean,
                volume=_numeric(item, "volume_fp", "volume"),
                open_interest=_numeric(item, "open_interest_fp", "open_interest"),
                has_malformed_price=MALFORMED in statuses,
            )
        )

    if skipped:
        coverage.fail(f"{skipped}/{len(raw)} candlesticks were unparseable")
    if malformed_prices:
        # Not merely skipped: a price we could not read means the parser may
        # be behind the wire format, which is a reason to distrust the rest.
        coverage.fail(
            f"{malformed_prices}/{len(raw)} candlesticks carried an unreadable "
            "price field (wire format may have changed)"
        )
    out.sort(key=lambda c: c.ts)
    return out, coverage


def _numeric(item: dict, *keys: str) -> float:
    """First readable numeric value among `keys`, else 0.0.

    Volume and open interest are counts: absent genuinely means none, unlike a
    price, where absent means no quote and zero means a 0c market.
    """
    for key in keys:
        raw = item.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw.strip())
            except ValueError:
                continue
    return 0.0


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
        # NO AGE HEURISTIC. `now - end > 90 days` was the rule this module was
        # told to stop using, and letting an unroutable market fall back to it
        # quietly reinstates it for exactly the markets whose partition could
        # not be established -- the ones most likely to be routed wrongly.
        return [], Coverage().fail(
            f"{ticker}: candlestick partition could not be established "
            "(no enumeration provenance and no readable settlement time); "
            "refusing to guess an endpoint"
        )

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


# Kalshi partitions market data at a cutoff timestamp: markets that settled
# BEFORE it are served from the historical archive and are absent from the live
# /markets endpoint. Enumerating only /markets therefore silently omits the
# older part of any window that straddles the cutoff -- and reports `complete`
# while doing it, because nothing failed. The archive candlestick path cannot
# recover a market that was never enumerated in the first place.
CUTOFF_PATH = "/historical/cutoff"
HISTORICAL_MARKETS_PATH = "/historical/markets"


def fetch_historical_cutoff(base_url: str = BASE_URL) -> tuple[datetime | None, Coverage]:
    """The live/historical partition boundary.

    Returns (cutoff, coverage). An unreadable cutoff yields None and INCOMPLETE
    coverage -- never a guessed boundary, because guessing it wrong routes an
    entire span of markets to an endpoint that does not have them.
    """
    try:
        payload = _get(f"{base_url}{CUTOFF_PATH}")
    except KalshiFetchError as exc:
        return None, Coverage().fail(f"could not read historical cutoff: {exc}")
    if not isinstance(payload, dict):
        return None, Coverage().fail("cutoff response was not an object")
    for key in ("market_settled_ts", "cutoff_ts", "cutoff"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc), Coverage()
        if isinstance(value, str):
            try:
                return (
                    datetime.fromisoformat(value.replace("Z", "+00:00")),
                    Coverage(),
                )
            except ValueError:
                continue
    return None, Coverage().fail(
        f"cutoff response had no recognisable timestamp (keys: {sorted(payload)})"
    )


def _iter_market_pages(path: str, series_ticker: str, base_url: str, max_pages: int):
    cursor = ""
    for page_no in range(max_pages):
        query = urllib.parse.urlencode(
            {k: v for k, v in
             {"series_ticker": series_ticker, "status": "settled",
              "limit": PAGE_LIMIT, "cursor": cursor}.items() if v}
        )
        try:
            payload = _get(f"{base_url}{path}?{query}")
        except KalshiFetchError as exc:
            yield [], Coverage().fail(f"{path} page {page_no}: {exc}")
            return
        if not isinstance(payload, dict):
            yield [], Coverage().fail(f"{path} page {page_no}: response was not an object")
            return
        markets = payload.get("markets")
        if not isinstance(markets, list):
            yield [], Coverage().fail(f"{path} page {page_no}: 'markets' missing or not a list")
            return
        yield markets, Coverage()
        cursor = payload.get("cursor") or ""
        if not cursor:
            return
    yield [], Coverage().fail(
        f"{path}: stopped at max_pages={max_pages} with a cursor outstanding; truncated"
    )


def enumerate_settled_markets(
    series_ticker: str,
    base_url: str = BASE_URL,
    max_pages: int = MAX_PAGES,
) -> tuple[dict[str, dict], Coverage]:
    """Every settled market for a series, across BOTH partitions.

    Reads the live endpoint and the historical archive, deduplicates on ticker
    (a market near the boundary can appear in both), and merges the coverage of
    each. A failure on either side makes the whole enumeration incomplete: half
    a window is not a smaller window, it is a biased one.
    """
    coverage = Coverage()
    markets: dict[str, dict] = {}
    seen_in = {"live": 0, "historical": 0}

    for label, path in (("live", "/markets"), ("historical", HISTORICAL_MARKETS_PATH)):
        for page, page_cov in _iter_market_pages(path, series_ticker, base_url, max_pages):
            coverage.merge(page_cov)
            for market in page:
                if not isinstance(market, dict):
                    continue
                ticker = str(market.get("ticker", "")).strip()
                if not ticker:
                    continue
                seen_in[label] += 1
                if ticker not in markets:
                    # Stamp where it came from: the routing fact the API itself
                    # established, rather than one inferred from a timestamp.
                    market[PARTITION_FIELD] = (
                        PARTITION_LIVE if label == "live" else PARTITION_ARCHIVE)
                    markets[ticker] = market

    if not markets and coverage.complete:
        coverage.fail(
            f"no settled markets found for series {series_ticker!r} in either "
            "partition -- verify the series ticker before concluding"
        )
    return markets, coverage


# Kalshi's documented settlement field is `settlement_ts`, an ISO STRING with
# sub-second precision, e.g. "2026-09-21T02:22:34.56292Z". An earlier version
# accepted it only as a number and listed `close_time` among its string keys,
# so on a real payload it returned close_time -- roughly three minutes earlier
# than actual settlement. Near the partition cutoff that routes a market to the
# wrong endpoint.
#
# close/expiration are contract lifecycle facts, NOT settlement, and are no
# longer consulted for this question at all.
SETTLEMENT_KEYS_ISO = ("settlement_ts", "settled_time", "settlement_time")
SETTLEMENT_KEYS_NUMERIC = ("settled_ts",)

# Where a market was actually enumerated from. This is the authoritative
# routing fact -- better than inferring a partition from a timestamp, because
# it is what the API itself did.
PARTITION_FIELD = "_partition"
PARTITION_LIVE, PARTITION_ARCHIVE = "live", "historical"


def settlement_time(market: dict) -> datetime | None:
    """When the market settled. None when no documented field carries it."""
    for key in SETTLEMENT_KEYS_ISO:
        value = market.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    for key in SETTLEMENT_KEYS_NUMERIC:
        value = market.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


# WHEN THE CONTRACT BECAME TRADEABLE. Multi-day lead times reach back before
# some markets existed: a 72h checkpoint on a Tuesday game lands on Saturday,
# and a contract listed Monday was simply not there. That is a SUBSTANTIVE
# feasibility result -- "the opportunity could not have been taken this early"
# -- and it is a different fact from "the archive failed" or "nobody quoted".
#
# An ABSENT open time is neither. It is not evidence the market was listed, so
# it gets its own status rather than being folded into either side (rule 17: a
# failed read is not a zero).
LISTING_KEYS_ISO = ("open_time", "open_ts_iso")
LISTING_KEYS_NUMERIC = ("open_ts",)


def market_open_time(market: dict) -> datetime | None:
    """When the contract opened for trading, or None when unreadable.

    This is the listing time, NOT the scheduled start (see
    `collect.market_start_time`) and NOT settlement. It answers exactly one
    question: at this checkpoint, did this contract yet exist?
    """
    for key in LISTING_KEYS_ISO:
        value = market.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    for key in LISTING_KEYS_NUMERIC:
        value = market.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def uses_archive(market: dict, cutoff: datetime | None) -> bool | None:
    """Which candlestick partition holds this market's data.

    Prefers the ENUMERATION PROVENANCE -- which endpoint actually returned this
    market -- and falls back to the settlement time against the published
    cutoff. Returns None when neither answers, so the caller records an
    unroutable market rather than guessing an endpoint.
    """
    partition = market.get(PARTITION_FIELD)
    if partition == PARTITION_ARCHIVE:
        return True
    if partition == PARTITION_LIVE:
        return False
    if cutoff is None:
        return None
    settled = settlement_time(market)
    if settled is None:
        return None
    return settled < cutoff


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
    from core.matcher import (
        ROSTERS, normalise_exchange_code, parse_kalshi_game_ticker,
        unknown_exchange_codes,
    )

    roster = ROSTERS.get(league.upper())
    if roster is None:
        print(f"unknown league {league!r}; known: {sorted(ROSTERS)}")
        return 2

    # Reads `yes_participant` -- the key the real-ticker parser returns. An
    # earlier version still read parsed["a"]/["b"], keys removed when the
    # parser was rewritten, so the free audit raised KeyError on the very
    # first real ticker. It had no test, which is why the suite stayed green.
    raw_codes: set[str] = set()
    tickers = malformed = 0
    coverage = Coverage()
    for page, page_cov in _iter_market_pages('/markets', series_ticker, base_url, MAX_PAGES):
        coverage.merge(page_cov)
        for market in page:
            tickers += 1
            parsed = parse_kalshi_game_ticker(str(market.get("ticker", "")))
            if parsed:
                raw_codes.add(parsed["yes_participant"])
            else:
                malformed += 1

    # Normalise through the SAME alias map the collector uses, or the audit
    # reports a gap the collector does not actually have (and vice versa).
    canonical = {normalise_exchange_code(c, league) for c in raw_codes}
    unresolved = unknown_exchange_codes(raw_codes, league)
    never_seen = sorted(set(roster) - canonical)

    print(f"series {series_ticker} / league {league}: {tickers:,} settled markets "
          f"({coverage})")
    if malformed:
        print(f"  {malformed:,} tickers did not fit the expected shape")
    print(f"  distinct exchange codes seen: {len(raw_codes)}")
    print(f"  UNRESOLVED exchange codes ({len(unresolved)}): "
          f"{', '.join(unresolved) or 'none'}")
    print(f"  roster teams never seen ({len(never_seen)}): "
          f"{', '.join(never_seen) or 'none'}")
    if unresolved:
        print("  -> add each to core.matcher.EXCHANGE_CODE_ALIASES with the date")
        print("     observed; until then every game for those teams is dropped.")
    if not coverage.complete:
        print(f"  -> enumeration was INCOMPLETE ({coverage}); the code list above")
        print("     may be missing teams that simply were not fetched.")
        return 1
    return 1 if unresolved else 0


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
