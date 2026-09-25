"""Scoring-instrument regressions.

Two defects here produced confident, wrong output, and both have a named
reproduction below. The instrument decides whether the whole strategy is worth
building, so it is driven against data whose right answer is known.
"""

import random
import unittest
from datetime import datetime, timedelta, timezone

from analysis.scoring import (
    Eligibility, MIN_GAMES, MIN_TRADED_GAMES, Observation, ScreenDiagnostics,
    ScreenRejection, as_trade, brier, build_report, cluster, compare,
    conditional_scores, decay_series, log_loss, realized_return,
    screen_diagnostics, side_quotes,
)
from core.fees import series_schedule
from data.kalshi_history import Coverage


# Timestamps are UTC-AWARE because the fee schedule is dated and resolving it
# against a naive timestamp means resolving it against whatever the reader
# assumes. The collector emits aware datetimes (`decision_cutoffs`), so a naive
# fixture would test a shape production never produces.
BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)


def obs(game, market, p_sharp, p_exch, outcome, when=None, bid=None, ask=None):
    return Observation(
        game_id=game, market_id=market,
        decision_at=when or BASE,
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
                BASE + timedelta(hours=3 * i))
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
                       BASE + timedelta(hours=3 * i)))
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
        self.assertIsNone(as_trade(Observation("E", "M", BASE,
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


class SeriesFeeTest(unittest.TestCase):
    """The return path must price with the study's own series schedule, AT THE
    DATE OF THE DECISION.

    Kalshi's KXMLBGAME multiplier halved on 2026-08-07. A study that prices by
    series alone gets one rate for the whole history; a study that prices at
    "now" reprices its own past every time it is re-run. Both produce a number,
    and the number is wrong by a factor of two on one side of that date.
    """

    JULY = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    SEPTEMBER = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)

    def _yes(self, o, **kw):
        return [q for q in side_quotes(o, series="KXMLBGAME", **kw)
                if q.side == "YES"][0]

    def test_side_quotes_price_at_the_decision_date_not_the_series(self):
        july = self._yes(obs("E", "M", 0.60, 0.50, 1, when=self.JULY,
                             bid=0.49, ask=0.51))
        september = self._yes(obs("E", "M", 0.60, 0.50, 1, when=self.SEPTEMBER,
                                  bid=0.49, ask=0.51))
        self.assertLess(september.fee, july.fee,
                        "the halved multiplier did not reach the pricing path")
        self.assertGreater(september.predicted_ev, july.predicted_ev)

    def test_the_identical_quote_differs_only_by_its_date(self):
        """Same price, same book, same series -- only `decision_at` moves."""
        july = self._yes(obs("E", "M", 0.60, 0.50, 1, when=self.JULY,
                             bid=0.49, ask=0.51))
        september = self._yes(obs("E", "M", 0.60, 0.50, 1, when=self.SEPTEMBER,
                                  bid=0.49, ask=0.51))
        self.assertEqual(july.entry_price, september.entry_price)
        self.assertEqual((july.fee, september.fee), (0.02, 0.01))

    def test_the_account_route_reaches_the_pricing_path(self):
        o = obs("E", "M", 0.60, 0.50, 1, when=self.SEPTEMBER, bid=0.49, ask=0.51)
        direct = self._yes(o, route="direct")
        non_direct = self._yes(o, route="non_direct")
        self.assertLess(direct.fee, non_direct.fee)
        self.assertEqual(direct.fee_route, "direct")
        # The pre-rounding charge is the same; only the alignment differs.
        self.assertEqual(direct.fee_raw, non_direct.fee_raw)

    def test_realized_return_threads_the_series_and_the_date(self):
        def sample(when):
            return [obs(f"E{i}", f"M{i}", 0.60, 0.50, i % 2, when=when,
                        bid=0.49, ask=0.51) for i in range(60)]

        september = realized_return(sample(self.SEPTEMBER),
                                    Eligibility(min_net_ev=0.0),
                                    bootstrap_rounds=100, series="KXMLBGAME")
        july = realized_return(sample(self.JULY), Eligibility(min_net_ev=0.0),
                               bootstrap_rounds=100, series="KXMLBGAME")
        self.assertGreater(september.mean_profit_per_contract,
                           july.mean_profit_per_contract)

    def test_a_decision_outside_the_recorded_schedule_refuses_to_price(self):
        """Proof the DATE is consulted, not just the series name.

        Before the earliest recorded entry there is no rate to apply, and
        borrowing the oldest one on file is the retroactive repricing this
        whole mechanism exists to prevent -- so it raises rather than
        returning a study-shaped number.
        """
        from core.fees import FeeScheduleUnresolved
        ancient = obs("E", "M", 0.60, 0.50, 1,
                      when=datetime(2025, 1, 1, tzinfo=timezone.utc),
                      bid=0.49, ask=0.51)
        with self.assertRaises(FeeScheduleUnresolved):
            side_quotes(ancient, series="KXMLBGAME")


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
    """These assert STRUCTURE -- labels, ordering, which gates exist -- so they
    run at a low bootstrap count. The interval widths are exercised by the
    tests that are actually about them; paying for 2000 rounds per route here
    only buys a slower suite."""

    ROUNDS = 100

    def test_incomplete_coverage_is_shouted_above_the_numbers(self):
        bad = Coverage().fail("odds quota exhausted mid-window")
        text = build_report(noisy(n=400), bad,
                             bootstrap_rounds=self.ROUNDS).render()
        self.assertIn("COVERAGE IS INCOMPLETE", text)
        self.assertLess(text.index("COVERAGE IS INCOMPLETE"),
                        text.index("[1] FORECAST ACCURACY"))

    def test_readiness_gates_on_return_and_coverage_only(self):
        """Global forecast accuracy is a DIAGNOSTIC. An earlier version made it
        a mandatory GO criterion while the premise section said it is neither
        sufficient nor necessary -- a contradiction."""
        report = build_report(noisy(n=400), Coverage(),
                              bootstrap_rounds=self.ROUNDS)
        names = [name for name, _, _ in report.readiness()]
        self.assertIn("positive net return on the frozen policy", names)
        self.assertIn("coverage complete", names)
        self.assertFalse(any("forecast" in n or "accuracy" in n for n in names),
                         "accuracy must not gate")

    def test_output_is_labelled_exploratory(self):
        """No holdout and no cost/delay robustness exist here, so no run of
        this code can be a GO."""
        report = build_report(noisy(n=400), Coverage(),
                              bootstrap_rounds=self.ROUNDS)
        self.assertTrue(report.is_exploratory())
        self.assertIn("EXPLORATORY", report.render())

    def test_nontraded_observations_do_not_satisfy_the_sample_gate(self):
        """A large unrelated universe must not satisfy the floor for a tiny
        trading subset."""
        traded = [obs(f"T{i}", f"T{i}", 0.60, 0.50, i % 2, bid=0.49, ask=0.51)
                  for i in range(40)]
        untraded = [obs(f"U{i}", f"U{i}", 0.51, 0.50, i % 2, bid=0.20, ask=0.80)
                    for i in range(900)]
        alone = build_report(traded, Coverage(), bootstrap_rounds=self.ROUNDS)
        padded = build_report(traded + untraded, Coverage(),
                              bootstrap_rounds=self.ROUNDS)
        gate = lambda r: [p for n, p, _ in r.readiness() if "sample" in n][0]
        self.assertEqual(alone.returns.games, padded.returns.games)
        self.assertEqual(gate(alone), gate(padded))
        self.assertGreater(padded.comparison.games, alone.comparison.games)

    def test_no_hit_rate_gate_remains(self):
        """The >50% rule is gone; nothing should reintroduce it."""
        text = build_report(noisy(n=400), Coverage(),
                             bootstrap_rounds=self.ROUNDS).render()
        self.assertNotIn("hit rate", text.lower().replace("not a hit rate", ""))

    def test_both_fee_routes_are_rendered_and_the_headline_is_named(self):
        """The account route is unresolved, so the report shows both.

        A single headline number priced at an unexamined default is exactly
        what "do not silently choose one" forbids -- and at one-contract size
        the route is most of the fee, not a rounding digit.
        """
        report = build_report(noisy(n=400), Coverage(), series="KXMLBGAME",
                              bootstrap_rounds=self.ROUNDS)
        text = report.render()
        self.assertIn("THE ACCOUNT ROUTE IS NOT RESOLVED", text)
        self.assertEqual({s.route for s in report.scenarios},
                         {"direct", "non_direct"})
        for route in ("direct", "non_direct"):
            self.assertIn(route, text)
        self.assertIn("<- headline", text)

    def test_the_headline_scenario_is_the_same_object_as_the_headline(self):
        """Not merely equal -- identical, so the table cannot drift from the
        sections above it (a second computation of one fact is rule 26)."""
        report = build_report(noisy(n=400), Coverage(), series="KXMLBGAME",
                              bootstrap_rounds=self.ROUNDS)
        headline = [s for s in report.scenarios if s.route == report.route][0]
        self.assertIs(headline.returns, report.returns)
        self.assertIs(headline.screen, report.screen)

    def test_readiness_reports_whether_the_route_would_change_the_verdict(self):
        """It does not decide which route is right; it says whether it matters."""
        report = build_report(noisy(n=400), Coverage(), series="KXMLBGAME",
                              bootstrap_rounds=self.ROUNDS)
        names = [name for name, _, _ in report.readiness()]
        self.assertIn("conclusion survives the unresolved account route", names)

    def test_a_cheaper_route_admits_at_least_as_many_trades(self):
        """Sanity on the direction: lower fees cannot reject a trade the
        higher-fee route admitted, at the same EV floor."""
        data = [obs(f"E{i}", f"M{i}", 0.60, 0.50, i % 2,
                    when=datetime(2026, 9, 8, tzinfo=timezone.utc),
                    bid=0.49, ask=0.51) for i in range(60)]
        report = build_report(data, Coverage(), series="KXMLBGAME",
                              eligibility=Eligibility(min_net_ev=0.085),
                              bootstrap_rounds=self.ROUNDS)
        by_route = {s.route: s for s in report.scenarios}
        self.assertGreaterEqual(by_route["direct"].screen.admitted,
                                by_route["non_direct"].screen.admitted)
        self.assertGreater(by_route["direct"].best_net_ev,
                           by_route["non_direct"].best_net_ev)

    def test_provenance_parts_survive_rendering_intact(self):
        """A provenance part containing a semicolon must not be torn in two.

        `render()` used to split the joined provenance on "; ", so
        "PR #27 review; transcribed, not re-read in-session" printed as two
        lines -- the second of which read as a provenance claim of its own.
        A string joined for one consumer is not a structure for another.
        """
        parts = ["source: an endpoint; read once", "CONFLICT: a PDF disagrees"]
        report = build_report(noisy(n=40), Coverage(), parts,
                              bootstrap_rounds=self.ROUNDS)
        self.assertEqual(report.provenance_lines, parts)
        text = report.render()
        self.assertIn("source: an endpoint; read once", text)

    def test_a_plain_string_provenance_still_renders(self):
        """The parameter takes either; a bare string must not become a list of
        characters."""
        report = build_report(noisy(n=40), Coverage(), "one line of stamps",
                              bootstrap_rounds=self.ROUNDS)
        self.assertEqual(report.provenance_lines, ["one line of stamps"])
        self.assertIn("one line of stamps", report.render())

    def test_return_section_states_its_own_limits(self):
        text = build_report(noisy(n=400), Coverage(),
                             bootstrap_rounds=self.ROUNDS).render()
        self.assertIn("no depth", text)
        self.assertIn("NOT included", text)


# --- the screen's named verdict against the screen it replaced ---------------
#
# FROZEN COPIES of `Eligibility.admits` and `screen_diagnostics` exactly as
# they stood before `Eligibility.verdict` existed. They are the oracle: the
# refactor that named each rejection is only admissible if it changed no
# admission, no count, and no exception -- the checkpoint result has to stay
# reproducible.

def _legacy_admits(el, o, venue="kalshi", role="taker", series=None,
                   route="non_direct"):
    if not el.price_band[0] <= o.p_exchange <= el.price_band[1]:
        return False
    if not el.min_minutes_to_start <= o.minutes_to_start <= el.max_minutes_to_start:
        return False
    if o.exchange_bid is None or o.exchange_ask is None:
        return False
    if o.exchange_ask - o.exchange_bid > el.max_spread:
        return False
    return as_trade(o, venue, role, el, series, route) is not None


def _legacy_screen_diagnostics(observations, el, venue="kalshi",
                               role="taker", series=None, route="non_direct"):
    d = ScreenDiagnostics(considered=len(observations))
    for o in observations:
        if not el.price_band[0] <= o.p_exchange <= el.price_band[1]:
            d.rejected_price_band += 1
            continue
        if not (el.min_minutes_to_start <= o.minutes_to_start
                <= el.max_minutes_to_start):
            d.rejected_lead_time += 1
            continue
        if o.exchange_bid is None or o.exchange_ask is None:
            d.rejected_no_quotes += 1
            continue
        if o.exchange_ask - o.exchange_bid > el.max_spread:
            d.rejected_spread += 1
            continue
        quotes = side_quotes(o, venue, role, series, route)
        if not quotes:
            d.rejected_no_quotes += 1
            continue
        best = max(quotes, key=lambda q: q.predicted_ev)
        d.best_net_ev.append(best.predicted_ev)
        d.best_gross_edge.append(best.win_probability - best.entry_price)
        if best.predicted_ev < el.min_net_ev:
            d.rejected_below_ev += 1
        else:
            d.admitted += 1
    return d


def _grid(seed=11, n=4000):
    """Observations spread across every gate's boundary, both sides of it."""
    rng = random.Random(seed)
    edges_p = [0.10, 0.149999, 0.15, 0.5, 0.85, 0.850001, 0.95]
    edges_m = [0.0, 4.999, 5.0, 60.0, 4380.0, 4380.001, 9000.0]
    spreads = [0.0, 0.01, 0.0999999, 0.10, 0.1000001, 0.3, -0.02]
    when = [datetime(2025, 1, 1, tzinfo=timezone.utc),       # before MLB's schedule
            datetime(2026, 7, 1, tzinfo=timezone.utc),
            datetime(2026, 9, 24, 17, tzinfo=timezone.utc)]
    out = []
    for i in range(n):
        mid = rng.choice(edges_p) if rng.random() < 0.4 else rng.uniform(0.02, 0.98)
        spread = rng.choice(spreads)
        bid, ask = round(mid - spread / 2, 6), round(mid + spread / 2, 6)
        roll = rng.random()
        if roll < 0.04:
            bid = None
        elif roll < 0.08:
            ask = None
        elif roll < 0.10:
            bid, ask = 0.0, 1.0
        delay = rng.choice([0.0, 0.0, 2.0])
        entry_bid = entry_ask = None
        if delay:
            shape = rng.random()
            if shape < 0.6:
                entry_bid, entry_ask = bid, ask
                if bid is not None and ask is not None:
                    shift = rng.uniform(-0.05, 0.05)
                    entry_bid, entry_ask = bid + shift, ask + shift
            elif shape < 0.75:
                entry_bid, entry_ask = 0.62, 0.55      # crossed at execution
        out.append(Observation(
            game_id=f"G{i}", market_id=f"M{i}",
            decision_at=rng.choice(when),
            minutes_to_start=(rng.choice(edges_m) if rng.random() < 0.4
                              else rng.uniform(0.0, 6000.0)),
            p_sharp=rng.uniform(0.01, 0.99), p_exchange=mid,
            outcome=rng.randint(0, 1), exchange_bid=bid, exchange_ask=ask,
            entry_delay_minutes=delay, entry_bid=entry_bid,
            entry_ask=entry_ask))
    # NaN in each field a gate reads: every comparison with it is False,
    # so a gate written as its own negation decides differently.
    nan = float("nan")
    for i, change in enumerate((dict(exchange_ask=nan), dict(exchange_bid=nan),
                                dict(p_exchange=nan), dict(minutes_to_start=nan),
                                dict(p_sharp=nan))):
        fields = dict(game_id=f"N{i}", market_id=f"N{i}", decision_at=when[2],
                      minutes_to_start=600.0, p_sharp=0.7, p_exchange=0.5,
                      outcome=0, exchange_bid=0.49, exchange_ask=0.51)
        fields.update(change)
        out.append(Observation(**fields))
    return out


def _nan_equal(fields):
    """NaN compares unequal to itself; the grid's NaN rows must still match."""
    return {k: ([("nan" if x != x else x) for x in v] if isinstance(v, list)
                else v) for k, v in fields.items()}


def _outcome(call):
    try:
        return ("value", call())
    except Exception as exc:                       # noqa: BLE001 -- compared
        return ("raises", type(exc).__name__)


class VerdictEquivalenceTest(unittest.TestCase):
    """Naming a rejection changed no decision the screen makes."""

    POLICIES = (Eligibility(), Eligibility(min_net_ev=0.0),
                Eligibility(min_net_ev=-0.05, max_spread=0.2),
                Eligibility(price_band=(0.3, 0.7), max_minutes_to_start=600.0))

    def test_admission_and_exceptions_match_the_screen_it_replaced(self):
        for policy in self.POLICIES:
            for series in (None, "KXNFLGAME", "KXMLBGAME"):
                for route in ("direct", "non_direct"):
                    for o in _grid(n=600):
                        with self.subTest(policy=policy, series=series,
                                          route=route, market=o.market_id):
                            self.assertEqual(
                                _outcome(lambda: policy.admits(
                                    o, series=series, route=route)),
                                _outcome(lambda: _legacy_admits(
                                    policy, o, series=series, route=route)))

    def test_the_diagnostic_counts_match_the_screen_they_replaced(self):
        grid = _grid(n=4000)
        # A series with no dated schedule prices every decision at the
        # generic rate. A DATED one refuses a decision before its first
        # entry -- KXNFLGAME's starts 2026-01-01, after the grid's 2025
        # decisions -- so it is compared over the decisions it can price.
        priced = {None: grid, "KXNCAAFGAME": grid}
        first = min(e.effective_from for e in series_schedule("KXNFLGAME"))
        priced["KXNFLGAME"] = [o for o in grid if o.decision_at >= first]
        for policy in self.POLICIES:
            for series, observations in priced.items():
                with self.subTest(policy=policy, series=series):
                    new = screen_diagnostics(observations, policy,
                                             series=series)
                    old = _legacy_screen_diagnostics(observations, policy,
                                                     series=series)
                    self.assertEqual(_nan_equal(vars(new)),
                                     _nan_equal(vars(old)))
                    self.assertGreater(new.admitted, 0)
                    self.assertGreater(new.rejected_below_ev, 0)

    def test_every_rejection_is_the_first_gate_that_refused(self):
        el = Eligibility()
        base = dict(game_id="G", market_id="M",
                    decision_at=datetime(2026, 9, 24, 17, tzinfo=timezone.utc),
                    minutes_to_start=600.0, p_sharp=0.62, p_exchange=0.50,
                    outcome=0, exchange_bid=0.49, exchange_ask=0.51)

        def verdict(**changes):
            return el.verdict(Observation(**{**base, **changes}))

        self.assertIsNone(verdict().rejection)
        self.assertTrue(verdict().admitted)
        cases = {
            ScreenRejection.PRICE_BAND: dict(p_exchange=0.90,
                                             minutes_to_start=9000.0),
            ScreenRejection.LEAD_TIME: dict(minutes_to_start=4380.5),
            ScreenRejection.NO_DECISION_QUOTES: dict(exchange_ask=None),
            ScreenRejection.SPREAD_TOO_WIDE: dict(exchange_bid=0.40,
                                                  exchange_ask=0.60),
            ScreenRejection.NO_EXECUTABLE_SIDE: dict(exchange_bid=0.52,
                                                     exchange_ask=0.51),
            ScreenRejection.BELOW_EV_FLOOR: dict(p_sharp=0.52),
        }
        for rejection, changes in cases.items():
            with self.subTest(rejection=rejection):
                result = verdict(**changes)
                self.assertIs(result.rejection, rejection)
                self.assertFalse(result.admitted)
        # A refused trade keeps its priced sides once the book gates passed.
        below = verdict(p_sharp=0.52)
        self.assertEqual({q.side for q in below.quotes}, {"YES", "NO"})
        self.assertEqual(below.best.side, "YES")
        self.assertEqual(verdict(p_exchange=0.90).quotes, ())


if __name__ == "__main__":
    unittest.main()
