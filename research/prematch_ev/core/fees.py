"""Exchange fee models.

The spec this study came from modelled fees as a flat fraction of the $1.00
payout (`W_net = 1 - F_rate`). Neither venue works that way. Both charge a
PARABOLIC fee AT EXECUTION, as a function of contract price:

    Kalshi     ceil(0.07 * m * C * P * (1-P))     taker, m = series multiplier
    Polymarket C * theta * P * (1-P)              taker; makers pay zero

The consequence that matters for strategy selection: as a fraction of STAKE --
which is the unit `net_edge = EV / price` is denominated in -- the Kalshi fee is

    fee / stake = 0.07 * m * (1 - price)

i.e. MONOTONICALLY DECREASING in price. At m = 1 a 5c contract costs 6.65% of
stake to trade; a 95c contract costs 0.35%. Cheap contracts are the expensive
ones.

No flat rate can approximate that. A constant cannot match a parabola; you only
get to choose which price region you are wrong in. Modelled at a flat 2%, every
contract clearing a 3% net-edge screen has a TRUE edge between -1.55% (at 5c)
and +4.75% (at 95c) -- see `tests/test_fees.py::FeeCurveTest`, which pins the
sign flip.

TWO THINGS ABOUT A FEE ARE SEPARATELY UNKNOWN, AND BOTH ARE MODELLED AS SUCH:

  1. WHICH SCHEDULE WAS IN FORCE ON THE DAY OF THE DECISION. Kalshi publishes
     per-series fee changes with a `scheduled_ts`, so the rate is a function of
     WHEN, not just of which series. Pricing a September 2026 decision at the
     rate a later run happens to read is retroactive repricing, and it moves
     every EV in the study. `resolve_schedule` takes a timestamp and returns
     only an entry whose `effective_from` precedes it; a decision earlier than
     the first recorded entry RAISES rather than extrapolating backwards.

  2. HOW THE CHARGE IS ROUNDED, which depends on the ACCOUNT ROUTE and is not a
     property of the market at all. Kalshi's fee-rounding rules align a direct
     member's balance at $0.0001 and a non-direct member's at $0.01, over a
     model fee that is itself ceiled to $0.000001. This study does not know
     which route applies, so it does not choose: `AccountRoute` is a required
     dimension of every quote and the study reports BOTH, labelled.

     At the one-contract size this study prices at, the alignment quantum is
     not a rounding detail -- it IS the fee. A 50c taker contract at m = 0.5
     owes $0.00875, which a non-direct account pays as $0.01 (+14%) and a
     direct account as $0.0088 (+0.6%). Silently picking one would set the
     study's entire cost basis by an unexamined default.

FEE SCHEDULES CHANGE. Each entry carries the source that produced it, the id
that source gave it, and the date someone actually read it. `describe()` prints
those stamps so a study report carries its own provenance, including what could
not be checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Literal, Sequence

Role = Literal["maker", "taker"]
AccountRoute = Literal["direct", "non_direct"]


class FeeScheduleUnresolved(ValueError):
    """The fee in force cannot be determined, so no number is returned.

    Deliberately an error rather than a fallback. A fee this model guesses
    shows up downstream as a confident EV, not as a failure -- the same shape
    as `polymarket_us_fee` borrowing the international theta.
    """


# --- Kalshi: the base curve -------------------------------------------------
# Taker:  ceil(0.07 * C * P * (1-P)) at multiplier 1.
# Maker:  same shape at roughly a quarter of the coefficient.
#
# These are the GENERIC coefficients, applied when a series has no recorded
# schedule. They are not universal; see `KALSHI_SERIES_SCHEDULES`.
KALSHI_TAKER_COEFF = 0.07
KALSHI_MAKER_COEFF = 0.0175
KALSHI_VERIFIED_ON = "2026-09-21"

# The MAKER coefficient has NOT been verified against a dated source, and no
# source consulted says whether a series multiplier scales maker fees the same
# way it scales taker fees. `fee_type=quadratic_with_maker_fees` establishes
# that makers are charged something on KXMLBGAME; it does not establish what.
# So a maker quote on a series WITH a recorded schedule raises. The study
# prices takers only, so this costs nothing and removes an invented number.
KALSHI_MAKER_MULTIPLIER_VERIFIED = False


# --- Kalshi: dated per-series schedules -------------------------------------


@dataclass(frozen=True)
class FeeScheduleEntry:
    """One recorded fee schedule and the window it governs.

    `effective_from` is when the entry took force, NOT when it was read.
    Keeping those apart is the whole point: an entry read today may have taken
    force a year ago, and an entry read today must never price a decision made
    before it took force.
    """

    effective_from: datetime
    multiplier: float
    fee_type: str
    source: str
    source_id: str | None
    observed_on: str
    observed_by: str
    note: str = ""

    def taker_coeff(self) -> float:
        return KALSHI_TAKER_COEFF * self.multiplier

    def label(self) -> str:
        return (f"multiplier={self.multiplier} type={self.fee_type} "
                f"effective {self.effective_from.date().isoformat()} "
                f"(id {self.source_id or 'none'}, read {self.observed_on})")


# HOW THESE ENTRIES GOT HERE, because it bounds what they are worth:
#
# Every entry was read from the PUBLIC endpoint
#   GET /trade-api/v2/series/fee_changes?series_ticker=<S>&show_historical=true
# with the current series endpoint (/trade-api/v2/series/<S>) read beside it.
# THE SESSIONS THAT WROTE THIS CODE COULD NOT RE-READ EITHER: egress to
# api.elections.kalshi.com is blocked here, so each entry is a transcription
# of a reading made elsewhere, not an independent confirmation of it, and says
# whose reading it was. Re-read before any result built on one is promoted.
#
#   KXMLBGAME  read 2026-09-21 during PR #27's review; the current series
#              endpoint agreed with the later entry.
#   KXNFLGAME  read 2026-09-25 on the owner's machine by `verify_fees.py`
#              (PR #27 comment 5833183856): both reads HTTP 200, one dated
#              change on record, the current series fields in agreement. The
#              raw bodies and their SHA-256s stay in that machine's
#              gitignored `study_output/fee_evidence/`.
#
# WHAT A DATED ENTRY SETTLES, AND WHAT IT DOES NOT: the MULTIPLIER in force
# at a decision, from the earliest recorded change on. Before that change it
# settles nothing, and a decision there is refused (`resolve_schedule`), not
# priced at the oldest entry. It never settles the ACCOUNT ROUTE or the
# rounding source's inconsistency -- those stay unresolved in every
# provenance record, beside the dated multiplier, not behind it.
#
# THE KXMLBGAME CONFLICT IS RETAINED, NOT RESOLVED. Kalshi's fee-schedule PDF
# dated July 7 still lists multiplier 1 for that series. The dated API says
# 0.5 from 2026-08-07. Those disagree for the September window this study
# covers, and nothing here adjudicates them -- `SCHEDULE_CONFLICTS` carries
# the dissent into `describe()` so a report cannot print "verified" over an
# open question.
KALSHI_SERIES_SCHEDULES: dict[str, tuple[FeeScheduleEntry, ...]] = {
    "KXMLBGAME": (
        FeeScheduleEntry(
            effective_from=datetime(2025, 10, 4, tzinfo=timezone.utc),
            multiplier=1.0,
            fee_type="quadratic_with_maker_fees",
            source="api.elections.kalshi.com/trade-api/v2/series/fee_changes"
                   "?series_ticker=KXMLBGAME&show_historical=true",
            source_id=None,
            observed_on="2026-09-21",
            observed_by="PR #27 review; transcribed, not re-read in-session",
            note="the endpoint reported a DATE only, so the intra-day boundary "
                 "is unknown; it matters only for decisions on 2025-10-04 "
                 "itself, which is outside every window studied so far",
        ),
        FeeScheduleEntry(
            effective_from=datetime(2026, 8, 7, 4, 59, 45, 131000, tzinfo=timezone.utc),
            multiplier=0.5,
            fee_type="quadratic_with_maker_fees",
            source="api.elections.kalshi.com/trade-api/v2/series/fee_changes"
                   "?series_ticker=KXMLBGAME&show_historical=true",
            source_id="38032af2-e3fa-4659-9280-da64300b544c",
            observed_on="2026-09-21",
            observed_by="PR #27 review; transcribed, not re-read in-session",
            note="the live series endpoint agreed at 0.5 when this was read",
        ),
    ),
    "KXNFLGAME": (
        FeeScheduleEntry(
            effective_from=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc),
            multiplier=1.0,
            fee_type="quadratic_with_maker_fees",
            source="api.elections.kalshi.com/trade-api/v2/series/fee_changes"
                   "?series_ticker=KXNFLGAME&show_historical=true",
            source_id="babedc22-e303-4aaf-8e0b-5016f1239786",
            observed_on="2026-09-25",
            observed_by="the owner's machine, by verify_fees.py (PR #27 "
                        "comment 5833183856): fee_changes and series both "
                        "HTTP 200; transcribed, not re-read in-session",
            note="the only dated change on record for this series, and the "
                 "current series fields agreed (multiplier 1, "
                 "quadratic_with_maker_fees). It confirms the GENERIC "
                 "multiplier for decisions from 2026-01-01T08:00Z on; nothing "
                 "dated says what applied before, so an earlier decision is "
                 "refused rather than priced here. The account route and the "
                 "rounding source remain unresolved: this entry is the "
                 "multiplier only",
        ),
    ),
}

# Dissent that the dated schedule does NOT settle. Printed by `describe()`.
SCHEDULE_CONFLICTS: dict[str, tuple[str, ...]] = {
    "KXMLBGAME": (
        "Kalshi's fee-schedule PDF dated 2026-07-07 lists multiplier 1 for "
        "this series, which disagrees with the dated API entry of 0.5 from "
        "2026-08-07 over the whole September 2026 window. The API is dated "
        "and the PDF is not, which is why the API is used -- but the conflict "
        "is unresolved and every September figure inherits it. A run of this "
        "study at multiplier 1 doubles the taker fee.",
    ),
}


def series_schedule(series: str | None) -> tuple[FeeScheduleEntry, ...]:
    return KALSHI_SERIES_SCHEDULES.get(series or "", ())


def resolve_schedule(series: str | None, at: datetime | None) -> FeeScheduleEntry | None:
    """The entry in force at `at`, or None when the series has no schedule.

    Raises `FeeScheduleUnresolved` in the two cases where an answer would be
    invented rather than looked up:

      * a series WITH a schedule priced without a timestamp -- the multiplier
        has changed within the period this study can reach, so "which rate"
        has no series-only answer;
      * a timestamp EARLIER than the first recorded entry -- the schedule then
        in force was not recorded, and extrapolating the oldest one backwards
        is exactly the retroactive repricing this function exists to prevent.

    Resolution is strictly `effective_from <= at`, so an entry recorded later
    can never reach back and change the price of an earlier decision.
    """
    entries = series_schedule(series)
    if not entries:
        return None
    if at is None:
        raise FeeScheduleUnresolved(
            f"series {series!r} has a DATED fee schedule "
            f"({len(entries)} recorded changes) but no decision timestamp was "
            "supplied, so the multiplier in force cannot be determined. Pass "
            "`at=` the moment the decision would have been made."
        )
    if at.tzinfo is None:
        raise FeeScheduleUnresolved(
            f"decision timestamp {at!r} is naive; fee schedules are dated in "
            "UTC and a naive timestamp silently resolves to whatever the "
            "reader assumes"
        )
    in_force = entry_in_force(entries, at)
    if in_force is None:
        earliest = min(e.effective_from for e in entries)
        raise FeeScheduleUnresolved(
            f"{at.isoformat()} precedes the earliest recorded fee schedule for "
            f"{series!r} ({earliest.isoformat()}). The schedule then in force "
            "was never recorded; it is not the oldest one on file."
        )
    return in_force


def entry_in_force(entries: Sequence[FeeScheduleEntry],
                   at: datetime) -> FeeScheduleEntry | None:
    """The newest entry that took force at or before `at`, or None.

    The one statement of "in force", shared by the pricing path and by
    `verify_fees.py`, which applies it to entries it has just read -- so a
    fetched schedule is judged by exactly the rule that would price with it.
    """
    started = [e for e in entries if e.effective_from <= at]
    return max(started, key=lambda e: e.effective_from) if started else None


def schedule_changes_within(series: str | None, start: datetime,
                            end: datetime) -> list[FeeScheduleEntry]:
    """Entries that take force INSIDE a window.

    A window that straddles a change is priced at two different rates, which is
    correct but easy to read as noise in the results. Callers surface it.
    """
    return sorted((e for e in series_schedule(series)
                   if start < e.effective_from <= end),
                  key=lambda e: e.effective_from)


# --- Rounding: the account route --------------------------------------------
# Kalshi's fee-rounding rules (docs.kalshi.com/getting_started/fee_rounding, as
# recorded in PR #27's review on 2026-09-21 -- this session cannot reach that
# page either) describe two steps:
#
#   1. the MODEL trade fee is ceiled to $0.000001;
#   2. the charge is then aligned to the account's balance precision, which is
#      $0.0001 for a direct member and $0.01 for a non-direct member, with a
#      signed revenue/balance alignment and a per-order accumulator.
#
# Both steps round a debit UP, so both are modelled as ceilings. That is the
# conservative reading of "signed alignment" for a fee, and it is stated here
# rather than buried, because the opposite reading would make every fee in the
# study smaller.
#
# THE SOURCE IS NOT SELF-CONSISTENT: the same document's prose says centicent
# while its general example table shows cent rounding. That is a second reason
# the route is reported rather than chosen.
MODEL_FEE_PRECISION = "0.000001"
ROUTE_ALIGNMENT: dict[str, str] = {
    "direct": "0.0001",
    "non_direct": "0.01",
}
ROUTE_LABELS: dict[str, str] = {
    "direct": "direct member, $0.0001 balance alignment",
    "non_direct": "non-direct member, $0.01 balance alignment",
}
# The DEFAULT is the more expensive of the two, so an unexamined call is
# conservative rather than flattering. It is NOT a resolution of the question:
# the account route for this study is unknown, and every report renders both.
DEFAULT_ROUTE: AccountRoute = "non_direct"
ROUTE_RESOLVED = False
FEE_ROUNDING_VERIFIED_ON = "2026-09-21"


# --- Polymarket -------------------------------------------------------------
# Verified 2026-09-21. Fee Structure V2; sports theta raised 0.03 -> 0.05 in
# July 2026. Makers pay zero and collect rebates funded by taker fees.
#
# The US entity (docs.polymarket.us) publishes a DIFFERENT schedule from the
# international venue. `polymarket_us_fee` deliberately raises rather than
# return a plausible number. A wrong fee here is invisible -- it shows up as a
# confident EV, not an error.
POLYMARKET_SPORTS_THETA = 0.05
POLYMARKET_VERIFIED_ON = "2026-09-21"


@dataclass(frozen=True)
class Fee:
    """A fee quote for one order.

    `dollars` is the total charged for the order, `per_contract` the same
    figure divided by size. `of_stake` is the fraction of notional it
    represents -- the unit that actually matters for a net-edge screen.

    `raw_dollars` is the charge BEFORE rounding. Keeping it lets a report show
    how much of a quote is the curve and how much is the alignment quantum,
    which at one-contract size is most of it.
    """

    dollars: float
    per_contract: float
    of_stake: float
    venue: str
    role: Role
    raw_dollars: float = 0.0
    route: str = ""
    multiplier: float = 1.0
    schedule: FeeScheduleEntry | None = None


def _quote(dollars: float, raw: float, n_contracts: float, price: float,
           venue: str, role: Role, route: str = "", multiplier: float = 1.0,
           schedule: FeeScheduleEntry | None = None) -> Fee:
    stake = n_contracts * price
    return Fee(
        dollars=dollars,
        per_contract=dollars / n_contracts if n_contracts else 0.0,
        of_stake=dollars / stake if stake else 0.0,
        venue=venue,
        role=role,
        raw_dollars=raw,
        route=route,
        multiplier=multiplier,
        schedule=schedule,
    )


def _validate(n_contracts: float, price: float) -> None:
    if n_contracts <= 0:
        raise ValueError(f"n_contracts must be positive, got {n_contracts}")
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1) exclusive, got {price}")


def _dec(value) -> Decimal:
    """A float's SHORTEST decimal form, which is the number a human wrote.

    `Decimal(0.035)` is 0.03499999999999999611421941381195...; `Decimal("0.035")`
    is 0.035. Everything in a fee schedule was written as a decimal literal, so
    that is the value to compute with.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(value)


def _ceil_to(dollars, quantum: str) -> float:
    """Round a debit UP to the next multiple of `quantum`.

    Decimal, not float arithmetic: the quanta here are $0.000001 and $0.0001,
    and `math.ceil(x * 10000) / 10000` on binary floats rounds a charge that
    lands exactly on a quantum up to the next one often enough to matter at
    these sizes.
    """
    q = Decimal(quantum)
    steps = (_dec(dollars) / q).to_integral_value(rounding=ROUND_CEILING)
    return float(steps * q)


def raw_kalshi_fee(coeff: float, n_contracts: float, price: float) -> Decimal:
    """The parabola, computed EXACTLY, before any rounding.

    In float, `0.035 * 100 * 0.50 * 0.50` is 0.8750000000000001 rather than
    0.875 -- and a ceiling turns that last bit into a whole extra quantum, so a
    direct-member charge of exactly $0.8750 bills as $0.8751. The noise is
    invisible until something rounds up, which is exactly what happens next, so
    the raw charge is computed in Decimal and only the rounded result becomes a
    float.
    """
    p = _dec(price)
    return _dec(coeff) * _dec(n_contracts) * p * (Decimal(1) - p)


def round_fee(raw_dollars, route: AccountRoute = DEFAULT_ROUTE) -> float:
    """Model precision first, then the account's balance alignment.

    Alignment applies per ORDER, not per contract, so on the non-direct route
    it is a meaningful extra cost on small orders: three contracts at 50c owe
    5.25c of raw fee at multiplier 1 and are charged 6c.
    """
    if route not in ROUTE_ALIGNMENT:
        raise ValueError(f"unknown account route {route!r}; "
                         f"known routes are {sorted(ROUTE_ALIGNMENT)}")
    modelled = _ceil_to(raw_dollars, MODEL_FEE_PRECISION)
    return _ceil_to(modelled, ROUTE_ALIGNMENT[route])


def kalshi_fee(
    n_contracts: float,
    price: float,
    role: Role = "taker",
    series: str | None = None,
    at: datetime | None = None,
    route: AccountRoute = DEFAULT_ROUTE,
) -> Fee:
    entry = resolve_schedule(series, at)
    if entry is None:
        multiplier = 1.0
        base = KALSHI_TAKER_COEFF if role == "taker" else KALSHI_MAKER_COEFF
        coeff = base
    else:
        if role == "maker" and not KALSHI_MAKER_MULTIPLIER_VERIFIED:
            raise FeeScheduleUnresolved(
                f"series {series!r} has a dated schedule (multiplier "
                f"{entry.multiplier}, {entry.fee_type}) but no consulted source "
                "says whether that multiplier scales MAKER fees, or what the "
                "maker coefficient is. Pricing a maker here would invent both. "
                "Verify the maker schedule and set "
                "KALSHI_MAKER_MULTIPLIER_VERIFIED before using maker quotes."
            )
        multiplier = entry.multiplier
        coeff = entry.taker_coeff()
    raw = raw_kalshi_fee(coeff, n_contracts, price)
    return _quote(round_fee(raw, route), float(raw), n_contracts, price,
                  "kalshi", role, route, multiplier, entry)


def polymarket_fee(
    n_contracts: float,
    price: float,
    role: Role = "taker",
    series: str | None = None,
    at: datetime | None = None,
    route: AccountRoute = DEFAULT_ROUTE,
    theta: float = POLYMARKET_SPORTS_THETA,
) -> Fee:
    # Makers pay nothing and are rebated; the rebate is deliberately NOT
    # modelled as negative fee. It is discretionary and funded from taker
    # flow, so counting on it would be booking revenue we cannot verify.
    raw = 0.0 if role == "maker" else n_contracts * theta * price * (1.0 - price)
    return _quote(raw, raw, n_contracts, price, "polymarket", role, route)


def polymarket_us_fee(n_contracts: float, price: float, role: Role = "taker",
                      series: str | None = None, at: datetime | None = None,
                      route: AccountRoute = DEFAULT_ROUTE) -> Fee:
    raise NotImplementedError(
        "The Polymarket US entity publishes a different fee schedule from the "
        "international venue and it has not been verified for this study. "
        "Verify against docs.polymarket.us and add it explicitly -- do not "
        "reuse the international theta, which would silently misprice every "
        "trade on that venue."
    )


_VENUES = {
    "kalshi": kalshi_fee,
    "polymarket": polymarket_fee,
    "polymarket_us": polymarket_us_fee,
}


def fee_for(venue: str, n_contracts: float, price: float, role: Role = "taker",
            series: str | None = None, at: datetime | None = None,
            route: AccountRoute = DEFAULT_ROUTE) -> Fee:
    """Dispatch to a venue's fee model. Unknown venue raises, never defaults.

    `series` AND `at` are both threaded through, because the rate is a function
    of both. A resolved schedule the pricing path cannot see is worse than no
    feature at all: `describe()` would report it while every fee was computed
    at the generic rate. Callers that price a trade MUST pass both.

    There is no fallback schedule on purpose. Defaulting an unknown venue to
    some other venue's numbers produces a confident, wrong EV -- the same
    failure shape as a default position owner.
    """
    _validate(n_contracts, price)
    try:
        model = _VENUES[venue]
    except KeyError:
        raise ValueError(
            f"unknown venue {venue!r}; known venues are {sorted(_VENUES)}"
        ) from None
    return model(n_contracts, price, role, series, at, route)


def unresolved_series_warning(series: str | None) -> str | None:
    """Says when a study is pricing at the generic rate for its own series."""
    if series and not series_schedule(series):
        return (f"series {series!r} has NO recorded fee schedule; the generic "
                "Kalshi coefficients are assumed and every return figure "
                "inherits that assumption")
    return None


def route_warning() -> str:
    return ("ACCOUNT ROUTE UNRESOLVED: rounding differs by account "
            f"({ROUTE_LABELS['direct']} vs {ROUTE_LABELS['non_direct']}). "
            "At one-contract size the quantum dominates the fee, so both are "
            "reported and neither is adopted.")


def describe_lines(series: str | None = None, at: datetime | None = None,
                   route: AccountRoute = DEFAULT_ROUTE) -> list[str]:
    """The provenance facts, one per line.

    `describe()` joins these; the report prints them separately. ONE source of
    the facts, two presentations -- a report that assembled its own version
    would eventually disagree with the one written into `coverage.json`, which
    is the artifact anyone checking the run would read.

    Never raises: a report must still print when the schedule cannot be
    resolved, and saying WHY it could not is the useful output.
    """
    parts = [
        f"kalshi base taker={KALSHI_TAKER_COEFF} maker={KALSHI_MAKER_COEFF} "
        f"(generic, verified {KALSHI_VERIFIED_ON})",
        f"polymarket sports theta={POLYMARKET_SPORTS_THETA} maker=0 "
        f"(verified {POLYMARKET_VERIFIED_ON})",
        "polymarket_us=UNVERIFIED (raises)",
        f"rounding: model ceil ${MODEL_FEE_PRECISION} then "
        f"{ROUTE_LABELS.get(route, route)} (recorded {FEE_ROUNDING_VERIFIED_ON}, "
        f"route resolved: {'yes' if ROUTE_RESOLVED else 'NO'})",
        f"series schedules recorded: {sorted(KALSHI_SERIES_SCHEDULES) or 'NONE'}",
    ]
    if series:
        try:
            entry = resolve_schedule(series, at)
        except FeeScheduleUnresolved as exc:
            parts.append(f"THIS RUN: {series} UNRESOLVED -- {exc}")
        else:
            if entry is None:
                parts.append(f"THIS RUN: {unresolved_series_warning(series)}")
            else:
                parts.append(
                    f"THIS RUN: {series} taker={entry.taker_coeff():.5g} "
                    f"[{entry.label()} via {entry.source}; {entry.observed_by}]")
        for conflict in SCHEDULE_CONFLICTS.get(series, ()):
            parts.append(f"UNRESOLVED CONFLICT: {conflict}")
    return parts


def fee_provenance(series: str | None, at: datetime | None,
                   route: AccountRoute = DEFAULT_ROUTE) -> dict:
    """Where a Kalshi taker fee priced at `at` came from, as a record.

    `describe_lines` is the same facts for a human; this is them for a
    machine-readable assessment, including the list of what is NOT known.
    `basis` is `dated_series_schedule` only when a recorded, dated entry
    priced it; `generic_coefficient` is an ASSUMPTION the series has no
    schedule of its own, never a verification that it has none.

    Never raises: an unresolvable schedule is reported as `unresolved`,
    because a record that cannot say why has lost the one fact it needed.
    """
    error = None
    try:
        entry = resolve_schedule(series, at)
    except FeeScheduleUnresolved as exc:
        entry, error = None, str(exc)
    basis = ("unresolved" if error else
             "dated_series_schedule" if entry else "generic_coefficient")
    multiplier = None if error else (entry.multiplier if entry else 1.0)
    unresolved: list[str] = []
    if basis == "generic_coefficient":
        unresolved.append(
            f"series_schedule: no dated fee schedule is recorded for "
            f"{series or 'this series'}; the generic multiplier 1 is ASSUMED, "
            f"not verified (verify_fees.py reads Kalshi's dated record)")
    elif basis == "unresolved":
        unresolved.append(f"series_schedule: {error}")
    if not ROUTE_RESOLVED:
        unresolved.append(
            f"account_route: unknown; priced on the {route} route "
            f"(${ROUTE_ALIGNMENT.get(route, '?')} alignment), the dearer "
            f"default -- a conservative choice, not an established fact")
    unresolved.append(
        "rounding_source: Kalshi's fee-rounding page is not self-consistent "
        "(centicent prose, cent examples); both steps are modelled as ceilings")
    unresolved.append(
        "order_size: priced as one 1-contract order; alignment is per ORDER, "
        "so a larger order pays less per contract (see the sensitivity)")
    for conflict in SCHEDULE_CONFLICTS.get(series or "", ()):
        unresolved.append(f"conflict: {conflict}")
    return {
        "venue": "kalshi",
        "role": "taker",
        "series": series,
        "priced_at": at.astimezone(timezone.utc).isoformat() if at else None,
        "basis": basis,
        "generic_taker_coefficient": KALSHI_TAKER_COEFF,
        "generic_verified_on": KALSHI_VERIFIED_ON,
        "multiplier": multiplier,
        "taker_coefficient": (None if multiplier is None
                              else KALSHI_TAKER_COEFF * multiplier),
        "schedule_entry": None if entry is None else {
            "effective_from": entry.effective_from.isoformat(),
            "multiplier": entry.multiplier,
            "fee_type": entry.fee_type,
            "source": entry.source,
            "source_id": entry.source_id,
            "observed_on": entry.observed_on,
            "observed_by": entry.observed_by,
            "note": entry.note,
        },
        "series_schedules_recorded": sorted(KALSHI_SERIES_SCHEDULES),
        "rounding": {
            "model_precision": MODEL_FEE_PRECISION,
            "route": route,
            "alignment": ROUTE_ALIGNMENT.get(route),
            "route_label": ROUTE_LABELS.get(route, route),
            "route_resolved": ROUTE_RESOLVED,
            "routes_known": dict(ROUTE_ALIGNMENT),
            "recorded_on": FEE_ROUNDING_VERIFIED_ON,
            "reading": "model fee ceiled, then the account alignment ceiled: "
                       "the conservative reading of signed alignment",
        },
        "contracts_per_order": 1,
        "error": error,
        "unresolved": unresolved,
    }


#: Multipliers a sensitivity table prices beside the one in force. 0.5 is
#: KXMLBGAME's dated value, included because it is the only per-series
#: reduction on record -- NOT because anything says it applies elsewhere.
SENSITIVITY_MULTIPLIERS = (1.0, 0.5)
SENSITIVITY_CONTRACTS = (1, 10, 100)


def fee_sensitivity(price: float, gross_edge: float, min_net_ev: float, *,
                    in_force: float | None,
                    in_force_label: str = "as priced",
                    multipliers: Sequence[float] = SENSITIVITY_MULTIPLIERS,
                    contracts: Sequence[int] = SENSITIVITY_CONTRACTS,
                    routes: Sequence[str] = ("direct", "non_direct")) -> dict:
    """How the verdict on one quote moves with each unresolved fee input.

    Every scenario is a HYPOTHESIS about the fee, labelled with where its
    multiplier came from; none is presented as the charge. The break-even
    multiplier is the largest one at which the quote would still clear the
    floor for a route and an order size -- the question a verified schedule
    would answer, stated so the answer can be read off without re-running.
    """
    _validate(1, price)
    budget = gross_edge - min_net_ev
    ordered: list[tuple[float, str]] = []
    if in_force is not None:
        ordered.append((in_force, in_force_label))
    for m in multipliers:
        if all(m != seen for seen, _ in ordered):
            ordered.append((m, "generic coefficient" if m == 1.0 else
                            "hypothetical: KXMLBGAME's dated value, not "
                            "known to apply to this series"))
    scenarios, break_even = [], []
    parabola = _dec(price) * (Decimal(1) - _dec(price))
    for route in routes:
        quantum = Decimal(ROUTE_ALIGNMENT[route])
        for n in contracts:
            for m, basis in ordered:
                raw = raw_kalshi_fee(KALSHI_TAKER_COEFF * m, n, price)
                per = round_fee(raw, route) / n
                net = gross_edge - per
                scenarios.append({
                    "route": route, "contracts": n, "multiplier": m,
                    "multiplier_basis": basis, "fee_per_contract": per,
                    "net_ev": net, "margin_to_floor": net - min_net_ev,
                    "clears_floor": not net < min_net_ev})
            if budget < 0:
                ceiling = None
                why = ("the gross edge is below the floor before any fee: "
                       "no multiplier clears it")
            else:
                charge = (_dec(n) * _dec(budget) / quantum).to_integral_value(
                    rounding=ROUND_FLOOR) * quantum
                ceiling = float(charge / (_dec(KALSHI_TAKER_COEFF) * _dec(n)
                                          * parabola))
                why = ("only a zero fee clears it on this route and size"
                       if ceiling == 0 else
                       "clears the floor at any multiplier up to this one")
            break_even.append({"route": route, "contracts": n,
                               "multiplier_at_most": ceiling, "note": why})
    return {
        "label": ("SENSITIVITY: hypothetical fee scenarios. None of them is "
                  "the established charge; see provenance.unresolved"),
        "price": price,
        "gross_edge": gross_edge,
        "min_net_ev": min_net_ev,
        "break_even_fee_per_contract": budget,
        "scenarios": scenarios,
        "break_even_multiplier": break_even,
    }


def describe(series: str | None = None, at: datetime | None = None,
             route: AccountRoute = DEFAULT_ROUTE) -> str:
    """Single-line provenance, for `coverage.json` and one-line CLI output."""
    return "; ".join(describe_lines(series, at, route))
