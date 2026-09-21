"""Fee model regressions.

The load-bearing assertion is `FeeCurveTest.test_flat_rate_screen_admits_negative_ev`:
a contract that clears a 3% net-edge screen under the originating spec's flat
fee model has NEGATIVE true EV at the cheap end of the price curve. That single
fact is why the flat model had to go, and it is the one a future simplification
would quietly reintroduce.
"""

import unittest

from core.fees import (
    KALSHI_TAKER_COEFF, fee_for, kalshi_fee, polymarket_fee, polymarket_us_fee,
)


class KalshiFeeTest(unittest.TestCase):
    def test_peaks_at_midpoint(self):
        mid = kalshi_fee(100, 0.50).per_contract
        for price in (0.10, 0.25, 0.75, 0.90):
            self.assertLess(kalshi_fee(100, price).per_contract, mid,
                            f"fee at {price} should be below the 50c peak")

    def test_fraction_of_stake_decreases_with_price(self):
        """0.07 * (1 - price): cheap contracts cost the most to trade."""
        fractions = [kalshi_fee(1000, p).of_stake for p in (0.1, 0.3, 0.5, 0.7, 0.9)]
        self.assertEqual(fractions, sorted(fractions, reverse=True))
        # At size, the ceiling is negligible and the smooth form should hold.
        self.assertAlmostEqual(kalshi_fee(10000, 0.25).of_stake,
                               KALSHI_TAKER_COEFF * 0.75, places=4)

    def test_ceiling_rounds_up_per_order(self):
        # 3 contracts at 50c: 0.07*3*0.25 = 5.25c, charged as 6c.
        self.assertAlmostEqual(kalshi_fee(3, 0.50).dollars, 0.06, places=10)
        # The ceiling is a real tax on small orders: above the smooth curve.
        self.assertGreater(kalshi_fee(3, 0.50).of_stake, KALSHI_TAKER_COEFF * 0.50)

    def test_maker_is_cheaper_than_taker(self):
        self.assertLessEqual(kalshi_fee(100, 0.5, "maker").dollars,
                             kalshi_fee(100, 0.5, "taker").dollars)


class CeilingTest(unittest.TestCase):
    """An earlier comment claimed maker fees 'usually round to $0.00 on small
    orders'. ceil() of any POSITIVE raw fee is at least one cent, so that is
    not merely optimistic, it is impossible -- and the rounding cuts the other
    way, making small orders relatively MORE expensive."""

    def test_a_positive_rate_can_never_round_to_zero(self):
        for n in (1, 2, 3, 5, 10):
            for price in (0.01, 0.02, 0.10, 0.50, 0.90, 0.99):
                self.assertGreaterEqual(kalshi_fee(n, price, "maker").dollars, 0.01)
                self.assertGreaterEqual(kalshi_fee(n, price, "taker").dollars, 0.01)

    def test_ceiling_makes_small_extreme_orders_expensive(self):
        # One contract at 2c owes 0.0343c of raw maker fee and is charged 1c.
        self.assertAlmostEqual(kalshi_fee(1, 0.02, "maker").of_stake, 0.50, places=6)

    def test_series_override_is_used_when_resolved(self):
        """Kalshi publishes per-series schedules; the generic coefficients are
        a default, not a universal rate."""
        from core.fees import SERIES_OVERRIDES
        SERIES_OVERRIDES["KXTEST"] = {"taker": 0.02}
        try:
            self.assertLess(kalshi_fee(1000, 0.5, "taker", series="KXTEST").dollars,
                            kalshi_fee(1000, 0.5, "taker").dollars)
        finally:
            SERIES_OVERRIDES.pop("KXTEST", None)

    def test_describe_states_whether_a_schedule_was_resolved(self):
        from core.fees import describe
        self.assertIn("series overrides resolved", describe())


class PolymarketFeeTest(unittest.TestCase):
    def test_maker_pays_nothing(self):
        self.assertEqual(polymarket_fee(100, 0.5, "maker").dollars, 0.0)

    def test_taker_follows_theta_parabola(self):
        self.assertAlmostEqual(polymarket_fee(100, 0.5).dollars, 100 * 0.05 * 0.25,
                               places=10)

    def test_us_entity_refuses_rather_than_guessing(self):
        """An unverified schedule must raise, not borrow the other venue's."""
        with self.assertRaises(NotImplementedError):
            polymarket_us_fee(10, 0.5)


class SeriesThreadingTest(unittest.TestCase):
    """A resolved per-series schedule that the PRICING path cannot see is worse
    than no feature: `describe()` reported an override as resolved while every
    fee was still computed at the generic rate."""

    def setUp(self):
        from core.fees import SERIES_OVERRIDES
        self.overrides = SERIES_OVERRIDES
        self.overrides["KXTEST"] = {"taker": 0.02}

    def tearDown(self):
        self.overrides.pop("KXTEST", None)

    def test_fee_for_accepts_and_applies_a_series(self):
        with_series = fee_for("kalshi", 1000, 0.5, "taker", series="KXTEST")
        generic = fee_for("kalshi", 1000, 0.5, "taker")
        self.assertLess(with_series.dollars, generic.dollars)

    def test_unknown_series_falls_back_to_generic_not_an_error(self):
        self.assertEqual(fee_for("kalshi", 1000, 0.5, "taker", series="KXOTHER").dollars,
                         fee_for("kalshi", 1000, 0.5, "taker").dollars)

    def test_describe_names_an_unresolved_series_for_this_run(self):
        from core.fees import describe
        self.assertIn("NO resolved fee schedule", describe("KXOTHER"))
        self.assertNotIn("NO resolved fee schedule", describe("KXTEST"))


class DispatchTest(unittest.TestCase):
    def test_unknown_venue_raises(self):
        with self.assertRaises(ValueError):
            fee_for("betfair", 10, 0.5)

    def test_invalid_inputs_raise(self):
        for n, price in ((0, 0.5), (-1, 0.5), (10, 0.0), (10, 1.0), (10, 1.5)):
            with self.assertRaises(ValueError):
                fee_for("kalshi", n, price)


class FeeCurveTest(unittest.TestCase):
    def test_flat_rate_screen_admits_negative_ev(self):
        """THE finding. A flat fee model passes losing trades as 3% winners.

        For each price, solve for the p_fair that exactly clears a 3% net-edge
        screen under `W_net = 1 - 0.02`, then evaluate the true EV with the
        real Kalshi fee. Cheap contracts come out NEGATIVE.
        """
        flat_rate, threshold = 0.02, 0.03
        negatives, positives = [], []
        for price in (0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90):
            p_fair = (threshold * price + price) / (1.0 - flat_rate)
            true_ev = p_fair - price - kalshi_fee(1000, price).per_contract
            (negatives if true_ev < 0 else positives).append(price)

        self.assertTrue(negatives, "expected a negative-EV region at low prices")
        self.assertTrue(positives, "expected a positive-EV region at high prices")
        # The sign flip is monotone: every negative price sits below every
        # positive one. That is what makes it a systematic selection bias
        # rather than scattered noise.
        self.assertLess(max(negatives), min(positives))


if __name__ == "__main__":
    unittest.main()
