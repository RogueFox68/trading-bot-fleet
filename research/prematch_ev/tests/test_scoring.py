"""Scoring-instrument regressions.

The scorer is the instrument that decides whether the whole strategy is worth
building. A broken instrument answers with the same confident formatting
whether or not it looked -- so it is driven against synthetic data with a KNOWN
ground truth, in all three directions.
"""

import random
import unittest
from datetime import datetime, timedelta

from analysis.scoring import (
    MIN_SAMPLE, Observation, brier, build_report, compare, decay_series,
    disagreement_buckets, log_loss,
)
from data.kalshi_history import Coverage


def synth(n, sharp_noise, exchange_noise, seed=7, start=datetime(2026, 5, 1)):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        true_p = rng.uniform(0.15, 0.85)
        clamp = lambda p: min(0.99, max(0.01, p))
        out.append(Observation(
            event_id=f"G{i}", observed_at=start + timedelta(hours=i),
            minutes_to_start=60.0,
            p_sharp=clamp(true_p + rng.gauss(0, sharp_noise)),
            p_exchange=clamp(true_p + rng.gauss(0, exchange_noise)),
            outcome=1 if rng.random() < true_p else 0,
        ))
    return out


class MetricTest(unittest.TestCase):
    def test_brier_bounds(self):
        self.assertEqual(brier([1.0, 0.0], [1, 0]), 0.0)
        self.assertEqual(brier([0.0, 1.0], [1, 0]), 1.0)

    def test_log_loss_finite_at_extremes(self):
        """A 0 or 1 prediction must not produce infinity and kill the run."""
        self.assertLess(log_loss([0.0], [1]), float("inf"))
        self.assertLess(log_loss([1.0], [0]), float("inf"))


class GroundTruthTest(unittest.TestCase):
    def test_detects_a_real_edge(self):
        c = compare(synth(2000, 0.01, 0.06))
        self.assertTrue(c.sharp_wins)
        self.assertLess(c.delta_ci_high, 0.0)
        self.assertIn("SHARP LINE WINS", c.verdict())

    def test_reports_no_edge_when_there_is_none(self):
        c = compare(synth(2000, 0.04, 0.04))
        self.assertFalse(c.sharp_wins)
        self.assertIn("NO EDGE", c.verdict())

    def test_does_not_claim_an_edge_when_inverted(self):
        """The failure that must never happen: claiming a win when the
        exchange is the better predictor."""
        c = compare(synth(2000, 0.06, 0.01))
        self.assertFalse(c.sharp_wins)
        self.assertGreater(c.brier_delta, 0.0)

    def test_small_sample_is_underpowered_not_a_finding(self):
        c = compare(synth(MIN_SAMPLE - 1, 0.01, 0.06))
        self.assertTrue(c.underpowered)
        self.assertIn("UNDERPOWERED", c.verdict())

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            compare([])


class DisagreementTest(unittest.TestCase):
    def test_buckets_shrink_as_threshold_rises(self):
        buckets = disagreement_buckets(synth(2000, 0.01, 0.06))
        counts = [b.n for b in buckets]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_hit_rates_are_complementary(self):
        for b in disagreement_buckets(synth(2000, 0.01, 0.06)):
            if b.n:
                self.assertAlmostEqual(b.sharp_hit_rate + b.exchange_hit_rate, 1.0,
                                       places=10)

    def test_empty_bucket_is_zero_not_a_crash(self):
        flat = [Observation("G", datetime(2026, 5, 1), 60.0, 0.5, 0.5, 1)]
        self.assertEqual(disagreement_buckets(flat)[0].n, 0)


class DecayTest(unittest.TestCase):
    def test_buckets_by_month_in_order(self):
        obs = synth(200, 0.01, 0.06) + synth(200, 0.01, 0.06, seed=9,
                                             start=datetime(2026, 8, 1))
        periods = [s.period for s in decay_series(obs)]
        self.assertEqual(periods, sorted(periods))
        self.assertGreater(len(periods), 1)


class ReportTest(unittest.TestCase):
    def test_incomplete_coverage_is_shouted_not_footnoted(self):
        """A figure computed from data that failed to load must not read as a
        finding. The warning goes above the numbers, not beside them."""
        bad = Coverage().fail("odds quota exhausted mid-window")
        text = build_report(synth(400, 0.01, 0.06), bad).render()
        self.assertIn("COVERAGE IS INCOMPLETE", text)
        self.assertIn("quota exhausted", text)
        self.assertLess(text.index("COVERAGE IS INCOMPLETE"), text.index("[1] ACCURACY"))

    def test_clean_report_has_all_four_sections(self):
        text = build_report(synth(400, 0.01, 0.06), Coverage()).render()
        for section in ("[1] ACCURACY", "[2] CALIBRATION", "[3] DISAGREEMENT", "[4] DECAY"):
            self.assertIn(section, text)
        self.assertNotIn("COVERAGE IS INCOMPLETE", text)


if __name__ == "__main__":
    unittest.main()
