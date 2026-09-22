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

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from analysis.scoring import (
    DEFAULT_MAX_MINUTES_TO_START, Eligibility, EntryPolicy, Observation,
    as_trade, build_report, checkpoint_slices, decay_series, fee_scenarios,
    price_paths, realized_return, select_entries, side_quotes,
)
from collect import (
    BASELINE_LEAD_MINUTES, BENIGN_GROUPS, Checkpoint, CheckpointMatrix, Ledger,
    STATUS_LISTING_UNKNOWN, STATUS_NOT_YET_LISTED, STATUS_OBSERVED,
    STATUS_UNJOINED, StartResolver, cell_is_benign, checkpoint_targets,
    decision_cutoffs,
    default_lead_grid, grid_reach, in_study_window, listing_status,
    observation_with_status, parse_lead_grid, record_cell_outcome,
)
from data.kalshi_history import Coverage
from data.odds_history import CreditLedger
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
        self.assertEqual((row.entry_bid, row.entry_ask), (0.49, 0.51),
                         "at zero delay both books are the same candle")
        self.assertEqual(row.entry_delay_minutes, 0.0)

    def test_a_delay_records_both_books_and_keeps_them_apart(self):
        """The execution quote goes in `entry_bid`/`entry_ask`, NOT in the
        decision book. An earlier version wrote it into `exchange_bid`/`ask`,
        which is what the screen and the side selection read -- so a delayed
        run decided using a price that did not exist at the decision."""
        candles = [Candle(self.CUTOFF - timedelta(minutes=1), 0.49, 0.51),
                   Candle(self.CUTOFF + timedelta(minutes=10), 0.60, 0.62)]
        row, status = observation_with_status(
            joined_market(), self._quotes(), candles, self.CUTOFF, "MLB",
            Ledger(), checkpoint=Checkpoint(1440),
            entry_delay=timedelta(minutes=10))
        self.assertEqual(status, STATUS_OBSERVED)
        self.assertEqual((row.exchange_bid, row.exchange_ask), (0.49, 0.51),
                         "the decision book must be what was visible at t")
        self.assertEqual((row.entry_bid, row.entry_ask), (0.60, 0.62),
                         "the execution book must be the later quote")
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


class SelectionRespectsFullEligibilityTest(unittest.TestCase):
    """THE defect: `select_entries` gated on `as_trade` alone.

    `as_trade` checks valid side prices and the net-EV floor. It does not know
    about the price band, the lead bounds or the spread cap -- those live in
    `Eligibility.admits`. So selection bought books `admits` rejects, and
    because the policy path IS the headline return, the SCREENED figure was
    looser than the unscreened one. Exactly backwards.
    """

    def test_a_wide_spread_is_refused_by_selection(self):
        """The reproduction from review: a 40-cent book."""
        o = Observation("game", "market", datetime(2026, 9, 5, tzinfo=UTC),
                        4320, 0.9, 0.5, 1, exchange_bid=0.3, exchange_ask=0.7)
        self.assertFalse(Eligibility().admits(o))
        entries, diag = select_entries([o], Eligibility())
        self.assertEqual(entries, [])
        self.assertEqual(diag.skipped_not_qualifying, 1)

    def test_a_price_outside_the_band_is_refused_by_selection(self):
        o = obs("G", "M", 1440, p_exch=0.95, p_sharp=0.99, bid=0.94, ask=0.96)
        self.assertFalse(Eligibility(min_net_ev=0.0).admits(o))
        self.assertEqual(select_entries([o], Eligibility(min_net_ev=0.0))[0], [])

    def test_a_lead_beyond_the_ceiling_is_refused_by_selection(self):
        o = obs("G", "M", 4320)
        tight = Eligibility(min_net_ev=0.0, max_minutes_to_start=1440.0)
        self.assertFalse(tight.admits(o))
        self.assertEqual(select_entries([o], tight)[0], [])

    def test_a_rejected_checkpoint_does_not_consume_the_game(self):
        """The case that makes this more than a counting error: a rejected
        early look must leave the game open, or one unexecutable 72h quote
        silently cancels every later chance to trade that game."""
        looks = [
            # 72h: unexecutable -- a 40-cent spread
            obs("G1", "M1", 4320, bid=0.30, ask=0.70),
            # 24h: a clean, narrow, qualifying book
            obs("G1", "M1", 1440, bid=0.49, ask=0.51),
        ]
        entries, diag = select_entries(looks, Eligibility(min_net_ev=0.0))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].checkpoint_label, "24h")
        self.assertEqual(diag.skipped_not_qualifying, 1)
        self.assertEqual(diag.skipped_game_already_entered, 0,
                         "the rejected look must not have consumed exposure")

    def test_selection_never_admits_what_the_screen_rejects(self):
        """The general property, over every condition `admits` enforces."""
        cases = [
            obs("A", "A", 1440, bid=0.30, ask=0.70),      # spread
            obs("B", "B", 1440, p_exch=0.95, bid=0.94, ask=0.96),  # band
            obs("C", "C", 4320),                           # lead (tight ceiling)
        ]
        elig = Eligibility(min_net_ev=0.0, max_minutes_to_start=1440.0)
        for o in cases:
            taken = select_entries([o], elig)[0]
            self.assertEqual(bool(taken), elig.admits(o),
                             f"{o.game_id}: selection and screen disagree")


class ScenariosVaryFeesOnlyTest(unittest.TestCase):
    """A column headed 'route' must differ by ROUTE.

    `fee_scenarios` recomputed the non-headline route WITHOUT the entry
    policy, so a three-checkpoint game showed 1 trade on the headline route
    and 3 on the other -- a policy difference printed where a reader is being
    invited to compare fees.
    """

    def _three_looks(self):
        start = datetime(2026, 9, 5, tzinfo=UTC)
        return [Observation("G", "M", start - timedelta(minutes=m), float(m),
                            0.60, 0.50, 1, exchange_bid=0.49, exchange_ask=0.51,
                            checkpoint_minutes=float(m))
                for m in (4320, 1440, 180)]

    def test_every_route_applies_the_same_policy(self):
        scenarios = fee_scenarios(self._three_looks(),
                                  Eligibility(min_net_ev=0.0),
                                  bootstrap_rounds=20, policy=EntryPolicy())
        counts = {s.route: s.returns.trades for s in scenarios}
        self.assertEqual(set(counts.values()), {1},
                         f"routes disagree on how many bets exist: {counts}")

    def test_the_report_headline_and_every_scenario_agree(self):
        report = build_report(self._three_looks(), Coverage(), "t",
                              eligibility=Eligibility(min_net_ev=0.0),
                              bootstrap_rounds=20, policy=EntryPolicy())
        for s in report.scenarios:
            self.assertEqual(s.returns.trades, report.returns.trades,
                             f"route {s.route} counted a different number of bets")

    def test_without_a_policy_every_route_still_agrees(self):
        """The no-policy path must be symmetric too, not merely the policy one."""
        scenarios = fee_scenarios(self._three_looks(),
                                  Eligibility(min_net_ev=0.0), bootstrap_rounds=20)
        counts = {s.route: s.returns.trades for s in scenarios}
        self.assertEqual(set(counts.values()), {3}, counts)

    def _drifting_looks(self):
        """One game, three checkpoints, at DIFFERENT prices.

        Identical prices would not catch this: the monthly figure is a ratio,
        so averaging three copies of one trade gives the same number as taking
        one. The prices have to move for the count to show up.
        """
        start = datetime(2026, 9, 5, tzinfo=UTC)
        return [Observation("G", "M", start - timedelta(minutes=m), float(m),
                            0.80, ask - 0.01, 1,
                            exchange_bid=round(ask - 0.02, 2), exchange_ask=ask,
                            checkpoint_minutes=float(m))
                for m, ask in ((4320, 0.51), (1440, 0.60), (180, 0.70))]

    def test_the_monthly_breakdown_is_computed_like_the_headline(self):
        """`decay_series` prints a net return in the same column family as the
        headline return, and was computing it with neither the series, the
        route, nor the policy -- so the month was priced at the generic 0.07
        where September's dated schedule says 0.035, over every qualifying
        checkpoint instead of the one bet the policy places."""
        looks = self._drifting_looks()
        elig = Eligibility(min_net_ev=0.0)
        kw = dict(series="KXMLBGAME", route="direct", policy=EntryPolicy())

        month = decay_series(looks, elig, bootstrap_rounds=20, **kw)
        headline = realized_return(looks, elig, bootstrap_rounds=20, **kw)
        self.assertEqual(len(month), 1)
        self.assertAlmostEqual(month[0].mean_return,
                               headline.mean_return_on_stake, places=12,
                               msg="the month and the headline disagree")

    def test_dropping_the_policy_changes_the_month(self):
        """Proof the threading is load-bearing, not decorative."""
        looks = self._drifting_looks()
        elig = Eligibility(min_net_ev=0.0)
        policed = decay_series(looks, elig, bootstrap_rounds=20,
                               series="KXMLBGAME", policy=EntryPolicy())
        unpoliced = decay_series(looks, elig, bootstrap_rounds=20,
                                 series="KXMLBGAME")
        self.assertNotEqual(policed[0].mean_return, unpoliced[0].mean_return)

    def test_dropping_the_series_changes_the_month(self):
        """The dated fee reaches the monthly figure too: generic 0.07 against
        September's 0.035 is a factor of two on every fee in it."""
        looks = self._drifting_looks()
        elig = Eligibility(min_net_ev=0.0)
        dated = decay_series(looks, elig, bootstrap_rounds=20,
                             series="KXMLBGAME", policy=EntryPolicy())
        generic = decay_series(looks, elig, bootstrap_rounds=20,
                               policy=EntryPolicy())
        self.assertNotEqual(dated[0].mean_return, generic[0].mean_return)


class CommittedSignalTest(unittest.TestCase):
    """A delayed entry pays the later price. It does not DECIDE on it.

    The first version put the execution quote into `exchange_bid`/`ask`, which
    is what the screen and the side selection read -- so a delayed run could
    qualify a trade the signal had not triggered, or flip YES to NO, using a
    price that did not exist at the decision.
    """

    START = GAME1

    def _committed(self, exec_bid, exec_ask, **kw):
        return Observation(
            game_id="G", market_id="M",
            decision_at=self.START - timedelta(hours=24), minutes_to_start=1440.0,
            p_sharp=kw.pop("p_sharp", 0.60), p_exchange=0.50, outcome=1,
            exchange_bid=0.49, exchange_ask=0.51,
            entry_bid=exec_bid, entry_ask=exec_ask,
            checkpoint_minutes=1440.0, entry_delay_minutes=10.0, **kw)

    def test_the_side_is_chosen_on_the_decision_book(self):
        """The market moving against you after t must not flip the side."""
        moved = self._committed(0.05, 0.07)   # collapsed after the decision
        quotes = side_quotes(moved)
        best = max(quotes, key=lambda q: q.predicted_ev)
        self.assertEqual(best.side, "YES",
                         "the side was re-chosen using post-decision prices")

    def test_the_screen_qualifies_on_the_decision_book(self):
        """A trade the signal triggered is not retrospectively disqualified."""
        moved = self._committed(0.30, 0.70)   # a 40-cent spread AFTER the fact
        self.assertTrue(Eligibility(min_net_ev=0.0).admits(moved))

    def test_but_the_trade_pays_the_execution_price(self):
        moved = self._committed(0.60, 0.62)
        trade = as_trade(moved, eligibility=Eligibility(min_net_ev=0.0))
        self.assertAlmostEqual(trade.entry_price, 0.51, places=9)
        self.assertGreater(trade.stake, 0.60, "execution must cost the later price")
        self.assertLess(trade.profit, 1.0 - 0.51,
                        "profit must be net of what was actually paid")

    def test_zero_delay_is_unchanged(self):
        """The instantaneous bound stays bit-for-bit comparable."""
        plain = obs("G", "M", 1440, bid=0.49, ask=0.51)
        quotes = side_quotes(plain)
        for q in quotes:
            self.assertIsNone(q.exec_price)
            self.assertAlmostEqual(q.paid, q.cost, places=12)

    def test_an_adverse_move_shows_up_as_a_worse_return_not_a_rejection(self):
        """The whole point of the delay knob: the cost of being late is a
        smaller return, not a trade that quietly vanishes from the sample."""
        flat = self._committed(0.49, 0.51)
        adverse = self._committed(0.60, 0.62)
        elig = Eligibility(min_net_ev=0.0)
        a = as_trade(flat, eligibility=elig)
        b = as_trade(adverse, eligibility=elig)
        self.assertIsNotNone(b, "the late trade must still be in the sample")
        self.assertEqual(a.side, b.side)
        self.assertEqual(a.predicted_ev, b.predicted_ev, "the screen is identical")
        self.assertGreater(a.profit, b.profit, "but the outcome is worse")


class UnexecutableDelayedEntryTest(unittest.TestCase):
    """A delayed side with no usable execution price is NOT filled.

    `side_quotes` priced such a side at the DECISION book, because
    `SideQuote.paid` falls back when `exec_price` is None. So a side that
    could only have executed at 1.00 reported paying the 0.51 it was quoted at
    the decision -- the same silent substitution the delay knob exists to
    measure, one layer further down, and always in the flattering direction.
    """

    ELIG = Eligibility(min_net_ev=0.0)

    def _obs(self, **kw):
        return Observation("G", "M", datetime(2026, 9, 5, tzinfo=UTC), 1440.0,
                           0.8, 0.50, 1, exchange_bid=0.49, exchange_ask=0.51,
                           checkpoint_minutes=1440.0, **kw)

    def test_an_out_of_bounds_execution_price_is_not_a_fill(self):
        """The reproduction from review: execution ask of 1.00."""
        o = self._obs(entry_bid=0.99, entry_ask=1.0, entry_delay_minutes=10.0)
        self.assertIsNone(as_trade(o, eligibility=self.ELIG))

    def test_a_crossed_execution_book_is_not_a_book(self):
        """The collector admits a candle on its mid and price sanity, not on
        whether the two sides are ordered."""
        o = self._obs(entry_bid=0.70, entry_ask=0.60, entry_delay_minutes=10.0)
        self.assertIsNone(as_trade(o, eligibility=self.ELIG))

    def test_an_absent_execution_book_is_not_the_decision_book(self):
        o = self._obs(entry_delay_minutes=10.0)
        self.assertIsNone(as_trade(o, eligibility=self.ELIG))

    def test_a_one_sided_execution_book_fills_neither_side(self):
        o = self._obs(entry_bid=0.60, entry_delay_minutes=10.0)
        self.assertIsNone(as_trade(o, eligibility=self.ELIG))

    def test_a_valid_execution_price_is_what_gets_paid(self):
        o = self._obs(entry_bid=0.60, entry_ask=0.62, entry_delay_minutes=10.0)
        trade = as_trade(o, eligibility=self.ELIG)
        self.assertAlmostEqual(trade.paid, 0.64, places=9)
        self.assertNotAlmostEqual(trade.paid, trade.entry_price + trade.fee)

    def test_zero_delay_reuses_the_decision_book_by_definition(self):
        """The fallback is legitimate here and ONLY here: same candle."""
        o = self._obs()
        trade = as_trade(o, eligibility=self.ELIG)
        self.assertAlmostEqual(trade.paid, trade.entry_price + trade.fee, places=12)

    def test_a_delayed_quote_always_carries_its_execution_price(self):
        """The invariant that makes `paid`'s fallback unreachable when delayed."""
        o = self._obs(entry_bid=0.60, entry_ask=0.62, entry_delay_minutes=10.0)
        for q in side_quotes(o):
            self.assertTrue(q.delayed)
            self.assertIsNotNone(q.exec_price)
            self.assertIsNotNone(q.exec_fee)

    def test_the_execution_fee_is_resolved_at_the_execution_time(self):
        """A delay can cross a dated fee-schedule boundary. Pricing the
        execution at the DECISION's schedule would bill September's halved
        multiplier for an August trade, or the reverse."""
        from core.fees import fee_for
        before = datetime(2026, 8, 7, 4, 30, tzinfo=UTC)      # multiplier 1
        after = datetime(2026, 8, 7, 5, 30, tzinfo=UTC)       # multiplier 0.5
        o = Observation("G", "M", before, 1440.0, 0.8, 0.50, 1,
                        exchange_bid=0.49, exchange_ask=0.51,
                        entry_bid=0.49, entry_ask=0.51,
                        entry_at=after, entry_delay_minutes=60.0)
        quote = [q for q in side_quotes(o, series="KXMLBGAME") if q.side == "YES"][0]
        self.assertAlmostEqual(
            quote.exec_fee,
            fee_for("kalshi", 1.0, 0.51, "taker", "KXMLBGAME", after).dollars,
            places=12)
        self.assertNotAlmostEqual(quote.exec_fee, quote.fee, places=12)


class MonthBoundaryExposureTest(unittest.TestCase):
    """A game cannot regain exposure at a month boundary.

    `decay_series` selected entries INSIDE each month, so one game entered in
    August at its 72h checkpoint and again in September at its 24h one. Not
    hypothetical here: the early checkpoints of a Sept 1 game land in August.
    """

    def _straddling(self):
        return [
            Observation("G", "M", datetime(2026, 8, 31, 20, 0, tzinfo=UTC),
                        720.0, 0.60, 0.50, 1, exchange_bid=0.49,
                        exchange_ask=0.51, checkpoint_minutes=720.0),
            Observation("G", "M", datetime(2026, 9, 1, 8, 0, tzinfo=UTC),
                        60.0, 0.60, 0.50, 1, exchange_bid=0.49,
                        exchange_ask=0.51, checkpoint_minutes=60.0),
        ]

    def test_only_one_month_reports_a_trade(self):
        months = decay_series(self._straddling(), Eligibility(min_net_ev=0.0),
                              bootstrap_rounds=20, policy=EntryPolicy())
        traded = [m for m in months if m.mean_return == m.mean_return]
        self.assertEqual(len(traded), 1, "the game was entered in two months")
        self.assertEqual(traded[0].period, "2026-08",
                         "the entry belongs to its EARLIEST qualifying checkpoint")

    def test_the_monthly_trade_count_matches_the_global_selection(self):
        looks = self._straddling()
        entries, _ = select_entries(looks, Eligibility(min_net_ev=0.0),
                                    policy=EntryPolicy())
        months = decay_series(looks, Eligibility(min_net_ev=0.0),
                              bootstrap_rounds=20, policy=EntryPolicy())
        traded = [m for m in months if m.mean_return == m.mean_return]
        self.assertEqual(len(traded), len(entries))

    def test_the_forecast_diagnostic_still_covers_every_month(self):
        """Only the RETURN carries exposure. The per-row Brier comparison has
        none, so it must keep reporting both months."""
        months = decay_series(self._straddling(), Eligibility(min_net_ev=0.0),
                              bootstrap_rounds=20, policy=EntryPolicy())
        self.assertEqual([m.period for m in months], ["2026-08", "2026-09"])
        self.assertTrue(all(m.games == 1 for m in months))


class ArtifactRoundTripTest(unittest.TestCase):
    """A delayed run must be reproducible from its own saved observations.

    The writer kept `bid`/`ask` from the decision book and never saved the
    execution book, while its comment still claimed bid/ask WERE the entry
    book. So the saved artifacts of a delayed run could not reproduce the
    trade they described -- and would silently reproduce a cheaper one.
    """

    def _delayed_run(self, tmp):
        start = datetime(2026, 9, 14, 23, 0, tzinfo=UTC)
        obs_rows = [
            Observation(f"EVT{i}", f"M{i}", start - timedelta(minutes=1440),
                        1440.0, 0.70, 0.50, i % 2,
                        exchange_bid=0.49, exchange_ask=0.51,
                        entry_bid=0.60, entry_ask=0.62,
                        entry_at=start - timedelta(minutes=1430),
                        entry_delay_minutes=10.0, checkpoint_minutes=1440.0)
            for i in range(8)
        ]
        out = Path(tmp) / "study"
        with mock.patch.object(
            run_study, "collect",
            return_value=(obs_rows, Coverage(), CreditLedger(), Ledger(),
                          default_lead_grid(), CheckpointMatrix(),
                          StartResolver(league="MLB"))
        ):
            run_study.main([
                "--sport", "MLB", "--series", "KXMLBGAME",
                "--from", "2026-09-14", "--to", "2026-09-15",
                "--api-key", "K", "--out", str(out), "--cache-dir", "",
                "--entry-delay-minutes", "10", "--min-net-ev", "0.0",
            ])
        return obs_rows, json.loads((out / "observations.json").read_text())

    def _rebuild(self, payload):
        def parse(value):
            return datetime.fromisoformat(value) if value else None
        return [Observation(
            game_id=r["game_id"], market_id=r["market_id"],
            decision_at=parse(r["decision_at"]),
            minutes_to_start=r["minutes_to_start"], p_sharp=r["p_sharp"],
            p_exchange=r["p_exchange"], outcome=r["outcome"],
            exchange_bid=r["decision_bid"], exchange_ask=r["decision_ask"],
            entry_bid=r["entry_bid"], entry_ask=r["entry_ask"],
            entry_at=parse(r["entry_at"]),
            entry_delay_minutes=r["entry_delay_minutes"],
            checkpoint_minutes=r["checkpoint_minutes"],
        ) for r in payload["observations"]]

    def test_both_books_are_persisted_and_the_schema_is_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, payload = self._delayed_run(tmp)
        self.assertEqual(payload["schema"], run_study.OBSERVATION_SCHEMA)
        row = payload["observations"][0]
        self.assertEqual((row["decision_bid"], row["decision_ask"]), (0.49, 0.51))
        self.assertEqual((row["entry_bid"], row["entry_ask"]), (0.60, 0.62))
        self.assertEqual(row["entry_delay_minutes"], 10.0)
        self.assertNotIn("bid", row, "schema 1's ambiguous name must be gone")

    def test_selection_paid_cost_and_return_survive_the_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, payload = self._delayed_run(tmp)
        restored = self._rebuild(payload)
        elig = Eligibility(min_net_ev=0.0)

        a, _ = select_entries(original, elig, policy=EntryPolicy())
        b, _ = select_entries(restored, elig, policy=EntryPolicy())
        self.assertEqual(len(a), len(b))
        self.assertEqual([e.trade.side for e in a], [e.trade.side for e in b])
        for x, y in zip(a, b):
            self.assertAlmostEqual(x.trade.paid, y.trade.paid, places=12)
            self.assertAlmostEqual(x.trade.profit, y.trade.profit, places=12)

        ra = realized_return(original, elig, bootstrap_rounds=20, policy=EntryPolicy())
        rb = realized_return(restored, elig, bootstrap_rounds=20, policy=EntryPolicy())
        self.assertAlmostEqual(ra.mean_return_on_stake, rb.mean_return_on_stake,
                               places=12)

    def test_the_round_trip_would_fail_without_the_execution_book(self):
        """Proof the persisted field is load-bearing: drop it and the restored
        run reports a cheaper trade than the one that happened."""
        with tempfile.TemporaryDirectory() as tmp:
            original, payload = self._delayed_run(tmp)
        for row in payload["observations"]:
            row["entry_bid"] = row["entry_ask"] = None
        lossy = self._rebuild(payload)
        elig = Eligibility(min_net_ev=0.0)
        self.assertTrue(select_entries(original, elig, policy=EntryPolicy())[0])
        self.assertEqual(select_entries(lossy, elig, policy=EntryPolicy())[0], [],
                         "without the execution book the trade is unpriceable")


class OneClassifierTest(unittest.TestCase):
    """The matrix and the ledger must give one verdict per cell.

    The matrix called `exchange_quote_too_old_at_cutoff` a RESULT (`no_quote`
    -- listed, but nobody had a fresh price) while the ledger counted it as a
    parse/join FAILURE against coverage. Two answers to one question, in the
    two places a reader would check.
    """

    BENIGN = ("no_exchange_quote_at_cutoff", "exchange_quote_too_old_at_cutoff",
              "no_entry_quote_after_delay", "cutoff_at_or_after_start",
              STATUS_NOT_YET_LISTED, STATUS_OBSERVED)
    LOSSY = ("no_sharp_quote_available_at_cutoff", "sharp_quote_too_old_at_cutoff",
             "exchange_quote_malformed", "candles_unavailable",
             "yes_side_unresolvable", STATUS_LISTING_UNKNOWN, STATUS_UNJOINED)

    def test_the_ledger_files_a_benign_cell_as_an_exclusion(self):
        for status in self.BENIGN:
            led = Ledger()
            record_cell_outcome(led, status, "KX-A")
            self.assertIn(status, led.eligibility_exclusions, status)
            self.assertNotIn(status, led.rejections, status)

    def test_the_ledger_files_a_lossy_cell_as_a_rejection(self):
        for status in self.LOSSY:
            led = Ledger()
            record_cell_outcome(led, status, "KX-A")
            self.assertIn(status, led.rejections, status)
            self.assertNotIn(status, led.eligibility_exclusions, status)

    def test_the_two_readers_agree_on_every_known_status(self):
        """The general property. A status the matrix calls benign must not be
        a rejection in the ledger, and vice versa, for every status either
        side knows about."""
        matrix = CheckpointMatrix()
        for status in set(self.BENIGN) | set(self.LOSSY):
            led = Ledger()
            record_cell_outcome(led, status, "KX-A")
            matrix_says_benign = matrix.group_of(status) in BENIGN_GROUPS
            ledger_says_benign = status in led.eligibility_exclusions
            self.assertEqual(matrix_says_benign, ledger_says_benign,
                             f"{status}: matrix and ledger disagree")

    def test_a_missing_sharp_quote_stays_loud(self):
        """The 48h finding was 400/400 no_sharp. A provider not covering a
        game two days out is a coverage limit of the FEED, not a property of
        the market, so it must stay on the rejection side."""
        self.assertFalse(cell_is_benign("no_sharp_quote_available_at_cutoff"))
        led = Ledger()
        led.count("checkpoint_cells", 10, unit="game-checkpoints")
        record_cell_outcome(led, "no_sharp_quote_available_at_cutoff", "KX-A",
                            count=10)
        self.assertTrue(led.stages["checkpoint_cells"].rejected)

    def test_an_unknown_status_is_lossy_not_benign(self):
        self.assertFalse(cell_is_benign("some_new_reason"))


class UnjoinedContractTest(unittest.TestCase):
    """An unjoined contract's cells are an explained loss, not a blank.

    The collection loop only visits joined markets, so a contract that never
    matched a sharp event left every one of its cells `unreported` -- and
    after a run, `unreported` means the collector skipped rows, i.e. a bug.
    The live run showed exactly this: 2 contracts, 2 unreported at every one
    of the seven checkpoints.
    """

    def _matrix(self, markets, grid):
        matrix = CheckpointMatrix()
        matrix.expect(checkpoint_targets(markets, grid))
        return matrix

    def test_record_all_covers_every_checkpoint(self):
        markets = milbal()
        grid = default_lead_grid()
        matrix = self._matrix(markets, grid)
        ticker = list(markets)[0]
        matrix.record_all(ticker, STATUS_UNJOINED)
        for checkpoint in grid:
            self.assertEqual(matrix.statuses[(ticker, checkpoint.key)],
                             STATUS_UNJOINED)

    def test_the_denominator_is_unchanged(self):
        """The whole point of an independent denominator: labelling a loss
        must not shrink what it is a loss OUT OF."""
        markets = milbal()
        grid = default_lead_grid()
        matrix = self._matrix(markets, grid)
        before = len(matrix.statuses)
        matrix.record_all(list(markets)[0], STATUS_UNJOINED)
        self.assertEqual(len(matrix.statuses), before)
        self.assertEqual(before, len(markets) * len(grid))

    def test_unjoined_cells_are_no_longer_unreported(self):
        markets = milbal()
        grid = default_lead_grid()
        matrix = self._matrix(markets, grid)
        self.assertEqual(matrix.unreported(), len(markets) * len(grid))
        for ticker in markets:
            matrix.record_all(ticker, STATUS_UNJOINED)
        self.assertEqual(matrix.unreported(), 0)
        self.assertEqual(matrix.by_checkpoint()["4320"]["unjoined"], len(markets))

    def test_unjoined_counts_against_coverage_not_as_a_result(self):
        """It is an EXPLAINED loss, but still a loss: those cells were never
        reachable, so the study did not observe what it enumerated."""
        self.assertFalse(cell_is_benign(STATUS_UNJOINED))
        markets = milbal()
        matrix = self._matrix(markets, default_lead_grid())
        matrix.record_all(list(markets)[0], STATUS_UNJOINED)
        self.assertEqual(matrix.source_failures(), len(default_lead_grid()))
        led = Ledger()
        led.count("checkpoint_cells", 14, unit="game-checkpoints")
        record_cell_outcome(led, STATUS_UNJOINED, "KX-A", count=7)
        self.assertEqual(led.stages["checkpoint_cells"].rejected, 7)

    def test_the_matrix_renders_an_unjoined_column(self):
        markets = milbal()
        matrix = self._matrix(markets, default_lead_grid())
        matrix.record_all(list(markets)[0], STATUS_UNJOINED)
        self.assertIn("unjoined", matrix.render())


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
