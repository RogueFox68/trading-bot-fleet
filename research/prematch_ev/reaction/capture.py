"""The interface a collection machine must satisfy. No transport included.

WHAT THIS IS, AND WHY IT HAS NO IMPLEMENTATION
----------------------------------------------
Live capture is NOT AUTHORISED, and no paid collection is authorised either
-- the earlier 1,500-credit approval does not carry over, and a quoted
preflight figure is a price, not a permission. So this module is the
CONTRACT and its guards: what a conforming capture must declare before it
runs, what bounds it must respect while running, and what it must be
structurally incapable of doing.

There is deliberately no HTTP client here. A module that could fetch would
eventually fetch, and the distance between "the interface exists" and "a
credential was spent" should be a decision someone makes out loud rather than
an import away.

THREE THINGS A CAPTURE MUST DO
------------------------------
1. **Declare its budget before it starts, and be refused at the bound.**
   `CaptureBudget.spend` RAISES at the limit. It does not warn and continue:
   a warning on the last request is indistinguishable from a warning on the
   first, and the thing being bounded is money.

2. **Be read-only by construction.** `assert_read_only` parses the reaction
   package and fails if anything in it names an order-placing endpoint or a
   trading credential. Not a convention -- a check, because "we would never"
   is what every unbounded script was written under.

3. **Say what it will not do.** `CapturePlan.render` prints the endpoints,
   the request count, the estimated cost and the refusals, so a human
   approves a specific plan rather than the idea of one.

WHY THE PLAN IS SEPARATE FROM THE BUDGET
----------------------------------------
The plan is what a person approves. The budget is what the code enforces.
`CapturePlan.budget()` derives the second from the first, so an approved plan
cannot be executed with a wider bound than the one that was approved -- which
is the gap that turns "about 400 credits" into a surprise.

A note on the numbers below: the per-request credit costs are TRANSCRIBED
from the provider's documentation via `data.odds_history.estimate_credits`,
which this session cannot re-read. `CapturePlan` calls that function rather
than restating a figure, so a correction there reaches every plan.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.odds_history import estimate_credits                   # noqa: E402

REACTION_PACKAGE = Path(__file__).resolve().parent

# Names that would mean this package can trade, or can authenticate as a
# trader. Checked against the SOURCE, not against intent. `portfolio` and
# `orders` are Kalshi's own order-placing paths; the credential names are the
# ones a trading client would look for.
FORBIDDEN_CAPABILITIES = (
    "/trade-api/v2/portfolio",
    "/portfolio/orders",
    "create_order",
    "place_order",
    "cancel_order",
    "KALSHI_PRIVATE_KEY",
    "KALSHI_API_SECRET",
    "ALPACA_SECRET",
    "sign_request",
)

# Read-only endpoints a capture is permitted to reach. Anything else has to be
# added here deliberately, which is the point: the allowed set is a decision,
# not a side effect of whatever a URL builder happens to produce.
ALLOWED_ENDPOINTS = (
    "api.the-odds-api.com/v4/historical/sports/{sport}/odds",
    # The shadow monitor's live poll: the same book and market, current.
    "api.the-odds-api.com/v4/sports/{sport}/odds",
    "api.elections.kalshi.com/trade-api/v2/markets",
    # The shadow monitor's live book: public market data, bids only.
    "api.elections.kalshi.com/trade-api/v2/markets/{ticker}/orderbook",
    "api.elections.kalshi.com/trade-api/v2/historical/markets",
    "api.elections.kalshi.com/trade-api/v2/historical/cutoff",
    "api.elections.kalshi.com/trade-api/v2/series/{series}/markets/"
    "{ticker}/candlesticks",
    "api.elections.kalshi.com/trade-api/v2/historical/markets/"
    "{ticker}/candlesticks",
    "site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard",
    # verify_fees.py: Kalshi's own dated record of a series' fees. Public
    # market data, read by hand on a machine that can reach Kalshi.
    "api.elections.kalshi.com/trade-api/v2/series/fee_changes",
    "api.elections.kalshi.com/trade-api/v2/series/{series}",
)
# THIS LIST IS THE SET THE FETCHERS ACTUALLY CALL, and a test builds every one
# of those URLs through the real code and checks it against this list, in both
# directions. The first version was written from a reading of the providers'
# APIs rather than from `data/`: it named `/series/{series}/markets`, which no
# fetcher requests, and omitted `/markets`, `/historical/markets`,
# `/historical/cutoff` and the archive candlesticks -- so a collector enforcing
# it would have refused its own enumeration. An allow-list nothing enforced
# could be wrong in both directions without anyone noticing.


class CaptureRefused(RuntimeError):
    """A capture exceeded its declared bound, or tried something it may not.

    Raised. A capture that warns and continues past its budget has no budget,
    and the resource it is spending is not recoverable by noticing later.
    """


class TradingCapabilityPresent(RuntimeError):
    """The read-only guarantee is not structurally true any more."""


@dataclass
class CaptureBudget:
    """A hard bound on requests and credits. Refuses at the limit.

    Deliberately mutable and stateful: it is the thing that has to know how
    much has already been spent. `spend` is the only way to consume it, so
    there is one place where the bound is enforced rather than a check at
    each call site (rule 19 -- several call sites would each have their own
    idea of "nearly done").
    """

    max_requests: int
    max_credits: int
    requests_made: int = 0
    credits_spent: int = 0
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("max_requests", "max_credits"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int, "
                                 f"got {value!r}")

    @property
    def requests_left(self) -> int:
        return self.max_requests - self.requests_made

    @property
    def credits_left(self) -> int:
        return self.max_credits - self.credits_spent

    def would_exceed(self, credits: int) -> str | None:
        """Name the bound this spend would break, or None. Does not consume."""
        if self.requests_made + 1 > self.max_requests:
            return (f"request {self.requests_made + 1} exceeds the declared "
                    f"bound of {self.max_requests}")
        if self.credits_spent + credits > self.max_credits:
            return (f"{credits} credit(s) would take the total to "
                    f"{self.credits_spent + credits}, past the declared "
                    f"bound of {self.max_credits}")
        return None

    def spend(self, credits: int, what: str = "") -> None:
        """Consume budget, or REFUSE. The only way to consume it."""
        if not isinstance(credits, int) or credits < 0:
            raise ValueError(f"credits must be a non-negative int, "
                             f"got {credits!r}")
        problem = self.would_exceed(credits)
        if problem:
            raise CaptureRefused(f"{problem}{f' ({what})' if what else ''}")
        self.requests_made += 1
        self.credits_spent += credits
        self.log.append(f"{self.requests_made}: {credits} credit(s) {what}")

    def as_dict(self) -> dict:
        return {
            "max_requests": self.max_requests,
            "max_credits": self.max_credits,
            "requests_made": self.requests_made,
            "credits_spent": self.credits_spent,
            "requests_left": self.requests_left,
            "credits_left": self.credits_left,
        }


class ReadOnlyTransport(Protocol):
    """What a conforming capture's only outward call may look like.

    GET, a URL, and nothing else -- no method parameter, no body, no signing
    hook. A transport that cannot express a POST cannot place an order, and
    that is a stronger guarantee than a transport that simply does not.
    """

    def get(self, url: str, *, timeout: float) -> tuple[int, bytes]:
        """Return `(status, body)`. Must not raise on a non-200."""
        ...


def endpoint_allowed(url: str) -> bool:
    """Is this URL one of the declared read-only endpoints?

    Matched on the template with its `{placeholders}` relaxed, so a query
    string or a concrete ticker still matches while a different PATH does
    not. A capture pointed at an undeclared path is refused.
    """
    bare = url.split("?", 1)[0]
    for template in ALLOWED_ENDPOINTS:
        pattern = "^https?://" + re.escape(template).replace(
            r"\{", "{").replace(r"\}", "}")
        pattern = re.sub(r"\{[a-z_]+\}", "[^/]+", pattern) + "$"
        if re.match(pattern, bare):
            return True
    return False


def require_allowed_endpoint(url: str) -> None:
    if not endpoint_allowed(url):
        raise CaptureRefused(
            f"{url.split('?', 1)[0]} is not one of the declared read-only "
            f"endpoints. Add it to ALLOWED_ENDPOINTS deliberately, or do not "
            f"call it")


def assert_read_only(package: Path | None = None) -> None:
    """Fail if anything in the reaction package could trade or sign as one.

    A SOURCE-LEVEL check, because the guarantee has to survive someone adding
    a convenience import six months from now. "We would never place an order
    here" is what every script with an order-placing client in scope was
    written under.
    """
    package = package or REACTION_PACKAGE
    offenders: list[str] = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text()
        for name in FORBIDDEN_CAPABILITIES:
            # Skip this module's own declaration of the forbidden list.
            if path.name == "capture.py":
                continue
            if name in text:
                offenders.append(f"{path.name}: {name}")
    if offenders:
        raise TradingCapabilityPresent(
            "the reaction package is supposed to be structurally incapable "
            "of trading, and these names say otherwise: "
            + "; ".join(offenders))


#: The odds archive's own snapshot grid. A cadence finer than this buys
#: duplicates at full price; the manifest refuses one rather than quoting it.
ARCHIVE_GRID = timedelta(seconds=300)

#: The window is CLOSED AT BOTH ENDS: [kickoff - lead, kickoff]. Declared
#: here rather than left implicit, because the reviewer was right that a
#: half-open reading is equally defensible and the two differ by a request
#: per window. Closed is chosen because a move inside the FINAL interval is
#: only ever detected by a sample at the far end of it, and that interval is
#: the one closest to kickoff.
WINDOW_IS_CLOSED_AT_BOTH_ENDS = True


@dataclass(frozen=True)
class CaptureWindow:
    """One kickoff's observation window, and the cadence inside it."""

    kickoff: datetime
    lead: timedelta
    cadence: timedelta
    label: str = ""

    def __post_init__(self) -> None:
        if self.kickoff.tzinfo is None:
            raise ValueError("kickoff must be timezone-aware: a naive stamp "
                             "is not an instant, and the offset decides the "
                             "calendar date the cost model counts")
        for name in ("lead", "cadence"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta, "
                                 f"got {value!r}")
        if self.cadence < ARCHIVE_GRID:
            raise ValueError(
                f"cadence {self.cadence} is finer than the archive's own "
                f"{ARCHIVE_GRID} grid, so the extra requests would return "
                f"DUPLICATE snapshots at full price")
        if self.cadence > self.lead:
            raise ValueError(f"cadence {self.cadence} exceeds the whole "
                             f"{self.lead} window")

    @property
    def opens_at(self) -> datetime:
        return self.kickoff - self.lead

    def timestamps(self, align_to: timedelta | None = None
                   ) -> list[datetime]:
        """Every instant this window would request, oldest first.

        `align_to` snaps the grid to a common epoch boundary. WITHOUT IT,
        TWO CLUSTERS BARELY DEDUPLICATE: an NFL Sunday's 13:00 and 16:25
        kickoffs put their 30-minute grids 25 minutes apart, so the union of
        two 145-point windows is 290 points rather than about 152. Alignment
        is what makes a multi-cluster slate affordable, and it costs only
        that each window opens up to one cadence early.
        """
        start, step = self.opens_at, self.cadence
        if align_to is not None:
            if align_to <= timedelta(0):
                raise ValueError("align_to must be positive")
            epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
            units = (start - epoch) // align_to
            start = epoch + units * align_to        # floor: never open late
        out, moment = [], start
        while moment < self.kickoff:
            out.append(moment)
            moment += step
        if WINDOW_IS_CLOSED_AT_BOTH_ENDS:
            out.append(self.kickoff)
        return out


@dataclass(frozen=True)
class TimestampManifest:
    """THE cost model: the actual UTC instants a capture would request.

    WHY THIS EXISTS RATHER THAN days x snapshots_per_day
    -----------------------------------------------------
    That arithmetic answered a different question and was wrong twice over
    for a 72-hour window:

    * `CapturePlan.days` counts INCLUSIVE CALENDAR DATES. Thursday 13:00 to
      Sunday 13:00 is 72 elapsed hours but FOUR dates, so 48/day quoted 192
      requests for a window that holds 145.
    * It cannot express two kickoff clusters sharing most of their windows,
      which is the normal case on an NFL Sunday, so it could only sum or
      guess.

    A manifest is the instants themselves: deduplicated, sorted, and
    countable. Everything downstream -- requests, credits, the retry
    reserve, the enforced budget -- is derived from `len(timestamps)`.
    """

    timestamps: tuple[datetime, ...]
    baselines: frozenset[datetime]
    windows: tuple[CaptureWindow, ...]
    aligned_to: timedelta | None
    per_window_total: int
    retry_fraction: float

    @property
    def requests(self) -> int:
        return len(self.timestamps)

    @property
    def cache_hits(self) -> int:
        """Requests the union saves over collecting each window separately.

        A snapshot is SPORT-WIDE: one response carries the whole slate at
        that instant, so an instant shared by two clusters is fetched once.
        """
        return self.per_window_total - self.requests

    @property
    def measurement_requests(self) -> int:
        """Samples a move could actually be detected FROM.

        Exactly ONE instant in a manifest cannot produce a measurement: the
        earliest. A stream's first observation is never a move, so that
        request buys a baseline and nothing else.

        It is not one per window, which a first version counted. A snapshot
        is SPORT-WIDE -- one response carries every game on the slate -- so
        a later kickoff's games are already present in the earliest snapshot
        and their first measurable interval starts there too, not at their
        own window's nominal open. Each window's open is when coverage is
        GUARANTEED, not when that game first appears.

        THIS RIDES ON ONE ASSUMPTION: that the earliest snapshot really does
        carry every cluster's games. Whether the sharp book quotes an NFL
        game 72 hours out is UNVERIFIED and is precisely what the coverage
        probe exists to settle. If it does not, the later clusters' real
        baselines move later and this count is optimistic by that much.
        """
        return max(0, self.requests - 1)

    @property
    def retry_reserve(self) -> int:
        from math import ceil
        return ceil(self.requests * self.retry_fraction)

    @property
    def requests_with_retries(self) -> int:
        return self.requests + self.retry_reserve

    @property
    def credits(self) -> int:
        """Priced through the shared model, one request at a time.

        `estimate_credits(1, n)` is n requests; the days/per-day split is
        the caller's arithmetic, and it is that split that went wrong.
        """
        return estimate_credits(1, self.requests_with_retries)

    @property
    def calendar_dates(self) -> int:
        return len({t.astimezone(timezone.utc).date()
                    for t in self.timestamps})

    @property
    def span(self) -> timedelta:
        if not self.timestamps:
            return timedelta(0)
        return self.timestamps[-1] - self.timestamps[0]

    def disagreement_with_calendar_estimate(self) -> str | None:
        """How badly `days x per-day` would misprice this manifest, if at all.

        Reported rather than silently corrected: the old figure is what a
        reader recognises, and the gap is the finding.
        """
        if not self.windows:
            return None
        cadence = self.windows[0].cadence
        if any(w.cadence != cadence for w in self.windows):
            return None                       # no single per-day rate exists
        per_day = round(timedelta(days=1) / cadence)
        naive = self.calendar_dates * per_day
        if naive == self.requests:
            return None
        return (f"days x snapshots_per_day quotes {naive:,} requests "
                f"({self.calendar_dates} calendar date(s) x {per_day:,}); "
                f"the manifest holds {self.requests:,}. The window spans "
                f"{self.span.total_seconds() / 3600:.0f} elapsed hours, "
                f"which is not the same as the dates it touches")

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "requests_with_retries": self.requests_with_retries,
            "retry_reserve": self.retry_reserve,
            "retry_fraction": self.retry_fraction,
            "credits": self.credits,
            "baseline_samples": 1 if self.timestamps else 0,
            "measurement_samples": self.measurement_requests,
            "window_opens": sorted(t.isoformat() for t in self.baselines),
            "cache_hits_from_shared_instants": self.cache_hits,
            "windows": len(self.windows),
            "aligned_to_seconds": (self.aligned_to.total_seconds()
                                   if self.aligned_to else None),
            "calendar_dates_touched": self.calendar_dates,
            "elapsed_hours": self.span.total_seconds() / 3600,
            "window_closed_at_both_ends": WINDOW_IS_CLOSED_AT_BOTH_ENDS,
            "first": (self.timestamps[0].isoformat()
                      if self.timestamps else None),
            "last": (self.timestamps[-1].isoformat()
                     if self.timestamps else None),
            # The instants THEMSELVES. A manifest that reported only a count
            # and two endpoints was an estimate with better arithmetic; what
            # someone approves before a spend is the list of requests.
            "instants": [t.astimezone(timezone.utc).isoformat()
                         for t in self.timestamps],
            "calendar_estimate_disagreement":
                self.disagreement_with_calendar_estimate(),
        }

    def budget(self) -> CaptureBudget:
        """The enforced bound, INCLUDING the retry reserve.

        The reserve was previously discussed in prose while
        `CapturePlan.budget()` derived a bound without it -- so the first
        retried request past the base count would have raised mid-window,
        ending the run with a hole in exactly the series the study measures.
        """
        return CaptureBudget(max_requests=self.requests_with_retries,
                             max_credits=self.credits)

    def render(self) -> str:
        lines = ["REQUEST MANIFEST (the instants, not an estimate)", ""]
        lines.append(f"    windows                    {len(self.windows):>7,}")
        lines.append(f"    aligned to                 "
                     f"{(str(int(self.aligned_to.total_seconds())) + 's') if self.aligned_to else 'not aligned':>7}")
        lines.append(f"    distinct instants          {self.requests:>7,}")
        lines.append(f"      of which baseline        "
                     f"{1 if self.timestamps else 0:>7,}")
        lines.append(f"      of which measurement     "
                     f"{self.measurement_requests:>7,}")
        lines.append(f"    window opens (guaranteed)  "
                     f"{len(self.baselines):>7,}")
        lines.append(f"    saved by shared instants   {self.cache_hits:>7,}")
        lines.append(f"    retry reserve ({self.retry_fraction:.0%})        "
                     f"{self.retry_reserve:>7,}")
        lines.append(f"    REQUESTS TO AUTHORISE      "
                     f"{self.requests_with_retries:>7,}")
        lines.append(f"    CREDITS TO AUTHORISE       {self.credits:>7,}")
        lines.append("")
        lines.append(f"    span                       "
                     f"{self.span.total_seconds() / 3600:>7.0f} elapsed hours"
                     f" across {self.calendar_dates} date(s)")
        disagreement = self.disagreement_with_calendar_estimate()
        if disagreement:
            lines.append("")
            lines.append("    *** the calendar-date estimate DISAGREES:")
            lines.append(f"        {disagreement}")
        return "\n".join(lines)


def build_manifest(windows: Sequence[CaptureWindow], *,
                   align_to: timedelta | None = None,
                   retry_fraction: float = 0.10) -> TimestampManifest:
    """Enumerate, deduplicate and price the instants a capture would request."""
    if not windows:
        raise ValueError("a manifest with no window prices nothing")
    if not (0.0 <= retry_fraction < 1.0):
        raise ValueError(f"retry_fraction must be in [0, 1), got "
                         f"{retry_fraction!r}")
    seen: set[datetime] = set()
    baselines: set[datetime] = set()
    per_window_total = 0
    for window in windows:
        stamps = window.timestamps(align_to)
        per_window_total += len(stamps)
        if stamps:
            baselines.add(stamps[0])
        seen.update(stamps)
    # `baselines` records each window's OPEN, which is where coverage is
    # guaranteed -- not which instants are unusable. Only the earliest
    # instant of the whole manifest is unusable, because a sport-wide
    # snapshot carries every cluster's games (see `measurement_requests`).
    return TimestampManifest(
        timestamps=tuple(sorted(seen)),
        baselines=frozenset(b for b in baselines if b in seen),
        windows=tuple(windows), aligned_to=align_to,
        per_window_total=per_window_total, retry_fraction=retry_fraction)


@dataclass(frozen=True)
class CapturePlan:
    """What a human approves, BEFORE anything is spent.

    Every field is a commitment. `render` prints them together with the
    refusals, so the approval is of a specific plan rather than of the idea
    of collecting some data.
    """

    purpose: str
    sport: str
    series: str
    first_day: datetime
    last_day: datetime
    snapshots_per_day: int
    contracts_expected: int
    candlestick_requests_per_contract: int = 1
    schedule_requests: int = 0
    regions: int = 1
    markets: int = 1
    note: str = ""
    #: When present, THIS is the cost model and the days x snapshots_per_day
    #: arithmetic becomes a cross-check that must agree. The fields above
    #: cannot express two kickoff clusters sharing most of their windows, and
    #: `days` counts inclusive calendar DATES rather than elapsed hours -- so
    #: for a 72-hour window opening Thursday and closing Sunday they quoted
    #: 192 requests for 145 instants.
    manifest: "TimestampManifest | None" = None

    @property
    def days(self) -> int:
        return max(1, (self.last_day.date() - self.first_day.date()).days + 1)

    @property
    def odds_credits(self) -> int:
        """TRANSCRIBED cost model, called rather than restated.

        `data.odds_history.estimate_credits` owns the provider's pricing. A
        figure copied into this module would be a second cost model, and the
        one that gets corrected would not be this one.
        """
        if self.manifest is not None:
            return self.manifest.credits
        return estimate_credits(self.days, self.snapshots_per_day,
                                self.regions, self.markets)

    @property
    def odds_requests(self) -> int:
        if self.manifest is not None:
            return self.manifest.requests_with_retries
        return self.days * self.snapshots_per_day

    @property
    def free_requests(self) -> int:
        """Kalshi candlesticks and the ESPN scoreboard cost no credits."""
        return (self.contracts_expected
                * self.candlestick_requests_per_contract
                + self.schedule_requests)

    @property
    def total_requests(self) -> int:
        return self.odds_requests + self.free_requests

    def budget(self) -> CaptureBudget:
        """The enforced bound, DERIVED from the approved plan.

        So an approved plan cannot be executed with a wider bound than the
        one that was approved. That gap is how "about 400 credits" becomes a
        surprise.
        """
        # With a manifest the request bound already carries the retry
        # reserve. Without one it does not, and the prose promising 10%
        # would have been enforced as 0%: the first retried request past
        # the base count raises, ending the run with a hole in the very
        # series the study measures.
        return CaptureBudget(max_requests=self.total_requests,
                             max_credits=self.odds_credits)

    def cost_model(self) -> str:
        return "timestamp_manifest" if self.manifest else "days_x_per_day"

    def refusals(self) -> tuple[str, ...]:
        """What a conforming capture is structurally unable to do."""
        return (
            "place an order, of any size, on any venue",
            "authenticate as a trader (no private key, no signing)",
            "reach any endpoint outside ALLOWED_ENDPOINTS",
            "exceed the request or credit bound derived from this plan",
            "write to the checkpoint study's output or its published result",
            "label a window overlapping 2026-09-01..16 as a holdout",
        )

    def as_dict(self) -> dict:
        return {
            "purpose": self.purpose,
            "sport": self.sport,
            "series": self.series,
            "window": [self.first_day.date().isoformat(),
                       self.last_day.date().isoformat()],
            "days": self.days,
            "snapshots_per_day": self.snapshots_per_day,
            "contracts_expected": self.contracts_expected,
            "requests": {
                "odds_archive": self.odds_requests,
                "free_exchange_and_schedule": self.free_requests,
                "total": self.total_requests,
            },
            "estimated_credits": self.odds_credits,
            "credits_note": ("from data.odds_history.estimate_credits, a "
                             "TRANSCRIBED cost model this session cannot "
                             "re-verify; it is a PRICE, not an authorisation"),
            "allowed_endpoints": list(ALLOWED_ENDPOINTS),
            "refusals": list(self.refusals()),
            "authorised": False,
            "authorisation_note": (
                "no paid collection and no live capture is authorised. The "
                "earlier 1,500-credit approval does not carry over, and this "
                "figure is a quote. A plan is not a permission"),
            "note": self.note,
        }

    def render(self) -> str:
        lines = ["CAPTURE PLAN -- NOT AUTHORISED, NOT EXECUTED", ""]
        lines.append(f"  purpose   {self.purpose}")
        lines.append(f"  sport     {self.sport}   series {self.series}")
        lines.append(f"  window    {self.first_day.date()} .. "
                     f"{self.last_day.date()}  ({self.days} days)")
        lines.append("")
        lines.append(f"  odds archive requests      "
                     f"{self.odds_requests:>7,}  "
                     f"({self.snapshots_per_day}/day)")
        lines.append(f"  free exchange + schedule   "
                     f"{self.free_requests:>7,}")
        lines.append(f"  TOTAL requests             "
                     f"{self.total_requests:>7,}")
        lines.append(f"  ESTIMATED CREDITS          "
                     f"{self.odds_credits:>7,}")
        lines.append("")
        lines.append("  That credit figure is a PRICE, not an authorisation.")
        lines.append("  No paid collection is authorised; the earlier")
        lines.append("  1,500-credit approval does not carry over.")
        lines.append("")
        lines.append("  ENDPOINTS a conforming capture may reach:")
        for endpoint in ALLOWED_ENDPOINTS:
            lines.append(f"    {endpoint}")
        lines.append("")
        lines.append("  It is structurally unable to:")
        for refusal in self.refusals():
            lines.append(f"    - {refusal}")
        if self.note:
            lines.append("")
            lines.append(f"  {self.note}")
        return "\n".join(lines)
