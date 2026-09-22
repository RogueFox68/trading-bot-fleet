"""Episode accounting: the units, the reconciliation, and the holdout guard.

THE DEFECT THIS SUITE EXISTS FOR
--------------------------------
Twice in this study a specific, credible number measured the wrong
population. The clearest was `50.0% of 4 eligible contracts lost`, where
resolution failures were counted per EVENT against a stage denominated in
CONTRACTS. Nothing looked wrong; a percentage just described a denominator it
was not computed over.

So the load-bearing tests here are about units and reconciliation, not about
totals:

* `test_one_move_on_a_two_contract_game_is_three_different_numbers` -- 1
  trigger, 2 screened rows, 1 entry, each under its own name.
* `test_every_stage_reconciles_on_a_healthy_run` -- because the ledger prints
  a `***` warning when a breakdown does not sum to its total, and a warning
  that fires on every healthy run is noise (rule 10). An earlier version of
  the ledger had exactly that: a stage whose breakdown mixed units and a note
  excusing it.
* `test_declaring_a_development_window_a_holdout_raises` -- September 1-16 is
  the data every threshold here was declared against. A run over it is
  exploratory however it is labelled.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import Eligibility, EntryPolicy               # noqa: E402
from reaction.detector import MovePolicy, detect_moves              # noqa: E402
from reaction.episodes import (                                     # noqa: E402
    DEVELOPMENT_WINDOW, DataRole, EpisodeLedger, HoldoutViolation,
    StageCount, Unit, build_ledger, classify_window,
)
from reaction.measure import ReactionPolicy, measure_reaction        # noqa: E402
from reaction.screen import ScreenResult, screen_reaction            # noqa: E402
from tests.test_reaction_measure import (                            # noqa: E402
    AFTER_PRICES, BEFORE_PRICES, EVENT, START, at, env, make_trigger,
)
from tests.test_reaction_screen import BID, book                     # noqa: E402

UTC = timezone.utc
# Both contracts of ONE game. They share the provider event id, which is what
# makes them mutually exclusive rather than a hedge.
HOME_TICKER = "KXNFLGAME-26SEP13DALNYG-NYG"
AWAY_TICKER = "KXNFLGAME-26SEP13DALNYG-DAL"


def pipeline(*, quotes=None, candles=None, tickers=((HOME_TICKER, True),),
             settled_yes=1, declared_role=DataRole.UNDECLARED,
             entry_policy=None, eligibility=None):
    """Run the whole chain and build a ledger, the way a replay would.

    Deliberately end to end: the ledger's job is to account for what the
    other layers produced, so a test that fed it hand-made counts would
    check its arithmetic and not its accounting.
    """
    quotes = quotes if quotes is not None else [
        env(at(200), *BEFORE_PRICES, at(201)),
        env(at(195), *AFTER_PRICES, at(196)),
    ]
    candles = candles if candles is not None else book()
    detector_result = detect_moves(quotes)
    reactions, screen_result = [], ScreenResult()
    for trigger in detector_result.triggers:
        for ticker, yes_is_home in tickers:
            reaction = measure_reaction(trigger, candles,
                                        market_ticker=ticker,
                                        yes_is_home=yes_is_home)
            reactions.append(reaction)
            screen_result.screened.append(screen_reaction(
                reaction, trigger, candles, market_ticker=ticker,
                yes_is_home=yes_is_home, start=START,
                settled_yes=settled_yes))
    return build_ledger(
        detector_result=detector_result, reactions=reactions,
        screen_result=screen_result, observation_count=len(quotes),
        declared_role=declared_role, move_policy=MovePolicy(),
        reaction_policy=ReactionPolicy(),
        entry_policy=entry_policy or EntryPolicy(),
        eligibility=eligibility or Eligibility())


class UnitDisciplineTest(unittest.TestCase):
    """Counts in different units are different numbers, under their own name."""

    def test_one_move_on_a_two_contract_game_is_three_different_numbers(self):
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        self.assertEqual(ledger.stage("moves detected").total, 1)
        self.assertEqual(ledger.stage("contract rows screened").total, 2)
        self.assertEqual(ledger.stage("entries selected").total, 1)
        self.assertEqual(ledger.stage("games with any detected move").total, 1)

    def test_each_stage_names_the_unit_it_counts(self):
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        units = {s.stage: s.unit for s in ledger.stages}
        self.assertIs(units["moves detected"], Unit.TRIGGERS)
        self.assertIs(units["contract rows screened"], Unit.SCREENED)
        self.assertIs(units["entries selected"], Unit.ENTRIES)
        self.assertIs(units["games with any detected move"], Unit.GAMES)
        for row in ledger.as_dict()["stages"]:
            self.assertIn(row["unit"], {u.value for u in Unit})

    def test_a_stage_cannot_be_built_without_a_unit(self):
        """The unit is positional and required. It is not decoration."""
        with self.assertRaises(TypeError):
            StageCount("some stage", 7)          # type: ignore[arg-type]

    def test_the_rendered_ledger_prints_the_unit_beside_every_total(self):
        text = pipeline().render()
        for unit in (Unit.TRIGGERS, Unit.SCREENED, Unit.ENTRIES, Unit.GAMES):
            self.assertIn(unit.value, text)
        self.assertIn("each count in its OWN unit", text)

    def test_the_selection_diagnostics_name_their_own_unit(self):
        """`looks_considered` counts contract rows, not games or triggers."""
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        selection = ledger.as_dict()["selection"]
        self.assertEqual(selection["looks_considered"], 2)
        self.assertEqual(selection["games_seen"], 1)
        self.assertEqual(selection["entries_taken"], 1)
        self.assertIn("SCREENED CONTRACT ROWS", selection["unit_note"])


class ReconciliationTest(unittest.TestCase):
    """A breakdown that does not sum to its total is a wrong claim."""

    def test_every_stage_reconciles_on_a_healthy_run(self):
        """Or the ledger's own warning is noise on every run (rule 10)."""
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        for stage in ledger.stages:
            with self.subTest(stage=stage.stage):
                self.assertTrue(stage.reconciles,
                                f"{stage.stage}: breakdown sums to "
                                f"{sum(stage.breakdown.values())}, total is "
                                f"{stage.total}")
        self.assertTrue(ledger.reconciles)
        self.assertNotIn("DOES NOT SUM", ledger.render())
        self.assertTrue(ledger.as_dict()["all_stages_reconcile"])

    def test_detector_outcomes_account_for_every_observation(self):
        """One outcome per observation: a trigger, or a named rejection."""
        ledger = pipeline()
        outcomes = ledger.stage("detector outcomes")
        self.assertEqual(outcomes.total, 2, "two quotes, two outcomes")
        self.assertEqual(outcomes.breakdown["triggered"], 1)
        self.assertEqual(outcomes.breakdown["first_observation"], 1)
        self.assertTrue(outcomes.reconciles)

    def test_a_declared_gap_makes_outcomes_exceed_observations(self):
        """And the difference IS the declared absences, as the note says.

        `note_gap` records an outcome with no observation behind it -- a
        market omitted from a provider response produces no envelope at all.
        So the two totals legitimately differ, which is why they are separate
        stages in separate units rather than one number.
        """
        from reaction.detector import MoveDetector
        quotes = [env(at(200), *BEFORE_PRICES, at(201)),
                  env(at(195), *AFTER_PRICES, at(196))]
        detector = MoveDetector(MovePolicy())
        detector.observe(quotes[0])
        detector.note_gap(quotes[0].provenance.market_id, at(198), "absent")
        detector.observe(quotes[1])
        ledger = build_ledger(
            detector_result=detector.result, reactions=[],
            screen_result=ScreenResult(), observation_count=len(quotes))
        fed = ledger.stage("provider observations fed").total
        outcomes = ledger.stage("detector outcomes")
        self.assertEqual(fed, 2)
        self.assertEqual(outcomes.total, 3)
        self.assertEqual(outcomes.total - fed, 1, "the declared gap")
        self.assertTrue(outcomes.reconciles)
        self.assertIn("declared_gap", outcomes.breakdown)

    def test_a_non_reconciling_stage_is_called_out_loudly(self):
        """The warning has to be reachable, or it is not a warning."""
        broken = StageCount("invented", Unit.TRIGGERS, 10,
                            breakdown={"a": 3, "b": 4})
        self.assertFalse(broken.reconciles)
        self.assertIn("*** BREAKDOWN SUMS TO 7, NOT 10", broken.render())
        ledger = EpisodeLedger(
            window=classify_window([at(195)]), stages=[broken])
        self.assertFalse(ledger.reconciles)
        self.assertIn("DOES NOT SUM", ledger.render())

    def test_an_empty_breakdown_is_not_a_claim(self):
        bare = StageCount("total only", Unit.GAMES, 12)
        self.assertTrue(bare.reconciles)
        self.assertNotIn("***", bare.render())


class MutualExclusionTest(unittest.TestCase):
    """Both contracts of a game are one episode and at most one bet."""

    def test_taking_one_contract_closes_the_game_to_the_other(self):
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        self.assertEqual(len(ledger.entries), 1,
                         "buying both sides of one game is not a hedge, it is "
                         "paying two fees to hold a certainty")
        self.assertEqual(len(ledger.episodes), 1)
        episode = ledger.episodes[0]
        self.assertEqual(episode.game_id, EVENT)
        self.assertEqual(len(episode.contract_ids), 2)
        self.assertEqual(
            ledger.as_dict()["selection"]["skipped_game_already_entered"], 1)

    def test_several_moves_on_one_game_are_one_episode(self):
        """Seven looks at one outcome are not seven bets."""
        quotes = [env(at(200), *BEFORE_PRICES, at(201)),
                  env(at(180), *AFTER_PRICES, at(181)),
                  env(at(160), *BEFORE_PRICES, at(161))]
        ledger = pipeline(quotes=quotes, candles=book(210.0, 120.0))
        self.assertEqual(ledger.stage("moves detected").total, 2, "out and back")
        self.assertEqual(len(ledger.episodes), 1, "one game, one episode")
        self.assertLessEqual(len(ledger.entries), 1)
        self.assertEqual(ledger.episodes[0].as_dict()["counts"]["triggers"], 2)

    def test_the_entry_is_the_earliest_qualifying_move(self):
        """Chronological. Picking the best move is choosing with hindsight."""
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        entry = ledger.episodes[0].as_dict()["entry"]
        self.assertIsNotNone(entry)
        self.assertIn("FIRST qualifying move", entry["note"])
        self.assertEqual(entry["decision_at"], at(195).isoformat())

    def test_the_ledger_reports_selections_own_diagnostics(self):
        """rule 26: it prints the selector's verdict, it does not recount."""
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        self.assertIsNotNone(ledger.selection)
        self.assertEqual(ledger.selection.taken, len(ledger.entries))
        self.assertEqual(ledger.selection.games_entered,
                         sum(1 for e in ledger.episodes if e.entry))


class HoldoutTest(unittest.TestCase):
    """September 1-16 2026 is development data. Labelling cannot change that."""

    def test_the_development_window_is_what_the_study_was_built_on(self):
        self.assertEqual(DEVELOPMENT_WINDOW,
                         (date(2026, 9, 1), date(2026, 9, 16)))

    def test_declaring_a_development_window_a_holdout_raises(self):
        """Raised, not warned -- the same treatment as lookahead.

        A mislabelled holdout does not corrupt one figure. It corrupts how
        every figure in the run is read, silently, and permanently once the
        number has been quoted.
        """
        with self.assertRaises(HoldoutViolation) as caught:
            classify_window([at(200), at(195)], DataRole.HOLDOUT)
        message = str(caught.exception)
        self.assertIn("overlaps the development window", message)
        self.assertIn("EXPLORATORY whatever it is labelled", message)

    def test_a_development_run_is_exploratory_however_it_is_declared(self):
        for declared in (DataRole.UNDECLARED, DataRole.DEVELOPMENT):
            with self.subTest(declared=declared.value):
                verdict = classify_window([at(200), at(195)], declared)
                self.assertTrue(verdict.overlaps_development)
                self.assertIs(verdict.effective_role, DataRole.DEVELOPMENT)
                self.assertTrue(verdict.exploratory)

    def test_a_window_touching_one_development_day_still_overlaps(self):
        """Any overlap disqualifies. A partial overlap is still fitted data."""
        september = datetime(2026, 9, 16, 23, 0, tzinfo=UTC)
        october = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
        self.assertTrue(classify_window([september, october])
                        .overlaps_development)
        with self.assertRaises(HoldoutViolation):
            classify_window([september, october], DataRole.HOLDOUT)

    def test_a_window_clear_of_development_may_be_declared_a_holdout(self):
        """The guard must not refuse a genuine holdout, only a false one."""
        verdict = classify_window(
            [datetime(2026, 10, 5, 12, 0, tzinfo=UTC),
             datetime(2026, 10, 9, 12, 0, tzinfo=UTC)], DataRole.HOLDOUT)
        self.assertFalse(verdict.overlaps_development)
        self.assertIs(verdict.effective_role, DataRole.HOLDOUT)
        self.assertFalse(verdict.exploratory)

    def test_holdout_available_is_false_because_none_has_been_collected(self):
        """Stated as a fact about this study, not left to be inferred."""
        row = pipeline().as_dict()["window"]
        self.assertFalse(row["holdout_available"])
        self.assertIn("no chronological holdout has been collected",
                      row["holdout_note"])

    def test_an_empty_run_does_not_overlap_anything(self):
        """No data is not development data, and it is not a holdout either."""
        verdict = classify_window([])
        self.assertFalse(verdict.overlaps_development)
        self.assertIsNone(verdict.first)
        self.assertIs(verdict.effective_role, DataRole.UNDECLARED)
        self.assertTrue(verdict.exploratory)

    def test_the_rendered_ledger_says_the_role_out_loud(self):
        text = pipeline().render()
        self.assertIn("ROLE:   DEVELOPMENT", text)
        self.assertIn("(EXPLORATORY)", text)
        self.assertIn("DEVELOPMENT data", text)


class LedgerShapeTest(unittest.TestCase):
    """The machine-readable record carries what a reader needs to check it."""

    def test_the_ledger_is_json_serialisable(self):
        import json
        payload = json.loads(json.dumps(
            pipeline(tickers=((HOME_TICKER, True),
                              (AWAY_TICKER, False))).as_dict()))
        self.assertEqual(payload["schema"], "reaction_episode_ledger/1")
        self.assertEqual(len(payload["episodes"]), 1)
        # Assert the stage SET, not its size: a count tells a later reader
        # nothing and breaks whenever a stage is added, which is how a
        # brittle assertion gets "fixed" by bumping the number.
        self.assertEqual([s["stage"] for s in payload["stages"]], [
            "provider observations fed",
            "detector outcomes",
            "streams that produced a move",
            "moves detected",
            "reactions measured",
            "contract rows screened",
            "entries selected",
            "games with any detected move",
        ])
        self.assertEqual(
            {s["unit"] for s in payload["stages"]},
            {u.value for u in Unit},
            "every declared unit should appear exactly where it belongs")

    def test_every_declared_policy_travels_with_the_run(self):
        """A result without its thresholds cannot be checked for fitting."""
        declared = pipeline().as_dict()["declared_policies"]
        self.assertEqual(declared["move_detection"]["devig_method"], "shin")
        self.assertFalse(declared["move_detection"]["tuned_on_outcomes"])
        self.assertFalse(declared["reaction_measurement"]["tuned_on_outcomes"])
        self.assertIn("at most 1 entry/game", declared["entry"])
        self.assertEqual(declared["eligibility"]["min_net_ev"], 0.01)
        self.assertIn("--policy", declared["note"])

    def test_an_episode_carries_its_streams_and_its_contracts_apart(self):
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        episode = ledger.episodes[0]
        self.assertEqual(len(episode.stream_ids), 1, "one book, one stream")
        self.assertEqual(len(episode.contract_ids), 2, "two contracts")
        self.assertIn("pinnacle", episode.stream_ids[0])

    def test_every_trigger_reaction_and_screened_row_is_in_the_record(self):
        """The denominator is preserved end to end, not just summarised."""
        ledger = pipeline(tickers=((HOME_TICKER, True), (AWAY_TICKER, False)))
        row = ledger.episodes[0].as_dict()
        self.assertEqual(len(row["triggers"]), 1)
        self.assertEqual(len(row["reactions"]), 2)
        self.assertEqual(len(row["screened"]), 2)

    def test_a_run_with_no_moves_still_produces_a_ledger(self):
        """A zero result and a broken run must not look the same."""
        ledger = pipeline(quotes=[env(at(200), *BEFORE_PRICES, at(201))])
        self.assertEqual(ledger.stage("moves detected").total, 0)
        self.assertEqual(ledger.episodes, [])
        self.assertTrue(ledger.reconciles)
        self.assertEqual(
            ledger.stage("detector outcomes").breakdown["first_observation"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
