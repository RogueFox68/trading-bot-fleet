"""Exchange fee models.

The spec this study came from modelled fees as a flat fraction of the $1.00
payout (`W_net = 1 - F_rate`). Neither venue works that way. Both charge a
PARABOLIC fee AT EXECUTION, as a function of contract price:

    Kalshi     ceil(0.07 * C * P * (1-P))          taker, rounded up to a cent
    Polymarket C * theta * P * (1-P)               taker; makers pay zero

The consequence that matters for strategy selection: as a fraction of STAKE --
which is the unit `net_edge = EV / price` is denominated in -- the Kalshi fee is

    fee / stake = 0.07 * (1 - price)

i.e. MONOTONICALLY DECREASING in price. A 5c contract costs 6.65% of stake to
trade; a 95c contract costs 0.35%. Cheap contracts are the expensive ones.

No flat rate can approximate that. A constant cannot match a parabola; you only
get to choose which price region you are wrong in. Modelled at a flat 2%, every
contract clearing a 3% net-edge screen has a TRUE edge between -1.55% (at 5c)
and +4.75% (at 95c) -- see `tests/test_fees.py::FeeCurveTest`, which pins the
sign flip.

FEE SCHEDULES CHANGE. Each is stamped with the date it was last verified and
the source that verified it. Re-check before trusting a result built on them;
`describe()` prints the stamps so a study report can carry its own provenance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

Role = Literal["maker", "taker"]

# --- Kalshi -----------------------------------------------------------------
# Taker:  ceil(0.07 * C * P * (1-P)), rounded UP to the next cent per order.
# Maker:  same shape at roughly a quarter of the coefficient.
#
# THESE COEFFICIENTS ARE NOT UNIVERSAL. Kalshi publishes per-series schedules
# and some series carry different rates or maker treatment entirely. Resolve
# the schedule for the series actually being studied before trusting a result
# built on these; `SERIES_OVERRIDES` is where a resolved one belongs.
#
# A NOTE ON THE ROUNDING, because an earlier version of this comment claimed
# maker fees "usually round to $0.00 on small orders": that is impossible.
# ceil() of any POSITIVE raw fee is at least one cent, so a positive maker
# rate can never round down to nothing. The rounding cuts the other way -- it
# makes SMALL orders relatively more expensive, and most so at the extremes.
# One contract at 2c owes 0.0343c of raw maker fee and is charged 1c: fifty
# percent of stake. `tests/test_fees.py::CeilingTest` pins this.
KALSHI_TAKER_COEFF = 0.07
KALSHI_MAKER_COEFF = 0.0175
KALSHI_VERIFIED_ON = "2026-09-21"

# series ticker -> {"taker": coeff, "maker": coeff}. Empty because no series
# schedule has been resolved yet; `fee_for` takes a `series` and raises on an
# unresolved one only when the caller asks it to, so a study can state which
# schedule it actually used rather than assuming the generic one.
SERIES_OVERRIDES: dict[str, dict[str, float]] = {}

# --- Polymarket -------------------------------------------------------------
# Verified 2026-09-21. Fee Structure V2; sports theta raised 0.03 -> 0.05 in
# July 2026. Makers pay zero and collect rebates funded by taker fees.
#
# The US entity (docs.polymarket.us) publishes a DIFFERENT schedule from the
# international venue. `POLYMARKET_US_*` is a placeholder: it is NOT verified,
# and `polymarket_us` deliberately raises rather than return a plausible number.
# A wrong fee here is invisible -- it shows up as a confident EV, not an error.
POLYMARKET_SPORTS_THETA = 0.05
POLYMARKET_VERIFIED_ON = "2026-09-21"


@dataclass(frozen=True)
class Fee:
    """A fee quote for one order.

    `dollars` is the total charged for the order, `per_contract` the same
    figure divided by size. `of_stake` is the fraction of notional it
    represents -- the unit that actually matters for a net-edge screen.
    """

    dollars: float
    per_contract: float
    of_stake: float
    venue: str
    role: Role


def _quote(dollars: float, n_contracts: float, price: float, venue: str, role: Role) -> Fee:
    stake = n_contracts * price
    return Fee(
        dollars=dollars,
        per_contract=dollars / n_contracts if n_contracts else 0.0,
        of_stake=dollars / stake if stake else 0.0,
        venue=venue,
        role=role,
    )


def _validate(n_contracts: float, price: float) -> None:
    if n_contracts <= 0:
        raise ValueError(f"n_contracts must be positive, got {n_contracts}")
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1) exclusive, got {price}")


def _ceil_cent(dollars: float) -> float:
    """Round up to the next whole cent.

    Kalshi applies this per ORDER, not per contract, so it is a meaningful
    extra cost on small orders: three contracts at 50c owe 5.25c of raw fee
    and are charged 6c.
    """
    return math.ceil(round(dollars * 100, 9)) / 100


def kalshi_fee(
    n_contracts: float,
    price: float,
    role: Role = "taker",
    series: str | None = None,
) -> Fee:
    override = SERIES_OVERRIDES.get(series or "", {})
    default = KALSHI_TAKER_COEFF if role == "taker" else KALSHI_MAKER_COEFF
    coeff = override.get(role, default)
    raw = coeff * n_contracts * price * (1.0 - price)
    return _quote(_ceil_cent(raw), n_contracts, price, "kalshi", role)


def polymarket_fee(
    n_contracts: float,
    price: float,
    role: Role = "taker",
    theta: float = POLYMARKET_SPORTS_THETA,
) -> Fee:
    # Makers pay nothing and are rebated; the rebate is deliberately NOT
    # modelled as negative fee. It is discretionary and funded from taker
    # flow, so counting on it would be booking revenue we cannot verify.
    raw = 0.0 if role == "maker" else n_contracts * theta * price * (1.0 - price)
    return _quote(raw, n_contracts, price, "polymarket", role)


def polymarket_us_fee(n_contracts: float, price: float, role: Role = "taker") -> Fee:
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


def fee_for(venue: str, n_contracts: float, price: float, role: Role = "taker") -> Fee:
    """Dispatch to a venue's fee model. Unknown venue raises, never defaults.

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
    return model(n_contracts, price, role)


def describe() -> str:
    """Provenance line for a study report, so results carry their own stamps."""
    return (
        f"kalshi taker={KALSHI_TAKER_COEFF} maker={KALSHI_MAKER_COEFF} "
        f"(verified {KALSHI_VERIFIED_ON}); "
        f"polymarket sports theta={POLYMARKET_SPORTS_THETA} maker=0 "
        f"(verified {POLYMARKET_VERIFIED_ON}); "
        f"polymarket_us=UNVERIFIED (raises); "
        f"series overrides resolved: {sorted(SERIES_OVERRIDES) or 'NONE -- generic rates assumed'}"
    )
