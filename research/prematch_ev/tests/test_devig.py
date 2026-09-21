"""De-vig, EV and sizing regressions."""

import unittest

from core.devig import (
    DevigError, EdgeQuote, american_to_implied, devig, devig_american,
    devig_multiplicative, devig_shin, kelly_stake, net_ev,
)


class AmericanOddsTest(unittest.TestCase):
    def test_conversion(self):
        self.assertAlmostEqual(american_to_implied(-200), 200 / 300, places=10)
        self.assertAlmostEqual(american_to_implied(170), 100 / 270, places=10)

    def test_zero_is_not_a_price(self):
        with self.assertRaises(DevigError):
            american_to_implied(0)


class DevigTest(unittest.TestCase):
    def test_both_methods_normalise(self):
        for odds in ([-200, 170], [-110, -110], [-500, 375], [120, -140]):
            r = devig_american(odds)
            self.assertAlmostEqual(sum(r.multiplicative), 1.0, places=10)
            self.assertAlmostEqual(sum(r.shin), 1.0, places=10)

    def test_shin_moves_favourite_up(self):
        """The favourite-longshot correction, and the reason both are carried.

        Multiplicative distributes vig proportionally, which overstates the
        longshot. Shin widens the spread in the correcting direction.
        """
        r = devig_american([-200, 170])
        self.assertGreater(r.shin[0], r.multiplicative[0])   # favourite up
        self.assertLess(r.shin[1], r.multiplicative[1])      # longshot down
        self.assertGreater(r.disagreement(), 0.001)

    def test_disagreement_grows_with_vig(self):
        thin = devig_american([-105, -105]).disagreement()
        fat = devig_american([-140, 110]).disagreement()
        self.assertGreater(fat, thin)

    def test_crossed_book_refused(self):
        """A booksum <= 1.0 is arbitrage or stale quotes, not a vigged book."""
        with self.assertRaises(DevigError):
            devig_multiplicative([0.45, 0.45])
        with self.assertRaises(DevigError):
            devig_shin([0.45, 0.45])

    def test_degenerate_inputs_refused(self):
        for raw in ([0.9], [0.0, 1.2], [-0.2, 1.4]):
            with self.assertRaises(DevigError):
                devig(raw)

    def test_shin_z_in_range(self):
        r = devig_american([-200, 170])
        self.assertGreaterEqual(r.shin_z, 0.0)
        self.assertLess(r.shin_z, 1.0)


class NetEvTest(unittest.TestCase):
    def test_fee_lives_in_cost_basis_not_payout(self):
        q = net_ev(0.60, 0.50, 100, "kalshi", "taker")
        expected = 0.60 * 100 - (100 * 0.50 + q.fee_dollars)
        self.assertAlmostEqual(q.ev_dollars, expected, places=10)

    def test_maker_beats_taker_at_same_price(self):
        self.assertGreater(net_ev(0.60, 0.50, 100, "kalshi", "maker").ev_dollars,
                           net_ev(0.60, 0.50, 100, "kalshi", "taker").ev_dollars)

    def test_screen_requires_absolute_floor_too(self):
        """A 3% net edge on a cheap contract is a fraction of a probability
        point, which is inside the de-vig model's own error. The percentage
        alone must not be sufficient."""
        cheap = net_ev(0.1051, 0.10, 100)
        self.assertLess(cheap.absolute_edge, 0.015)
        self.assertFalse(cheap.passes(min_net_edge=0.0, min_absolute_edge=0.015))

    def test_price_band_gate(self):
        self.assertFalse(net_ev(0.99, 0.95, 100).in_price_band)
        self.assertTrue(net_ev(0.60, 0.50, 100).in_price_band)
        # Outside the band, `passes` refuses even a large edge by default.
        self.assertFalse(net_ev(0.20, 0.05, 100).passes())

    def test_rejects_non_probability(self):
        with self.assertRaises(ValueError):
            net_ev(1.4, 0.5, 10)


class KellyTest(unittest.TestCase):
    def test_fee_shrinks_the_stake(self):
        """Fee-inclusive Kelly must size below the fee-free figure.

        At a 3% edge on a 50c contract the difference is large, not marginal:
        the execution fee is a real part of the amount at risk.
        """
        with_fee = kelly_stake(0.5255, 0.50, 1000.0, "kalshi", "taker")
        maker = kelly_stake(0.5255, 0.50, 1000.0, "kalshi", "maker")
        self.assertGreater(maker.dollars, with_fee.dollars)
        self.assertLess(with_fee.dollars, 7.50)

    def test_no_edge_sizes_zero(self):
        k = kelly_stake(0.40, 0.50, 1000.0)
        self.assertEqual(k.dollars, 0.0)
        self.assertEqual(k.contracts, 0)
        self.assertEqual(k.capped_by, "kelly_zero")

    def test_per_bet_cap_binds(self):
        k = kelly_stake(0.70, 0.50, 100_000.0, max_per_bet=50.0)
        self.assertEqual(k.dollars, 50.0)
        self.assertEqual(k.capped_by, "per_bet_cap")

    def test_contracts_priced_at_outlay_not_price(self):
        """Contract count must divide by price+fee, or the order overspends."""
        k = kelly_stake(0.70, 0.50, 1000.0, max_per_bet=50.0)
        self.assertLessEqual(k.contracts * 0.50, k.dollars)

    def test_bankroll_must_be_positive(self):
        with self.assertRaises(ValueError):
            kelly_stake(0.6, 0.5, 0.0)


if __name__ == "__main__":
    unittest.main()
