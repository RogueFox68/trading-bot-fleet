"""Multi-day lead-grid regressions.

The study began with ONE checkpoint 60 minutes before start. That measures the
late pre-game market and nothing else, and the thesis it was built to test --
that injuries, scratches and suspensions reprice a game over DAYS -- lives
almost entirely outside that window. A single late snapshot cannot reject that
thesis, because it was never looking where the effect would be.

Every test here pins one way the widening could have gone wrong QUIETLY, which
is the only way that matters: each of these failures produces a plausible
report rather than an error.

    >24h eligibility        a 24h ceiling silently drops every 72/48h row and
                            the diagnostics read it as absent opportunity
    preceding-window fetch  the inputs for a 72h checkpoint predate the study
                            window; fetching them must not widen the universe
    not-yet-listed          a contract that did not exist yet is a RESULT, not
                            a gap, and must not be counted as a failure
    missing checkpoints     a cell with no data stays in the denominator, with
                            the reason it failed, or coverage measures itself
    delayed entry           a signal acted on late enters at the later price,
                            and a missing late quote is NOT a fill at the
                            price you saw
    repeated-game exposure  seven checkpoints on one game are seven looks at
                            ONE outcome, not seven independent bets
"""

import unittest
from datetime import datetime, timedelta, timezone

from analysis.scoring import (
    DEFAULT_MAX_MINUTES_TO_START, Eligibility, EntryPolicy, Observation,
    build_report, checkpoint_slices, price_paths, realized_return,
    select_entries,
)
from collect import (
    BASELINE_LEAD_MINUTES, Checkpoint, CheckpointMatrix, Ledger,
    STATUS_LISTING_UNKNOWN, STATUS_NOT_YET_LISTED, STATUS_OBSERVED,
    checkpoint_targets, decision_cutoffs, default_lead_grid, grid_reach,
    in_study_window, listing_status, observation_with_status, parse_lead_grid,
)
from data.kalshi_history import Coverage
import run_study

from tests.test_collect import Candle, GAME1, event_ticker, market, milbal, quote

UTC = timezone.utc


def joined_market(start=GAME1, participant="MIL"):
    from collect import JoinedMarket
    ev = event_ticker(start)
    return JoinedMarket(f"{ev}-{participant}", ev, participant, start, 1,
                        "evt-1", None)


def obs(game, market_id, checkpoint_minutes, start=GAME1, p_sharp=0.60,
        p_exch=0.50, outcome=1, bid=0.49, ask=0.51):
    decision_at = start - timedelta(minutes=checkpoint_minutes)
    return Observation(
        game_id=game, market_id=market_id, decision_at=decision_at,
        minutes_to_start=float(checkpoint_minutes), p_sharp=p_sharp,
        p_exchange=p_exch, outcome=outcome, exchange_bid=bid, exchange_ask=ask,
        checkpoint_minutes=float(checkpoint_minutes),
    )


class LeadGridTest(unittest.TestCase):
    def test_the_default_grid_spans_three_days_and_keeps_the_baseline(self):
        grid = default_lead_grid()
        self.assertEqual([c.minutes for c in grid],
                         [4320, 2880, 1440, 720, 360, 180, 60])
        self.assertEqual(grid_reach(grid), timedelta(hours=72))
        self.assertTrue(grid[-1].baseline, "the 60-minute point must stay labelled")

    def test_the_baseline_rides_along_even_when_not_named(self):
        """The late window is the only one with data already inspected. A run
        that dropped it would have nothing to compare the fresh grid against."""
        grid = parse_lead_grid("72h,24h")
        self.assertIn(BASELINE_LEAD_MINUTES, [c.minutes for c in grid])
        self.assertTrue([c for c in grid if c.minutes == 60][0].baseline)

    def test_leads_are_ordered_earliest_first(self):
        """`select_entries` takes the FIRST qualifying checkpoint, so this
        order is load-bearing, not cosmetic."""
        grid = parse_lead_grid("3h,72h,24h")
        self.assertEqual([c.minutes for c in grid], sorted(
            [c.minutes for c in grid], reverse=True))

    def test_cutoffs_deduplicate_across_games_and_checkpoints(self):
        """Two games at the same minute share all seven cutoffs."""
        starts = [GAME1, GAME1]
        self.assertEqual(len(decision_cutoffs(starts, default_lead_grid())), 7)

    def test_a_bare_number_is_still_a_single_checkpoint(self):
        self.assertEqual(len(decision_cutoffs([GAME1, GAME1], 60)), 1)

    def test_an_unparseable_lead_raises_rather_than_being_dropped(self):
        for spec in ("", "abc", "0h", "-3h"):
            with self.assertRaises(ValueError):
                parse_lead_grid(spec)


class EligibilityCeilingTest(unittest.TestCase):
    """THE silent filter. A 24h ceiling over a 72h grid fetches the early
    observations and then discards them for being early, and the screen
    diagnostics report that as 'rejected: lead time' -- which reads like the
    market having no opportunity that far out."""

    def test_the_old_ceiling_would_have_dropped_every_early_row(self):
        early = obs("G", "M", 4320)
        old = Eligibility(min_net_ev=0.0, max_minutes_to_start=24 * 60.0)
        self.assertFalse(old.admits(early))

    def test_the_default_ceiling_admits_the_whole_default_grid(self):
        elig = Eligibility(min_net_ev=0.0)
        self.assertGreaterEqual(elig.max_minutes_to_start,
                                max(c.minutes for c in default_lead_grid()))
        for checkpoint in default_lead_grid():
            self.assertTrue(elig.admits(obs("G", "M", checkpoint.minutes)),
                            f"{checkpoint.label} was excluded by the ceiling")

    def test_a_ceiling_below_the_grid_is_refused_not_applied(self):
        args = run_study.parse_args(
            ["--from", "2026-09-01", "--to", "2026-09-02",
             "--lead-grid", "72h", "--max-lead-minutes", "1440"])
        with self.assertRaises(ValueError) as caught:
            run_study.eligibility_for(args, run_study.lead_grid_for(args))
        self.assertIn("below the earliest checkpoint", str(caught.exception))

    def test_the_derived_ceiling_follows_a_widened_grid(self):
        args = run_study.parse_args(
            ["--from", "2026-09-01", "--to", "2026-09-02", "--lead-grid", "168h"])
        elig = run_study.eligibility_for(args, run_study.lead_grid_for(args))
        self.assertGreaterEqual(elig.max_minutes_to_start, 168 * 60)

    def test_single_checkpoint_mode_still_works_for_the_baseline(self):
        args = run_study.parse_args(
            ["--from", "2026-09-01", "--to", "2026-09-02", "--lead-minutes", "60"])
        grid = run_study.lead_grid_for(args)
        self.assertEqual([c.minutes for c in grid], [60.0])
        self.assertTrue(grid[0].baseline)


class PrecedingWindowFetchTest(unittest.TestCase):
    """Inputs reach back before the window; the UNIVERSE does not."""

    START = datetime(2026, 9, 1, tzinfo=UTC)
    END = datetime(2026, 9, 3, tzinfo=UTC)

    def test_cutoffs_precede_the_study_window(self):
        first_game = datetime(2026, 9, 1, 23, 0, tzinfo=UTC)
        cutoffs = decision_cutoffs([first_game], default_lead_grid())
        self.assertTrue(min(cutoffs) < self.START,
                        "a 72h checkpoint must reach into the preceding days")
        self.assertGreaterEqual(self.START - min(cutoffs), timedelta(hours=48))

    def test_an_earlier_game_is_still_excluded_from_the_universe(self):
        """Fetching Aug 29's snapshot is not the same as studying Aug 29's
        games. The universe filter keys on the GAME's start, not on which
        snapshots were bought."""
        earlier = datetime(2026, 8, 29, 23, 0, tzinfo=UTC)
        m = market("KX-EARLY", settled=earlier + timedelta(hours=3))
        self.assertFalse(in_study_window(m, self.START, self.END))

    def test_targets_cover_every_contract_times_every_checkpoint(self):
        markets = milbal()
        targets = checkpoint_targets(markets, default_lead_grid())
        self.assertEqual(len(targets), len(markets) * 7)
        self.assertEqual(len({t.checkpoint.minutes for t in targets}), 7)


class NotYetListedTest(unittest.TestCase):
    """A contract that did not exist is a RESULT, not a gap."""

    def test_a_checkpoint_before_listing_is_not_yet_listed(self):
        m = market("KX-A")
        m["open_time"] = "2026-07-03T12:00:00Z"        # one day before GAME1
        self.assertEqual(listing_status(m, GAME1 - timedelta(hours=72)),
                         STATUS_NOT_YET_LISTED)
        self.assertIsNone(listing_status(m, GAME1 - timedelta(hours=3)))

    def test_an_unreadable_listing_time_is_its_own_bucket(self):
        """Not evidence the market was listed (rule 17: a failed read is not
        a zero), and not a confirmed absence either."""
        self.assertEqual(listing_status({}, GAME1), STATUS_LISTING_UNKNOWN)

    def test_not_listed_does_not_count_as_a_source_failure(self):
        matrix = CheckpointMatrix()
        cp = Checkpoint(4320)
        matrix.expect(checkpoint_targets(milbal(), [cp]))
        for ticker in milbal():
            matrix.record(ticker, cp, STATUS_NOT_YET_LISTED)
        self.assertEqual(matrix.source_failures(), 0)
        self.assertEqual(matrix.by_checkpoint()["4320"]["not_listed"], 2)

    def test_an_empty_matrix_says_it_is_blind_not_clean(self):
        """A header and an explanation over an EMPTY table reads as 'nothing
        went wrong here'. It is confident formatting over an unasked question
        -- the same shape as reporting a tuple's arity as a bar count."""
        text = CheckpointMatrix().render()
        self.assertIn("NO CELLS", text)
        self.assertIn("blind rather than clean", text)

    def test_a_populated_matrix_renders_a_row_per_checkpoint(self):
        matrix = CheckpointMatrix()
        matrix.expect(checkpoint_targets(milbal(), default_lead_grid()))
        lines = matrix.render().splitlines()
        for checkpoint in default_lead_grid():
            self.assertTrue(any(checkpoint.label in ln for ln in lines),
                            f"{checkpoint.label} missing from the table")

    def test_a_malformed_price_does_count_as_a_source_failure(self):
        matrix = CheckpointMatrix()
        cp = Checkpoint(180)
        matrix.expect(checkpoint_targets(milbal(), [cp]))
        for ticker in milbal():
            matrix.record(ticker, cp, "exchange_quote_malformed")
        self.assertEqual(matrix.source_failures(), 2)

    def test_an_unknown_status_is_treated_as_a_failure_not_ignored(self):
        """A status nobody classified must not quietly land in a benign
        bucket -- that is how a new failure mode becomes invisible."""
        matrix = CheckpointMatrix()
        cp = Checkpoint(180)
        matrix.expect(checkpoint_targets(milbal(), [cp]))
        matrix.record(list(milbal())[0], cp, "some_new_reason")
        self.assertEqual(matrix.source_failures(), 1)
        # ...and the cell nobody resolved is counted apart from it. Before a
        # run every cell is unreported; folding the two together would make
        # the preflight report the whole universe as broken.
        self.assertEqual(matrix.unreported(), 1)


class MissingCheckpointTest(unittest.TestCase):
    """An empty cell keeps its denominator AND its reason."""

    def test_every_expected_cell_is_present_before_any_fetch(self):
        matrix = CheckpointMatrix()
        matrix.expect(checkpoint_targets(milbal(), default_lead_grid()))
        self.assertEqual(len(matrix.statuses), 14)
        self.assertEqual(matrix.observed(), 0)
        self.assertEqual(matrix.by_checkpoint()["4320"]["unreported"], 2)

    def test_a_cell_with_no_exchange_quote_records_why(self):
        cutoff = GAME1 - timedelta(hours=24)
        obs_row, status = observation_with_status(
            joined_market(), [quote("evt-1", GAME1,
                                    snapshot=cutoff - timedelta(seconds=30),
                                    last_update=cutoff - timedelta(minutes=2))],
            [], cutoff, "MLB", Ledger(), checkpoint=Checkpoint(1440))
        self.assertIsNone(obs_row)
        self.assertEqual(status, "no_exchange_quote_at_cutoff")

    def test_a_cell_with_no_sharp_quote_records_why(self):
        cutoff = GAME1 - timedelta(hours=24)
        obs_row, status = observation_with_status(
            joined_market(), [], [Candle(cutoff - timedelta(minutes=1), 0.49, 0.51)],
            cutoff, "MLB", Ledger(), checkpoint=Checkpoint(1440))
        self.assertIsNone(obs_row)
        self.assertEqual(status, "no_sharp_quote_available_at_cutoff")

    def test_a_successful_cell_reports_observed_and_its_checkpoint(self):
        cutoff = GAME1 - timedelta(hours=24)
        obs_row, status = observation_with_status(
            joined_market(),
            [quote("evt-1", GAME1, snapshot=cutoff - timedelta(seconds=30),
                   last_update=cutoff - timedelta(minutes=2))],
            [Candle(cutoff - timedelta(minutes=1), 0.49, 0.51)],
            cutoff, "MLB", Ledger(), checkpoint=Checkpoint(1440))
        self.assertEqual(status, STATUS_OBSERVED)
        self.assertEqual(obs_row.checkpoint_minutes, 1440)
        self.assertEqual(obs_row.checkpoint_label, "24h")

    def test_the_two_entry_points_cannot_disagree(self):
        """`observation_at_cutoff` is a VIEW of `observation_with_status`.
        Two implementations would drift, and they would drift in exactly the
        run where the matrix was the thing you were reading."""
        from collect import observation_at_cutoff
        cutoff = GAME1 - timedelta(hours=24)
        args = (joined_market(),
                [quote("evt-1", GAME1, snapshot=cutoff - timedelta(seconds=30),
                       last_update=cutoff - timedelta(minutes=2))],
                [Candle(cutoff - timedelta(minutes=1), 0.49, 0.51)],
                cutoff, "MLB", Ledger())
        plain = observation_at_cutoff(*args)
        rich, status = observation_with_status(*args)
        self.assertEqual(status, STATUS_OBSERVED)
        self.assertEqual(plain.market_id, rich.market_id)
        self.assertEqual(plain.p_exchange, rich.p_exchange)


class DelayedEntryTest(unittest.TestCase):
    """A signal acted on late gets the LATER price, or no fill at all."""

    CUTOFF = GAME1 - timedelta(hours=24)

    def _quotes(self):
        return [quote("evt-1", GAME1,
                      snapshot=self.CUTOFF - timedelta(seconds=30),
                      last_update=self.CUTOFF - timedelta(minutes=2))]

    def test_zero_delay_enters_at_the_observation_candle(self):
        """The instantaneous bound, preserved bit for bit so the baseline
        stays comparable."""
        candles = [Candle(self.CUTOFF - timedelta(minutes=1), 0.49, 0.51),
                   Candle(self.CUTOFF + timedelta(minutes=10), 0.60, 0.62)]
        row, status = observation_with_status(
            joined_market(), self._quotes(), candles, self.CUTOFF, "MLB",
            Ledger(), checkpoint=Checkpoint(1440))
        self.assertEqual(status, STATUS_OBSERVED)
        self.assertEqual((row.exchange_bid, row.exchange_ask), (0.49, 0.51))
        self.assertEqual(row.entry_delay_minutes, 0.0)

    def test_a_delay_enters_at_the_first_quote_after_the_delay(self):
        candles = [Candle(self.CUTOFF - timedelta(minutes=1), 0.49, 0.51),
                   Candle(self.CUTOFF + timedelta(minutes=10), 0.60, 0.62)]
        row, status = observation_with_status(
            joined_market(), self._quotes(), candles, self.CUTOFF, "MLB",
            Ledger(), checkpoint=Checkpoint(1440),
            entry_delay=timedelta(minutes=10))
        self.assertEqual(status, STATUS_OBSERVED)
        self.assertEqual((row.exchange_bid, row.exchange_ask), (0.60, 0.62))
        # The OBSERVED price is still the one the signal was compared against.
        self.assertAlmostEqual(row.p_exchange, 0.50, places=9)
        self.assertEqual(row.entry_at, self.CUTOFF + timedelta(minutes=10))

    def test_a_missing_delayed_quote_is_not_a_fill_at_the_observed_price(self):
        """THE substitution this parameter exists to prevent. Falling back to
        the price you saw reports a fill that could not have happened, at
        exactly the moment the market had moved away from you."""
        candles = [Candle(self.CUTOFF - timedelta(minutes=1), 0.49, 0.51)]
        row, status = observation_with_status(
            joined_market(), self._quotes(), candles, self.CUTOFF, "MLB",
            Ledger(), checkpoint=Checkpoint(1440),
            entry_delay=timedelta(minutes=10))
        self.assertIsNone(row)
        self.assertEqual(status, "no_entry_quote_after_delay")

    def test_a_quote_far_past_the_delay_is_a_different_moment(self):
        """Within tolerance it is a late version of the same instant; beyond
        it, reaching for whatever came next is silent substitution again."""
        candles = [Candle(self.CUTOFF - timedelta(minutes=1), 0.49, 0.51),
                   Candle(self.CUTOFF + timedelta(hours=5), 0.60, 0.62)]
        row, status = observation_with_status(
            joined_market(), self._quotes(), candles, self.CUTOFF, "MLB",
            Ledger(), checkpoint=Checkpoint(1440),
            entry_delay=timedelta(minutes=10),
            entry_tolerance=timedelta(minutes=15))
        self.assertIsNone(row)
        self.assertEqual(status, "no_entry_quote_after_delay")

    def test_an_entry_after_start_is_refused(self):
        """Entries stay strictly pre-game, whatever the delay does."""
        cutoff = GAME1 - timedelta(minutes=5)
        candles = [Candle(cutoff - timedelta(minutes=1), 0.49, 0.51),
                   Candle(GAME1 + timedelta(minutes=5), 0.60, 0.62)]
        row, status = observation_with_status(
            joined_market(),
            [quote("evt-1", GAME1, snapshot=cutoff - timedelta(seconds=30),
                   last_update=cutoff - timedelta(minutes=2))],
            candles, cutoff, "MLB", Ledger(), checkpoint=Checkpoint(5),
            entry_delay=timedelta(minutes=10))
        self.assertIsNone(row)
        self.assertEqual(status, "no_entry_quote_after_delay")


class RepeatedGameExposureTest(unittest.TestCase):
    """Seven looks at one game are not seven bets."""

    def _seven_looks(self, game="G1", market_id="M1"):
        return [obs(game, market_id, c.minutes) for c in default_lead_grid()]

    def test_one_game_yields_at_most_one_entry(self):
        entries, diag = select_entries(self._seven_looks(),
                                       Eligibility(min_net_ev=0.0))
        self.assertEqual(len(entries), 1)
        self.assertEqual(diag.considered, 7)
        self.assertEqual(diag.skipped_game_already_entered, 6)

    def test_the_entry_is_the_EARLIEST_qualifying_checkpoint(self):
        """Chronological, because the 72h decision is made without knowing
        what the 24h one will look like."""
        entries, _ = select_entries(self._seven_looks(),
                                    Eligibility(min_net_ev=0.0))
        self.assertEqual(entries[0].checkpoint_label, "72h")

    def test_an_early_look_that_fails_the_screen_defers_to_a_later_one(self):
        looks = [obs("G1", "M1", 4320, p_sharp=0.50),   # no edge yet
                 obs("G1", "M1", 1440, p_sharp=0.60)]   # edge appears
        entries, _ = select_entries(looks, Eligibility(min_net_ev=0.05))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].checkpoint_label, "24h")

    def test_both_contracts_of_one_game_cannot_both_be_entered(self):
        """Mutually exclusive outcomes deduplicate through the game cap: they
        share a game_id, so taking one closes the game to the other."""
        looks = [obs("G1", "M1-MIL", 1440, p_sharp=0.60),
                 obs("G1", "M1-BAL", 1440, p_sharp=0.60, outcome=0)]
        entries, _ = select_entries(looks, Eligibility(min_net_ev=0.0))
        self.assertEqual(len(entries), 1)

    def test_separate_games_each_get_their_own_entry(self):
        looks = self._seven_looks("G1", "M1") + self._seven_looks("G2", "M2")
        entries, diag = select_entries(looks, Eligibility(min_net_ev=0.0))
        self.assertEqual(len(entries), 2)
        self.assertEqual(diag.games_entered, 2)

    def test_the_return_is_computed_on_the_policy_not_on_every_qualifying_row(self):
        """Without the policy, one game's seven qualifying looks each book a
        profit -- the same outcome counted seven times, with a bootstrap that
        thinks it has seven times the evidence."""
        looks = self._seven_looks("G1", "M1") + self._seven_looks("G2", "M2")
        elig = Eligibility(min_net_ev=0.0)
        unpoliced = realized_return(looks, elig, bootstrap_rounds=50)
        policed = realized_return(looks, elig, bootstrap_rounds=50,
                                  policy=EntryPolicy())
        self.assertEqual(unpoliced.trades, 14)
        self.assertEqual(policed.trades, 2)
        self.assertEqual(policed.games, 2)

    def test_a_higher_cap_is_honoured_when_predeclared(self):
        entries, _ = select_entries(self._seven_looks(),
                                    Eligibility(min_net_ev=0.0),
                                    policy=EntryPolicy(max_entries_per_game=3))
        self.assertEqual(len(entries), 3)


class CheckpointDiagnosticsTest(unittest.TestCase):
    def _grid_looks(self):
        out = []
        for i in range(4):
            for c in default_lead_grid():
                out.append(obs(f"G{i}", f"M{i}", c.minutes))
        return out

    def test_slices_are_ordered_earliest_lead_first(self):
        slices = checkpoint_slices(self._grid_looks(), Eligibility(min_net_ev=0.0))
        self.assertEqual([s.label for s in slices],
                         ["72h", "48h", "24h", "12h", "6h", "3h", "1h"])

    def test_the_baseline_slice_is_marked(self):
        slices = checkpoint_slices(self._grid_looks(), Eligibility(min_net_ev=0.0),
                                   baseline_minutes=BASELINE_LEAD_MINUTES)
        self.assertTrue([s for s in slices if s.minutes == 60][0].baseline)
        self.assertFalse([s for s in slices if s.minutes == 4320][0].baseline)

    def test_each_slice_counts_games_not_rows(self):
        slices = checkpoint_slices(self._grid_looks(), Eligibility(min_net_ev=0.0))
        for s in slices:
            self.assertEqual(s.games, 4)
            self.assertEqual(s.observations, 4)

    def test_price_paths_need_more_than_one_checkpoint(self):
        self.assertEqual(price_paths([obs("G", "M", 60)]), [])
        paths = price_paths(self._grid_looks())
        self.assertEqual(len(paths), 4)
        self.assertEqual(len(paths[0].points), 7)

    def test_a_path_reports_divergence_between_the_two_sources(self):
        looks = [obs("G", "M", 4320, p_sharp=0.50, p_exch=0.50),
                 obs("G", "M", 180, p_sharp=0.70, p_exch=0.55)]
        path = price_paths(looks)[0]
        self.assertAlmostEqual(path.sharp_move, 0.20, places=9)
        self.assertAlmostEqual(path.exchange_move, 0.05, places=9)
        self.assertAlmostEqual(path.divergence, 0.15, places=9)


class ReportLayoutTest(unittest.TestCase):
    """The report must SHOW the new structure, not just compute it."""

    def _report(self):
        looks = []
        for i in range(6):
            for c in default_lead_grid():
                looks.append(obs(f"G{i}", f"M{i}", c.minutes, outcome=i % 2))
        return build_report(looks, Coverage(), "fees: test",
                            eligibility=Eligibility(min_net_ev=0.0),
                            bootstrap_rounds=50, policy=EntryPolicy(),
                            baseline_minutes=BASELINE_LEAD_MINUTES,
                            checkpoint_coverage="GAME x CHECKPOINT COVERAGE\n  (stub)")

    def test_the_report_breaks_results_out_by_lead_time(self):
        text = self._report().render()
        self.assertIn("BY LEAD TIME", text)
        for label in ("72h", "48h", "24h", "12h", "6h", "3h"):
            self.assertIn(label, text)

    def test_the_report_warns_against_summing_the_lead_rows(self):
        text = self._report().render()
        self.assertIn("NEVER summed", text)

    def test_the_report_states_the_entry_policy_and_its_effect(self):
        report = self._report()
        text = report.render()
        self.assertIn("ENTRY POLICY (predeclared)", text)
        self.assertIn("ENTRY SELECTION", text)
        self.assertEqual(report.selection.taken, 6, "one entry per game")
        self.assertEqual(report.selection.considered, 42)

    def test_the_report_carries_the_checkpoint_coverage_matrix(self):
        self.assertIn("GAME x CHECKPOINT COVERAGE", self._report().render())

    def test_the_report_labels_the_sparse_grid_limitation(self):
        text = self._report().render()
        self.assertIn("cannot establish minute-scale reaction lag", text)

    def test_the_headline_return_uses_the_policy(self):
        report = self._report()
        self.assertEqual(report.returns.trades, 6)
        self.assertEqual(report.returns.games, 6)


if __name__ == "__main__":
    unittest.main()
