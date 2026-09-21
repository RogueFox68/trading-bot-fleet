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
from dataclasses import dataclass, field
from datetime import datetime

from core.fees import fee_for
from data.kalshi_history import Coverage

# Distinct GAMES, not rows. Deliberately not presented as a power calculation:
# it is a floor below which nothing is reported, and the interval is what
# actually says whether the evidence is sufficient.
MIN_GAMES = 200
EPSILON = 1e-6
DEFAULT_BOOTSTRAP = 2000


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
    p_exchange: float              # mid at the cutoff
    outcome: int                   # 1 if THIS contract's YES settled true
    yes_participant: str = ""
    exchange_bid: float | None = None
    exchange_ask: float | None = None
    sharp_at: datetime | None = None
    exchange_at: datetime | None = None
    devig_method: str = "shin"

    @property
    def disagreement(self) -> float:
        return self.p_sharp - self.p_exchange


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
    """A PREDECLARED filter. Freeze it before looking at returns."""

    min_disagreement: float = 0.02
    price_band: tuple[float, float] = (0.15, 0.85)
    max_minutes_to_start: float = 24 * 60.0
    min_minutes_to_start: float = 5.0

    def admits(self, o: Observation) -> bool:
        if abs(o.disagreement) < self.min_disagreement:
            return False
        if not self.price_band[0] <= o.p_exchange <= self.price_band[1]:
            return False
        if not self.min_minutes_to_start <= o.minutes_to_start <= self.max_minutes_to_start:
            return False
        return o.exchange_bid is not None and o.exchange_ask is not None


@dataclass(frozen=True)
class Trade:
    """What acting on one observation would have cost and returned.

    Buying YES pays the ask and wins $1 when the contract settles true.
    Buying NO pays (1 - bid) -- the mirror of the YES book -- and wins $1 when
    it settles false. Both legs carry the venue's execution fee on the price
    actually paid. Getting the NO mapping wrong inverts half the sample, so it
    is computed explicitly rather than by negating a YES return.
    """

    game_id: str
    side: str                  # "YES" | "NO"
    entry_price: float
    fee: float
    payout: float              # 1.0 or 0.0
    profit: float              # payout - entry_price - fee, per contract

    @property
    def return_on_stake(self) -> float:
        cost = self.entry_price + self.fee
        return self.profit / cost if cost else 0.0


def as_trade(o: Observation, venue: str = "kalshi", role: str = "taker") -> Trade | None:
    """The trade the signal implies, priced at the executable quote.

    Sharp above the exchange means the contract looks cheap: buy YES at the
    ASK. Sharp below means it looks rich: buy NO, which costs (1 - bid).
    Crossed or missing quotes yield None rather than a synthesised price.
    """
    if o.exchange_bid is None or o.exchange_ask is None:
        return None
    if not 0.0 < o.exchange_ask < 1.0 or not 0.0 < o.exchange_bid < 1.0:
        return None
    if o.exchange_bid > o.exchange_ask:
        return None

    if o.disagreement > 0:
        side, entry, payout = "YES", o.exchange_ask, float(o.outcome)
    else:
        side, entry, payout = "NO", 1.0 - o.exchange_bid, float(1 - o.outcome)
    if not 0.0 < entry < 1.0:
        return None

    fee = fee_for(venue, 1.0, entry, role).dollars
    return Trade(o.game_id, side, entry, fee, payout, payout - entry - fee)


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
) -> ReturnReport:
    """Net-of-fee return from acting on the signal, on the eligible subset."""
    eligibility = eligibility or Eligibility()
    eligible = [o for o in observations if eligibility.admits(o)]

    def mean_return(sample: list[Observation]) -> float:
        trades = [t for t in (as_trade(o, venue, role) for o in sample) if t]
        if not trades:
            raise ValueError("no trades in sample")
        staked = sum(t.entry_price + t.fee for t in trades)
        return sum(t.profit for t in trades) / staked if staked else 0.0

    trades = [t for t in (as_trade(o, venue, role) for o in eligible) if t]
    if not trades:
        return ReturnReport(0, 0, 0.0, 0.0, float("nan"), float("nan"),
                            0, 0, 0.0, eligibility, venue, role)

    staked = sum(t.entry_price + t.fee for t in trades)
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
) -> list[DecaySlice]:
    """The comparison by month, WITH uncertainty.

    A monthly point estimate drifting toward zero is not evidence the edge was
    arbitraged away: game mix and noise move it too. The interval is what
    distinguishes a trend from a smaller sample.
    """
    by_month: dict[str, list[Observation]] = {}
    for o in observations:
        by_month.setdefault(o.decision_at.strftime("%Y-%m"), []).append(o)

    out: list[DecaySlice] = []
    for period in sorted(by_month):
        members = by_month[period]
        lo, hi = cluster_bootstrap(members, _brier_delta, bootstrap_rounds)
        ret = realized_return(members, eligibility, bootstrap_rounds=bootstrap_rounds)
        out.append(DecaySlice(
            period, len(cluster(members)), _brier_delta(members), lo, hi,
            ret.mean_return_on_stake if ret.trades else float("nan"),
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
    provenance: str = ""
    ledger_text: str = ""

    def go_criteria(self) -> list[tuple[str, bool, str]]:
        """The criteria, each with its own verdict.

        There is no single pass/fail line on purpose. An earlier version made
        ">50% disagreement hit rate and rising" a go criterion, which a base
        rate satisfies; and it treated "no significant difference" as proof the
        strategy was dead, when it means the evidence is insufficient.
        """
        c, r = self.comparison, self.returns
        return [
            ("coverage complete", self.coverage.complete, str(self.coverage)),
            ("sample above floor", not c.underpowered,
             f"{c.games} distinct games ({c.rows} rows)"),
            ("sharp forecasts better", bool(c.sharp_wins), c.verdict().split(":")[0]),
            ("positive net return on the declared subset", r.profitable(),
             r.verdict().split(":")[0]),
        ]

    def render(self) -> str:
        lines: list[str] = []
        add = lines.append
        add("=" * 76)
        add("PRE-MATCH +EV THESIS TEST")
        add("=" * 76)
        if self.provenance:
            add(f"fees: {self.provenance}")
        add(f"coverage: {self.coverage}")
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

        r = self.returns
        add("[2] NET RETURN on the predeclared eligible subset")
        add(f"    filter: disagreement >= {r.eligibility.min_disagreement}, "
            f"price in {r.eligibility.price_band}, "
            f"{r.eligibility.min_minutes_to_start:.0f}-"
            f"{r.eligibility.max_minutes_to_start:.0f} min to start")
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

        add("[6] GO CRITERIA")
        for name, passed, detail in self.go_criteria():
            add(f"    [{'PASS' if passed else 'no  '}] {name}")
            add(f"           {detail}")
        add("")
        add("    All four must hold. None of them is sufficient alone, and failing")
        add("    the accuracy test means INSUFFICIENT EVIDENCE, not a dead strategy.")
        add("=" * 76)
        return "\n".join(lines)


def build_report(
    observations: list[Observation],
    coverage: Coverage | None = None,
    provenance: str = "",
    eligibility: Eligibility | None = None,
    ledger_text: str = "",
) -> StudyReport:
    outcomes = [o.outcome for o in observations]
    return StudyReport(
        comparison=compare(observations),
        returns=realized_return(observations, eligibility),
        conditional=conditional_scores(observations),
        sharp_calibration=calibration([o.p_sharp for o in observations], outcomes),
        exchange_calibration=calibration([o.p_exchange for o in observations], outcomes),
        decay=decay_series(observations, eligibility),
        coverage=coverage or Coverage(),
        provenance=provenance,
        ledger_text=ledger_text,
    )
