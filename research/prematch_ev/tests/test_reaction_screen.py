"""The event-driven layer screens through the CHECKPOINT study's policy.

WHAT THESE TESTS ARE FOR
------------------------
Not to check that a screen works -- `analysis.scoring` owns the screen and
`test_scoring.py` covers it. These check that this layer DOES NOT HAVE ITS
OWN, and that the adapter feeding it cannot smuggle in a price nobody could
have traded at.

The load-bearing assertions are:

* `test_the_verdict_is_the_checkpoint_studys_own` -- the screened verdict is
  identical to calling `as_trade` on the observation directly, so there is
  no second opinion to drift.
* `test_no_threshold_is_redefined_here` -- parses this layer's source and
  fails if it declares its own EV threshold, price band or spread bound.
  That is the only guard that survives someone "tidying up" the adapter by
  inlining a constant.
* `test_a_midpoint_disagreement_on_a_wide_book_is_still_refused` -- the
  round-2 defect, reproduced through the new entry point, with the same
  -0.09 per contract it produced then.
* `test_settlement_unknown_changes_nothing_in_the_output` -- the serialised
  row is IDENTICAL for both placeholder outcomes, which is what proves no
  settlement-derived number escapes.

FIXTURES are imported from `test_reaction_measure` rather than copied. A
duplicated fixture helper is a fixture helper that drifts, and this study has
shipped four invented-fixture defects already.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import Eligibility, as_trade                # noqa: E402
from reaction.measure import ReactionOutcome                      # noqa: E402
from reaction.screen import (                                     # noqa: E402
    SCREENABLE, UNKNOWN_SETTLEMENT, ScreenRefusal, decision_book,
    executable_sides, execution_candle, observation_for, screen_all,
    screen_reaction,
)
from tests.test_reaction_measure import (                          # noqa: E402
    AFTER_PRICES, BEFORE_PRICES, EXCHANGE_AFTER, EXCHANGE_BEFORE, START,
    TICKER, at, candle, make_trigger, measure, series, step,
)

# The exchange sits at 0.56/0.58 -- mid 0.57, a 2c spread, executable -- while
# the book's post-move fair for the HOME side is 0.6352785. Buying YES at the
# ASK is +0.0353 per contract after fee. Every number here is checked against
# the real fee model rather than asserted.
BID, ASK = 0.56, 0.58


def book(first=210.0, last=160.0, bid=BID, ask=ASK, overrides=None):
    """A flat two-sided exchange book, one candle a minute.

    `overrides` maps minutes-before-start to a `(bid, ask)` pair. A dict
    argument rather than `**kwargs`, because minute keys are numbers and
    Python keyword names are not.
    """
    overrides = overrides or {}
    out = []
    minute = first
    while minute >= last:
        b, a = overrides.get(minute, (bid, ask))
        out.append(candle(at(minute), (b + a) / 2.0, spread=a - b))
        minute -= 1
    return out


def moved_book(first=210.0, last=160.0, switch_at=185.0,
               after=(0.64, 0.66)):
    """Flat at BID/ASK, then re-priced to `after` from `switch_at` onward."""
    switched = {m: after for m in range(int(first), int(last) - 1, -1)
                if m <= switch_at}
    return book(first, last, overrides=switched)


def screen(candles=None, trigger=None, **kwargs):
    candles = candles if candles is not None else book()
    trigger = trigger or make_trigger()
    reaction = measure(candles, trigger)
    kwargs.setdefault("market_ticker", TICKER)
    kwargs.setdefault("yes_is_home", True)
    kwargs.setdefault("start", START)
    return screen_reaction(reaction, trigger, candles, **kwargs)


class NoSecondScreenTest(unittest.TestCase):
    """This layer picks the INSTANTS. It does not redefine tradeable."""

    def test_the_verdict_is_the_checkpoint_studys_own(self):
        screened = screen(settled_yes=1)
        self.assertIsNotNone(screened.observation)
        direct = as_trade(screened.observation, eligibility=Eligibility())
        self.assertIsNotNone(direct)
        self.assertEqual(screened.trade.side, direct.side)
        self.assertEqual(screened.trade.entry_price, direct.entry_price)
        self.assertEqual(screened.trade.predicted_ev, direct.predicted_ev)
        self.assertEqual(screened.trade.fee, direct.fee)

    def test_no_threshold_is_redefined_here(self):
        """Source-level, because it is the only guard that survives a tidy-up.

        An inlined 0.01 or (0.15, 0.85) in this module would be a second
        policy that agrees today. Rule 19: it would be changed on one side.
        """
        source = (Path(__file__).resolve().parent.parent
                  / "reaction" / "screen.py").read_text()
        code = "\n".join(line.split("#")[0] for line in source.splitlines()
                         if not line.strip().startswith(("*", '"')))
        for forbidden in ("min_net_ev =", "min_net_edge =", "price_band =",
                          "max_spread =", "0.15", "0.85", "PRICE_BAND"):
            self.assertNotIn(forbidden, code,
                             f"{forbidden!r} declares a screening threshold "
                             f"in the adapter; it belongs in "
                             f"analysis.scoring")

    def test_the_eligibility_object_is_passed_through_not_reinterpreted(self):
        """A stricter policy must bite here exactly as it does there."""
        strict = Eligibility(min_net_ev=0.99)
        screened = screen(settled_yes=1, eligibility=strict)
        self.assertFalse(screened.admitted)
        self.assertIsNone(screened.trade)
        self.assertIsNone(screened.predicted_ev)


class ExecutablePriceTest(unittest.TestCase):
    """The trade happens at the ask, or at 1 - bid. Never at the mid."""

    def test_buying_yes_pays_the_ask(self):
        screened = screen(settled_yes=1)
        self.assertEqual(screened.trade.side, "YES")
        self.assertAlmostEqual(screened.trade.entry_price, ASK, places=9)
        self.assertNotAlmostEqual(screened.trade.entry_price,
                                  (BID + ASK) / 2.0, places=6)

    def test_the_predicted_edge_is_measured_from_the_ask_not_the_mid(self):
        screened = screen(settled_yes=1)
        trigger = make_trigger()
        against_ask = trigger.fair_after_home - ASK
        against_mid = trigger.fair_after_home - (BID + ASK) / 2.0
        self.assertLess(screened.trade.predicted_ev, against_ask,
                        "the fee has to come off too")
        self.assertLess(screened.trade.predicted_ev, against_mid)
        self.assertAlmostEqual(screened.trade.predicted_ev, 0.0352785,
                               places=6)

    def test_selling_yes_is_priced_as_buying_no_at_one_minus_bid(self):
        """The mirror mapping, asserted -- getting it wrong inverts half."""
        screened = screen(settled_yes=1)
        sides = {q.side: q for q in executable_sides(screened.observation)}
        self.assertAlmostEqual(sides["YES"].entry_price, ASK, places=9)
        self.assertAlmostEqual(sides["NO"].entry_price, 1.0 - BID, places=9)
        self.assertAlmostEqual(sides["YES"].win_probability
                               + sides["NO"].win_probability, 1.0, places=9)

    def test_a_midpoint_disagreement_on_a_wide_book_is_still_refused(self):
        """ROUND 2's defect, through the new entry point.

        bid .40 / ask .60 with the book's fair at .53 cleared a midpoint
        screen and bought YES at .60 plus fee -- a predicted -0.09 per
        contract before the outcome was known. The same figure, here.
        """
        wide = book(bid=0.40, ask=0.60)
        # 101/-125 de-vigs to a home fair of 0.5290216: inside the spread.
        trigger = make_trigger(after=(101, -125))
        self.assertAlmostEqual(trigger.fair_after_home, 0.5290216, places=6)
        screened = screen(wide, trigger, settled_yes=1)
        self.assertFalse(screened.admitted,
                         "a 20c spread is not an executable book")
        self.assertIsNone(screened.trade)
        sides = {q.side: q for q in executable_sides(screened.observation)}
        self.assertAlmostEqual(sides["YES"].predicted_ev, -0.0909784,
                               places=6)
        self.assertLess(sides["NO"].predicted_ev, sides["YES"].predicted_ev)

    def test_a_crossed_decision_book_is_refused(self):
        """Both sides present and the wrong way round. A book this is not."""
        crossed = book(bid=0.60, ask=0.55)
        self.assertIs(screen(crossed, settled_yes=1).refusal,
                      ScreenRefusal.CROSSED_DECISION_BOOK)

    def test_a_one_sided_book_is_caught_upstream_by_the_same_filter(self):
        """And so the screen needs no second one-sided check.

        `usable_candle` requires a mid, and `Candle.mid` is None whenever
        either side is -- so a one-sided book never reaches the screen's
        price logic at all. It surfaces one layer up, as the reaction having
        no baseline. Writing a one-sided refusal into the screen too would
        be unreachable code wearing the costume of a guard.
        """
        one_sided = [candle(c.ts, None) for c in book()]
        screened = screen(one_sided, settled_yes=1)
        self.assertEqual(screened.reaction_outcome,
                         ReactionOutcome.NO_BASELINE.value)
        self.assertIs(screened.refusal,
                      ScreenRefusal.NOT_A_MOVE_WE_COULD_TRADE)

    def test_a_candle_list_that_does_not_cover_the_trigger_is_refused(self):
        """NO_DECISION_BOOK, reached the one way it can be.

        `screen_reaction` takes its candles as a separate argument from the
        reaction, so a caller can hand it a list the reaction was never
        measured against. That mismatch is the reachable case, and it has to
        refuse rather than screen against whatever it happens to find.
        """
        measured_on = book()
        reaction = measure(measured_on, make_trigger())
        self.assertIs(reaction.outcome, ReactionOutcome.NO_RESPONSE)
        screened = screen_reaction(reaction, make_trigger(),
                                   book(150.0, 120.0),   # all AFTER the trigger
                                   market_ticker=TICKER, yes_is_home=True,
                                   start=START, settled_yes=1)
        self.assertIs(screened.refusal, ScreenRefusal.NO_DECISION_BOOK)

    def test_the_decision_book_is_the_same_candle_the_measurement_used(self):
        """rule 19: one filter, so the two layers cannot price it apart."""
        candles = book()
        trigger = make_trigger()
        reaction = measure(candles, trigger)
        chosen = decision_book(candles, trigger.detected_at)
        self.assertAlmostEqual(chosen.mid, reaction.exchange_before, places=12)
        self.assertLessEqual(chosen.ts, trigger.detected_at)


class DelayTest(unittest.TestCase):
    """A delay pays what the market became, and never reaches backwards."""

    def test_a_delay_into_a_moved_book_collapses_the_edge(self):
        """Owner's delay-misses-the-gap case, in dollars.

        The exchange re-prices to 0.64/0.66 at minute 185. A ten-minute
        delay from a trigger at minute 195 executes into that book, and the
        entry is priced there -- not at the 0.58 we saw. No extra gate does
        this: `side_quotes` simply pays the later price.
        """
        candles = moved_book(switch_at=185.0)
        prompt = screen(candles, settled_yes=1)
        late = screen(candles, settled_yes=1,
                      entry_delay=timedelta(minutes=10))
        self.assertAlmostEqual(prompt.trade.entry_price, ASK, places=9)
        self.assertIsNotNone(late.trade)
        self.assertAlmostEqual(late.trade.entry_price, ASK, places=9,
                               msg="the DECISION price is what the screen "
                                   "reasons about and must not move")
        self.assertGreater(late.trade.paid, prompt.trade.paid,
                           "but what it PAID is the moved book")
        self.assertAlmostEqual(late.trade.paid - late.trade.fee, 0.66,
                               places=9)

    def test_the_survives_delay_diagnostic_explains_the_collapse(self):
        """Three answers, carried onto the screened row."""
        candles = moved_book(switch_at=185.0)
        self.assertTrue(screen(candles, settled_yes=1,
                               entry_delay=timedelta(seconds=300))
                        .survives_delay)
        self.assertFalse(screen(candles, settled_yes=1,
                                entry_delay=timedelta(seconds=600))
                         .survives_delay)
        self.assertIsNone(screen(candles, settled_yes=1,
                                 entry_delay=timedelta(seconds=570))
                          .survives_delay,
                          "the delay falls inside the lag interval")

    def test_a_missing_delayed_quote_is_not_a_fill_at_the_decision_price(self):
        """The whole error the delay knob exists to measure."""
        candles = book(210.0, 190.0)          # nothing after minute 190
        screened = screen(candles, settled_yes=1,
                          entry_delay=timedelta(minutes=10))
        self.assertIsNone(screened.observation.entry_bid)
        self.assertIsNone(screened.observation.entry_ask)
        self.assertIsNone(screened.trade,
                          "a side with no execution price was filled anyway")
        self.assertEqual(executable_sides(screened.observation), [])

    def test_a_delayed_entry_never_reaches_backwards(self):
        earlier = execution_candle(book(), at(196), timedelta(minutes=5))
        self.assertGreaterEqual(earlier.ts, at(196))

    def test_a_quote_beyond_the_tolerance_is_not_taken(self):
        candles = book(210.0, 196.0) + book(120.0, 119.0)
        self.assertIsNone(execution_candle(candles, at(190),
                                           timedelta(minutes=5)))

    def test_zero_delay_means_the_execution_book_is_the_decision_book(self):
        screened = screen(settled_yes=1)
        self.assertEqual(screened.entry_delay_seconds, 0.0)
        self.assertAlmostEqual(screened.trade.paid,
                               screened.trade.entry_price
                               + screened.trade.fee, places=12)


class SettlementTest(unittest.TestCase):
    """A realized figure without settlement is withheld, not defaulted."""

    def test_settlement_unknown_changes_nothing_in_the_output(self):
        """THE structural proof that no settlement-derived number escapes.

        The placeholder outcome is a real value that `side_quotes` maps to a
        payout. If any realized figure leaked into the row, flipping the
        placeholder would change the row. It does not, because the realized
        block is absent entirely.
        """
        import reaction.screen as module
        rows = []
        for placeholder in (0, 1):
            original = module.UNKNOWN_SETTLEMENT
            try:
                module.UNKNOWN_SETTLEMENT = placeholder
                rows.append(screen(settled_yes=None).as_dict())
            finally:
                module.UNKNOWN_SETTLEMENT = original
        self.assertEqual(rows[0], rows[1],
                         "a settlement-derived number reached the output")
        self.assertIsNone(rows[0]["realized"])
        self.assertFalse(rows[0]["settlement_known"])
        self.assertIn("indistinguishable from a real one",
                      rows[0]["realized_withheld_because"])

    def test_the_predicted_figure_survives_unknown_settlement(self):
        """Predicted EV never reads the outcome, so it is still reportable."""
        unknown = screen(settled_yes=None)
        known = screen(settled_yes=1)
        self.assertIsNotNone(unknown.predicted_ev)
        self.assertEqual(unknown.predicted_ev, known.predicted_ev)
        self.assertIsNone(unknown.realized_profit)
        self.assertIsNotNone(known.realized_profit)

    def test_the_placeholder_cannot_manufacture_a_winning_leg(self):
        """Zero, deliberately: a truthy sentinel pays the YES leg out."""
        self.assertEqual(UNKNOWN_SETTLEMENT, 0)
        observation, refusal, _ = observation_for(
            measure(book()), make_trigger(), book(),
            market_ticker=TICKER, yes_is_home=True, start=START,
            settled_yes=None)
        self.assertIsNone(refusal)
        self.assertEqual(observation.outcome, 0)

    def test_coverage_says_how_much_can_carry_a_realized_figure(self):
        result = screen_all([
            (measure(book()), make_trigger(), book(), {"settled_yes": 1}),
            (measure(book()), make_trigger(), book(), {"settled_yes": None}),
        ], market_ticker=TICKER, yes_is_home=True, start=START)
        coverage = result.settlement_coverage()
        self.assertEqual(coverage["screened"], 2)
        self.assertEqual(coverage["settlement_known"], 1)
        self.assertEqual(coverage["settlement_unknown"], 1)
        self.assertTrue(coverage["realized_reportable"])


class DenominatorTest(unittest.TestCase):
    """The rows that falsify the thesis stay in the denominator."""

    def test_an_already_priced_reaction_is_still_screened(self):
        """Dropping it would remove the falsifying rows from the count.

        The exchange having moved first does not make the observation
        unreal. It makes its edge small -- which is the finding, and it has
        to be counted, not filtered.
        """
        self.assertIn(ReactionOutcome.ALREADY_PRICED, SCREENABLE)
        early = step(215, 160, EXCHANGE_BEFORE, EXCHANGE_AFTER, switch_at=205)
        two_sided = [candle(c.ts, c.mid, spread=0.02) for c in early]
        screened = screen(two_sided, settled_yes=1)
        self.assertEqual(screened.reaction_outcome,
                         ReactionOutcome.ALREADY_PRICED.value)
        self.assertIsNone(screened.refusal,
                          "a falsifying row was refused rather than counted")
        self.assertIsNotNone(screened.observation)

    def test_a_blind_or_censored_reaction_is_still_screened(self):
        """Whether the exchange followed does not change what we could buy."""
        for outcome in (ReactionOutcome.NO_RESPONSE,
                        ReactionOutcome.BLIND_INTERVAL,
                        ReactionOutcome.OPPOSITE_DIRECTION):
            self.assertIn(outcome, SCREENABLE)

    def test_an_undateable_reaction_is_refused_and_named(self):
        candles = book()
        reaction = measure(series(190, 160, EXCHANGE_BEFORE))
        self.assertIs(reaction.outcome, ReactionOutcome.NO_BASELINE)
        screened = screen_reaction(reaction, make_trigger(), candles,
                                   market_ticker=TICKER, yes_is_home=True,
                                   start=START, settled_yes=1)
        self.assertIs(screened.refusal,
                      ScreenRefusal.NOT_A_MOVE_WE_COULD_TRADE)
        self.assertIn("no decision instant", screened.detail)

    def test_refusals_are_counted_by_name(self):
        result = screen_all([
            (measure(book()), make_trigger(), book(), {"settled_yes": 1}),
            (measure(book()), make_trigger(), book(),
             {"settled_yes": 1, "start": None}),
            (measure(book(bid=0.60, ask=0.55)), make_trigger(),
             book(bid=0.60, ask=0.55), {"settled_yes": 1}),
        ], market_ticker=TICKER, yes_is_home=True, start=START)
        self.assertEqual(result.refusal_counts(), {
            ScreenRefusal.NO_START_TIME.value: 1,
            ScreenRefusal.CROSSED_DECISION_BOOK.value: 1,
        })
        self.assertEqual(result.as_dict()["total"], 3)
        self.assertEqual(len(result.observations), 1)


class StartTimeTest(unittest.TestCase):
    """A kickoff is supplied or refused. It is never inferred."""

    def test_a_missing_kickoff_is_refused_not_assumed(self):
        screened = screen(settled_yes=1, start=None)
        self.assertIs(screened.refusal, ScreenRefusal.NO_START_TIME)
        self.assertIn("never inferred from a settlement or expiry stamp",
                      screened.detail)

    def test_a_trigger_at_or_after_kickoff_is_not_a_pre_match_opportunity(self):
        candles = book(20.0, -20.0)
        trigger = make_trigger(baseline_snapshot=at(5), baseline_observed=at(6),
                               moved_snapshot=at(-5), moved_observed=at(-4))
        self.assertGreater(trigger.detected_at, START)
        screened = screen(candles, trigger, settled_yes=1)
        self.assertIs(screened.refusal,
                      ScreenRefusal.TRIGGER_AT_OR_AFTER_START)

    def test_lead_time_is_measured_from_the_executable_clock(self):
        screened = screen(settled_yes=1)
        self.assertAlmostEqual(screened.observation.minutes_to_start, 195.0,
                               places=6)
        self.assertEqual(screened.observation.decision_at, at(195))


class ProvenanceTest(unittest.TestCase):
    """The observation says where every number came from."""

    def test_the_observation_carries_both_book_clocks_apart(self):
        screened = screen(settled_yes=1)
        observation = screened.observation
        self.assertEqual(observation.sharp_snapshot_at, at(195),
                         "the snapshot that revealed the move")
        self.assertEqual(observation.sharp_at, at(196),
                         "the provider's observation stamp -- a DIFFERENT "
                         "clock, kept apart")
        self.assertNotEqual(observation.sharp_at, observation.sharp_snapshot_at)
        self.assertLessEqual(observation.exchange_at, observation.decision_at)

    def test_the_devig_method_travels_with_the_observation(self):
        self.assertEqual(screen(settled_yes=1).observation.devig_method,
                         "shin")

    def test_the_yes_participant_comes_from_the_ticker_suffix(self):
        """Never from splitting the matchup body, which has several readings."""
        self.assertEqual(screen(settled_yes=1).observation.yes_participant,
                         "NYG")

    def test_the_orientation_decides_the_sharp_probability(self):
        home = screen(settled_yes=1, yes_is_home=True).observation
        away = screen(settled_yes=1, yes_is_home=False).observation
        self.assertAlmostEqual(home.p_sharp + away.p_sharp, 1.0, places=9)
        self.assertGreater(home.p_sharp, away.p_sharp)

    def test_the_row_reports_the_reaction_outcome_and_ordering(self):
        row = screen(moved_book(switch_at=185.0), settled_yes=1).as_dict()
        self.assertEqual(row["reaction_outcome"],
                         ReactionOutcome.RESPONDED.value)
        self.assertEqual(row["ordering"], "book_led")
        self.assertIn("never reads settlement", row["predicted"]["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
