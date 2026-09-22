"""The odds provider's CURRENT-odds endpoint, for the shadow monitor. Paid.

The archive (`odds_history`) answers "what did the book quote at 14:05"; this
answers "what does it quote now", which is the only way to learn how old a
price is by the time it reaches us. Same provider, same book, same parser --
`parse_snapshot` reads the body once it is given the envelope the archive
already has.

COST, TRANSCRIBED -- AND CHECKED ON THE FIRST ANSWER
----------------------------------------------------
The provider charges the current-odds endpoint 1 credit per market per
region, and counts every ten named bookmakers as one region. So `h2h` from
Pinnacle alone is **1 credit per call**. That is the provider's documentation
as relayed (its docs are blocked from the session that wrote this), so it is
PROVIDER_DOC evidence, not a measurement. Every successful answer carries the
provider's own charge in `x-requests-last`; the shadow monitor compares it to
`CREDITS_PER_LIVE_CALL` on the first answer and stops if they disagree,
because a cap priced at the wrong unit cost is not a cap.

THREE CLOCKS PER POLL, NOT ONE
------------------------------
`headers_at` is when the status line and headers arrived -- the clock the
provider's `Date` header is compared with, since both mark the response
STARTING. `received_at` is when the whole body had arrived: nothing in a
response can be acted on before it is complete, so this, not the headers, is
the first instant the quotes existed for us. It used to be stamped the moment
`urlopen` returned, before the body was read: a body that took eight seconds
to arrive was credited to us eight seconds early, and every opportunity
measured from it was eight seconds longer than it was. What the monitor then
does with the body -- decoding, parsing -- ends at its own `ready_at`, which
is what a decision is dated by.

ONE ATTEMPT PER CALL
--------------------
The archive fetcher retries three times, because a missed snapshot is a hole
that no later request fills. A live poll is different: the next poll IS the
retry, a minute later, with fresher data than a retried request could return.
So a failed call is reported and not repeated, and never costs more than one
reservation.

THE CREDENTIAL
--------------
It goes into the URL because the provider requires it there, and nowhere
else: not the recorded URL (`recorded_url` strips the query), not a failure
message (`redact`), not a log.
"""

from __future__ import annotations

import email.utils
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .cache import redact
from .kalshi_history import Coverage
from .odds_history import (
    BASE_URL, DEFAULT_BOOKMAKERS, REQUEST_TIMEOUT, SPORT_KEYS, CreditLedger,
)

#: PROVIDER_DOC: 1 per market per region; <=10 named books count as 1 region.
CREDITS_PER_LIVE_CALL = 1

LIVE_ODDS_PATH = "/sports/{sport_key}/odds"


def live_odds_url(sport: str, api_key: str,
                  bookmakers: str = DEFAULT_BOOKMAKERS,
                  base_url: str = BASE_URL) -> str:
    """The exact URL a live poll requests. The same book, market and odds
    format as the archive request, so one parser reads both."""
    sport_key = SPORT_KEYS.get(sport.upper(), sport)
    query = urllib.parse.urlencode({
        "apiKey": api_key,
        "bookmakers": bookmakers,
        "markets": "h2h",
        "oddsFormat": "american",
    })
    return f"{base_url}{LIVE_ODDS_PATH.format(sport_key=sport_key)}?{query}"


def recorded_url(url: str) -> str:
    """What a record may say was requested: the path, never the query."""
    return url.split("?", 1)[0]


def _header_int(headers: Any, name: str) -> int | None:
    try:
        raw = headers.get(name)
        return None if raw is None else int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return None


def _header_date(headers: Any) -> datetime | None:
    """The provider's own clock for this response (HTTP `Date`), or None."""
    try:
        raw = headers.get("Date")
    except AttributeError:
        return None
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass
class LiveOdds:
    """One live poll: the raw body and every clock around it."""

    sent_at: datetime
    headers_at: datetime | None = None        # status and headers arrived
    received_at: datetime | None = None       # the WHOLE body had arrived
    payload: Any = None
    provider_date: datetime | None = None     # the provider's `Date` header
    charged: int | None = None                # `x-requests-last`
    used: int | None = None                   # `x-requests-used`
    remaining: int | None = None              # `x-requests-remaining`
    status: int | None = None
    coverage: Coverage = field(default_factory=Coverage)

    @property
    def ok(self) -> bool:
        return self.payload is not None and self.coverage.complete

    def snapshot_body(self) -> dict:
        """The body in the archive's envelope, so `parse_snapshot` reads it.

        The envelope's `timestamp` is the PROVIDER'S clock for the response
        when it sent one, and our receipt time only when it did not -- which
        is recorded as such, never passed off as the provider's.
        """
        stamp = self.provider_date or self.received_at or self.sent_at
        return {"timestamp": stamp.astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data": self.payload,
                "snapshot_clock": ("provider Date header"
                                   if self.provider_date else
                                   "local receipt (no Date header)")}


def fetch_live_odds(sport: str, api_key: str, *, ledger: CreditLedger,
                    now: Callable[[], datetime],
                    bookmakers: str = DEFAULT_BOOKMAKERS,
                    base_url: str = BASE_URL) -> LiveOdds:
    """One live poll. Reserves before the attempt; never retries.

    `CreditCapReached` is not caught: a monitor that has spent what it was
    allowed stops, it does not skip a tick and try again.
    """
    ledger.spend_or_raise(CREDITS_PER_LIVE_CALL)
    url = live_odds_url(sport, api_key, bookmakers, base_url)
    result = LiveOdds(sent_at=now())
    try:
        request = urllib.request.Request(url,
                                         headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as resp:
            result.headers_at = now()
            headers = resp.headers
            ledger.observe(headers)
            result.status = getattr(resp, "status", None)
            result.provider_date = _header_date(headers)
            result.charged = _header_int(headers, "x-requests-last")
            result.used = _header_int(headers, "x-requests-used")
            result.remaining = _header_int(headers, "x-requests-remaining")
            if result.status not in (None, 200):
                result.received_at = now()
                result.coverage.fail(f"HTTP {result.status}")
                return result
            body = resp.read()
            # Not before: a response is not in hand until all of it is.
            result.received_at = now()
        payload = json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
            OSError, ValueError) as exc:
        result.received_at = result.received_at or now()
        result.coverage.fail(redact(f"live odds poll failed: {exc}"))
        return result
    if not isinstance(payload, list):
        result.coverage.fail(
            f"live odds body was {type(payload).__name__}, expected a list of "
            f"events")
    result.payload = payload
    return result
