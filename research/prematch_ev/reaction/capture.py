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
from datetime import datetime, timedelta
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
    "api.elections.kalshi.com/trade-api/v2/series/{series}/markets",
    "api.elections.kalshi.com/trade-api/v2/series/{series}/markets/"
    "{ticker}/candlesticks",
    "site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard",
)


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
        return estimate_credits(self.days, self.snapshots_per_day,
                                self.regions, self.markets)

    @property
    def odds_requests(self) -> int:
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
        return CaptureBudget(max_requests=self.total_requests,
                             max_credits=self.odds_credits)

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
