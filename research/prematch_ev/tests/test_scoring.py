"""Scoring-instrument regressions.

Two defects here produced confident, wrong output, and both have a named
reproduction below. The instrument decides whether the whole strategy is worth
building, so it is driven against data whose right answer is known.
"""

import random
import unittest
from datetime import datetime, timedelta

from analysis.scoring import (
    Eligibility, MIN_GAMES, MIN_TRADED_GAMES, Observation, as_trade, brier,
    build_report, cluster, compare, conditional_scores, decay_series, log_loss,
    realized_return, side_quotes,
)
from data.kalshi_history import Coverage


def obs(game, market, p_sharp, p_exch, outcome, when=None, bid=None, ask=None):
    return Observation(
        game_id=game, market_id=market,
        decision_at=when or datetime(2026, 5, 1),
        minutes_to_start=120.0, p_sharp=p_sharp, p_exchange=p_exch,
        outcome=outcome,
        exchange_bid=bid if bid is not None else round(p_exch - 0.01, 2),
        exchange_ask=ask if ask is not None else round(p_exch + 0.01, 2),
    )


def synth(mid, sharp, true_p, n=1000, seed=3):
    """Fixed prices, so the right answer is known exactly."""
    rng = random.Random(seed)
    return [obs(f"EVT{i}", f"M{i}", sharp, mid,
                1 if rng.random() < true_p else 0,
                datetime(2026, 5, 1) + timedelta(hours=3 * i))
            for i in range(n)]


def noisy(n=600, sharp_noise=0.05, exchange_noise=0.05, seed=3):
    """Both forecasts noisy. Equal noise is the NULL; unequal is a real edge."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        true_p = rng.uniform(0.25, 0.75)
        mid = round(min(0.90, max(0.10, true_p + rng.gauss(0, exchange_noise))), 2)
        s = min(0.99, max(0.01, true_p + rng.gauss(0, sharp_noise)))
        out.append(obs(f"EVT{i}", f"M{i}", s, mid,
                       1 if rng.random() < true_p else 0,
                       datetime(2026, 5, 1) + timedelta(hours=3 * i)))
    return out


class MetricTest(unittest.TestCase):
    def test_brier_bounds(self):
        self.assertEqual(brier([1.0, 0.0], [1, 0]), 0.0)
        self.assertEqual(brier([0.0, 1.0], [1, 0]), 1.0)

    def test_log_loss_finite_at_extremes(self):
        self.assertLess(log_loss([0.0], [1]), float("inf"))
        self.assertLess(log_loss([1.0], [0]), float("inf"))


class ClusteringTest(unittest.TestCase):
    """THE regression. Rows are not independent evidence; games are.

    Kalshi lists one contract per team, so a game yields two rows whose
    outcomes are complements. Resampling rows i.i.d. counted them as two
    independent draws, doubling the apparent sample and narrowing the interval.
    """

    def test_duplicate_rows_do_not_increase_sample_size(self):
        one = obs("EVT1", "M1", 0.60, 0.50, 1)
        c = compare([one] * 200)
        self.assertEqual(c.games, 1)
        self.assertEqual(c.rows, 200)
        self.assertTrue(c.underpowered)
        self.assertIn("UNDERPOWERED", c.verdict())

    def test_duplicates_do_not_produce_a_zero_width_interval(self):
        """The pre-fix instrument reported a 0.00000-wide 95% CI and
        'SHARP LINE WINS' from 200 copies of one observation."""
        c = compare([obs("EVT1", "M1", 0.60, 0.50, 1)] * 200)
        self.assertNotIn("WINS", c.verdict())
        self.assertFalse(c.sharp_wins)

    def test_complementary_contracts_cluster_as_one_game(self):
        """Both team contracts of one game share a game_id, so the pair counts
        once -- adding the mirror row must not add a game."""
        rows = []
        for i in range(300):
            rows.append(obs(f"EVT{i}", f"{i}-YES", 0.60, 0.55, 1))
            rows.append(obs(f"EVT{i}", f"{i}-NO", 0.40, 0.45, 0))
        c = compare(rows)
        self.assertEqual(c.games, 300)
        self.assertEqual(c.rows, 600)

    def test_cluster_groups_by_game(self):
        rows = [obs("A", "A-1", 0.5, 0.5, 1), obs("A", "A-2", 0.5, 0.5, 0),
                obs("B", "B-1", 0.5, 0.5, 1)]
        self.assertEqual({k: len(v) for k, v in cluster(rows).items()}, {"A": 2, "B": 1})


class HitRateReplacementTest(unittest.TestCase):
    """The replaced statistic returned the base rate of the favoured side.

    Both reproductions below were confirmed against the pre-fix code, which
    reported 'sharp right 80.0%' for the first and '26.5%' for the second --
    exactly backwards on both.
    """

    def test_calibrated_exchange_is_not_beaten_by_a_worse_sharp_line(self):
        # exchange .80 and correct; sharp .85 and wrong; buying at .80 is 0 EV.
        data = synth(0.80, 0.85, 0.80, n=1200)
        self.assertFalse(compare(data, bootstrap_rounds=400).sharp_wins)
        self.assertIn("EXCHANGE IS THE BETTER FORECAST", compare(data, bootstrap_rounds=400).verdict())
        self.assertFalse(realized_return(
            data, Eligibility(price_band=(0.10, 0.90)),
            bootstrap_rounds=400).profitable())

    def test_correct_sharp_line_on_a_longshot_is_recognised(self):
        # exchange .20 and wrong; sharp .25 and right; buying at .20 is +.05 EV.
        data = synth(0.20, 0.25, 0.25, n=1200, seed=5)
        self.assertTrue(compare(data, bootstrap_rounds=400).sharp_wins)
        self.assertTrue(realized_return(
            data, Eligibility(price_band=(0.10, 0.90)),
            bootstrap_rounds=400).profitable())

    def test_conditional_scores_are_proper_not_hit_rates(self):
        data = synth(0.20, 0.25, 0.25, n=1200, seed=5)
        readings = {cs.reading() for cs in conditional_scores(data, (0.02,), bootstrap_rounds=300)}
        self.assertIn("sharp better", readings)


class TradeMappingTest(unittest.TestCase):
    """Getting the NO leg wrong inverts half the sample."""

    def test_sharp_above_buys_yes_at_the_ask(self):
        t = as_trade(obs("E", "M", 0.60, 0.50, 1, bid=0.49, ask=0.51))
        self.assertEqual(t.side, "YES")
        self.assertAlmostEqual(t.entry_price, 0.51)
        self.assertEqual(t.payout, 1.0)

    def test_sharp_below_buys_no_at_one_minus_bid(self):
        t = as_trade(obs("E", "M", 0.40, 0.50, 0, bid=0.49, ask=0.51))
        self.assertEqual(t.side, "NO")
        self.assertAlmostEqual(t.entry_price, 0.51)
        self.assertEqual(t.payout, 1.0, "NO wins when the contract settles false")

    def test_no_leg_loses_when_contract_settles_true(self):
        t = as_trade(obs("E", "M", 0.40, 0.50, 1, bid=0.49, ask=0.51))
        self.assertEqual(t.side, "NO")
        self.assertEqual(t.payout, 0.0)

    def test_fee_is_charged_on_the_price_paid(self):
        t = as_trade(obs("E", "M", 0.60, 0.50, 1, bid=0.49, ask=0.51))
        self.assertGreater(t.fee, 0.0)
        self.assertAlmostEqual(t.profit, 1.0 - t.entry_price - t.fee)

    def test_missing_or_crossed_quotes_yield_no_trade(self):
        self.assertIsNone(as_trade(Observation("E", "M", datetime(2026, 5, 1),
                                               120.0, 0.6, 0.5, 1)))
        self.assertIsNone(as_trade(obs("E", "M", 0.60, 0.50, 1, bid=0.60, ask=0.40)))


class NetEvScreenTest(unittest.TestCase):
    """THE selection regression: eligibility screened on disagreement with the
    MIDPOINT while execution happens at the ask, so a wide book admitted trades
    with a large predicted LOSS before the outcome was known."""

    WIDE = dict(bid=0.40, ask=0.60)     # 20c spread, mid .50

    def test_wide_spread_negative_ev_is_rejected(self):
        o = obs("E", "M", 0.53, 0.50, 1, **self.WIDE)
        yes = [q for q in side_quotes(o) if q.side == "YES"][0]
        self.assertLess(yes.predicted_ev, 0.0)
        self.assertAlmostEqual(yes.predicted_ev, -0.09, places=2)
        self.assertFalse(Eligibility(min_net_ev=0.01).admits(o))
        self.assertIsNone(as_trade(o, eligibility=Eligibility(min_net_ev=0.01)))

    def test_genuinely_positive_ev_is_accepted(self):
        o = obs("E", "M", 0.60, 0.50, 1, bid=0.49, ask=0.51)
        el = Eligibility(min_net_ev=0.01)
        self.assertTrue(el.admits(o))
        t = as_trade(o, eligibility=el)
        self.assertEqual(t.side, "YES")
        self.assertGreater(t.predicted_ev, 0.01)

    def test_direction_comes_from_ev_not_from_the_midpoint(self):
        """Both sides are priced; the better predicted EV wins."""
        o = obs("E", "M", 0.30, 0.50, 0, bid=0.49, ask=0.51)
        self.assertEqual(as_trade(o, eligibility=Eligibility(min_net_ev=0.0)).side,
                         "NO")

    def test_spread_cap_rejects_an_unexecutable_book(self):
        o = obs("E", "M", 0.95, 0.50, 1, **self.WIDE)
        self.assertFalse(Eligibility(min_net_ev=0.0, max_spread=0.05).admits(o))

    def test_threshold_is_predeclared_and_binds(self):
        o = obs("E", "M", 0.60, 0.50, 1, bid=0.49, ask=0.51)
        self.assertTrue(Eligibility(min_net_ev=0.05).admits(o))
        self.assertFalse(Eligibility(min_net_ev=0.50).admits(o))


class GroundTruthTest(unittest.TestCase):
    def test_detects_a_real_edge(self):
        """Sharp materially less noisy than the exchange."""
        data = noisy(n=1500, sharp_noise=0.01, exchange_noise=0.10, seed=4)
        self.assertTrue(compare(data, bootstrap_rounds=400).sharp_wins)

    def test_cluster_bootstrap_is_conservative_where_rows_were_not(self):
        """A smaller effect that a row-level bootstrap would have called
        significant now reads as insufficient evidence at the same n.

        This is the fix working, not a loss of power: the extra width is the
        dependence between a game's two contracts, which the row bootstrap
        spent as if it were independent evidence.
        """
        data = noisy(n=800, sharp_noise=0.01, exchange_noise=0.07, seed=4)
        c = compare(data, bootstrap_rounds=400)
        self.assertLess(c.brier_delta, 0.0, "the effect is there in the point estimate")
        self.assertFalse(c.sharp_wins, "but the interval must not claim it at this n")
        self.assertIn("INSUFFICIENT EVIDENCE", c.verdict())

    def test_underpowered_below_the_game_floor(self):
        data = noisy(n=MIN_GAMES - 1)
        self.assertTrue(compare(data).underpowered)

    def test_insufficient_evidence_is_not_proof_of_no_edge(self):
        data = noisy(n=600)
        self.assertIn("not proof", compare(data, bootstrap_rounds=400).verdict())

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            compare([])


class DecayTest(unittest.TestCase):
    def test_months_in_order_and_carry_uncertainty(self):
        data = noisy(n=900)
        slices = decay_series(data)
        self.assertEqual([s.period for s in slices], sorted(s.period for s in slices))
        self.assertTrue(any(s.ci_low == s.ci_low for s in slices),
                        "a monthly point estimate without an interval is not a trend")


class ReportTest(unittest.TestCase):
    def test_incomplete_coverage_is_shouted_above_the_numbers(self):
        bad = Coverage().fail("odds quota exhausted mid-window")
        text = build_report(noisy(n=400), bad).render()
        self.assertIn("COVERAGE IS INCOMPLETE", text)
        self.assertLess(text.index("COVERAGE IS INCOMPLETE"),
                        text.index("[1] FORECAST ACCURACY"))

    def test_readiness_gates_on_return_and_coverage_only(self):
        """Global forecast accuracy is a DIAGNOSTIC. An earlier version made it
        a mandatory GO criterion while the premise section said it is neither
        sufficient nor necessary -- a contradiction."""
        report = build_report(noisy(n=400), Coverage())
        names = [name for name, _, _ in report.readiness()]
        self.assertIn("positive net return on the frozen policy", names)
        self.assertIn("coverage complete", names)
        self.assertFalse(any("forecast" in n or "accuracy" in n for n in names),
                         "accuracy must not gate")

    def test_output_is_labelled_exploratory(self):
        """No holdout and no cost/delay robustness exist here, so no run of
        this code can be a GO."""
        report = build_report(noisy(n=400), Coverage())
        self.assertTrue(report.is_exploratory())
        self.assertIn("EXPLORATORY", report.render())

    def test_nontraded_observations_do_not_satisfy_the_sample_gate(self):
        """A large unrelated universe must not satisfy the floor for a tiny
        trading subset."""
        traded = [obs(f"T{i}", f"T{i}", 0.60, 0.50, i % 2, bid=0.49, ask=0.51)
                  for i in range(40)]
        untraded = [obs(f"U{i}", f"U{i}", 0.51, 0.50, i % 2, bid=0.20, ask=0.80)
                    for i in range(900)]
        alone = build_report(traded, Coverage())
        padded = build_report(traded + untraded, Coverage())
        gate = lambda r: [p for n, p, _ in r.readiness() if "sample" in n][0]
        self.assertEqual(alone.returns.games, padded.returns.games)
        self.assertEqual(gate(alone), gate(padded))
        self.assertGreater(padded.comparison.games, alone.comparison.games)

    def test_no_hit_rate_gate_remains(self):
        """The >50% rule is gone; nothing should reintroduce it."""
        text = build_report(noisy(n=400), Coverage()).render()
        self.assertNotIn("hit rate", text.lower().replace("not a hit rate", ""))

    def test_return_section_states_its_own_limits(self):
        text = build_report(noisy(n=400), Coverage()).render()
        self.assertIn("no depth", text)
        self.assertIn("NOT included", text)


if __name__ == "__main__":
    unittest.main()
