"""Does the de-vigged sharp line predict settlement better than the exchange?

...and, separately, would acting on the difference have made money net of the
fee actually charged. Those are two questions and an earlier version of this
module conflated them.

WHAT CHANGED, AND WHY, because both defects produced confident output:

1. THE BOOTSTRAP RESAMPLED ROWS, NOT GAMES. Kalshi lists a separate contract
   for each team in a game, so one game yields two rows whose outcomes are
   perfectly dependent. Resampling rows i.i.d. treats them as independent
   evidence: it doubles the apparent sample and narrows the interval. Driven to
   its limit, 200 copies of a single observation reported n=200, not
   underpowered, and a ZERO-WIDTH interval reading "SHARP LINE WINS". The
   bootstrap now resamples whole GAMES, and `n` counts distinct games.

2. THE DISAGREEMENT HIT-RATE MEASURED THE BASE RATE OF THE FAVOURED SIDE.
   Counting YES as a sharp "win" whenever p_sharp > p_exchange returns how
   often the side sharp leaned toward happened to occur -- which is the base
   rate, not skill, and not profit. Two reproductions, both confirmed here:

     exchange .80 (calibrated), sharp .85 (wrong), true p .80
        -> "sharp right" 80.0%, while buying at .80 has ZERO gross EV
     exchange .20 (wrong),      sharp .25 (right), true p .25
        -> "sharp right" 26.5%, while buying at .20 has +.05 gross EV

   The statistic rewarded being wrong on favourites and punished being right on
   longshots, and the README made ">50% and rising" a go criterion. It is gone.
   In its place: a conditional proper-score difference on the eligible subset
   (which forecast is better WHERE THEY DISAGREE), and a realized return
   measured against the executable quote net of fees (whether acting on the
   difference paid). Neither can be satisfied by a base rate.

WHAT THIS STILL DOES NOT ESTABLISH. Realized return here is computed against a
historical quote with no depth attached, for one contract held to settlement.
It is indicative of an opportunity, not a demonstration of achievable fills,
and it says nothing about a strategy that exits before start. Fixed data and
operating costs are not in it. A positive number here is a reason to build a
forward recorder, not a reason to trade.
"""

from __future__ import annotations

import math
import random
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from core.fees import (
    DEFAULT_ROUTE, ROUTE_ALIGNMENT, ROUTE_LABELS, fee_for,
)
from data.kalshi_history import Coverage

# Distinct GAMES, not rows. Deliberately not presented as a power calculation:
# it is a floor below which nothing is reported, and the interval is what
# actually says whether the evidence is sufficient.
MIN_GAMES = 200
# The gate that matters is on games the policy would actually have TRADED.
# An earlier version gated on all forecast games, so a large unrelated universe
# could satisfy the floor for a tiny trading subset.
MIN_TRADED_GAMES = 200
EPSILON = 1e-6
DEFAULT_BOOTSTRAP = 2000
# 72 hours plus an hour of slack, matching `collect.DEFAULT_LEAD_GRID_MINUTES`.
# Deliberately a named constant rather than `24 * 60` inline: the old ceiling
# was a literal, and a literal is what made widening the grid a silent filter.
DEFAULT_MAX_MINUTES_TO_START = 72 * 60.0 + 60.0


@dataclass(frozen=True)
class Observation:
    """One exchange contract, compared against the sharp line at one cutoff.

    `game_id` is the CLUSTER key -- both team contracts of one game share it,
    and every statistic here resamples on it. `market_id` identifies the row.

    `p_sharp` must already be oriented to THIS contract's YES participant; the
    collector owns that and an unoriented probability is a silently inverted
    signal, not a smaller effect.
    """

    game_id: str
    market_id: str
    decision_at: datetime          # the one information cutoff both respect
    minutes_to_start: float
    p_sharp: float
    p_exchange: float              # mid at the cutoff -- the FORECAST price
    outcome: int                   # 1 if THIS contract's YES settled true
    yes_participant: str = ""
    # The book AS SEEN AT THE DECISION. The trigger, the side and the
    # screen all read these, and only these -- see `entry_bid`/`entry_ask`.
    exchange_bid: float | None = None
    exchange_ask: float | None = None
    sharp_at: datetime | None = None          # when the book moved the price
    sharp_snapshot_at: datetime | None = None  # when the archive captured it
    exchange_at: datetime | None = None        # the OBSERVATION candle
    devig_method: str = "shin"
    # Which checkpoint of the lead grid this row is. Rows at different
    # checkpoints on the same game are repeated looks at one outcome, not
    # independent bets -- `select_entries` is what turns them into a policy.
    checkpoint_minutes: float | None = None
    entry_at: datetime | None = None           # when the entry was executable
    entry_delay_minutes: float = 0.0
    # The book AT EXECUTION, t + delay. Separate from the decision book on
    # purpose: a committed signal freezes its trigger and its side at t and
    # only then pays whatever the market has become. Letting the execution
    # price back into the screen would qualify -- or reverse -- a trade using
    # a price the trader could not have seen, which is lookahead wearing the
    # costume of a cost model. None means "same as the decision book", which
    # is exactly true at zero delay.
    entry_bid: float | None = None
    entry_ask: float | None = None
    # Pre-game exit, present only when an exit quote was actually found.
    exit_at: datetime | None = None
    exit_bid: float | None = None
    exit_ask: float | None = None

    @property
    def disagreement(self) -> float:
        return self.p_sharp - self.p_exchange

    def execution_book(self) -> tuple[float | None, float | None]:
        """(bid, ask) at execution, falling back to the decision book."""
        bid = self.entry_bid if self.entry_bid is not None else self.exchange_bid
        ask = self.entry_ask if self.entry_ask is not None else self.exchange_ask
        return bid, ask

    @property
    def checkpoint_key(self) -> str:
        if self.checkpoint_minutes is None:
            return "unlabelled"
        return f"{self.checkpoint_minutes:g}"

    @property
    def checkpoint_label(self) -> str:
        m = self.checkpoint_minutes
        if m is None:
            return "unlabelled"
        return f"{int(m // 60)}h" if m >= 60 and m % 60 == 0 else f"{m:g}m"


def _clamp(p: float) -> float:
    return min(1.0 - EPSILON, max(EPSILON, p))


def brier(predictions: list[float], outcomes: list[int]) -> float:
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def log_loss(predictions: list[float], outcomes: list[int]) -> float:
    total = 0.0
    for p, o in zip(predictions, outcomes):
        p = _clamp(p)
        total -= math.log(p) if o else math.log(1.0 - p)
    return total / len(predictions)


# --- clustering -------------------------------------------------------------


def cluster(observations: list[Observation]) -> dict[str, list[Observation]]:
    grouped: dict[str, list[Observation]] = {}
    for o in observations:
        grouped.setdefault(o.game_id, []).append(o)
    return grouped


def cluster_bootstrap(
    observations: list[Observation],
    statistic,
    rounds: int = DEFAULT_BOOTSTRAP,
    seed: int = 20260921,
) -> tuple[float, float]:
    """95% interval for `statistic`, resampling whole GAMES.

    `statistic` takes a list of observations and returns a float. Resampling
    games rather than rows is what stops two contracts on one game -- whose
    outcomes are complements, not independent draws -- from counting twice.
    """
    groups = list(cluster(observations).values())
    if len(groups) < 2:
        return float("nan"), float("nan")

    rng = random.Random(seed)
    values: list[float] = []
    for _ in range(rounds):
        sample: list[Observation] = []
        for _ in range(len(groups)):
            sample.extend(groups[rng.randrange(len(groups))])
        try:
            values.append(statistic(sample))
        except (ZeroDivisionError, ValueError):
            continue
    if len(values) < 2:
        return float("nan"), float("nan")
    values.sort()
    return (
        values[int(0.025 * len(values))],
        values[min(len(values) - 1, int(0.975 * len(values)))],
    )


# --- accuracy ---------------------------------------------------------------


@dataclass
class ScoreComparison:
    games: int
    rows: int
    sharp_brier: float
    exchange_brier: float
    sharp_log_loss: float
    exchange_log_loss: float
    brier_delta: float                     # negative => sharp is better
    delta_ci_low: float
    delta_ci_high: float
    sharp_wins: bool
    underpowered: bool

    def verdict(self) -> str:
        if self.underpowered:
            return (
                f"UNDERPOWERED: {self.games} distinct games < {MIN_GAMES} floor "
                f"({self.rows} rows). No conclusion either way."
            )
        if self.brier_delta != self.brier_delta:
            return "NOT MEASURABLE: too few clusters to bootstrap."
        if self.sharp_wins:
            return (
                f"SHARP LINE IS THE BETTER FORECAST: Brier delta "
                f"{self.brier_delta:+.5f} (95% CI {self.delta_ci_low:+.5f} to "
                f"{self.delta_ci_high:+.5f}). NOTE: better forecasting is not "
                "by itself a tradable edge -- see the return section."
            )
        if self.delta_ci_low > 0:
            return (
                f"EXCHANGE IS THE BETTER FORECAST: Brier delta "
                f"{self.brier_delta:+.5f} (95% CI {self.delta_ci_low:+.5f} to "
                f"{self.delta_ci_high:+.5f}). The thesis is inverted."
            )
        return (
            f"INSUFFICIENT EVIDENCE: Brier delta {self.brier_delta:+.5f} "
            f"(95% CI {self.delta_ci_low:+.5f} to {self.delta_ci_high:+.5f}) "
            "straddles zero. This is not proof of no edge."
        )


def _brier_delta(sample: list[Observation]) -> float:
    return (
        brier([o.p_sharp for o in sample], [o.outcome for o in sample])
        - brier([o.p_exchange for o in sample], [o.outcome for o in sample])
    )


def compare(
    observations: list[Observation], bootstrap_rounds: int = DEFAULT_BOOTSTRAP
) -> ScoreComparison:
    if not observations:
        raise ValueError("no observations to score")

    outcomes = [o.outcome for o in observations]
    games = len(cluster(observations))
    delta = _brier_delta(observations)
    lo, hi = cluster_bootstrap(observations, _brier_delta, bootstrap_rounds)

    return ScoreComparison(
        games=games,
        rows=len(observations),
        sharp_brier=brier([o.p_sharp for o in observations], outcomes),
        exchange_brier=brier([o.p_exchange for o in observations], outcomes),
        sharp_log_loss=log_loss([o.p_sharp for o in observations], outcomes),
        exchange_log_loss=log_loss([o.p_exchange for o in observations], outcomes),
        brier_delta=delta,
        delta_ci_low=lo,
        delta_ci_high=hi,
        sharp_wins=hi == hi and hi < 0.0,
        underpowered=games < MIN_GAMES,
    )


# --- eligibility and realized return ----------------------------------------


@dataclass(frozen=True)
class Eligibility:
    """A PREDECLARED policy. Freeze it before looking at returns.

    THE SCREEN IS PREDICTED NET EV AT THE EXECUTABLE PRICE, not disagreement
    with the midpoint. An earlier version screened on |p_sharp - mid| >= 0.02,
    but the trade happens at the ask (or 1 - bid), so on a wide book a passing
    signal could be a large predicted LOSS: bid .40 / ask .60 / sharp .53
    cleared the screen and bought YES at .60 plus 2c of fee -- a predicted
    **-0.09 per contract** before the outcome was known. That tests buying
    midpoint disagreements, not a positive-EV policy, and it can bury a viable
    narrower policy under trades it already knew were negative.

    Midpoint disagreement survives as a DIAGNOSTIC (`conditional_scores`), not
    as the selection rule.
    """

    min_net_ev: float = 0.01          # predicted, per contract, after fee
    price_band: tuple[float, float] = (0.15, 0.85)
    # The ceiling must COVER THE LEAD GRID. At 24h it silently discarded every
    # 72h, 48h and 24h-and-a-bit observation -- the rows a multi-day study
    # exists to look at -- as "too far from start", which reads in the
    # diagnostics as absent opportunity rather than as a misconfiguration.
    # `run_study` derives it from the grid actually in use and refuses a
    # ceiling that does not reach the earliest checkpoint.
    max_minutes_to_start: float = DEFAULT_MAX_MINUTES_TO_START
    min_minutes_to_start: float = 5.0
    max_spread: float = 0.10          # a book this wide is not executable

    def admits(self, o: "Observation", venue: str = "kalshi",
               role: str = "taker", series: str | None = None,
               route: str = DEFAULT_ROUTE) -> bool:
        if not self.price_band[0] <= o.p_exchange <= self.price_band[1]:
            return False
        if not self.min_minutes_to_start <= o.minutes_to_start <= self.max_minutes_to_start:
            return False
        if o.exchange_bid is None or o.exchange_ask is None:
            return False
        if o.exchange_ask - o.exchange_bid > self.max_spread:
            return False
        return as_trade(o, venue, role, self, series, route) is not None


@dataclass(frozen=True)
class SideQuote:
    """One executable side, priced with its own fee and outcome mapping."""

    side: str
    entry_price: float         # the DECISION price, seen at t
    fee: float                 # the fee on the decision price
    win_probability: float     # p_sharp for YES, 1 - p_sharp for NO
    payout: float              # realised: 1.0 or 0.0
    fee_raw: float = 0.0       # the charge BEFORE the account's alignment
    fee_route: str = ""
    exec_price: float | None = None   # the price actually paid, at t + delay
    exec_fee: float | None = None

    @property
    def cost(self) -> float:
        """What the screen reasons about: the price and fee visible at t."""
        return self.entry_price + self.fee

    @property
    def paid(self) -> float:
        """What the trade actually cost. Equals `cost` at zero delay."""
        price = self.exec_price if self.exec_price is not None else self.entry_price
        fee = self.exec_fee if self.exec_fee is not None else self.fee
        return price + fee

    @property
    def predicted_ev(self) -> float:
        """EV before the outcome is known -- what a screen must select on.

        Priced at the DECISION book. An earlier version priced it at the
        execution book, so a delayed run selected its side and cleared its
        threshold using a price from after the decision: the screen could
        reject a trade the signal had triggered, or flip YES to NO, on
        information that did not exist at t.
        """
        return self.win_probability - self.cost

    @property
    def profit(self) -> float:
        """Realised: settled payout less what was actually paid."""
        return self.payout - self.paid


def side_quotes(o: "Observation", venue: str = "kalshi",
                role: str = "taker", series: str | None = None,
                route: str = DEFAULT_ROUTE) -> list[SideQuote]:
    """Both executable sides of one contract, each priced net of its own fee.

    YES pays the ask and wins when the contract settles true. NO pays
    (1 - bid) -- the mirror of the YES book -- and wins when it settles false.
    Computed explicitly rather than by negating a YES figure, because getting
    the NO mapping wrong inverts half the sample.

    The fee is resolved AT `o.decision_at`, not at whatever schedule is current
    when the study runs. Kalshi's per-series multiplier changes on a dated
    `scheduled_ts`, so pricing a September decision at a later rate is
    retroactive repricing -- and it moves every EV here, not a rounding digit:
    KXMLBGAME's recorded change halves the taker coefficient.
    """
    if o.exchange_bid is None or o.exchange_ask is None:
        return []
    if not 0.0 < o.exchange_ask < 1.0 or not 0.0 < o.exchange_bid < 1.0:
        return []
    if o.exchange_bid > o.exchange_ask:
        return []

    exec_bid, exec_ask = o.execution_book()

    out: list[SideQuote] = []
    for side, entry, exec_entry, win_p, payout in (
        ("YES", o.exchange_ask, exec_ask, o.p_sharp, float(o.outcome)),
        ("NO", 1.0 - o.exchange_bid, None if exec_bid is None else 1.0 - exec_bid,
         1.0 - o.p_sharp, float(1 - o.outcome)),
    ):
        if not 0.0 < entry < 1.0:
            continue
        fee = fee_for(venue, 1.0, entry, role, series, o.decision_at, route)
        exec_price = exec_fee = None
        if exec_entry is not None and 0.0 < exec_entry < 1.0 and exec_entry != entry:
            exec_price = exec_entry
            exec_fee = fee_for(venue, 1.0, exec_entry, role, series,
                               o.decision_at, route).dollars
        out.append(SideQuote(side, entry, fee.dollars, win_p, payout,
                             fee.raw_dollars, fee.route, exec_price, exec_fee))
    return out


@dataclass(frozen=True)
class Trade:
    """What acting on one observation would have cost and returned."""

    game_id: str
    side: str
    entry_price: float         # the DECISION price
    fee: float
    payout: float
    profit: float
    predicted_ev: float
    paid: float | None = None  # what execution actually cost, at t + delay

    @property
    def stake(self) -> float:
        return self.paid if self.paid is not None else self.entry_price + self.fee

    @property
    def return_on_stake(self) -> float:
        return self.profit / self.stake if self.stake else 0.0


def as_trade(o: "Observation", venue: str = "kalshi", role: str = "taker",
             eligibility: "Eligibility | None" = None,
             series: str | None = None,
             route: str = DEFAULT_ROUTE) -> Trade | None:
    """The best executable side, if it clears the predeclared EV threshold.

    Both sides are priced and the better PREDICTED EV wins. The direction is
    not taken from the sign of a midpoint disagreement, because the midpoint is
    not a price anyone trades at.
    """
    quotes = side_quotes(o, venue, role, series, route)
    if not quotes:
        return None
    best = max(quotes, key=lambda q: q.predicted_ev)
    threshold = eligibility.min_net_ev if eligibility else 0.0
    if best.predicted_ev < threshold:
        return None
    return Trade(o.game_id, best.side, best.entry_price, best.fee,
                 best.payout, best.profit, best.predicted_ev, best.paid)


@dataclass
class ScreenDiagnostics:
    """Why the frozen policy rejected what it rejected.

    A run that finds no trades and a run whose collection broke both print
    "no eligible trades". These counts, and the EV distribution underneath
    them, are what tells the two apart -- and they are the honest alternative
    to loosening a frozen threshold until trades appear.
    """

    considered: int = 0
    rejected_price_band: int = 0
    rejected_lead_time: int = 0
    rejected_no_quotes: int = 0
    rejected_spread: int = 0
    rejected_below_ev: int = 0
    admitted: int = 0
    best_net_ev: list[float] = field(default_factory=list)
    best_gross_edge: list[float] = field(default_factory=list)

    def _pct(self, values, q):
        if not values:
            return float("nan")
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    def render(self) -> str:
        lines = ["SCREEN DIAGNOSTICS"]
        lines.append(f"    considered              {self.considered:>8,}")
        for label, n in (("rejected: price band", self.rejected_price_band),
                         ("rejected: lead time", self.rejected_lead_time),
                         ("rejected: no quotes", self.rejected_no_quotes),
                         ("rejected: spread too wide", self.rejected_spread),
                         ("rejected: below EV floor", self.rejected_below_ev)):
            lines.append(f"    {label:24}{n:>8,}")
        lines.append(f"    ADMITTED                {self.admitted:>8,}")
        if self.best_net_ev:
            lines.append("  best predicted NET EV per contract (after fee):")
            lines.append(f"    min {min(self.best_net_ev):+.6f}   "
                         f"median {self._pct(self.best_net_ev, 0.5):+.6f}   "
                         f"max {max(self.best_net_ev):+.6f}")
        if self.best_gross_edge:
            lines.append("  best GROSS edge per contract (before fee):")
            lines.append(f"    min {min(self.best_gross_edge):+.6f}   "
                         f"median {self._pct(self.best_gross_edge, 0.5):+.6f}   "
                         f"max {max(self.best_gross_edge):+.6f}")
        if self.considered and not self.admitted:
            lines.append("  -> NO TRADES. The distribution above says whether that is")
            lines.append("     absent edge or a collection failure. It is not a reason")
            lines.append("     to loosen the frozen threshold.")
        return "\n".join(lines)


def screen_diagnostics(observations: list["Observation"],
                       eligibility: "Eligibility | None" = None,
                       venue: str = "kalshi", role: str = "taker",
                       series: str | None = None,
                       route: str = DEFAULT_ROUTE) -> ScreenDiagnostics:
    """Count why each observation passed or failed the frozen policy."""
    eligibility = eligibility or Eligibility()
    d = ScreenDiagnostics(considered=len(observations))
    for o in observations:
        if not eligibility.price_band[0] <= o.p_exchange <= eligibility.price_band[1]:
            d.rejected_price_band += 1
            continue
        if not (eligibility.min_minutes_to_start <= o.minutes_to_start
                <= eligibility.max_minutes_to_start):
            d.rejected_lead_time += 1
            continue
        if o.exchange_bid is None or o.exchange_ask is None:
            d.rejected_no_quotes += 1
            continue
        if o.exchange_ask - o.exchange_bid > eligibility.max_spread:
            d.rejected_spread += 1
            continue
        quotes = side_quotes(o, venue, role, series, route)
        if not quotes:
            d.rejected_no_quotes += 1
            continue
        best = max(quotes, key=lambda q: q.predicted_ev)
        d.best_net_ev.append(best.predicted_ev)
        d.best_gross_edge.append(best.win_probability - best.entry_price)
        if best.predicted_ev < eligibility.min_net_ev:
            d.rejected_below_ev += 1
        else:
            d.admitted += 1
    return d


@dataclass
class ReturnReport:
    games: int
    trades: int
    mean_profit_per_contract: float
    mean_return_on_stake: float
    ci_low: float
    ci_high: float
    yes_trades: int
    no_trades: int
    total_staked: float
    eligibility: Eligibility
    venue: str
    role: str

    def profitable(self) -> bool:
        return self.ci_low == self.ci_low and self.ci_low > 0.0

    def verdict(self) -> str:
        if self.trades == 0:
            return "NO ELIGIBLE TRADES under the declared filter."
        if self.ci_low != self.ci_low:
            return f"{self.trades} trades across {self.games} games -- too few clusters to bootstrap."
        body = (
            f"{self.trades} trades ({self.yes_trades} YES / {self.no_trades} NO) "
            f"across {self.games} games; mean "
            f"{self.mean_profit_per_contract:+.4f}/contract, "
            f"{self.mean_return_on_stake:+.2%} on stake "
            f"(95% CI {self.ci_low:+.2%} to {self.ci_high:+.2%})"
        )
        if self.profitable():
            return f"POSITIVE NET RETURN: {body}. Indicative of an opportunity, NOT proof of fills."
        if self.ci_high == self.ci_high and self.ci_high < 0:
            return f"NEGATIVE NET RETURN: {body}."
        return f"INSUFFICIENT EVIDENCE ON RETURN: {body}; interval straddles zero."


def realized_return(
    observations: list[Observation],
    eligibility: Eligibility | None = None,
    venue: str = "kalshi",
    role: str = "taker",
    bootstrap_rounds: int = DEFAULT_BOOTSTRAP,
    series: str | None = None,
    route: str = DEFAULT_ROUTE,
    policy: "EntryPolicy | None" = None,
) -> ReturnReport:
    """Net-of-fee return from acting on the signal.

    With an `EntryPolicy`, the subset is what the policy would actually have
    BOUGHT -- one entry per game at its earliest qualifying checkpoint -- not
    every row that cleared the screen. On a multi-checkpoint grid those differ
    by a lot: a game quoted at seven checkpoints can clear the screen at five
    of them, and counting five is counting one outcome five times.

    Without a policy the behaviour is unchanged, which is what the
    single-checkpoint baseline needs.
    """
    eligibility = eligibility or Eligibility()
    if policy is not None:
        selected, _ = select_entries(observations, eligibility, venue, role,
                                     series, route, policy)
        eligible = [e.observation for e in selected]
    else:
        eligible = [o for o in observations
                    if eligibility.admits(o, venue, role, series, route)]

    def mean_return(sample: list[Observation]) -> float:
        trades = [t for t in (as_trade(o, venue, role, eligibility, series, route)
                              for o in sample) if t]
        if not trades:
            raise ValueError("no trades in sample")
        staked = sum(t.stake for t in trades)
        return sum(t.profit for t in trades) / staked if staked else 0.0

    trades = [t for t in (as_trade(o, venue, role, eligibility, series, route)
                          for o in eligible) if t]
    if not trades:
        return ReturnReport(0, 0, 0.0, 0.0, float("nan"), float("nan"),
                            0, 0, 0.0, eligibility, venue, role)

    staked = sum(t.stake for t in trades)
    lo, hi = cluster_bootstrap(eligible, mean_return, bootstrap_rounds)
    return ReturnReport(
        games=len({t.game_id for t in trades}),
        trades=len(trades),
        mean_profit_per_contract=sum(t.profit for t in trades) / len(trades),
        mean_return_on_stake=sum(t.profit for t in trades) / staked if staked else 0.0,
        ci_low=lo,
        ci_high=hi,
        yes_trades=sum(1 for t in trades if t.side == "YES"),
        no_trades=sum(1 for t in trades if t.side == "NO"),
        total_staked=staked,
        eligibility=eligibility,
        venue=venue,
        role=role,
    )


# --- conditional accuracy (replaces the hit-rate table) ---------------------


@dataclass
class ConditionalScore:
    threshold: float
    games: int
    rows: int
    brier_delta: float
    ci_low: float
    ci_high: float

    def reading(self) -> str:
        if self.rows == 0:
            return "no games"
        if self.brier_delta != self.brier_delta or self.ci_low != self.ci_low:
            return "too few clusters"
        if self.ci_high < 0:
            return "sharp better"
        if self.ci_low > 0:
            return "exchange better"
        return "inconclusive"


def conditional_scores(
    observations: list[Observation],
    thresholds: tuple[float, ...] = (0.01, 0.02, 0.03, 0.05, 0.08),
    bootstrap_rounds: int = 500,
) -> list[ConditionalScore]:
    """Which forecast is better WHERE THEY DISAGREE, as a proper score.

    This replaces the hit-rate table. A Brier difference cannot be satisfied by
    the base rate of the favoured side: it penalises confident wrongness on
    both sides symmetrically, which is exactly what the old statistic did not.
    """
    out: list[ConditionalScore] = []
    for threshold in thresholds:
        subset = [o for o in observations if abs(o.disagreement) >= threshold]
        if not subset:
            out.append(ConditionalScore(threshold, 0, 0, float("nan"),
                                        float("nan"), float("nan")))
            continue
        lo, hi = cluster_bootstrap(subset, _brier_delta, bootstrap_rounds)
        out.append(ConditionalScore(
            threshold, len(cluster(subset)), len(subset),
            _brier_delta(subset), lo, hi,
        ))
    return out


# --- calibration ------------------------------------------------------------


@dataclass
class CalibrationBin:
    low: float
    high: float
    n: int
    mean_prediction: float
    actual_rate: float

    @property
    def bias(self) -> float:
        return self.mean_prediction - self.actual_rate


def calibration(
    predictions: list[float], outcomes: list[int], edges: tuple[float, ...] = (
        0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
) -> list[CalibrationBin]:
    bins: list[CalibrationBin] = []
    for low, high in zip(edges, edges[1:]):
        members = [(p, o) for p, o in zip(predictions, outcomes) if low <= p < high]
        if not members:
            continue
        bins.append(CalibrationBin(
            low, high, len(members),
            sum(p for p, _ in members) / len(members),
            sum(o for _, o in members) / len(members),
        ))
    return bins


# --- decay ------------------------------------------------------------------


@dataclass
class DecaySlice:
    period: str
    games: int
    brier_delta: float
    ci_low: float
    ci_high: float
    mean_return: float


def decay_series(
    observations: list[Observation],
    eligibility: Eligibility | None = None,
    bootstrap_rounds: int = 400,
    series: str | None = None,
    route: str = DEFAULT_ROUTE,
    policy: "EntryPolicy | None" = None,
) -> list[DecaySlice]:
    """The comparison by month, WITH uncertainty.

    A monthly point estimate drifting toward zero is not evidence the edge was
    arbitraged away: game mix and noise move it too. The interval is what
    distinguishes a trend from a smaller sample.

    `series`, `route` and `policy` are threaded for the same reason they are
    everywhere else: this is a RETURN breakdown, and without them the monthly
    figure was priced at the generic fee -- 0.07 where the dated September
    schedule says 0.035 -- and counted every qualifying checkpoint instead of
    the one bet the policy would have placed. A number in the same column as
    the headline return has to be computed the same way as the headline
    return.
    """
    by_month: dict[str, list[Observation]] = {}
    for o in observations:
        by_month.setdefault(o.decision_at.strftime("%Y-%m"), []).append(o)

    out: list[DecaySlice] = []
    for period in sorted(by_month):
        members = by_month[period]
        lo, hi = cluster_bootstrap(members, _brier_delta, bootstrap_rounds)
        ret = realized_return(members, eligibility,
                              bootstrap_rounds=bootstrap_rounds,
                              series=series, route=route, policy=policy)
        out.append(DecaySlice(
            period, len(cluster(members)), _brier_delta(members), lo, hi,
            ret.mean_return_on_stake if ret.trades else float("nan"),
        ))
    return out


# --- entry selection --------------------------------------------------------


@dataclass(frozen=True)
class EntryPolicy:
    """How repeated looks at one game become at most one bet.

    PREDECLARED, and applied chronologically. Seven checkpoints on one game are
    seven looks at ONE outcome, not seven independent bets: summing every
    qualifying row as if each were a separate profit inflates both the sample
    and the return, and picking whichever checkpoint did best is choosing with
    hindsight. Neither is a policy anyone could have followed.

    The baseline here is the one a trader could actually have executed: process
    checkpoints in time order and take the FIRST that qualifies, then stop
    looking at that game. Mutually exclusive contracts fall out of the same
    rule -- both team contracts of a game share `game_id`, so taking one closes
    the game to the other, which is what "do not buy both sides" means
    mechanically.
    """

    max_entries_per_game: int = 1
    allow_reentry: bool = False
    contracts_per_entry: int = 1

    def describe(self) -> str:
        return (f"at most {self.max_entries_per_game} entry/game at the EARLIEST "
                f"qualifying checkpoint, chronological, "
                f"{self.contracts_per_entry} contract(s) per entry, "
                f"re-entry {'allowed' if self.allow_reentry else 'disallowed'}")


@dataclass
class SelectionDiagnostics:
    """Why each look did or did not become a bet."""

    considered: int = 0
    taken: int = 0
    skipped_game_already_entered: int = 0
    skipped_not_qualifying: int = 0
    games_seen: int = 0
    games_entered: int = 0

    def render(self) -> str:
        return "\n".join([
            "ENTRY SELECTION",
            f"    looks considered        {self.considered:>8,}",
            f"    -> entries taken        {self.taken:>8,}",
            f"    skipped: game entered   {self.skipped_game_already_entered:>8,}",
            f"    skipped: not qualifying {self.skipped_not_qualifying:>8,}",
            f"    games seen / entered    {self.games_seen:>8,} / {self.games_entered:,}",
        ])


@dataclass(frozen=True)
class SelectedEntry:
    observation: Observation
    trade: Trade

    @property
    def checkpoint_label(self) -> str:
        return self.observation.checkpoint_label


def select_entries(
    observations: list[Observation],
    eligibility: Eligibility | None = None,
    venue: str = "kalshi",
    role: str = "taker",
    series: str | None = None,
    route: str = DEFAULT_ROUTE,
    policy: EntryPolicy | None = None,
) -> tuple[list[SelectedEntry], SelectionDiagnostics]:
    """Apply the entry policy chronologically. Returns the bets, and why.

    Chronological is load-bearing: the decision at the 72h checkpoint is made
    without knowing what the 24h one will look like. Sorting by anything else
    -- or scanning for the best row per game -- uses information the trader did
    not have.
    """
    eligibility = eligibility or Eligibility()
    policy = policy or EntryPolicy()
    diag = SelectionDiagnostics()
    ordered = sorted(observations, key=lambda o: (o.decision_at, o.market_id))
    diag.games_seen = len({o.game_id for o in observations})

    entries_by_game: dict[str, int] = {}
    out: list[SelectedEntry] = []
    for o in ordered:
        diag.considered += 1
        if entries_by_game.get(o.game_id, 0) >= policy.max_entries_per_game:
            diag.skipped_game_already_entered += 1
            continue
        # THE FULL ELIGIBILITY RULE, not just the EV threshold. `as_trade`
        # checks side prices and the net-EV floor and nothing else -- it does
        # not know about the price band, the lead bounds or the spread cap. A
        # selection gated on it alone bought a 40-cent spread that
        # `Eligibility.admits` rejects, and because the policy path IS the
        # headline, the screened figure was looser than the unscreened one.
        if not eligibility.admits(o, venue, role, series, route):
            diag.skipped_not_qualifying += 1
            continue
        trade = as_trade(o, venue, role, eligibility, series, route)
        if trade is None:
            # `admits` already implies this; not assuming it stays that way.
            diag.skipped_not_qualifying += 1
            continue
        entries_by_game[o.game_id] = entries_by_game.get(o.game_id, 0) + 1
        out.append(SelectedEntry(o, trade))
        diag.taken += 1
    diag.games_entered = len(entries_by_game)
    return out, diag


# --- per-checkpoint diagnostics ---------------------------------------------


@dataclass
class CheckpointSlice:
    """One lead time's opportunity frequency and edge -- NOT a profit line.

    These are shown SEPARATELY and never summed. Every game appears at every
    checkpoint it was quoted at, so adding the rows counts one outcome many
    times; and reading down the column for the best one is retrospective
    selection. The policy figure is `select_entries`, and only that.
    """

    key: str
    label: str
    minutes: float
    baseline: bool
    observations: int = 0
    qualifying: int = 0
    games: int = 0
    gross_edges: list[float] = field(default_factory=list)
    net_evs: list[float] = field(default_factory=list)

    @property
    def opportunity_rate(self) -> float:
        return self.qualifying / self.observations if self.observations else 0.0

    def _mean(self, values: list[float]) -> float:
        return sum(values) / len(values) if values else float("nan")

    @property
    def mean_gross_edge(self) -> float:
        return self._mean(self.gross_edges)

    @property
    def mean_net_ev(self) -> float:
        return self._mean(self.net_evs)

    @property
    def best_net_ev(self) -> float:
        return max(self.net_evs) if self.net_evs else float("nan")


def checkpoint_slices(
    observations: list[Observation],
    eligibility: Eligibility | None = None,
    venue: str = "kalshi",
    role: str = "taker",
    series: str | None = None,
    route: str = DEFAULT_ROUTE,
    baseline_minutes: float | None = None,
) -> list[CheckpointSlice]:
    """Opportunity frequency and edge by lead time, earliest lead first."""
    eligibility = eligibility or Eligibility()
    slices: dict[str, CheckpointSlice] = {}
    games: dict[str, set[str]] = {}
    for o in observations:
        key = o.checkpoint_key
        sl = slices.get(key)
        if sl is None:
            minutes = o.checkpoint_minutes if o.checkpoint_minutes is not None else -1.0
            sl = slices[key] = CheckpointSlice(
                key=key, label=o.checkpoint_label, minutes=minutes,
                baseline=(baseline_minutes is not None
                          and o.checkpoint_minutes == baseline_minutes),
            )
            games[key] = set()
        sl.observations += 1
        games[key].add(o.game_id)
        quotes = side_quotes(o, venue, role, series, route)
        if quotes:
            best = max(quotes, key=lambda q: q.predicted_ev)
            sl.gross_edges.append(best.win_probability - best.entry_price)
            sl.net_evs.append(best.predicted_ev)
            if eligibility.admits(o, venue, role, series, route):
                sl.qualifying += 1
    for key, sl in slices.items():
        sl.games = len(games[key])
    return sorted(slices.values(), key=lambda s: -s.minutes)


def render_checkpoint_slices(slices: list[CheckpointSlice]) -> str:
    lines = [
        "BY LEAD TIME  (shown separately -- NEVER summed)",
        "    Each game appears at every checkpoint it was quoted at, so adding",
        "    these rows counts one outcome repeatedly, and reading off the best",
        "    row is retrospective selection. The policy figure is the selected",
        "    entries, below.",
        f"    {'lead':<14}{'obs':>8}{'games':>8}{'qualify':>9}{'rate':>8}"
        f"{'mean gross':>13}{'mean net EV':>13}{'best net EV':>13}",
    ]
    for s in slices:
        lines.append(
            f"    {s.label + (' *' if s.baseline else ''):<14}{s.observations:>8,}"
            f"{s.games:>8,}{s.qualifying:>9,}{s.opportunity_rate:>8.1%}"
            f"{s.mean_gross_edge:>+13.6f}{s.mean_net_ev:>+13.6f}"
            f"{s.best_net_ev:>+13.6f}")
    if any(s.baseline for s in slices):
        lines.append("    * baseline checkpoint -- data already inspected, kept "
                     "separate from the fresh grid")
    return "\n".join(lines)


# --- price paths ------------------------------------------------------------


@dataclass
class PricePath:
    """One game's sharp and exchange prices in time order.

    Divergence over days is the thing the multi-day design is looking for; a
    single late snapshot cannot show it either way.
    """

    game_id: str
    market_id: str
    points: list[tuple[float, datetime, float, float]] = field(default_factory=list)

    @property
    def sharp_move(self) -> float:
        return self.points[-1][2] - self.points[0][2] if len(self.points) > 1 else 0.0

    @property
    def exchange_move(self) -> float:
        return self.points[-1][3] - self.points[0][3] if len(self.points) > 1 else 0.0

    @property
    def divergence(self) -> float:
        """How differently the two moved over the observed span."""
        return self.sharp_move - self.exchange_move


def price_paths(observations: list[Observation]) -> list[PricePath]:
    """Chronological sharp/exchange prices per contract across checkpoints."""
    by_market: dict[str, PricePath] = {}
    for o in sorted(observations, key=lambda o: o.decision_at):
        path = by_market.get(o.market_id)
        if path is None:
            path = by_market[o.market_id] = PricePath(o.game_id, o.market_id)
        path.points.append((o.checkpoint_minutes or float("nan"), o.decision_at,
                            o.p_sharp, o.p_exchange))
    return [p for p in by_market.values() if len(p.points) > 1]


def render_price_paths(paths: list[PricePath], limit: int = 10) -> str:
    if not paths:
        return ("PRICE MOVEMENT ACROSS CHECKPOINTS\n"
                "    no contract was observed at more than one checkpoint, so "
                "no path exists")
    ranked = sorted(paths, key=lambda p: -abs(p.divergence))
    lines = [
        "PRICE MOVEMENT ACROSS CHECKPOINTS",
        f"    {len(paths):,} contracts observed at 2+ checkpoints; "
        f"largest divergences first",
        f"    {'market':<34}{'sharp move':>13}{'exch move':>13}{'divergence':>13}",
    ]
    for p in ranked[:limit]:
        lines.append(f"    {p.market_id[:33]:<34}{p.sharp_move:>+13.4f}"
                     f"{p.exchange_move:>+13.4f}{p.divergence:>+13.4f}")
    lines.append("    A sparse grid shows whether the two diverge over DAYS. It")
    lines.append("    cannot establish minute-scale reaction lag, and it does not")
    lines.append("    claim to have caught every news-driven move -- only what")
    lines.append("    happened to fall between two checkpoints.")
    return "\n".join(lines)


# --- fee scenarios ----------------------------------------------------------


@dataclass
class FeeScenario:
    """One account route, priced end to end.

    Kalshi's rounding rules align a direct member's balance at $0.0001 and a
    non-direct member's at $0.01. Which one applies is a fact about the
    ACCOUNT, not about the market, and this study does not know it. At the
    one-contract size priced here the quantum is not a rounding digit -- it is
    most of the fee -- so the study reports both and adopts neither.
    """

    route: str
    label: str
    screen: ScreenDiagnostics
    returns: ReturnReport

    @property
    def best_net_ev(self) -> float:
        return max(self.screen.best_net_ev) if self.screen.best_net_ev else float("nan")

    @property
    def median_fee_alignment(self) -> str:
        return ROUTE_ALIGNMENT.get(self.route, "?")


def fee_scenarios(
    observations: list["Observation"],
    eligibility: "Eligibility | None" = None,
    venue: str = "kalshi",
    role: str = "taker",
    series: str | None = None,
    bootstrap_rounds: int = DEFAULT_BOOTSTRAP,
    already: "FeeScenario | None" = None,
    policy: "EntryPolicy | None" = None,
) -> list[FeeScenario]:
    """Every account route the fee model knows, each priced in full.

    Deliberately derived from `ROUTE_ALIGNMENT` rather than a literal pair, so
    a third route added to the fee model appears here without being listed
    twice (the same reason the fleet keeps one bot registry).

    `already` splices in a route the caller has ALREADY computed -- the report's
    headline figures are one of these scenarios, and recomputing a 2000-round
    cluster bootstrap to print the same number twice is waste. It is the same
    object, so the table can never disagree with the sections above it.

    A SCENARIO VARIES THE FEE AND NOTHING ELSE. `policy` is threaded through
    for that reason: without it the headline route applied one entry per game
    while the other route counted every qualifying checkpoint, so a
    three-checkpoint game showed 1 trade against 3 -- a policy difference
    printed in a column headed "route", where it reads as the cheaper account
    finding three times the opportunities.
    """
    out: list[FeeScenario] = []
    for route in sorted(ROUTE_ALIGNMENT):
        if already is not None and already.route == route:
            out.append(already)
            continue
        out.append(FeeScenario(
            route=route,
            label=ROUTE_LABELS.get(route, route),
            screen=screen_diagnostics(observations, eligibility, venue, role,
                                      series, route),
            returns=realized_return(observations, eligibility, venue, role,
                                    bootstrap_rounds, series, route, policy),
        ))
    return out


# --- report -----------------------------------------------------------------


@dataclass
class StudyReport:
    comparison: ScoreComparison
    returns: ReturnReport
    conditional: list[ConditionalScore]
    sharp_calibration: list[CalibrationBin]
    exchange_calibration: list[CalibrationBin]
    decay: list[DecaySlice]
    coverage: Coverage = field(default_factory=Coverage)
    slices: list[CheckpointSlice] = field(default_factory=list)
    paths: list[PricePath] = field(default_factory=list)
    selection: SelectionDiagnostics | None = None
    policy: "EntryPolicy | None" = None
    checkpoint_coverage: str = ""
    provenance: str = ""
    # The SAME facts, pre-split by their producer. Rendering used to split
    # `provenance` on "; ", which silently tore apart any part that contained
    # one -- "PR #27 review; transcribed, not re-read in-session" became two
    # lines, one of which read as a provenance claim of its own. A string
    # joined for one consumer is not a structure for another.
    provenance_lines: list[str] = field(default_factory=list)
    ledger_text: str = ""
    screen: ScreenDiagnostics | None = None
    scenarios: list[FeeScenario] = field(default_factory=list)
    route: str = DEFAULT_ROUTE

    def readiness(self) -> list[tuple[str, bool, str]]:
        """Diagnostics and gates, separated.

        Global forecast accuracy is a DIAGNOSTIC, not a gate. The premise
        section says plainly that global Brier superiority is neither
        sufficient for positive net return nor necessary for a useful
        conditional policy -- and an earlier version then made it a mandatory
        GO criterion, contradicting itself. It is reported, not required.

        The sample gate counts games the policy would have TRADED, not every
        game forecast: a large unrelated universe must not satisfy the floor
        for a tiny trading subset.
        """
        r = self.returns
        rows = [
            ("coverage complete", self.coverage.complete, str(self.coverage)),
            ("traded sample above floor", r.games >= MIN_TRADED_GAMES,
             f"{r.games} traded games ({r.trades} trades) vs floor {MIN_TRADED_GAMES}"),
            ("positive net return on the frozen policy", r.profitable(),
             r.verdict().split(":")[0]),
        ]
        if self.scenarios:
            # The account route is unresolved, so a conclusion that holds on
            # one route and not the other is a conclusion about an unexamined
            # default, not about the strategy. This does not decide which
            # route is right; it reports whether it would matter.
            verdicts = {s.returns.profitable() for s in self.scenarios}
            rows.append((
                "conclusion survives the unresolved account route",
                len(verdicts) == 1,
                "; ".join(f"{s.route}: "
                          f"{'profitable' if s.returns.profitable() else 'not profitable'} "
                          f"({s.returns.trades} trades)" for s in self.scenarios),
            ))
        return rows

    def is_exploratory(self) -> bool:
        """True until a chronological holdout and cost/delay robustness exist.

        Neither is implemented here, so no run of this code can be a GO. The
        label is structural, not a judgement about a particular result.
        """
        return True

    def render(self) -> str:
        lines: list[str] = []
        add = lines.append
        add("=" * 76)
        add("PRE-MATCH +EV THESIS TEST")
        add("=" * 76)
        parts = self.provenance_lines or ([self.provenance] if self.provenance else [])
        if parts:
            add("FEE MODEL PROVENANCE")
            for part in parts:
                for i, line in enumerate(textwrap.wrap(part, 72) or [""]):
                    add(f"    {'' if i == 0 else '  '}{line}")
        add(f"coverage: {self.coverage}")
        if self.scenarios:
            add("")
            add("FEE ROUNDING SCENARIOS -- THE ACCOUNT ROUTE IS NOT RESOLVED")
            add("    Kalshi aligns a direct member's balance at $0.0001 and a")
            add("    non-direct member's at $0.01, over a model fee ceiled to")
            add("    $0.000001. At the one-contract size priced here the quantum")
            add("    IS most of the fee. Both are shown; neither is adopted.")
            add(f"    {'route':<12}{'align':>9}{'admitted':>10}"
                f"{'best net EV':>14}{'return':>32}")
            for sc in self.scenarios:
                flag = "  <- headline" if sc.route == self.route else ""
                add(f"    {sc.route:<12}{'$' + sc.median_fee_alignment:>9}"
                    f"{sc.screen.admitted:>10,}{sc.best_net_ev:>+14.6f}"
                    f"{sc.returns.verdict().split(':')[0]:>32}{flag}")
        if not self.coverage.complete:
            add("")
            add("!! COVERAGE IS INCOMPLETE. Figures below describe the data that")
            add("!! was READ, not the data that EXISTS. Do not conclude from them.")
        add("")

        if self.ledger_text:
            add(self.ledger_text)
            add("")

        c = self.comparison
        add(f"[1] FORECAST ACCURACY  ({c.games:,} games / {c.rows:,} contract rows)")
        add(f"    sharp     Brier {c.sharp_brier:.5f}   log loss {c.sharp_log_loss:.5f}")
        add(f"    exchange  Brier {c.exchange_brier:.5f}   log loss {c.exchange_log_loss:.5f}")
        add(f"    -> {c.verdict()}")
        add("")

        if self.checkpoint_coverage:
            add(self.checkpoint_coverage)
            add("")

        if self.slices:
            add(render_checkpoint_slices(self.slices))
            add("")

        if self.paths:
            add(render_price_paths(self.paths))
            add("")

        if self.policy is not None:
            add("ENTRY POLICY (predeclared)")
            add(f"    {self.policy.describe()}")
            add("")
        if self.selection is not None:
            add(self.selection.render())
            add("")

        if self.screen is not None:
            add(self.screen.render())
            add("")

        r = self.returns
        add("[2] NET RETURN on the predeclared eligible subset")
        add(f"    frozen policy: predicted net EV >= {r.eligibility.min_net_ev:+.3f}/contract, "
            f"price in {r.eligibility.price_band}, spread <= {r.eligibility.max_spread:.2f}, "
            f"{r.eligibility.min_minutes_to_start:.0f}-"
            f"{r.eligibility.max_minutes_to_start:.0f} min to start")
        add("    (selection is on PREDICTED EV at the executable price, not on")
        add("     disagreement with the midpoint -- the midpoint is not tradeable)")
        add(f"    priced as {r.role} on {r.venue}, held to settlement")
        add(f"    -> {r.verdict()}")
        add("    Historical quotes carry no depth: this is indicative of an")
        add("    opportunity, not a demonstration of achievable fills. Fixed data")
        add("    and operating costs are NOT included.")
        add("")

        add("[3] WHERE THEY DISAGREE  (proper score, not a hit rate)")
        add(f"    {'>= delta':>9} {'games':>7} {'brier delta':>13} {'95% CI':>24}  reading")
        for cs in self.conditional:
            if cs.rows == 0:
                add(f"    {cs.threshold:>9.2f} {'--':>7} {'--':>13} {'--':>24}  no games")
                continue
            ci = (f"[{cs.ci_low:+.5f}, {cs.ci_high:+.5f}]"
                  if cs.ci_low == cs.ci_low else "[not measurable]")
            add(f"    {cs.threshold:>9.2f} {cs.games:>7,} {cs.brier_delta:>+13.5f} "
                f"{ci:>24}  {cs.reading()}")
        add("")

        add("[4] CALIBRATION  (bias = mean prediction - actual rate)")
        add(f"    {'band':>12} {'n':>6} {'sharp bias':>12} {'exch bias':>12}")
        exch = {(b.low, b.high): b for b in self.exchange_calibration}
        for b in self.sharp_calibration:
            e = exch.get((b.low, b.high))
            add(f"    {b.low:.2f}-{b.high:.2f}  {b.n:>6,} {b.bias:+12.4f} "
                f"{e.bias:+12.4f}" if e else
                f"    {b.low:.2f}-{b.high:.2f}  {b.n:>6,} {b.bias:+12.4f} {'--':>12}")
        add("")

        add("[5] OVER TIME  (with uncertainty -- a drifting point estimate is not a trend)")
        add(f"    {'month':>9} {'games':>7} {'brier delta':>13} {'95% CI':>24} {'net return':>11}")
        for s in self.decay:
            ci = (f"[{s.ci_low:+.5f}, {s.ci_high:+.5f}]"
                  if s.ci_low == s.ci_low else "[not measurable]")
            ret = "--" if s.mean_return != s.mean_return else f"{s.mean_return:+.2%}"
            add(f"    {s.period:>9} {s.games:>7,} {s.brier_delta:>+13.5f} {ci:>24} {ret:>11}")
        add("")

        add("[6] READINESS")
        for name, passed, detail in self.readiness():
            add(f"    [{'PASS' if passed else 'no  '}] {name}")
            add(f"           {detail}")
        add("")
        add(f"    [diag] forecast accuracy: {c.verdict().split(':')[0]}")
        add("           Diagnostic only. Global Brier superiority is neither")
        add("           sufficient for net return nor necessary for a useful")
        add("           conditional policy, so it does not gate anything.")
        add("")
        if self.is_exploratory():
            add("    >> RESULT IS EXPLORATORY, NOT A GO. <<")
            add("    No chronological holdout and no delay/cost robustness check")
            add("    exist in this code, so nothing it prints can clear that bar.")
            add("    Passing every line above means the policy is worth testing")
            add("    out of sample -- not that it is worth trading.")
        add("=" * 76)
        return "\n".join(lines)


def build_report(
    observations: list[Observation],
    coverage: Coverage | None = None,
    provenance: "str | Sequence[str]" = "",
    eligibility: Eligibility | None = None,
    ledger_text: str = "",
    series: str | None = None,
    route: str = DEFAULT_ROUTE,
    bootstrap_rounds: int = DEFAULT_BOOTSTRAP,
    policy: "EntryPolicy | None" = None,
    baseline_minutes: float | None = None,
    checkpoint_coverage: str = "",
) -> StudyReport:
    lines = [provenance] if isinstance(provenance, str) else list(provenance)
    lines = [ln for ln in lines if ln]
    outcomes = [o.outcome for o in observations]
    headline_returns = realized_return(observations, eligibility, series=series,
                                       bootstrap_rounds=bootstrap_rounds,
                                       route=route, policy=policy)
    selected, selection = select_entries(
        observations, eligibility or Eligibility(), series=series, route=route,
        policy=policy or EntryPolicy()) if policy is not None else ([], None)
    headline_screen = screen_diagnostics(observations, eligibility, series=series,
                                         route=route)
    headline = FeeScenario(route=route, label=ROUTE_LABELS.get(route, route),
                           screen=headline_screen, returns=headline_returns)
    return StudyReport(
        comparison=compare(observations, bootstrap_rounds=bootstrap_rounds),
        returns=headline_returns,
        conditional=conditional_scores(observations),
        sharp_calibration=calibration([o.p_sharp for o in observations], outcomes),
        exchange_calibration=calibration([o.p_exchange for o in observations], outcomes),
        decay=decay_series(observations, eligibility, series=series,
                           route=route, policy=policy),
        coverage=coverage or Coverage(),
        provenance="; ".join(lines),
        provenance_lines=lines,
        ledger_text=ledger_text,
        screen=headline_screen,
        scenarios=fee_scenarios(observations, eligibility, series=series,
                                bootstrap_rounds=bootstrap_rounds,
                                already=headline, policy=policy),
        route=route,
        slices=checkpoint_slices(observations, eligibility, series=series,
                                 route=route, baseline_minutes=baseline_minutes),
        paths=price_paths(observations),
        selection=selection,
        policy=policy,
        checkpoint_coverage=checkpoint_coverage,
    )
