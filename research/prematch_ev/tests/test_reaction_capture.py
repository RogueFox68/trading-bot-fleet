"""The capture contract: bounded, read-only, and refused at the limit.

WHAT IS UNDER TEST
------------------
Not a capture -- the captures are `collect_reaction.py` and
`shadow_monitor.py`, tested in their own suites, and nothing here authorises
either. These check the CONTRACT and its guards:

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

import contextlib
import io
import re
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

# --- the timestamp manifest -------------------------------------------------
#
# The cost model used to be `days x snapshots_per_day`, which answered a
# different question and was wrong twice over for a 72-hour window: `days`
# counts INCLUSIVE CALENDAR DATES (Thursday to Sunday is 72 elapsed hours but
# four dates), and the formula cannot express two kickoff clusters sharing
# most of their windows. It quoted 192 requests for a window holding 145.

from datetime import timedelta as _td                              # noqa: E402

from reaction.capture import (                                     # noqa: E402
    ARCHIVE_GRID, WINDOW_IS_CLOSED_AT_BOTH_ENDS, CaptureWindow,
    build_manifest,
)

#: One NFL Sunday (2026-09-27), the three kickoff clusters in UTC.
CLUSTERS = (datetime(2026, 9, 27, 17, 0, tzinfo=UTC),
            datetime(2026, 9, 27, 20, 25, tzinfo=UTC),
            datetime(2026, 9, 28, 0, 20, tzinfo=UTC))
LEAD = _td(hours=72)


def windows(cadence_minutes: int, kickoffs=CLUSTERS):
    cadence = _td(minutes=cadence_minutes)
    return [CaptureWindow(k, LEAD, cadence) for k in kickoffs]


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


def _matches(template: str, url: str) -> bool:
    """Does `url` fall under this ONE allow-list entry?"""
    bare = url.split("?", 1)[0]
    pattern = "^https?://" + re.escape(template).replace(
        r"\{", "{").replace(r"\}", "}")
    pattern = re.sub(r"\{[a-z_]+\}", "[^/]+", pattern) + "$"
    return re.match(pattern, bare) is not None


class EndpointTest(unittest.TestCase):
    """The allowed set is a decision, not whatever a URL builder produces."""

    @staticmethod
    def _requested_urls() -> list[str]:
        """Every URL the real fetchers build, captured at the socket boundary.

        The allow-list was first written from a reading of the providers'
        APIs rather than from `data/`, and it was wrong in BOTH directions: it
        named an endpoint no fetcher calls and omitted four that they do. So
        these URLs are not typed here -- each fetcher is driven, and whatever
        reaches `urlopen` is what gets checked.
        """
        from unittest import mock
        from data import espn_schedule, kalshi_history, odds_history, odds_live
        from data.odds_history import CreditLedger
        seen: list[str] = []

        def record(request, *args, **kwargs):
            seen.append(getattr(request, "full_url", request))
            raise OSError("recorded, not sent")

        at = datetime(2026, 9, 13, 17, tzinfo=UTC)
        with mock.patch("urllib.request.urlopen", side_effect=record), \
                mock.patch("time.sleep"):
            odds_history.fetch_snapshot("NFL", at, "KEY")
            kalshi_history.fetch_historical_cutoff()
            kalshi_history.enumerate_settled_markets("KXNFLGAME", max_pages=1)
            for archive in (False, True):
                kalshi_history.fetch_candlestick_payload(
                    "KXNFLGAME-26SEP13DALNYG-NYG", "KXNFLGAME", at,
                    at + _td(hours=1), use_archive=archive)
            espn_schedule.fetch_schedule("NFL", at.date(), at.date(),
                                         buffer_days=0)
            # The shadow monitor's live fetchers.
            odds_live.fetch_live_odds("NFL", "KEY", ledger=CreditLedger(),
                                      now=lambda: at)
            kalshi_history.enumerate_open_markets("KXNFLGAME", max_pages=1)
            kalshi_history.fetch_orderbook_payload(
                "KXNFLGAME-26SEP13DALNYG-NYG")
        return seen

    def test_every_url_a_fetcher_requests_is_declared(self):
        urls = self._requested_urls()
        self.assertTrue(urls, "no fetcher reached the socket boundary")
        for url in urls:
            with self.subTest(url=url.split("?", 1)[0]):
                self.assertTrue(endpoint_allowed(url))

    def test_every_declared_endpoint_is_one_a_fetcher_requests(self):
        """A phantom entry is a permission nothing needs -- and the first
        version's phantom was on the exchange host."""
        urls = self._requested_urls()
        for template in ALLOWED_ENDPOINTS:
            with self.subTest(endpoint=template):
                probe = [u for u in urls
                         if endpoint_allowed(u)
                         and _matches(template, u)]
                self.assertTrue(probe, f"{template} is declared but no "
                                       f"fetcher requests it")

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
    """The proposal's numbers are recomputed from the manifest that prices it.

    A document nothing verifies drifts, and a credit figure in a proposal is
    read as a fact. Every cost row here is rebuilt with `build_manifest` --
    so a change to the cost model, the window convention or the retry
    reserve fails the build instead of quietly mispricing a decision someone
    is about to make.

    Three earlier revisions each shipped a defect this class now pins: a
    near-kickoff window substituted for the multi-day one, a decision rule
    that no run could satisfy, and a cost table computed from calendar dates
    rather than instants.
    """

    PROPOSAL = (Path(__file__).resolve().parent.parent / "REACTION_PILOT.md")

    #: label -> (cadence minutes, kickoff clusters, retry fraction, lead h)
    OPTIONS = {
        "0": (1440, CLUSTERS[:1], 0.0, 72),
        "A": (30, CLUSTERS, 0.10, 72),
        "B": (60, CLUSTERS, 0.10, 72),
        "C": (15, CLUSTERS, 0.10, 72),
        "D": (5, CLUSTERS, 0.10, 72),
        "D48": (5, CLUSTERS, 0.10, 48),
    }
    #: The owner's call (2026-09-22): finer data over more of it.
    RECOMMENDED = "D"
    FALLBACK = "D48"
    COARSE = "A"
    TRAP = "B"
    PROBE = "0"
    BUDGET = 20_000

    def setUp(self):
        self.raw = self.PROPOSAL.read_text()
        # Assert the CLAIM, not the typography: prose carries en-dashes,
        # markdown emphasis, sentence capitals and LINE WRAPPING, and a test
        # that breaks on any of those gets "fixed" by loosening it until it
        # checks nothing.
        text = (self.raw.lower()
                .replace("\u2013", "-").replace("\u2014", "-")
                .replace("\u2192", "->")
                .replace("*", "").replace("`", ""))
        self.text = " ".join(text.split())

    def assertClaim(self, needle: str, where: str | None = None):
        haystack = self.text if where is None else where
        if " ".join(needle.lower().split()) not in haystack:
            self.fail(f"the proposal does not claim {needle!r}")

    #: Phrases that mark a quotation as a RETRACTION rather than an
    #: assertion. SPECIFIC ones only: an earlier list included "it does
    #: not", which appears in ordinary prose and excused whatever sat near
    #: it -- including a re-asserted claim placed next to the correction.
    RETRACTION_MARKERS = (
        "was simply wrong", "is removed", "are removed",
        "a previous revision", "an earlier claim", "an earlier revision",
        "substituted", "that was a different thesis",
    )

    def refuteClaim(self, needle: str):
        """The proposal must not ASSERT this, though it may retract it.

        A plain absence check fails for the right reason here: the document
        quotes each removed claim in order to correct it, and deleting the
        quotation to satisfy a test would lose the record of why it changed.

        So each occurrence must carry a retraction marker IN ITS OWN SENTENCE
        OR AN ADJACENT ONE. A first version accepted any marker within 400
        characters, and it had no teeth exactly where a regression is most
        likely: a sentence reverted to the old claim right beside the
        correction was excused by the correction's own markers.
        """
        target = " ".join(needle.lower().split())
        sentences = [part for part in
                     re.split(r"(?<=[.!?])\s+", self.text) if part]
        for index, sentence in enumerate(sentences):
            if target not in sentence:
                continue
            scope = " ".join(sentences[max(0, index - 1): index + 2])
            if not any(marker in scope
                       for marker in self.RETRACTION_MARKERS):
                self.fail(f"the proposal asserts {needle!r} with no "
                          f"retraction in or beside that sentence: "
                          f"{sentence[:120]!r}")

    def _row(self, label: str) -> str:
        rows = [line for line in self.raw.splitlines()
                if line.startswith(f"| **{label}**")
                or line.startswith(f"| {label} |")]
        self.assertEqual(len(rows), 1,
                         f"option {label} is not a single table row")
        return rows[0]

    def _manifest(self, label: str, *, aligned: bool = True):
        cadence, kickoffs, retry, lead = self.OPTIONS[label]
        align = (None if label == self.PROBE or not aligned
                 else _td(minutes=cadence))
        spans = [CaptureWindow(k, _td(hours=lead), _td(minutes=cadence))
                 for k in kickoffs]
        return build_manifest(spans, align_to=align, retry_fraction=retry)

    def test_every_cost_row_is_recomputed_from_the_manifest(self):
        for label in self.OPTIONS:
            with self.subTest(option=label):
                manifest, row = self._manifest(label), self._row(label)
                self.assertIn(f"| {manifest.requests:,} |", row,
                              f"option {label}: distinct instants")
                self.assertIn(f"| {manifest.requests_with_retries:,} |", row,
                              f"option {label}: requests with retries")
                self.assertIn(f"**{manifest.credits:,}**", row,
                              f"option {label}: credit figure")

    def test_each_bracket_column_is_its_own_cadence(self):
        """The whole cost/resolution argument rests on that identity."""
        for label, (cadence, _, _, _) in self.OPTIONS.items():
            if label == self.PROBE:
                continue
            with self.subTest(option=label):
                self.assertIn(f"| {cadence * 60:,}s |", self._row(label))

    def test_the_recommended_option_is_the_one_the_text_argues_for(self):
        credits = self._manifest(self.RECOMMENDED).credits
        self.assertClaim(f"recommendation: option "
                         f"{self.RECOMMENDED.lower()}, {credits:,} credits")
        self.assertClaim(f"option {self.RECOMMENDED.lower()} = "
                         f"{credits:,} credits")
        fallback = self._manifest(self.FALLBACK).credits
        self.assertClaim(f"or {self.FALLBACK.lower()} at {fallback:,} if the "
                         f"sharp book first appears at t-48h")

    def test_the_recommendation_is_the_finest_grid_the_archive_sells(self):
        """Finer over more: the recommended cadence IS the archive floor."""
        cadence = self.OPTIONS[self.RECOMMENDED][0]
        self.assertEqual(_td(minutes=cadence), ARCHIVE_GRID)
        self.assertClaim("finer data over more of it")

    def test_the_budget_arithmetic_is_the_manifests(self):
        """The recommendation fits the stated budget, and the remainder the
        text quotes is what is actually left after it."""
        probe = self._manifest(self.PROBE).credits
        run = self._manifest(self.RECOMMENDED).credits
        left = self.BUDGET - probe - run
        self.assertGreaterEqual(left, 0)
        self.assertClaim(f"{self.BUDGET:,}-credit account budget")
        self.assertClaim(f"{left:,} credits remain of the {self.BUDGET:,}")

    def test_the_international_kickoff_increment_is_the_manifests(self):
        london = datetime(2026, 9, 27, 13, 30, tzinfo=UTC)
        cadence, kickoffs, retry, lead = self.OPTIONS[self.RECOMMENDED]
        spans = [CaptureWindow(k, _td(hours=lead), _td(minutes=cadence))
                 for k in (london, *kickoffs)]
        wider = build_manifest(spans, align_to=_td(minutes=cadence),
                               retry_fraction=retry)
        base = self._manifest(self.RECOMMENDED)
        self.assertClaim(f"adds {wider.requests - base.requests} instants and "
                         f"{wider.credits - base.credits:,} credits")

    def test_the_probe_gates_the_measurement_spend(self):
        credits = self._manifest(self.PROBE).credits
        self.assertClaim(f"{credits:,}-credit coverage probe")
        self.assertClaim("unverified for nfl")
        self.assertClaim("nothing below should be approved before it answers")

    def test_the_recommendation_covers_the_multi_day_horizon(self):
        row = self._row(self.RECOMMENDED).lower()
        self.assertIn(f"{self.OPTIONS[self.RECOMMENDED][0]}-min", row)
        self.assertEqual(self.OPTIONS[self.RECOMMENDED][3], 72)
        self.assertClaim("t-72h -> kickoff")
        self.refuteClaim("3h before each of 3 kickoff clusters")

    def test_the_cheap_option_is_marked_as_unable_to_answer(self):
        cadence, _, _, _ = self.OPTIONS[self.TRAP]
        self.assertClaim(f"option {self.TRAP.lower()} is a trap")
        self.assertClaim(f"{cadence * 60:,}s")

    def test_the_alignment_saving_is_the_real_one(self):
        """On a coarse grid, unaligned clusters nearly triple the bill, which
        is not obvious and is the single biggest lever in the table."""
        unaligned = self._manifest(self.COARSE, aligned=False)
        aligned = self._manifest(self.COARSE)
        self.assertClaim(f"{unaligned.requests:,} instants instead of "
                         f"{aligned.requests:,}")
        self.assertClaim(f"{unaligned.credits:,} credits instead of "
                         f"{aligned.credits:,}")

    def test_alignment_saves_nothing_at_the_floor(self):
        """NFL kickoffs sit on five-minute marks, so the recommended grid
        shares instants unaided -- the claim, and the manifest behind it."""
        self.assertEqual(
            self._manifest(self.RECOMMENDED, aligned=False).requests,
            self._manifest(self.RECOMMENDED).requests)
        self.assertClaim("at the 5-minute floor alignment saves nothing")

    def test_the_calendar_estimate_disagreement_is_stated(self):
        manifest = build_manifest(windows(30, CLUSTERS[:1]))
        self.assertClaim("inclusive calendar dates")
        self.assertClaim("192")
        self.assertClaim(f"{manifest.requests}")

    # --- the rule ---------------------------------------------------------

    def test_the_unsatisfiable_rule_is_named_and_its_ceiling_is_the_code(self):
        """The defect: a rule needing >1,800s from a policy that tops out at
        1,740s. Both numbers are taken from the code, not the prose."""
        from reaction.measure import ReactionPolicy
        policy = ReactionPolicy()
        self.assertEqual(policy.max_reportable_lag_seconds, 1740.0)
        self.assertClaim(f"beyond {policy.max_wait.total_seconds():,.0f}s")
        self.assertClaim(f"{policy.max_reportable_lag_seconds:,.0f}s")
        self.assertClaim("no measured reaction could ever have satisfied it")

    def test_the_declared_rule_is_reachable_against_the_declared_policy(self):
        """The whole point. A rule nothing can satisfy is not a rule."""
        from reaction.episodes import FeasibilityRule
        from reaction.measure import ReactionPolicy
        rule = FeasibilityRule()
        self.assertIsNone(rule.unreachable_against(ReactionPolicy()))
        self.assertIsNone(rule.min_lag_seconds)
        self.assertClaim(f"at least {rule.min_determinate}")
        self.assertClaim(f"{rule.min_book_led_fraction:.0%}")

    def test_all_three_verdicts_are_documented(self):
        from reaction.episodes import Feasibility
        for verdict in Feasibility:
            if verdict is Feasibility.UNREACHABLE:
                continue                      # named in its own section
            with self.subTest(verdict=verdict.value):
                self.assertClaim(verdict.value)

    # --- the framing the reviewer corrected -------------------------------

    def test_horizon_and_response_lag_are_kept_independent(self):
        """The objective is a move DAYS out that Kalshi may follow in
        SECONDS. A revision that treated long lags as the thesis had
        substituted a slower-response study for the stated one."""
        self.assertClaim("two separate dimensions")
        self.assertClaim("may do in seconds")
        self.refuteClaim("long lags are precisely the regime this thesis is "
                         "about")

    def test_the_cadence_is_named_as_the_pollers_own_latency(self):
        """The honest reason cadence matters, and it does not make fast
        responses uninteresting -- only invisible to this pilot."""
        self.assertClaim("simulated poller's own latency")
        self.assertClaim("cannot rule out faster ones")

    def test_the_catches_every_move_overclaim_is_gone(self):
        self.refuteClaim("catches every move")
        self.assertClaim("reverses inside one interval")
        self.assertClaim("only the net change between consecutive samples")

    def test_a_null_result_carries_no_claim_about_short_lags(self):
        cadence = self.OPTIONS[self.RECOMMENDED][0]
        self.refuteClaim("means the lag is under 30 minutes")
        self.assertClaim(f"does not establish that the lag is shorter than "
                         f"{cadence} minutes")
        self.assertClaim(f"on a {cadence}-minute grid, over this sample")
        self.assertClaim("sparse moves")

    def test_insufficient_events_is_a_declared_possible_outcome(self):
        self.assertClaim("may well return insufficient")
        self.assertClaim("it is not a negative result")

    def test_the_refutation_check_has_teeth_beside_a_correction(self):
        """A check nothing has seen fail is a decoration.

        The first `refuteClaim` accepted any marker within 400 characters,
        so a sentence reverted to the old claim RIGHT BESIDE the correction
        passed -- excused by the correction's own words. This reverts §3
        exactly that way and requires the check to catch it.
        """
        reverted = self.raw.replace(
            "If the recommended run returns `stop`, that means: **on a "
            "5-minute grid,\nover this sample, these sources rarely "
            "established the book leading.**",
            "If the recommended run returns `stop`, it means the lag is under "
            "30 minutes.")
        self.assertNotEqual(reverted, self.raw, "the anchor text moved")
        text = (reverted.lower()
                .replace("\u2013", "-").replace("\u2014", "-")
                .replace("\u2192", "->").replace("*", "").replace("`", ""))
        original, self.text = self.text, " ".join(text.split())
        try:
            with self.assertRaises(AssertionError):
                self.refuteClaim("means the lag is under 30 minutes")
        finally:
            self.text = original

    def test_the_rule_is_counted_in_book_moves(self):
        self.assertClaim("of the book moves whose brackets actually order")
        self.assertClaim("each exchange response counts once")
        self.assertClaim("shared_response")
        self.refuteClaim("derived from the cadence rather than chosen freely")

    def test_it_does_not_claim_to_be_the_capture_study(self):
        self.assertClaim("constrained long-lived-discrepancy probe")
        self.assertClaim("does not stand in for it")

    # --- the horizon, and the commands that carry it -------------------------

    def test_the_horizon_is_not_one_cadence_interval(self):
        """At 5 minutes, one interval would right-censor every follower
        slower than about four minutes -- the ones the thesis is about."""
        import collect_reaction
        cadence = _td(minutes=self.OPTIONS[self.RECOMMENDED][0])
        horizon = collect_reaction.replay_max_wait_seconds(cadence)
        self.assertEqual(horizon, 1800)
        self.assertClaim(f"--max-wait {horizon} at "
                         f"{self.OPTIONS[self.RECOMMENDED][0]} minutes")
        self.assertClaim("whichever is longer")
        self.refuteClaim("max_wait should be one cadence interval")

    def _commands(self, script: str) -> list[list[str]]:
        """Every documented invocation of `script`, as argv."""
        import shlex
        found = []
        for line in self.raw.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.startswith(f"python3 {script} "):
                found.append(shlex.split(line)[2:])
        return found

    def test_the_documented_collector_commands_parse(self):
        """A command in a proposal gets pasted. Each one is parsed by the
        collector's own parser, so a renamed flag fails the build here
        rather than on the owner's machine."""
        import collect_reaction
        commands = self._commands("collect_reaction.py")
        self.assertGreaterEqual(len(commands), 4)
        self.assertFalse(collect_reaction.build_parser().allow_abbrev)
        for argv in commands:
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    collect_reaction.parse_args(argv)

    def test_the_documented_monitor_commands_parse(self):
        import shadow_monitor
        commands = self._commands("shadow_monitor.py")
        self.assertGreaterEqual(len(commands), 3)
        self.assertFalse(shadow_monitor.build_parser().allow_abbrev)
        for argv in commands:
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    shadow_monitor.build_parser().parse_args(argv)

    def test_the_monitor_prices_are_the_monitors_own(self):
        """Every figure in the table, and the one in the budget paragraph,
        from `session_price` -- the function that sets the cap."""
        from shadow_monitor import session_price
        for hours in (24, 72):
            row = [line for line in self.raw.splitlines()
                   if line.startswith(f"| {hours}h |")]
            self.assertEqual(len(row), 1, f"no {hours}h row")
            for seconds in (60, 120, 300):
                price = session_price(hours, _td(seconds=seconds))
                with self.subTest(hours=hours, seconds=seconds):
                    self.assertIn(f"| {price:,} |", row[0] + " |")
        self.assertClaim(f"72 hours at one poll a minute is "
                         f"{session_price(72, _td(seconds=60)):,} credits")
        self.assertClaim(f"--spend {session_price(72, _td(seconds=60))}")
        self.assertClaim(f"an hour at 15 seconds "
                         f"({session_price(1, _td(seconds=15)):,} credits)")

    def test_the_documented_replay_commands_parse(self):
        """Exactly as spelled: argparse would otherwise accept a stale flag
        that happens to be a prefix of the current one."""
        import run_reaction
        commands = self._commands("run_reaction.py")
        self.assertTrue(any("--max-wait" in argv for argv in commands))
        for argv in commands:
            with self.subTest(argv=argv):
                parser = run_reaction.build_parser()
                parser.allow_abbrev = False
                with contextlib.redirect_stderr(io.StringIO()):
                    parser.parse_args(argv)

    # --- unchanged guarantees ---------------------------------------------

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
        self.assertClaim("300-second archive floor")
        self.assertClaim("60s")

    def test_the_operational_bounds_are_stated(self):
        for claim in ("one snapshot is the whole sport's slate",
                      "duplicate at full price",
                      "earliest instant is a pure baseline",
                      "10% reserve is inside the enforced bound"):
            with self.subTest(claim=claim):
                self.assertClaim(claim)

    def test_the_holdout_section_matches_the_code(self):
        from reaction.episodes import DEVELOPMENT_WINDOW
        self.assertClaim("september 1-16 2026")
        self.assertEqual(DEVELOPMENT_WINDOW[0].isoformat(), "2026-09-01")
        self.assertEqual(DEVELOPMENT_WINDOW[1].isoformat(), "2026-09-16")


class ReadmeCurrencyTest(unittest.TestCase):
    """The README's reaction section is checked against the code.

    It had gone stale in the worst way: the capability table still graded
    "when did the BOOK move" as ANSWERABLE on the strength of `last_update`
    -- the exact defect review finding 1 was about -- it listed a
    `stale_content` rejection that no longer exists, and its status section
    said four built layers were "not built and not faked".

    A README claiming a capability the sources do not have is the same class
    of error as a dashboard reading a number beside a flag that contradicts
    it: whoever reads it reads the claim. So the claims are pinned.
    """

    README = (Path(__file__).resolve().parent.parent / "README.md")

    def setUp(self):
        self.raw = self.README.read_text()
        self.section = self.raw[self.raw.index("## The reaction-lag study"):]
        self.text = self.section.lower()

    def assertClaim(self, needle: str, where: str | None = None):
        """assertIn without dumping the whole section on failure. Whitespace
        is normalised on both sides: a claim is not refuted by the line it
        happens to wrap on."""
        haystack = " ".join(
            (self.text if where is None else where.lower()).split())
        if " ".join(needle.lower().split()) not in haystack:
            self.fail(f"the README does not claim {needle!r}")

    def _listed_rejections(self) -> set[str]:
        """Parse the one paragraph that enumerates them, not the whole text.

        Scanning the whole section for backticked tokens picked up field
        names and module paths, and required an underscore to filter them --
        which silently excluded `undeviggable`, the one rejection with no
        underscore in it. A parser whose filter can drop a real entry cannot
        report a missing one.
        """
        import re
        marker = "Every non-trigger is named and counted:"
        start = self.section.index(marker) + len(marker)
        paragraph = self.section[start:self.section.index("\n\n", start)]
        return set(re.findall(r"`([a-z_]+)`", paragraph))

    def test_every_rejection_reason_is_listed_and_none_is_invented(self):
        """The check that would have caught `stale_content` surviving."""
        from reaction.detector import Rejection
        listed = self._listed_rejections()
        declared = {r.value for r in Rejection}
        self.assertFalse(declared - listed,
                         f"the README does not list these rejections: "
                         f"{sorted(declared - listed)}")
        self.assertFalse(listed - declared,
                         f"the README lists rejections the code does not "
                         f"emit: {sorted(listed - declared)}")

    def test_the_capability_table_matches_the_audits_own_verdicts(self):
        """Including the one that was wrong: the BOOK's change instant."""
        from reaction import capability as cap
        for needle, expected in (
            ("BOOK actually change", cap.Answerability.UNANSWERABLE),
            ("PROVIDER last observe", cap.Answerability.ANSWERABLE),
            ("OUR SYSTEM", cap.Answerability.INTERVAL_CENSORED),
            ("LIVE feed", cap.Answerability.UNANSWERABLE),
        ):
            with self.subTest(question=needle):
                self.assertIs(cap.verdict_for(needle).answerability, expected)
        self.assertClaim("when did the BOOK actually change its price")
        self.assertClaim("bracketed only")
        self.assertNotIn("**answerable** (`last_update`", self.section,
                         "the corrected verdict has regressed")

    def test_the_resolution_floor_in_the_readme_is_the_audits_own(self):
        from reaction.capability import reaction_resolution_floor_seconds
        floor = reaction_resolution_floor_seconds()
        self.assertEqual(floor, 300.0)
        self.assertClaim(f"Reaction resolution floor: {floor:.0f}s")

    def test_the_status_section_does_not_call_a_built_layer_unbuilt(self):
        """A line claiming less exists than does is read too."""
        status = self.section[self.section.index("### Status"):]
        built = status[:status.index("**Not built:**")]
        for layer in ("reaction measurement", "opportunity screen",
                      "episode ledger", "offline replay", "backtest collector",
                      "shadow monitor"):
            self.assertIn(layer, built, f"{layer} is not listed as built")
        unbuilt = status[status.index("**Not built:**"):]
        self.assertIn("places an order", unbuilt)
        for layer in ("reaction measurement", "episode ledger",
                      "live capture"):
            self.assertNotIn(layer, unbuilt.split("No orders")[0])

    def test_every_module_the_readme_names_actually_imports(self):
        import importlib
        for module in ("reaction.capability", "reaction.clocks",
                       "reaction.detector", "reaction.measure",
                       "reaction.screen", "reaction.episodes",
                       "reaction.replay", "reaction.capture",
                       "collect_reaction", "shadow_monitor"):
            with self.subTest(module=module):
                self.assertClaim(module.split(".")[-1] + ".py")
                importlib.import_module(module)

    def test_the_named_test_cases_are_claimed_and_present(self):
        """The README says every named case exists. It has to be true."""
        import tests.test_reaction_measure as measure_tests
        import tests.test_reaction_clocks as clock_tests
        self.assertClaim("Every named test case is present")
        for case, holder in (
            ("BookLeadsTest", measure_tests),
            ("OrderingTest", measure_tests),
            ("CensoringTest", measure_tests),
            ("DelayTest", measure_tests),
            ("CheckpointCounterexampleTest", measure_tests),
            ("DelayedEntryTest", clock_tests),
        ):
            with self.subTest(case=case):
                self.assertTrue(hasattr(holder, case),
                                f"{holder.__name__}.{case} is claimed by the "
                                f"README and does not exist")

    def test_the_no_orders_commitment_is_still_there(self):
        """It used to say "No paid requests ... none is implemented", false
        once the collector shipped, and then "no live capture", false once
        the shadow monitor did. What is still true is pinned instead: no
        orders, and paid requests in two places, each behind one flag."""
        self.assertClaim("No orders: none is authorised and none can be "
                         "placed")
        self.assertClaim("Paid requests exist in exactly two places, "
                         "`collect_reaction.py` and `shadow_monitor.py`")
        self.assertClaim("behind an explicit `--spend`")
        self.assertClaim("none is authorised")
        flat = " ".join(self.text.split())
        self.assertNotIn("no paid requests. none is authorised", flat)
        self.assertNotIn("no live capture: neither", flat)

    def test_the_documented_commands_parse_exactly(self):
        """The commands in this section get pasted; each is parsed by the
        real CLI with abbreviations off, so a stale flag fails here."""
        import shlex
        import collect_reaction
        import run_reaction
        import shadow_monitor
        parsers = {"collect_reaction.py": collect_reaction.build_parser,
                   "run_reaction.py": run_reaction.build_parser,
                   "shadow_monitor.py": shadow_monitor.build_parser}
        seen = 0
        for line in self.section.splitlines():
            line = line.split("#", 1)[0].strip()
            for script, build in parsers.items():
                if not line.startswith(f"python3 {script} "):
                    continue
                seen += 1
                parser = build()
                parser.allow_abbrev = False
                with self.subTest(line=line):
                    with contextlib.redirect_stderr(io.StringIO()):
                        parser.parse_args(shlex.split(line)[2:])
        self.assertGreaterEqual(seen, 8)


class ManifestTest(unittest.TestCase):
    """The instants, enumerated -- because the estimate was wrong."""

    def test_a_single_72h_window_holds_145_instants_not_192(self):
        """The reviewer's arithmetic, reproduced as a regression.

        72 elapsed hours at 30 minutes, closed at both ends, is 145 samples.
        The same window touches FOUR calendar dates, and 4 x 48 is 192.
        """
        manifest = build_manifest(windows(30, CLUSTERS[:1]))
        self.assertEqual(manifest.requests, 145)
        self.assertEqual(manifest.calendar_dates, 4)
        self.assertEqual(manifest.span, LEAD)
        disagreement = manifest.disagreement_with_calendar_estimate()
        self.assertIsNotNone(disagreement)
        self.assertIn("192", disagreement)
        self.assertIn("145", disagreement)

    def test_the_window_is_closed_at_both_ends_and_says_so(self):
        """A half-open reading is equally defensible and differs by one
        request per window, so the choice is declared, not implied."""
        self.assertTrue(WINDOW_IS_CLOSED_AT_BOTH_ENDS)
        manifest = build_manifest(windows(30, CLUSTERS[:1]))
        self.assertEqual(manifest.timestamps[0], CLUSTERS[0] - LEAD)
        self.assertEqual(manifest.timestamps[-1], CLUSTERS[0])
        self.assertTrue(manifest.as_dict()["window_closed_at_both_ends"])

    def test_clusters_deduplicate_only_when_aligned(self):
        """Alignment is most of the bill, and it is not obvious.

        Three kickoffs minutes apart put their 30-minute grids out of phase,
        so the union of three 145-point windows is 435 points -- no sharing
        at all. Snapped to a common boundary it is 161.
        """
        unaligned = build_manifest(windows(30))
        aligned = build_manifest(windows(30), align_to=_td(minutes=30))
        self.assertEqual(unaligned.requests, 435)
        self.assertEqual(unaligned.cache_hits, 0)
        self.assertEqual(aligned.requests, 161)
        self.assertEqual(aligned.cache_hits, 276)
        self.assertLess(aligned.credits, unaligned.credits)

    def test_only_the_earliest_instant_is_a_pure_baseline(self):
        """A snapshot is SPORT-WIDE, so a later cluster's games are already
        in the first one. Counting one baseline per window (a first version
        did) understates the measurable samples by one per extra cluster."""
        manifest = build_manifest(windows(30), align_to=_td(minutes=30))
        self.assertEqual(manifest.measurement_requests, manifest.requests - 1)
        self.assertEqual(manifest.as_dict()["baseline_samples"], 1)
        # the per-window opens are still reported, as guaranteed coverage
        self.assertEqual(len(manifest.baselines), 3)

    def test_the_retry_reserve_is_inside_the_enforced_bound(self):
        """It was promised in prose while `budget()` derived a bound without
        it, so the first retried request past the base count would have
        raised mid-window -- a hole in the series the study measures."""
        manifest = build_manifest(windows(30), align_to=_td(minutes=30),
                                  retry_fraction=0.10)
        self.assertEqual(manifest.retry_reserve, 17)
        self.assertEqual(manifest.requests_with_retries, 178)
        budget = manifest.budget()
        self.assertEqual(budget.max_requests, 178)
        self.assertEqual(budget.max_credits, manifest.credits)
        # and the bound still refuses past it
        for _ in range(178):
            budget.spend(0)
        with self.assertRaises(CaptureRefused):
            budget.spend(0)

    def test_credits_come_from_the_shared_cost_model(self):
        from data.odds_history import estimate_credits
        manifest = build_manifest(windows(30), align_to=_td(minutes=30))
        self.assertEqual(manifest.credits,
                         estimate_credits(1, manifest.requests_with_retries))

    def test_a_cadence_finer_than_the_archive_grid_is_refused(self):
        """Those requests return DUPLICATE snapshots at full price."""
        self.assertEqual(ARCHIVE_GRID, _td(seconds=300))
        with self.assertRaises(ValueError) as caught:
            CaptureWindow(CLUSTERS[0], LEAD, _td(minutes=1))
        self.assertIn("DUPLICATE", str(caught.exception))

    def test_a_naive_kickoff_is_refused(self):
        """The offset decides which calendar date the estimate counts."""
        with self.assertRaises(ValueError):
            CaptureWindow(datetime(2026, 9, 27, 17, 0), LEAD, _td(minutes=30))

    def test_the_manifest_enumerates_every_instant_it_prices(self):
        """What someone approves before a spend is the list of requests. A
        manifest reporting only a count and two endpoints was an estimate
        with better arithmetic."""
        manifest = build_manifest(windows(30), align_to=_td(minutes=30))
        instants = manifest.as_dict()["instants"]
        self.assertEqual(len(instants), manifest.requests)
        self.assertEqual(instants, sorted(instants))
        self.assertEqual(len(set(instants)), len(instants))
        self.assertTrue(all(i.endswith("+00:00") for i in instants))

    def test_an_empty_manifest_is_refused_rather_than_priced_at_zero(self):
        with self.assertRaises(ValueError):
            build_manifest([])

    def test_a_plan_carrying_a_manifest_prices_from_it(self):
        """And the calendar arithmetic becomes a cross-check, not the model."""
        manifest = build_manifest(windows(30), align_to=_td(minutes=30))
        plain = plan(snapshots_per_day=48)
        with_manifest = plan(snapshots_per_day=48, manifest=manifest)
        self.assertEqual(plain.cost_model(), "days_x_per_day")
        self.assertEqual(with_manifest.cost_model(), "timestamp_manifest")
        self.assertEqual(with_manifest.odds_credits, manifest.credits)
        self.assertEqual(with_manifest.odds_requests,
                         manifest.requests_with_retries)
        self.assertNotEqual(plain.odds_credits, with_manifest.odds_credits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
