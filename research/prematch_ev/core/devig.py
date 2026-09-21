"""De-vigging, expected value, and position sizing.

Two de-vig methods are implemented side by side, deliberately, because the
choice is not cosmetic and this study exists partly to measure the difference.

MULTIPLICATIVE divides each raw implied probability by the booksum. That
distributes the vig in proportion to raw probability, which is known to be the
wrong shape: books load proportionally more margin onto longshots. So
multiplicative OVERSTATES longshot true probability and understates favourites.

That bias is not neutral for this strategy -- it points the same direction as
two other effects:

  1. A percentage net-edge screen (EV / price) demands ~18x less probability
     edge on a 5c contract than on a 90c one (0.26pp vs 4.59pp).
  2. The Kalshi fee as a fraction of stake is 0.07 * (1 - price): 6.65% at 5c
     against 0.35% at 95c.

Threshold loosest, model most biased upward, and fees highest -- all on cheap
contracts. Three errors compounding into the same corner of the price curve.
`screen()` therefore requires an ABSOLUTE probability floor alongside the
percentage, and `recommended_price_band()` states where the model is trustworthy.

SHIN's method models a proportion z of informed traders and solves for the z
that makes the fair probabilities sum to 1. It produces a wider spread than
multiplicative -- favourites up, longshots down -- which is the correction the
bias calls for. z is solved numerically by bisection rather than a closed form:
the closed form only exists for the two-outcome case, and a root-find that is
obviously correct beats an algebraic identity that is hard to check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .fees import Role, fee_for

# Outside this band the de-vig model error and the fee-as-fraction-of-stake
# curve both grow faster than any plausible edge. Not a hard gate -- a stated
# assumption the study reports against, so results can be split by band.
PRICE_BAND = (0.15, 0.85)

# Defaults from the originating spec, corrected: the percentage alone is not a
# sufficient screen (see module docstring), so an absolute floor rides with it.
MIN_NET_EDGE = 0.03
MIN_ABSOLUTE_EDGE = 0.015


class DevigError(ValueError):
    """Raised when odds cannot be de-vigged. Never returns a plausible guess."""


def american_to_implied(odds: float) -> float:
    """American moneyline -> raw implied probability (vig included)."""
    if odds == 0:
        raise DevigError("American odds of 0 are not a price")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def decimal_to_implied(odds: float) -> float:
    if odds <= 1.0:
        raise DevigError(f"decimal odds must exceed 1.0, got {odds}")
    return 1.0 / odds


@dataclass(frozen=True)
class DevigResult:
    """Fair probabilities under both methods, plus the book's overround.

    Both are carried so a study can report the spread between them. When they
    disagree by more than the edge being claimed, the edge is a model artifact.
    """

    multiplicative: tuple[float, ...]
    shin: tuple[float, ...]
    overround: float
    shin_z: float

    def disagreement(self) -> float:
        """Largest absolute gap between the two methods, in probability."""
        return max(abs(m - s) for m, s in zip(self.multiplicative, self.shin))


def _check_raw(raw: Sequence[float]) -> None:
    if len(raw) < 2:
        raise DevigError(f"need at least 2 outcomes to de-vig, got {len(raw)}")
    if any(p <= 0.0 for p in raw):
        raise DevigError(f"raw probabilities must be positive, got {tuple(raw)}")
    if sum(raw) <= 1.0:
        # A booksum at or below 1.0 is an arbitrage or a stale/crossed quote,
        # not a normal book. Refuse it rather than "de-vig" a negative margin.
        raise DevigError(
            f"booksum {sum(raw):.4f} <= 1.0 -- crossed or stale quotes, not a "
            "vigged book; refusing to de-vig"
        )


def devig_multiplicative(raw: Sequence[float]) -> tuple[float, ...]:
    _check_raw(raw)
    total = sum(raw)
    return tuple(p / total for p in raw)


def _shin_probs(raw: Sequence[float], z: float, booksum: float) -> tuple[float, ...]:
    if z >= 1.0:
        return tuple(p * p / booksum for p in raw)  # limit as z -> 1
    return tuple(
        ((z * z + 4.0 * (1.0 - z) * p * p / booksum) ** 0.5 - z) / (2.0 * (1.0 - z))
        for p in raw
    )


def devig_shin(raw: Sequence[float], tolerance: float = 1e-12) -> tuple[tuple[float, ...], float]:
    """Shin de-vig. Returns (fair probabilities, solved z).

    g(z) = sum(p_i(z)) - 1 is continuous and decreasing on [0, 1): at z=0 it
    equals sqrt(booksum) - 1 > 0, and at the z->1 limit it is
    sum(raw^2)/booksum - 1 < 0 for any real book. So a root always exists and
    bisection always brackets it.
    """
    _check_raw(raw)
    booksum = sum(raw)

    def g(z: float) -> float:
        return sum(_shin_probs(raw, z, booksum)) - 1.0

    lo, hi = 0.0, 1.0
    if g(lo) <= 0.0:
        # Booksum ~1.0; multiplicative and Shin coincide and z is 0.
        return devig_multiplicative(raw), 0.0

    for _ in range(200):
        mid = (lo + hi) / 2.0
        if g(mid) > 0.0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tolerance:
            break

    z = (lo + hi) / 2.0
    probs = _shin_probs(raw, z, booksum)
    total = sum(probs)
    # Renormalise off the last ulp of bisection error so callers can rely on
    # these summing to exactly 1.
    return tuple(p / total for p in probs), z


def devig(raw: Sequence[float]) -> DevigResult:
    """Run both methods over one book."""
    mult = devig_multiplicative(raw)
    shin, z = devig_shin(raw)
    return DevigResult(
        multiplicative=mult, shin=shin, overround=sum(raw) - 1.0, shin_z=z
    )


def devig_american(odds: Sequence[float]) -> DevigResult:
    return devig([american_to_implied(o) for o in odds])


# --- Expected value ---------------------------------------------------------


@dataclass(frozen=True)
class EdgeQuote:
    """What one hypothetical order is actually worth, fees included."""

    p_fair: float
    price: float
    n_contracts: float
    venue: str
    role: Role
    fee_dollars: float
    fee_per_contract: float
    ev_dollars: float
    ev_per_contract: float
    net_edge: float          # EV / stake -- the spec's threshold unit
    absolute_edge: float     # p_fair - price, in probability points
    in_price_band: bool

    def passes(
        self,
        min_net_edge: float = MIN_NET_EDGE,
        min_absolute_edge: float = MIN_ABSOLUTE_EDGE,
        require_band: bool = True,
    ) -> bool:
        if require_band and not self.in_price_band:
            return False
        return self.net_edge >= min_net_edge and self.absolute_edge >= min_absolute_edge


def net_ev(
    p_fair: float,
    price: float,
    n_contracts: float,
    venue: str = "kalshi",
    role: Role = "taker",
) -> EdgeQuote:
    """EV of buying `n_contracts` at `price`, net of that venue's real fee.

    Fees are charged at EXECUTION on both venues, so they belong in the cost
    basis, not as a haircut on the payout:

        cost   = n * price + fee(n, price)
        payout = n * 1.00   on a win, 0 otherwise
        EV     = p * n - cost
    """
    if not 0.0 <= p_fair <= 1.0:
        raise ValueError(f"p_fair must be a probability, got {p_fair}")
    fee = fee_for(venue, n_contracts, price, role)
    cost = n_contracts * price + fee.dollars
    ev = p_fair * n_contracts - cost
    stake = n_contracts * price
    return EdgeQuote(
        p_fair=p_fair,
        price=price,
        n_contracts=n_contracts,
        venue=venue,
        role=role,
        fee_dollars=fee.dollars,
        fee_per_contract=fee.per_contract,
        ev_dollars=ev,
        ev_per_contract=ev / n_contracts,
        net_edge=ev / stake if stake else 0.0,
        absolute_edge=p_fair - price,
        in_price_band=PRICE_BAND[0] <= price <= PRICE_BAND[1],
    )


def recommended_price_band() -> tuple[float, float]:
    return PRICE_BAND


# --- Sizing -----------------------------------------------------------------


@dataclass(frozen=True)
class KellyStake:
    fraction_full: float
    fraction_scaled: float
    dollars: float
    contracts: int
    capped_by: str | None       # None | "per_bet_cap" | "kelly_zero" | "bankroll"


def kelly_stake(
    p_fair: float,
    price: float,
    bankroll: float,
    venue: str = "kalshi",
    role: Role = "taker",
    kelly_multiplier: float = 0.25,
    max_per_bet: float = 50.0,
) -> KellyStake:
    """Fractional Kelly with the execution fee inside the payoff odds.

    Your outlay per contract is price + fee, not price, and that outlay is what
    is lost on a loss -- so the fee belongs in `b`, not bolted on afterwards.

    The fee is linear in size before the per-order ceiling, so the per-contract
    rate used here is size-independent and the sizing does not need to iterate.
    The ceiling is applied afterwards, by `net_ev`, against the integer contract
    count actually ordered.
    """
    if bankroll <= 0:
        raise ValueError(f"bankroll must be positive, got {bankroll}")

    # Smooth (pre-ceiling) per-contract rate, taken at unit size.
    fee_pc = fee_for(venue, 1.0, price, role).dollars
    outlay = price + fee_pc
    if outlay >= 1.0:
        # Fees alone exceed the payout; no size is correct.
        return KellyStake(0.0, 0.0, 0.0, 0, "kelly_zero")

    b = (1.0 - outlay) / outlay
    f_full = (p_fair * b - (1.0 - p_fair)) / b
    f_scaled = max(0.0, kelly_multiplier * f_full)

    if f_scaled <= 0.0:
        return KellyStake(f_full, 0.0, 0.0, 0, "kelly_zero")

    dollars = bankroll * f_scaled
    capped: str | None = None
    if dollars > max_per_bet:
        dollars, capped = max_per_bet, "per_bet_cap"
    if dollars > bankroll:
        dollars, capped = bankroll, "bankroll"

    contracts = int(dollars // outlay)
    return KellyStake(f_full, f_scaled, dollars, contracts, capped)
