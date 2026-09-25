"""Fee model regressions.

Three load-bearing assertions, each pinning a defect that produced confident
numbers rather than an error:

`FeeCurveTest.test_flat_rate_screen_admits_negative_ev` -- a contract that
clears a 3% net-edge screen under the originating spec's FLAT fee model has
NEGATIVE true EV at the cheap end of the price curve. That single fact is why
the flat model had to go, and it is the one a future simplification would
quietly reintroduce.

`DatedScheduleTest.test_a_later_entry_never_reprices_an_earlier_decision` --
Kalshi's per-series multiplier is dated. Pricing a September 2026 decision at
whatever rate is current when the study RUNS is retroactive repricing, and for
KXMLBGAME it is a factor of two on every fee in the study.

`RoundingScenarioTest.test_both_recorded_routes_are_reported_not_chosen` --
the account route ($0.0001 direct vs $0.01 non-direct balance alignment) is a
fact about the ACCOUNT, not the market, and it is unresolved for this study. At
the one-contract size the study prices at, that quantum IS most of the fee.

WHAT THESE TESTS ARE BUILT FROM, because it bounds what they prove. The dated
schedule entries were read from Kalshi's public `fee_changes` endpoint on
2026-09-21 during PR #27's review and are TRANSCRIBED here; the rounding rules
come from the same review's reading of docs.kalshi.com/getting_started/
fee_rounding. Neither host is reachable from the session that wrote these
tests, so the worked examples below are derived from the RULES AS RECORDED --
they are not transcriptions of that page's own example table, which the review
notes disagrees with its own prose (table: cents, prose: centicents). These
tests pin the model against the recorded rules. They cannot confirm the rules.
"""

import unittest
from datetime import datetime, timedelta, timezone

from core.fees import (
    DEFAULT_ROUTE, KALSHI_TAKER_COEFF, KALSHI_SERIES_SCHEDULES,
    MODEL_FEE_PRECISION, ROUTE_ALIGNMENT, ROUTE_RESOLVED, SCHEDULE_CONFLICTS,
    FeeScheduleEntry, FeeScheduleUnresolved, _ceil_to, describe, fee_for,
    kalshi_fee, polymarket_fee, polymarket_us_fee, resolve_schedule,
    round_fee, schedule_changes_within,
)

# The recorded KXMLBGAME change, as read on 2026-09-21.
SWITCH_TS = datetime(2026, 8, 7, 4, 59, 45, 131000, tzinfo=timezone.utc)
SEPTEMBER = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)
JULY = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


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
        # At size, the alignment is negligible and the smooth form should hold.
        self.assertAlmostEqual(kalshi_fee(10000, 0.25).of_stake,
                               KALSHI_TAKER_COEFF * 0.75, places=4)

    def test_alignment_rounds_up_per_order(self):
        # 3 contracts at 50c: 0.07*3*0.25 = 5.25c, charged as 6c non-direct.
        self.assertAlmostEqual(kalshi_fee(3, 0.50).dollars, 0.06, places=10)
        # The alignment is a real tax on small orders: above the smooth curve.
        self.assertGreater(kalshi_fee(3, 0.50).of_stake, KALSHI_TAKER_COEFF * 0.50)

    def test_maker_is_cheaper_than_taker(self):
        self.assertLessEqual(kalshi_fee(100, 0.5, "maker").dollars,
                             kalshi_fee(100, 0.5, "taker").dollars)

    def test_the_quote_carries_its_pre_rounding_charge(self):
        """`raw_dollars` is how a report shows what is curve and what is quantum."""
        q = kalshi_fee(1, 0.50)
        self.assertAlmostEqual(q.raw_dollars, 0.0175, places=12)
        self.assertEqual(q.dollars, 0.02)
        self.assertGreater(q.dollars, q.raw_dollars)


class CeilingTest(unittest.TestCase):
    """An earlier comment claimed maker fees 'usually round to $0.00 on small
    orders'. A ceiling of any POSITIVE raw fee is at least one quantum, so that
    is not merely optimistic, it is impossible -- and the rounding cuts the
    other way, making small orders relatively MORE expensive."""

    def test_a_positive_rate_can_never_round_to_zero(self):
        for n in (1, 2, 3, 5, 10):
            for price in (0.01, 0.02, 0.10, 0.50, 0.90, 0.99):
                self.assertGreaterEqual(kalshi_fee(n, price, "maker").dollars, 0.01)
                self.assertGreaterEqual(kalshi_fee(n, price, "taker").dollars, 0.01)

    def test_ceiling_makes_small_extreme_orders_expensive(self):
        # One contract at 2c owes 0.0343c of raw maker fee and is charged 1c.
        self.assertAlmostEqual(kalshi_fee(1, 0.02, "maker").of_stake, 0.50, places=6)

    def test_an_exact_multiple_is_not_pushed_to_the_next_quantum(self):
        """A charge landing exactly on the quantum stays there.

        Float arithmetic is the reason this is a test and not an assumption:
        `math.ceil(0.44 * 100) / 100` is the kind of expression that returns
        0.45 for a charge of exactly 44 cents.
        """
        self.assertEqual(round_fee(0.44, "non_direct"), 0.44)
        self.assertEqual(round_fee(0.875, "direct"), 0.875)
        self.assertEqual(round_fee(1.75, "non_direct"), 1.75)


class DatedScheduleTest(unittest.TestCase):
    """The rate is a function of WHEN, not only of which series."""

    def test_september_2026_resolves_to_the_halved_coefficient(self):
        entry = resolve_schedule("KXMLBGAME", SEPTEMBER)
        self.assertEqual(entry.multiplier, 0.5)
        self.assertAlmostEqual(entry.taker_coeff(), 0.035, places=12)
        self.assertEqual(entry.source_id, "38032af2-e3fa-4659-9280-da64300b544c")

    def test_before_the_change_resolves_to_the_earlier_entry(self):
        entry = resolve_schedule("KXMLBGAME", JULY)
        self.assertEqual(entry.multiplier, 1.0)
        self.assertAlmostEqual(entry.taker_coeff(), 0.07, places=12)

    def test_the_boundary_is_the_scheduled_timestamp_itself(self):
        """Inclusive at the instant, exclusive one microsecond before.

        The endpoint dates the change to the millisecond. A boundary that is
        off by one entry is off by a factor of two on every fee in the window.
        """
        self.assertEqual(resolve_schedule("KXMLBGAME", SWITCH_TS).multiplier, 0.5)
        self.assertEqual(
            resolve_schedule("KXMLBGAME",
                             SWITCH_TS - timedelta(microseconds=1)).multiplier,
            1.0)

    def test_a_later_entry_never_reprices_an_earlier_decision(self):
        """THE retroactivity guard.

        A schedule read six months from now will carry entries this study has
        never seen. Resolution is strictly `effective_from <= at`, so adding
        one cannot move the price of a decision made before it took force.
        """
        before = kalshi_fee(1000, 0.5, series="KXMLBGAME", at=SEPTEMBER).dollars
        original = KALSHI_SERIES_SCHEDULES["KXMLBGAME"]
        future = FeeScheduleEntry(
            effective_from=datetime(2026, 10, 1, tzinfo=timezone.utc),
            multiplier=0.25, fee_type="quadratic_with_maker_fees",
            source="test", source_id="future", observed_on="2027-01-01",
            observed_by="test",
        )
        KALSHI_SERIES_SCHEDULES["KXMLBGAME"] = original + (future,)
        try:
            self.assertEqual(
                kalshi_fee(1000, 0.5, series="KXMLBGAME", at=SEPTEMBER).dollars,
                before,
                "a schedule entry taking force in October repriced September")
            # ...and it DOES apply after it takes force, so this is a dated
            # resolution rather than a frozen one.
            october = datetime(2026, 10, 2, tzinfo=timezone.utc)
            self.assertLess(
                kalshi_fee(1000, 0.5, series="KXMLBGAME", at=october).dollars,
                before)
        finally:
            KALSHI_SERIES_SCHEDULES["KXMLBGAME"] = original

    def test_a_scheduled_series_without_a_timestamp_raises(self):
        """The multiplier has changed within reach of this study, so there is
        no series-only answer to give."""
        with self.assertRaises(FeeScheduleUnresolved):
            kalshi_fee(100, 0.5, series="KXMLBGAME")

    def test_a_naive_timestamp_raises(self):
        with self.assertRaises(FeeScheduleUnresolved):
            kalshi_fee(100, 0.5, series="KXMLBGAME", at=datetime(2026, 9, 8))

    def test_a_date_before_the_earliest_entry_raises(self):
        """The schedule then in force was not recorded. It is not the oldest
        one on file, and extrapolating backwards is the same defect as
        extrapolating forwards."""
        with self.assertRaises(FeeScheduleUnresolved):
            kalshi_fee(100, 0.5, series="KXMLBGAME",
                       at=datetime(2025, 1, 1, tzinfo=timezone.utc))

    def test_an_unscheduled_series_prices_generically_without_a_timestamp(self):
        """No schedule recorded means no dated question to answer."""
        self.assertIsNone(resolve_schedule("KXNOSUCH", None))
        self.assertEqual(kalshi_fee(1000, 0.5, series="KXNOSUCH").dollars,
                         kalshi_fee(1000, 0.5).dollars)

    def test_maker_on_a_scheduled_series_refuses_rather_than_guessing(self):
        """`quadratic_with_maker_fees` says makers are charged. It does not say
        what, and no source says whether the multiplier scales maker fees."""
        with self.assertRaises(FeeScheduleUnresolved):
            kalshi_fee(100, 0.5, "maker", series="KXMLBGAME", at=SEPTEMBER)

    def test_a_mid_window_change_is_detectable(self):
        straddling = schedule_changes_within(
            "KXMLBGAME",
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 31, tzinfo=timezone.utc))
        self.assertEqual([e.multiplier for e in straddling], [0.5])
        clear = schedule_changes_within(
            "KXMLBGAME", datetime(2026, 9, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 30, tzinfo=timezone.utc))
        self.assertEqual(clear, [])


class ScheduleConflictTest(unittest.TestCase):
    """The PDF and the API disagree for the window this study covers. The
    disagreement is carried, not adjudicated -- stamping one 'verified' is how
    an open question becomes a silent assumption."""

    def test_describe_carries_the_unresolved_conflict(self):
        text = describe("KXMLBGAME", SEPTEMBER)
        self.assertIn("UNRESOLVED CONFLICT", text)
        self.assertIn("PDF", text)
        self.assertTrue(SCHEDULE_CONFLICTS["KXMLBGAME"])

    def test_describe_names_the_resolved_entry_and_its_source(self):
        text = describe("KXMLBGAME", SEPTEMBER)
        self.assertIn("taker=0.035", text)
        self.assertIn("38032af2-e3fa-4659-9280-da64300b544c", text)
        self.assertIn("fee_changes", text)

    def test_describe_says_the_provenance_was_transcribed_not_reverified(self):
        self.assertIn("not re-read in-session", describe("KXMLBGAME", SEPTEMBER))

    def test_describe_never_raises_on_an_unresolvable_run(self):
        """A report must still print when the schedule cannot be resolved, and
        saying WHY is the useful output."""
        text = describe("KXMLBGAME", None)
        self.assertIn("UNRESOLVED", text)

    def test_describe_names_an_unscheduled_series(self):
        self.assertIn("NO recorded fee schedule", describe("KXOTHER", SEPTEMBER))


class RoundingScenarioTest(unittest.TestCase):
    """Worked examples derived from the recorded rules (see module docstring).

    Model fee ceils to $0.000001, then the charge aligns to the account's
    balance precision: $0.0001 direct, $0.01 non-direct.
    """

    def test_worked_example_one_contract_at_fifty_cents(self):
        """0.035 * 1 * 0.50 * 0.50 = $0.00875 raw."""
        direct = kalshi_fee(1, 0.50, series="KXMLBGAME", at=SEPTEMBER,
                            route="direct")
        non_direct = kalshi_fee(1, 0.50, series="KXMLBGAME", at=SEPTEMBER,
                                route="non_direct")
        self.assertAlmostEqual(direct.raw_dollars, 0.00875, places=12)
        self.assertEqual(direct.dollars, 0.0088)      # ceil to $0.0001
        self.assertEqual(non_direct.dollars, 0.01)    # ceil to $0.01
        # The gap is the whole point: +0.6% vs +14.3% over the raw charge.
        self.assertGreater(non_direct.dollars / direct.dollars, 1.13)

    def test_worked_example_one_contract_at_thirty_cents(self):
        """0.035 * 1 * 0.30 * 0.70 = $0.00735 raw."""
        self.assertEqual(
            kalshi_fee(1, 0.30, series="KXMLBGAME", at=SEPTEMBER,
                       route="direct").dollars, 0.0074)
        self.assertEqual(
            kalshi_fee(1, 0.30, series="KXMLBGAME", at=SEPTEMBER,
                       route="non_direct").dollars, 0.01)

    def test_worked_example_at_size_the_route_stops_mattering(self):
        """0.035 * 100 * 0.25 = $0.875 raw -- exact at $0.0001, 0.6% at $0.01.

        This is why the route matters HERE specifically: the study prices one
        contract at a time to get a per-contract rate, which is the worst case
        for a per-order quantum.

        It is also the float-noise case: in binary, that product is
        0.8750000000000001, and a ceiling turns the last bit into a whole
        extra quantum. Asserting the exact $0.8750 is what pins the Decimal
        computation -- a charge inflated by one ten-thousandth of a dollar
        would otherwise look like a rounding rule working correctly.
        """
        direct = kalshi_fee(100, 0.50, series="KXMLBGAME", at=SEPTEMBER,
                            route="direct").dollars
        non_direct = kalshi_fee(100, 0.50, series="KXMLBGAME", at=SEPTEMBER,
                                route="non_direct").dollars
        self.assertEqual(direct, 0.875)
        self.assertEqual(non_direct, 0.88)
        self.assertLess(non_direct / direct, 1.01)

    def test_both_recorded_routes_are_reported_not_chosen(self):
        """The route is unresolved and the module says so rather than picking.

        The default exists because a function needs one; it is the MORE
        expensive route, so an unexamined call is conservative rather than
        flattering, and `ROUTE_RESOLVED` stays false so every report has to
        say the question is open.
        """
        self.assertFalse(ROUTE_RESOLVED)
        self.assertEqual(DEFAULT_ROUTE, "non_direct")
        self.assertEqual(
            kalshi_fee(1, 0.50, series="KXMLBGAME", at=SEPTEMBER).dollars,
            kalshi_fee(1, 0.50, series="KXMLBGAME", at=SEPTEMBER,
                       route="non_direct").dollars,
            "the default must be the conservative route, not the cheap one")
        self.assertIn("route resolved: NO", describe("KXMLBGAME", SEPTEMBER))

    def test_the_model_precision_step_is_immaterial_on_both_known_routes(self):
        """A finding worth pinning, not a detail.

        The source's prose says centicents while its own example table shows
        cents, and that ambiguity sits at the MODEL step. It does not matter:
        $0.0001 and $0.01 are both whole multiples of $0.000001, so ceiling to
        the model precision first and then to the alignment gives exactly the
        alignment ceiling alone. The prose/table conflict is therefore confined
        to the ACCOUNT ROUTE question, which is the one reported both ways.
        """
        for raw in (0.0000004, 0.00875, 0.00735, 0.123456789, 0.875, 1.7500001):
            for route, quantum in ROUTE_ALIGNMENT.items():
                self.assertEqual(round_fee(raw, route), _ceil_to(raw, quantum),
                                 f"model step changed the charge at {raw} "
                                 f"on the {route} route")

    def test_the_model_precision_is_itself_a_ceiling(self):
        self.assertEqual(_ceil_to(0.0000001, MODEL_FEE_PRECISION), 0.000001)
        self.assertEqual(_ceil_to(0.000001, MODEL_FEE_PRECISION), 0.000001)

    def test_an_unknown_route_raises(self):
        with self.assertRaises(ValueError):
            round_fee(0.01, "omnibus")


class ReviewSensitivityTest(unittest.TestCase):
    """Reproduces the exact figures the PR review reported, from the model.

    The review's offline sensitivity check changed ONLY the MLB taker
    coefficient to 0.035, kept whole-cent rounding, and reported the smoke
    sample's best predicted net EV moving from -$0.014525 to -$0.004525 on a
    maximum pre-fee edge of $0.0055. Those numbers are arithmetic on one quote,
    so they can be reproduced here without the sample -- and reproducing them
    is what says this implementation matches the check that motivated it.
    """

    ASK = 0.51
    WIN_P = 0.515475        # gross edge 0.005475, the sample's best

    def _net_ev(self, fee: float) -> float:
        return self.WIN_P - self.ASK - fee

    def test_the_old_generic_coefficient_reproduces_minus_0_014525(self):
        fee = kalshi_fee(1, self.ASK).dollars           # generic 0.07, cents
        self.assertEqual(fee, 0.02)
        self.assertAlmostEqual(self._net_ev(fee), -0.014525, places=9)

    def test_the_dated_september_coefficient_reproduces_minus_0_004525(self):
        fee = kalshi_fee(1, self.ASK, series="KXMLBGAME", at=SEPTEMBER,
                         route="non_direct").dollars
        self.assertEqual(fee, 0.01)
        self.assertAlmostEqual(self._net_ev(fee), -0.004525, places=9)

    def test_the_direct_route_is_cheaper_and_still_negative(self):
        """The scenario the review could not price, and the reason to report
        both: it is the most favourable combination available, and the sample's
        best quote is STILL below zero under it."""
        fee = kalshi_fee(1, self.ASK, series="KXMLBGAME", at=SEPTEMBER,
                         route="direct").dollars
        self.assertEqual(fee, 0.0088)
        self.assertAlmostEqual(self._net_ev(fee), -0.003325, places=9)
        self.assertLess(self._net_ev(fee), 0.0)


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

    def test_fee_for_accepts_and_applies_a_series_and_a_date(self):
        dated = fee_for("kalshi", 1000, 0.5, "taker", "KXMLBGAME", SEPTEMBER)
        generic = fee_for("kalshi", 1000, 0.5, "taker")
        self.assertLess(dated.dollars, generic.dollars)
        self.assertEqual(dated.multiplier, 0.5)
        self.assertIsNotNone(dated.schedule)

    def test_unknown_series_falls_back_to_generic_not_an_error(self):
        self.assertEqual(
            fee_for("kalshi", 1000, 0.5, "taker", "KXOTHER", SEPTEMBER).dollars,
            fee_for("kalshi", 1000, 0.5, "taker").dollars)

    def test_fee_for_refuses_a_scheduled_series_without_a_date(self):
        with self.assertRaises(FeeScheduleUnresolved):
            fee_for("kalshi", 1000, 0.5, "taker", "KXMLBGAME")


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
