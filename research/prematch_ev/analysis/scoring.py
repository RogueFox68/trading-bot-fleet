"""Does the de-vigged sharp line predict settlement better than the exchange?

This is the whole thesis, and it needs no fill model, no order book simulation
and no execution logic. Three columns per settled game -- the sharp fair
probability, the exchange price, and the binary outcome -- answer it.

If the sharp line does not beat the exchange price at predicting outcomes,
the strategy is dead and no implementation quality rescues it.

Four questions, in the order they should be asked:

  1. ACCURACY.  Brier and log loss for each predictor, with a bootstrap
     confidence interval on the DIFFERENCE. The difference is what matters
     and it is paired -- both predictors see the same games -- so the paired
     interval is far tighter than two separate ones.
  2. CALIBRATION. A predictor can win on Brier while being systematically
     biased in the price region you intend to trade.
  3. DISAGREEMENT. When the two disagree materially, who is right? This is
     the money table: an edge only exists where they differ, so pooled
     accuracy over games they agree on dilutes exactly the signal being tested.
  4. DECAY. All of the above, bucketed by month. Kalshi's sports markets are
     young and their volume has ramped hard; an edge that existed in early
     2025 and has since been arbitraged away produces an encouraging POOLED
     number and no tradeable present. Decay is reported as a time series
     because a mean would hide the only thing worth knowing.

Everything here is pure Python -- no numpy, pandas, scipy or sklearn -- so the
study runs anywhere the puller runs, including inside the fleet container.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from data.kalshi_history import Coverage

# Below this many settled games, differences are noise. The report states the
# shortfall rather than printing a number that looks like a finding.
MIN_SAMPLE = 200
# Predictions are clamped off 0 and 1 so log loss stays finite. A book quoting
# a true 0 or 1 is a settled market, not a forecast.
EPSILON = 1e-6


@dataclass(frozen=True)
class Observation:
    """One settled game, as seen by both predictors at one moment pre-kickoff."""

    event_id: str
    observed_at: datetime
    minutes_to_start: float
    p_sharp: float            # de-vigged sharp fair probability for YES
    p_exchange: float         # exchange mid (or the price actually tradeable)
    outcome: int              # 1 YES settled true, 0 false
    exchange_bid: float | None = None
    exchange_ask: float | None = None
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


@dataclass
class ScoreComparison:
    n: int
    sharp_brier: float
    exchange_brier: float
    sharp_log_loss: float
    exchange_log_loss: float
    brier_delta: float                     # negative => sharp is better
    delta_ci_low: float
    delta_ci_high: float
    sharp_wins: bool                       # CI strictly below zero
    underpowered: bool

    def verdict(self) -> str:
        if self.underpowered:
            return (
                f"UNDERPOWERED: {self.n} games < {MIN_SAMPLE} minimum. "
                "No conclusion either way."
            )
        if self.sharp_wins:
            return (
                f"SHARP LINE WINS: Brier delta {self.brier_delta:+.5f} "
                f"(95% CI {self.delta_ci_low:+.5f} to {self.delta_ci_high:+.5f}), "
                "interval excludes zero."
            )
        if self.delta_ci_low > 0:
            return (
                f"EXCHANGE WINS: Brier delta {self.brier_delta:+.5f} "
                f"(95% CI {self.delta_ci_low:+.5f} to {self.delta_ci_high:+.5f}). "
                "The sharp line is the WORSE predictor -- the thesis is inverted."
            )
        return (
            f"NO EDGE DEMONSTRATED: Brier delta {self.brier_delta:+.5f} "
            f"(95% CI {self.delta_ci_low:+.5f} to {self.delta_ci_high:+.5f}) "
            "straddles zero."
        )


def compare(
    observations: list[Observation],
    bootstrap_rounds: int = 2000,
    seed: int = 20260921,
) -> ScoreComparison:
    """Paired accuracy comparison with a bootstrap CI on the difference."""
    if not observations:
        raise ValueError("no observations to score")

    sharp = [o.p_sharp for o in observations]
    exch = [o.p_exchange for o in observations]
    out = [o.outcome for o in observations]
    n = len(observations)

    sb, eb = brier(sharp, out), brier(exch, out)
    per_game = [(s - o) ** 2 - (e - o) ** 2 for s, e, o in zip(sharp, exch, out)]

    rng = random.Random(seed)
    deltas = []
    for _ in range(bootstrap_rounds):
        sample = [per_game[rng.randrange(n)] for _ in range(n)]
        deltas.append(sum(sample) / n)
    deltas.sort()
    lo = deltas[int(0.025 * bootstrap_rounds)]
    hi = deltas[int(0.975 * bootstrap_rounds) - 1]

    return ScoreComparison(
        n=n,
        sharp_brier=sb,
        exchange_brier=eb,
        sharp_log_loss=log_loss(sharp, out),
        exchange_log_loss=log_loss(exch, out),
        brier_delta=sb - eb,
        delta_ci_low=lo,
        delta_ci_high=hi,
        sharp_wins=hi < 0.0,
        underpowered=n < MIN_SAMPLE,
    )


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
        bins.append(
            CalibrationBin(
                low, high, len(members),
                sum(p for p, _ in members) / len(members),
                sum(o for _, o in members) / len(members),
            )
        )
    return bins


@dataclass
class DisagreementBucket:
    threshold: float
    n: int
    sharp_correct: int
    exchange_correct: int

    @property
    def sharp_hit_rate(self) -> float:
        return self.sharp_correct / self.n if self.n else 0.0

    @property
    def exchange_hit_rate(self) -> float:
        return self.exchange_correct / self.n if self.n else 0.0


def disagreement_buckets(
    observations: list[Observation],
    thresholds: tuple[float, ...] = (0.01, 0.02, 0.03, 0.05, 0.08),
) -> list[DisagreementBucket]:
    """When the two predictors materially disagree, which one is right?

    "Correct" means the predictor on the right side of 0.5 relative to the
    other -- i.e. when sharp says higher and the outcome was YES, sharp was
    right. Only games where they disagree by at least the threshold count,
    because those are the only games the strategy would have traded.
    """
    buckets = []
    for threshold in thresholds:
        members = [o for o in observations if abs(o.disagreement) >= threshold]
        if not members:
            buckets.append(DisagreementBucket(threshold, 0, 0, 0))
            continue
        sharp_right = sum(
            1 for o in members
            if (o.disagreement > 0 and o.outcome == 1)
            or (o.disagreement < 0 and o.outcome == 0)
        )
        buckets.append(
            DisagreementBucket(threshold, len(members), sharp_right,
                               len(members) - sharp_right)
        )
    return buckets


@dataclass
class DecaySlice:
    period: str
    n: int
    brier_delta: float
    sharp_hit_rate_at_2pct: float


def decay_series(observations: list[Observation]) -> list[DecaySlice]:
    """The comparison bucketed by calendar month.

    A pooled average over a period in which the exchange's volume grew by an
    order of magnitude describes a market that no longer exists. If the delta
    trends toward zero, the edge is being arbitraged away and the pooled
    number is a historical fact, not a forecast.
    """
    by_month: dict[str, list[Observation]] = {}
    for o in observations:
        by_month.setdefault(o.observed_at.strftime("%Y-%m"), []).append(o)

    out = []
    for period in sorted(by_month):
        members = by_month[period]
        sharp = [o.p_sharp for o in members]
        exch = [o.p_exchange for o in members]
        outcomes = [o.outcome for o in members]
        traded = [o for o in members if abs(o.disagreement) >= 0.02]
        hit = (
            sum(1 for o in traded
                if (o.disagreement > 0 and o.outcome == 1)
                or (o.disagreement < 0 and o.outcome == 0)) / len(traded)
            if traded else float("nan")
        )
        out.append(
            DecaySlice(period, len(members),
                       brier(sharp, outcomes) - brier(exch, outcomes), hit)
        )
    return out


@dataclass
class StudyReport:
    comparison: ScoreComparison
    sharp_calibration: list[CalibrationBin]
    exchange_calibration: list[CalibrationBin]
    disagreement: list[DisagreementBucket]
    decay: list[DecaySlice]
    coverage: Coverage = field(default_factory=Coverage)
    provenance: str = ""

    def render(self) -> str:
        lines: list[str] = []
        add = lines.append

        add("=" * 72)
        add("PRE-MATCH +EV THESIS TEST")
        add("=" * 72)
        if self.provenance:
            add(f"fees: {self.provenance}")
        add(f"coverage: {self.coverage}")
        if not self.coverage.complete:
            add("")
            add("!! COVERAGE IS INCOMPLETE. Figures below describe the data that")
            add("!! was READ, not the data that EXISTS. Do not conclude from them.")
        add("")

        c = self.comparison
        add(f"[1] ACCURACY  (n = {c.n:,} settled games)")
        add(f"    sharp     Brier {c.sharp_brier:.5f}   log loss {c.sharp_log_loss:.5f}")
        add(f"    exchange  Brier {c.exchange_brier:.5f}   log loss {c.exchange_log_loss:.5f}")
        add(f"    -> {c.verdict()}")
        add("")

        add("[2] CALIBRATION  (bias = mean prediction - actual rate)")
        add(f"    {'band':>12} {'n':>6} {'sharp bias':>12} {'exch bias':>12}")
        exch_by_band = {(b.low, b.high): b for b in self.exchange_calibration}
        for b in self.sharp_calibration:
            e = exch_by_band.get((b.low, b.high))
            e_bias = f"{e.bias:+12.4f}" if e else f"{'--':>12}"
            add(f"    {b.low:.2f}-{b.high:.2f}  {b.n:>6} {b.bias:+12.4f} {e_bias}")
        add("")

        add("[3] DISAGREEMENT  (only games the strategy would have traded)")
        add(f"    {'>= delta':>10} {'n':>7} {'sharp right':>13} {'exchange right':>15}")
        for d in self.disagreement:
            if d.n == 0:
                add(f"    {d.threshold:>10.2f} {d.n:>7}  {'-- no games --':>29}")
                continue
            add(f"    {d.threshold:>10.2f} {d.n:>7} {d.sharp_hit_rate:>12.1%} "
                f"{d.exchange_hit_rate:>14.1%}")
        add("")

        add("[4] DECAY  (is the edge still alive?)")
        add(f"    {'month':>9} {'n':>7} {'brier delta':>13} {'sharp hit @2%':>15}")
        for s in self.decay:
            hit = "--" if s.sharp_hit_rate_at_2pct != s.sharp_hit_rate_at_2pct \
                else f"{s.sharp_hit_rate_at_2pct:.1%}"
            add(f"    {s.period:>9} {s.n:>7} {s.brier_delta:>+13.5f} {hit:>15}")
        add("")
        add("A brier delta trending toward zero means the edge is being")
        add("arbitraged away. Read the trend, not the pooled mean.")
        add("=" * 72)
        return "\n".join(lines)


def build_report(
    observations: list[Observation],
    coverage: Coverage | None = None,
    provenance: str = "",
) -> StudyReport:
    outcomes = [o.outcome for o in observations]
    return StudyReport(
        comparison=compare(observations),
        sharp_calibration=calibration([o.p_sharp for o in observations], outcomes),
        exchange_calibration=calibration([o.p_exchange for o in observations], outcomes),
        disagreement=disagreement_buckets(observations),
        decay=decay_series(observations),
        coverage=coverage or Coverage(),
        provenance=provenance,
    )
