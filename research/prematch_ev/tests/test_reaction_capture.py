"""The capture contract: bounded, read-only, and refused at the limit.

WHAT IS UNDER TEST
------------------
Not a capture -- there isn't one, and live capture is not authorised. These
check the CONTRACT and its guards:

* `BudgetTest` -- the bound REFUSES rather than warns. A warning on the last
  request is indistinguishable from a warning on the first, and the thing
  being bounded is money.
* `ReadOnlyTest` -- the read-only guarantee is a source-level check over the
  whole reaction package, so it survives someone adding a convenient import.
  "We would never place an order here" is what every script with an
  order-placing client in scope was written under.
* `PlanTest` -- the enforced budget is DERIVED from the approved plan, so an
  approved plan cannot be run with a wider bound than the one approved.
* `AuthorisationTest` -- the plan says `authorised: False` and calls its
  credit figure a price. A plan is not a permission.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaction.capture import (                                    # noqa: E402
    ALLOWED_ENDPOINTS, FORBIDDEN_CAPABILITIES, CaptureBudget, CapturePlan,
    CaptureRefused, TradingCapabilityPresent, assert_read_only,
    endpoint_allowed, require_allowed_endpoint,
)

UTC = timezone.utc


def plan(**overrides) -> CapturePlan:
    base = dict(
        purpose="event-driven reaction pilot",
        sport="americanfootball_nfl", series="KXNFLGAME",
        first_day=datetime(2026, 9, 25, tzinfo=UTC),
        last_day=datetime(2026, 9, 29, tzinfo=UTC),
        snapshots_per_day=96, contracts_expected=32, schedule_requests=5)
    base.update(overrides)
    return CapturePlan(**base)


class BudgetTest(unittest.TestCase):
    """A bound that warns and continues is not a bound."""

    def test_spending_past_the_credit_bound_raises(self):
        budget = CaptureBudget(max_requests=10, max_credits=10)
        budget.spend(9, "first")
        with self.assertRaises(CaptureRefused) as caught:
            budget.spend(2, "one too many")
        self.assertIn("past the declared bound of 10", str(caught.exception))
        self.assertEqual(budget.credits_spent, 9, "the refusal did not consume")

    def test_spending_past_the_request_bound_raises(self):
        budget = CaptureBudget(max_requests=2, max_credits=1000)
        budget.spend(1)
        budget.spend(1)
        with self.assertRaises(CaptureRefused) as caught:
            budget.spend(1)
        self.assertIn("exceeds the declared bound of 2", str(caught.exception))
        self.assertEqual(budget.requests_made, 2)

    def test_the_exact_bound_is_allowed_and_the_next_is_not(self):
        """Off by one here costs a request, or blocks a legitimate plan."""
        budget = CaptureBudget(max_requests=3, max_credits=30)
        for _ in range(3):
            budget.spend(10)
        self.assertEqual(budget.credits_left, 0)
        self.assertEqual(budget.requests_left, 0)
        with self.assertRaises(CaptureRefused):
            budget.spend(0)

    def test_would_exceed_reports_without_consuming(self):
        """A caller has to be able to ask before committing."""
        budget = CaptureBudget(max_requests=5, max_credits=5)
        self.assertIsNone(budget.would_exceed(5))
        self.assertIsNotNone(budget.would_exceed(6))
        self.assertEqual(budget.requests_made, 0, "asking consumed budget")
        self.assertEqual(budget.credits_spent, 0)

    def test_a_negative_or_non_integer_spend_is_refused(self):
        budget = CaptureBudget(max_requests=5, max_credits=5)
        for bad in (-1, 1.5, "one", None):
            with self.subTest(credits=bad):
                with self.assertRaises(ValueError):
                    budget.spend(bad)          # type: ignore[arg-type]

    def test_a_negative_bound_is_refused_at_construction(self):
        for field in ("max_requests", "max_credits"):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    CaptureBudget(**{"max_requests": 1, "max_credits": 1,
                                     field: -1})

    def test_a_zero_budget_permits_nothing(self):
        """The honest default for an unauthorised capture."""
        budget = CaptureBudget(max_requests=0, max_credits=0)
        with self.assertRaises(CaptureRefused):
            budget.spend(0, "even a free request")

    def test_every_spend_is_logged_with_what_it_was_for(self):
        budget = CaptureBudget(max_requests=3, max_credits=30)
        budget.spend(10, "odds snapshot 1")
        budget.spend(0, "kalshi candles")
        self.assertEqual(len(budget.log), 2)
        self.assertIn("odds snapshot 1", budget.log[0])
        self.assertIn("0 credit(s)", budget.log[1])


class ReadOnlyTest(unittest.TestCase):
    """The guarantee is structural, and checked against the source."""

    def test_the_reaction_package_holds_no_trading_capability(self):
        assert_read_only()

    def test_the_check_actually_fires_on_a_trading_capability(self):
        """Or it is a decoration that passes because nothing is there.

        `assert_read_only` is only worth having if it fails when it should,
        so it is pointed at a directory that deliberately contains what it
        forbids.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "rogue.py").write_text(
                "def go(client):\n"
                "    return client.create_order(ticker='X', count=1)\n")
            with self.assertRaises(TradingCapabilityPresent) as caught:
                assert_read_only(path)
            self.assertIn("create_order", str(caught.exception))
            self.assertIn("rogue.py", str(caught.exception))

    def test_it_catches_a_signing_credential_too(self):
        """Not just orders: authenticating as a trader is the capability."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "signer.py").write_text("KEY = KALSHI_PRIVATE_KEY\n")
            with self.assertRaises(TradingCapabilityPresent):
                assert_read_only(path)

    def test_a_clean_directory_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "fine.py").write_text("def get(url):\n    return 200, b''\n")
            assert_read_only(path)

    def test_every_forbidden_name_is_detected(self):
        """A list nothing checks is a list that grows wrong entries."""
        for name in FORBIDDEN_CAPABILITIES:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory)
                    (path / "x.py").write_text(f"X = '{name}'\n")
                    with self.assertRaises(TradingCapabilityPresent):
                        assert_read_only(path)


class EndpointTest(unittest.TestCase):
    """The allowed set is a decision, not whatever a URL builder produces."""

    def test_the_declared_read_only_endpoints_are_allowed(self):
        for url in (
            "https://api.the-odds-api.com/v4/historical/sports/"
            "americanfootball_nfl/odds?apiKey=x&date=2026-09-25T00:00:00Z",
            "https://api.elections.kalshi.com/trade-api/v2/series/"
            "KXNFLGAME/markets",
            "https://api.elections.kalshi.com/trade-api/v2/series/KXNFLGAME"
            "/markets/KXNFLGAME-26SEP14DENKC-KC/candlesticks"
            "?period_interval=1",
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
            "scoreboard?dates=20260925",
        ):
            with self.subTest(url=url[:60]):
                self.assertTrue(endpoint_allowed(url))
                require_allowed_endpoint(url)

    def test_an_order_placing_path_is_refused(self):
        """Same host, different path. The host is not the permission."""
        url = "https://api.elections.kalshi.com/trade-api/v2/portfolio/orders"
        self.assertFalse(endpoint_allowed(url))
        with self.assertRaises(CaptureRefused) as caught:
            require_allowed_endpoint(url)
        self.assertIn("not one of the declared read-only endpoints",
                      str(caught.exception))

    def test_an_undeclared_host_is_refused(self):
        for url in ("https://example.com/anything",
                    "https://api.the-odds-api.com/v4/sports",
                    "http://localhost:8080/orders"):
            with self.subTest(url=url):
                self.assertFalse(endpoint_allowed(url))

    def test_a_credential_in_the_query_does_not_affect_the_verdict(self):
        """The path decides. A key is not a permission either."""
        with_key = ("https://api.the-odds-api.com/v4/historical/sports/"
                    "baseball_mlb/odds?apiKey=secret")
        without = ("https://api.the-odds-api.com/v4/historical/sports/"
                   "baseball_mlb/odds")
        self.assertEqual(endpoint_allowed(with_key), endpoint_allowed(without))
        self.assertTrue(endpoint_allowed(without))


class PlanTest(unittest.TestCase):
    """The budget is derived from the plan, so it cannot be widened."""

    def test_the_budget_matches_the_plan_exactly(self):
        subject = plan()
        budget = subject.budget()
        self.assertEqual(budget.max_requests, subject.total_requests)
        self.assertEqual(budget.max_credits, subject.odds_credits)
        self.assertEqual(budget.requests_made, 0)

    def test_the_derived_budget_refuses_the_request_after_the_plan(self):
        subject = plan(snapshots_per_day=1, contracts_expected=1,
                       schedule_requests=0,
                       first_day=datetime(2026, 9, 25, tzinfo=UTC),
                       last_day=datetime(2026, 9, 25, tzinfo=UTC))
        self.assertEqual(subject.total_requests, 2)
        budget = subject.budget()
        budget.spend(subject.odds_credits, "the one odds snapshot")
        budget.spend(0, "the one candlestick request")
        with self.assertRaises(CaptureRefused):
            budget.spend(0, "a request the plan did not declare")

    def test_the_credit_estimate_is_the_shared_cost_model(self):
        """rule 19: called, not restated, so a correction reaches every plan."""
        from data.odds_history import estimate_credits
        subject = plan()
        self.assertEqual(subject.odds_credits,
                         estimate_credits(subject.days,
                                          subject.snapshots_per_day,
                                          subject.regions, subject.markets))

    def test_free_requests_are_counted_but_cost_nothing(self):
        """Kalshi candles and the ESPN scoreboard are unauthenticated."""
        subject = plan(contracts_expected=32, schedule_requests=5)
        self.assertEqual(subject.free_requests, 37)
        cheaper = plan(contracts_expected=0, schedule_requests=0)
        self.assertEqual(subject.odds_credits, cheaper.odds_credits,
                         "free requests must not move the credit figure")
        self.assertGreater(subject.total_requests, cheaper.total_requests)

    def test_the_day_count_is_inclusive(self):
        """An off-by-one here under-quotes the cost of the last day."""
        self.assertEqual(plan(first_day=datetime(2026, 9, 25, tzinfo=UTC),
                              last_day=datetime(2026, 9, 25, tzinfo=UTC))
                         .days, 1)
        self.assertEqual(plan().days, 5)

    def test_denser_snapshots_cost_more_which_is_the_whole_tension(self):
        """Event-driven detection WANTS the 5-minute grid, and it is priced.

        A tighter cadence narrows the book-change bracket -- that is the
        measurement improving -- and it multiplies the credit figure. The
        plan has to make that visible rather than letting a default decide
        it.
        """
        fifteen_minute = plan(snapshots_per_day=96)
        five_minute = plan(snapshots_per_day=288)
        self.assertEqual(five_minute.odds_requests,
                         3 * fifteen_minute.odds_requests)
        self.assertGreater(five_minute.odds_credits,
                           fifteen_minute.odds_credits)


class AuthorisationTest(unittest.TestCase):
    """A plan is not a permission, and it says so."""

    def test_the_plan_declares_itself_unauthorised(self):
        row = plan().as_dict()
        self.assertFalse(row["authorised"])
        self.assertIn("does not carry over", row["authorisation_note"])
        self.assertIn("a PRICE, not an authorisation", row["credits_note"])

    def test_the_rendered_plan_leads_with_not_authorised(self):
        text = plan().render()
        self.assertIn("NOT AUTHORISED, NOT EXECUTED", text.splitlines()[0])
        self.assertIn("PRICE, not an authorisation", text)
        self.assertIn("1,500-credit approval does not carry over", text)

    def test_the_refusals_name_what_it_cannot_do(self):
        refusals = " ".join(plan().refusals())
        for commitment in ("place an order", "authenticate as a trader",
                           "outside ALLOWED_ENDPOINTS", "exceed the request",
                           "holdout"):
            self.assertIn(commitment, refusals)

    def test_the_plan_lists_the_endpoints_it_would_reach(self):
        row = plan().as_dict()
        self.assertEqual(row["allowed_endpoints"], list(ALLOWED_ENDPOINTS))
        for endpoint in ALLOWED_ENDPOINTS:
            self.assertIn(endpoint, plan().render())

    def test_no_transport_is_importable_from_this_module(self):
        """A module that could fetch would eventually fetch."""
        import reaction.capture as module
        source = Path(module.__file__).read_text()
        for name in ("import requests", "urllib.request", "http.client",
                     "urlopen", "socket"):
            self.assertNotIn(name, source,
                             f"{name!r} puts a transport one import away")


class PilotProposalTest(unittest.TestCase):
    """The proposal's numbers are checked against the cost model.

    A document nothing verifies drifts, and a credit figure in a proposal is
    read as a fact. These parse `REACTION_PILOT.md` and recompute every
    option in its cost table, so a change to `estimate_credits` -- or a typo
    in the table -- fails the build instead of quietly mispricing a decision
    someone is about to make.
    """

    PROPOSAL = (Path(__file__).resolve().parent.parent / "REACTION_PILOT.md")

    # (label, days, snapshots_per_day) as the table's rows describe them.
    OPTIONS = {
        "A": (1, 108), "B": (1, 36), "C": (1, 216),
        "D": (3, 108), "E": (10, 108),
    }

    def setUp(self):
        self.raw = self.PROPOSAL.read_text()
        # Assert the CLAIM, not the typography. Prose carries en-dashes,
        # markdown emphasis and sentence capitals, and a test that breaks on
        # a dash style gets "fixed" by loosening it until it checks nothing.
        self.text = (self.raw.lower()
                     .replace("\u2013", "-").replace("\u2014", "-")
                     .replace("*", "").replace("`", ""))

    def assertClaim(self, needle: str, where: str | None = None):
        """assertIn without dumping the whole document on failure."""
        haystack = self.text if where is None else where
        if needle.lower() not in haystack:
            self.fail(f"the proposal does not claim {needle!r}")

    def test_every_cost_table_row_matches_the_shared_cost_model(self):
        from data.odds_history import estimate_credits
        for label, (days, snaps) in self.OPTIONS.items():
            with self.subTest(option=label):
                requests = days * snaps
                credits = estimate_credits(days, snaps, 1, 1)
                row = [line for line in self.raw.splitlines()
                       if line.startswith(f"| **{label}**")
                       or line.startswith(f"| {label} |")]
                self.assertEqual(len(row), 1,
                                 f"option {label} is not a single table row")
                self.assertIn(f"| {requests:,} |", row[0].replace(",", ","),
                              f"option {label}: request count")
                self.assertIn(f"**{credits:,}**", row[0],
                              f"option {label}: credit figure")

    def test_the_recommended_option_is_the_one_the_text_argues_for(self):
        """And its figure is the derived one, not a restated one."""
        from data.odds_history import estimate_credits
        days, snaps = self.OPTIONS["A"]
        credits = estimate_credits(days, snaps, 1, 1)
        self.assertClaim(f"recommendation: option a, {credits:,} credits")
        self.assertClaim(f"option a = {credits:,} credits")

    def test_the_cheap_option_is_marked_as_unable_to_answer(self):
        """A table whose cheapest row is not flagged invites the wrong pick."""
        self.assertClaim("option b is a trap")
        self.assertClaim("900s")

    def test_the_proposal_states_it_is_not_authorised(self):
        status = self.text.split("---", 1)[0]
        self.assertClaim("a proposal", status)
        self.assertClaim("nothing here is authorised", status)
        self.assertClaim("nothing here has been", status)
        self.assertClaim("does not carry over")

    def test_the_resolution_floor_in_the_text_is_the_audits_own(self):
        """rule 19: one constant. A doc restating it would drift."""
        from reaction.capability import (
            KALSHI_CANDLE_FLOOR_SECONDS, ODDS_SNAPSHOT_GRID_SECONDS,
        )
        self.assertEqual(ODDS_SNAPSHOT_GRID_SECONDS, 300.0)
        self.assertEqual(KALSHI_CANDLE_FLOOR_SECONDS, 60.0)
        self.assertClaim("300-second grid")
        self.assertClaim("1-minute")
        self.assertClaim("beyond 300s")

    def test_the_proposal_declares_its_decision_rule_before_any_data(self):
        """Otherwise it is a rationalisation written after the fact."""
        self.assertClaim("declared now")
        self.assertClaim("one third")
        self.assertClaim("before any data")

    def test_the_holdout_section_matches_the_code(self):
        from reaction.episodes import DEVELOPMENT_WINDOW
        self.assertClaim("september 1-16 2026")
        self.assertEqual(DEVELOPMENT_WINDOW[0].isoformat(), "2026-09-01")
        self.assertEqual(DEVELOPMENT_WINDOW[1].isoformat(), "2026-09-16")
        self.assertClaim("holdoutviolation")
        self.assertClaim("holdout_available")

    def test_every_documented_cli_flag_exists(self):
        """A proposal that tells the reader to run something it cannot."""
        import run_reaction
        for flag in ("--capability", "--capability-verify", "--policy",
                     "--replay"):
            self.assertClaim(flag)
            parsed = run_reaction.parse_args(
                [flag, "x"] if flag == "--replay" else [flag])
            self.assertIsNotNone(parsed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
