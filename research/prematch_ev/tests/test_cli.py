"""End-to-end CLI regressions.

Both crashes in this file's scope shipped with 171 other tests passing, because
nothing drove `main()` or the audit command. A component suite cannot catch a
missing import in an entry point -- only calling the entry point can.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_study                                                   # noqa: E402
from analysis.scoring import (                                     # noqa: E402
    Eligibility, Observation, screen_diagnostics,
)
from collect import (                                              # noqa: E402
    CheckpointMatrix, Ledger, StartResolver, default_lead_grid,
)
from data.cache import (                                           # noqa: E402
    CreditCapReached, ResponseCache, key_for, redact,
)
from data.kalshi_history import Coverage                           # noqa: E402
from data.odds_history import CreditLedger                         # noqa: E402

UTC = timezone.utc


def args_for(out, **over):
    base = ["--sport", "MLB", "--series", "KXMLBGAME",
            "--from", "2026-09-14", "--to", "2026-09-15",
            "--api-key", "SECRET-KEY-VALUE", "--out", str(out),
            "--cache-dir", ""]
    for k, v in over.items():
        base += [k, str(v)]
    return base


def fake_observation(i=0):
    start = datetime(2026, 9, 14, 23, 0, tzinfo=UTC)
    return Observation(
        game_id=f"EVT{i}", market_id=f"M{i}",
        decision_at=start - timedelta(minutes=60), minutes_to_start=60.0,
        p_sharp=0.60, p_exchange=0.50, outcome=i % 2,
        exchange_bid=0.49, exchange_ask=0.51,
    )


class ArtifactPersistenceTest(unittest.TestCase):
    """THE crash: `write_coverage()` referenced EVENT_BODY_TIMEZONE, which
    run_study never imported. Every completed collection path raised NameError
    -- AFTER the credits had been spent, which is exactly when the persistence
    fix was supposed to help."""

    def _run(self, observations, ledger=None):
        ledger = ledger or Ledger()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "study"
            with mock.patch.object(
                run_study, "collect",
                return_value=(observations, Coverage(), CreditLedger(), ledger,
                              default_lead_grid(), CheckpointMatrix(),
                              StartResolver(league="MLB"))
            ):
                code = run_study.main(args_for(out))
            return code, out

    def test_zero_observations_still_writes_coverage(self):
        code, out = self._run([])
        self.assertEqual(code, 1)

    def test_zero_observations_coverage_is_readable_json(self):
        ledger = Ledger()
        ledger.count("contracts", 100, unit="contracts")
        ledger.reject("no_readable_start_time", "KX-X", count=100,
                      stage="contracts")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "study"
            with mock.patch.object(
                run_study, "collect",
                return_value=([], Coverage(), CreditLedger(), ledger,
                              default_lead_grid(), CheckpointMatrix(),
                              StartResolver(league="MLB"))
            ):
                run_study.main(args_for(out))
            payload = json.loads((out / "coverage.json").read_text())
        self.assertEqual(payload["observations_built"], 0)
        self.assertIn("collection", payload)
        self.assertIn("no_readable_start_time", payload["collection"]["rejections"])

    def test_nonzero_observations_writes_all_promised_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "study"
            with mock.patch.object(
                run_study, "collect",
                return_value=([fake_observation(i) for i in range(40)],
                              Coverage(), CreditLedger(), Ledger(),
                              default_lead_grid(), CheckpointMatrix(),
                              StartResolver(league="MLB"))
            ):
                code = run_study.main(args_for(out))
            self.assertEqual(code, 0)
            for name in ("report.txt", "coverage.json", "observations.json"):
                self.assertTrue((out / name).exists(), f"{name} must be written")
            json.loads((out / "observations.json").read_text())
            json.loads((out / "coverage.json").read_text())

    def test_no_credential_reaches_the_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "study"
            with mock.patch.object(
                run_study, "collect",
                return_value=([fake_observation()], Coverage(), CreditLedger(), Ledger(),
                              default_lead_grid(), CheckpointMatrix(),
                              StartResolver(league="MLB"))
            ):
                run_study.main(args_for(out))
            for name in ("report.txt", "coverage.json", "observations.json"):
                self.assertNotIn("SECRET-KEY-VALUE", (out / name).read_text())


class FeeRouteCliTest(unittest.TestCase):
    """The fee corrections have to reach the ARTIFACTS, not just the model.

    A dated schedule and a dual-route report that `main()` never wires up look
    exactly like a working one from the unit tests -- which is the failure this
    file exists for. `fake_observation` is stamped 2026-09-14, so these drive
    the real KXMLBGAME schedule through the real CLI.
    """

    def _run(self, **over):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "study"
            with mock.patch.object(
                run_study, "collect",
                return_value=([fake_observation(i) for i in range(40)],
                              Coverage(), CreditLedger(), Ledger(),
                              default_lead_grid(), CheckpointMatrix(),
                              StartResolver(league="MLB"))
            ):
                code = run_study.main(args_for(out, **over))
            return (code,
                    (out / "report.txt").read_text(),
                    json.loads((out / "coverage.json").read_text()))

    def test_the_report_names_the_dated_schedule_it_priced_with(self):
        _, report, _ = self._run()
        self.assertIn("38032af2-e3fa-4659-9280-da64300b544c", report)
        self.assertIn("taker=0.035", report)

    def test_the_report_carries_the_unresolved_pdf_conflict(self):
        _, report, _ = self._run()
        self.assertIn("UNRESOLVED CONFLICT", report)

    def test_the_report_shows_both_account_routes(self):
        _, report, _ = self._run()
        self.assertIn("THE ACCOUNT ROUTE IS NOT RESOLVED", report)
        self.assertIn("direct", report)
        self.assertIn("non_direct", report)

    def test_the_headline_route_is_selectable_and_recorded(self):
        _, report, coverage = self._run(**{"--fee-route": "direct"})
        self.assertEqual(coverage["run"]["fee_route_headline"], "direct")
        self.assertFalse(coverage["run"]["fee_route_resolved"])
        self.assertIn("$0.0001", report)

    def test_coverage_records_the_fee_provenance(self):
        _, _, coverage = self._run()
        fees_text = coverage["run"]["fees"]
        self.assertIsInstance(fees_text, str)
        self.assertIn("KXMLBGAME", fees_text)
        self.assertIn("not re-read in-session", fees_text)

    def test_a_window_straddling_a_fee_change_is_flagged(self):
        """The August change falls inside this window, so the run must say the
        decisions before and after it are priced differently."""
        args = run_study.parse_args(args_for(Path("/tmp/unused"),
                                             **{"--from": "2026-08-01",
                                                "--to": "2026-08-31"}))
        warnings = run_study.fee_schedule_warnings(args)
        self.assertEqual(len(warnings), 1)
        self.assertIn("CHANGES INSIDE", warnings[0])

    def test_a_window_clear_of_a_fee_change_is_not_flagged(self):
        args = run_study.parse_args(args_for(Path("/tmp/unused")))
        self.assertEqual(run_study.fee_schedule_warnings(args), [])

    def test_the_provenance_is_resolved_at_the_window_not_at_now(self):
        """A run of this study in six months must still print the rate that
        was in force in the window it studied."""
        def this_run(args):
            # The generic line always says 0.07, so asserting on the joined
            # text would pass whatever the resolution did. Only the THIS RUN
            # line reports what was actually applied.
            return [ln for ln in run_study.fee_provenance(args)
                    if ln.startswith("THIS RUN:")][0]

        july = run_study.parse_args(args_for(Path("/tmp/unused"),
                                             **{"--from": "2026-07-01",
                                                "--to": "2026-07-31"}))
        september = run_study.parse_args(args_for(Path("/tmp/unused")))
        self.assertIn("taker=0.07", this_run(july))
        self.assertIn("effective 2025-10-04", this_run(july))
        self.assertIn("taker=0.035", this_run(september))
        self.assertIn("effective 2026-08-07", this_run(september))


class PreflightTest(unittest.TestCase):
    """`--plan` prints a fixed offline estimate; it cannot validate the real
    cutoff count, which is what a pre-spend check needs."""

    def test_preflight_uses_the_shared_survey(self):
        with mock.patch.object(run_study, "survey",
                               return_value=run_study.SurveyResult(
                                   {}, [], None, Coverage(), Ledger(),
                                   default_lead_grid(), CheckpointMatrix(),
                                   StartResolver(league="MLB"))) as sv:
            run_study.main(["--preflight", "--sport", "MLB", "--series", "KXMLBGAME",
                            "--from", "2026-09-14", "--to", "2026-09-15",
                            "--api-key", "K"])
        sv.assert_called_once()

    def test_preflight_reports_the_real_cutoff_count(self):
        cutoffs = [datetime(2026, 9, 14, 22, 0, tzinfo=UTC),
                   datetime(2026, 9, 15, 1, 0, tzinfo=UTC)]
        with mock.patch.object(run_study, "survey",
                               return_value=run_study.SurveyResult(
                                   {"a": {}}, cutoffs, None,
                                   Coverage(), Ledger(), default_lead_grid(),
                                   CheckpointMatrix(),
                                   StartResolver(league="MLB"))):
            with mock.patch("builtins.print") as printed:
                code = run_study.main(
                    ["--preflight", "--sport", "MLB", "--series", "KXMLBGAME",
                     "--from", "2026-09-14", "--to", "2026-09-15", "--api-key", "K"])
        text = " ".join(str(c) for c in printed.call_args_list)
        self.assertEqual(code, 0)
        self.assertIn("base credit cost", text)
        self.assertIn("20", text, "2 cutoffs x 10 credits")
        # A base figure presented as the bill would understate a retrying run.
        self.assertIn("worst case w/ retries", text)
        self.assertIn("60", text, "2 cutoffs x 10 credits x 3 attempts")

    def test_preflight_fails_on_incomplete_coverage_before_spending(self):
        bad = Coverage().fail("enumeration truncated")
        with mock.patch.object(run_study, "survey",
                               return_value=run_study.SurveyResult(
                                   {}, [], None, bad, Ledger(),
                                   default_lead_grid(), CheckpointMatrix(),
                                   StartResolver(league="MLB"))):
            code = run_study.main(
                ["--preflight", "--sport", "MLB", "--series", "KXMLBGAME",
                 "--from", "2026-09-14", "--to", "2026-09-15", "--api-key", "K"])
        self.assertEqual(code, 1)


class CreditCapTest(unittest.TestCase):
    """A cap that only warns is not a cap."""

    def test_cap_refuses_the_call_that_would_exceed_it(self):
        led = CreditLedger(cap=30)
        led.spend_or_raise(); led.spend_or_raise(); led.spend_or_raise()
        with self.assertRaises(CreditCapReached):
            led.spend_or_raise()

    def test_no_cap_never_refuses(self):
        led = CreditLedger()
        for _ in range(50):
            led.spend_or_raise()
        self.assertEqual(led.spent_this_run, 500)

    def test_cap_counts_only_what_was_spent(self):
        led = CreditLedger(cap=100)
        led.spend_or_raise()
        self.assertEqual(led.spent_this_run, 10)


class RetryBudgetTest(unittest.TestCase):
    """A retry is not free. `spend_or_raise()` was called once BEFORE the retry
    loop, so three network attempts ran against a single debit and a 10-credit
    cap permitted three chargeable requests. A provider can process and charge
    a request whose response never reaches us."""

    AT = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)

    def _attempts_under_cap(self, exc, cap=10):
        attempts = {"n": 0}

        def boom(*a, **k):
            attempts["n"] += 1
            raise exc

        led = CreditLedger(cap=cap)
        raised = False
        with mock.patch("urllib.request.urlopen", side_effect=boom), \
             mock.patch("time.sleep"):
            try:
                run_study.fetch_snapshot("MLB", self.AT, "K", ledger=led)
            except CreditCapReached:
                raised = True
        return attempts["n"], led.spent_this_run, raised

    def test_ten_credit_cap_permits_one_attempt_on_timeout(self):
        attempts, spent, raised = self._attempts_under_cap(TimeoutError("t"))
        self.assertEqual(attempts, 1)
        self.assertEqual(spent, 10)
        self.assertTrue(raised, "the cap must propagate, not be retried away")

    def test_ten_credit_cap_permits_one_attempt_on_decode_failure(self):
        attempts, _, raised = self._attempts_under_cap(
            json.JSONDecodeError("bad", "doc", 0))
        self.assertEqual(attempts, 1)
        self.assertTrue(raised)

    def test_ten_credit_cap_permits_one_attempt_on_connection_error(self):
        attempts, _, raised = self._attempts_under_cap(OSError("reset"))
        self.assertEqual(attempts, 1)
        self.assertTrue(raised)

    def test_uncapped_retries_debit_every_attempt(self):
        attempts = {"n": 0}

        def boom(*a, **k):
            attempts["n"] += 1
            raise TimeoutError("t")

        led = CreditLedger()
        with mock.patch("urllib.request.urlopen", side_effect=boom), \
             mock.patch("time.sleep"):
            run_study.fetch_snapshot("MLB", self.AT, "K", ledger=led)
        self.assertGreater(attempts["n"], 1)
        self.assertEqual(led.spent_this_run, attempts["n"] * 10,
                         "every attempt must be accounted, not just the first")

    def test_cache_hit_bypasses_spending_entirely(self):
        """Debugging a local join must not cost money, even at a zero cap."""
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(key_for("MLB", self.AT, "pinnacle", "h2h"),
                      {"timestamp": "2026-09-15T20:40:00Z", "data": []})
            led = CreditLedger(cap=0)
            result = run_study.fetch_snapshot("MLB", self.AT, "K",
                                              ledger=led, cache=cache)
        self.assertEqual(led.spent_this_run, 0)
        self.assertTrue(result.coverage.complete)
        self.assertEqual(cache.hits, 1)

    def test_cap_reached_mid_collection_keeps_partial_artifacts(self):
        """CreditCapReached must reach the handler that persists diagnostics."""
        import collect as collect_mod
        self.assertIn("CreditCapReached", Path("run_study.py").read_text(),
                      "collect() must catch the cap and keep what it has")


class QuotaLabelTest(unittest.TestCase):
    """`used` is the ACCOUNT's cumulative usage from `x-requests-used`, not
    this run's spend. Presenting it as "credits used" made a 140-credit run
    report 340."""

    def test_this_run_and_cumulative_are_named_separately(self):
        led = CreditLedger()
        led.observe({"x-requests-used": "340", "x-requests-remaining": "19660"})
        led.spent_this_run = 140
        text = str(led)
        self.assertIn("140 credits reserved this run", text)
        self.assertIn("340 cumulative on the account", text)

    def test_cumulative_is_not_presented_as_this_runs_spend(self):
        led = CreditLedger()
        led.observe({"x-requests-used": "340"})
        self.assertNotIn("340 credits used", str(led))

    def test_unread_cumulative_is_unknown_not_zero(self):
        """A cache-only replay makes no request, so no header is seen. Printing
        "0 cumulative" claimed a fact the run never established."""
        led = CreditLedger()
        self.assertIsNone(led.used)
        self.assertIn("unknown cumulative", str(led))
        self.assertNotIn("0 cumulative", str(led))

    def test_spend_is_labelled_reserved_not_billed(self):
        """It is debited before each attempt, including ones that then fail, so
        it is an upper bound on what was actually billed."""
        led = CreditLedger()
        led.spend_or_raise()
        self.assertIn("reserved this run", str(led))


class ScreenDiagnosticsTest(unittest.TestCase):
    """A run that finds no trades and a run whose collection broke both print
    "no eligible trades". The distribution is what separates them."""

    def _obs(self, n, sharp, mid, bid, ask):
        return [Observation(f"E{i}", f"M{i}",
                            datetime(2026, 9, 14, tzinfo=UTC) + timedelta(hours=i),
                            60.0, sharp, mid, i % 2,
                            exchange_bid=bid, exchange_ask=ask)
                for i in range(n)]

    def test_absent_edge_is_distinguishable_from_collection_failure(self):
        d = screen_diagnostics(self._obs(40, 0.505, 0.50, 0.49, 0.51),
                               Eligibility(min_net_ev=0.01))
        self.assertEqual(d.considered, 40)
        self.assertEqual(d.admitted, 0)
        self.assertEqual(d.rejected_below_ev, 40,
                         "rejected on EV, not on missing data")
        self.assertEqual(d.rejected_no_quotes, 0)
        self.assertTrue(d.best_net_ev, "the distribution must be reported")

    def test_missing_quotes_are_counted_separately_from_thin_edge(self):
        obs = [Observation("E", "M", datetime(2026, 9, 14, tzinfo=UTC), 60.0,
                           0.60, 0.50, 1)]
        d = screen_diagnostics(obs, Eligibility(min_net_ev=0.01))
        self.assertEqual(d.rejected_no_quotes, 1)
        self.assertEqual(d.rejected_below_ev, 0)

    def test_each_filter_is_attributed(self):
        wide = screen_diagnostics(self._obs(5, 0.60, 0.50, 0.30, 0.70),
                                  Eligibility(min_net_ev=0.0, max_spread=0.05))
        self.assertEqual(wide.rejected_spread, 5)
        band = screen_diagnostics(self._obs(5, 0.99, 0.97, 0.96, 0.98),
                                  Eligibility(min_net_ev=0.0))
        self.assertEqual(band.rejected_price_band, 5)

    def test_report_includes_the_diagnostics(self):
        from data.kalshi_history import Coverage as Cov
        text = run_study.build_report(self._obs(40, 0.505, 0.50, 0.49, 0.51),
                                      Cov()).render()
        self.assertIn("SCREEN DIAGNOSTICS", text)
        self.assertIn("NO TRADES", text)
        self.assertIn("not a reason", text)


class CacheTest(unittest.TestCase):
    """The previous run spent 200 credits and lost its diagnostics to a local
    crash. Every retry of a local bug must not cost money again."""

    def test_key_never_contains_a_credential(self):
        at = datetime(2026, 9, 15, 20, 40, tzinfo=UTC)
        key = key_for("MLB", at, "pinnacle", "h2h")
        self.assertNotIn("SECRET", key)
        self.assertEqual(key, key_for("MLB", at, "pinnacle", "h2h"))

    def test_redact_scrubs_secrets_from_messages(self):
        self.assertNotIn("SECRET123",
                         redact("https://x/v4?apiKey=SECRET123&regions=us"))

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            self.assertIsNone(cache.get("k"))
            cache.put("k", {"data": [1]})
            self.assertEqual(cache.get("k"), {"data": [1]})

    def test_corrupt_entry_is_a_miss_not_a_crash_and_not_an_empty_response(self):
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            (Path(d) / "k.json").write_text("not json")
            self.assertIsNone(cache.get("k"), "must be a miss, never {}")
            self.assertTrue(cache.errors)

    def test_disabled_cache_is_inert(self):
        cache = ResponseCache(None)
        cache.put("k", {"a": 1})
        self.assertIsNone(cache.get("k"))
        self.assertFalse(cache.enabled)

    def test_no_credential_in_cache_paths(self):
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(key_for("MLB", datetime(2026, 9, 15, tzinfo=UTC),
                              "pinnacle", "h2h"), {"data": []})
            for path in Path(d).iterdir():
                self.assertNotIn("apiKey", path.name)
                self.assertNotIn("SECRET", path.name)


class AuditCommandTest(unittest.TestCase):
    """The FREE audit I recommended running crashed on the first real ticker:
    it still read parsed["a"]/["b"], keys removed when the parser was rewritten
    for real tickers. It had no test, so the suite stayed green."""

    REAL_PAGE = [
        {"ticker": "KXMLBGAME-26SEP152140MIAAZ-AZ"},
        {"ticker": "KXMLBGAME-26SEP152140MIAAZ-MIA"},
        {"ticker": "KXMLBGAME-26SEP131920SDSF-SF"},
        {"ticker": "KXMLBGAME-26SEP131920SDSF-SD"},
    ]

    def _audit(self, pages):
        import data.kalshi_history as kh
        with mock.patch.object(
            kh, "_iter_market_pages",
            return_value=iter([(pages, Coverage())])
        ):
            with mock.patch("builtins.print") as printed:
                code = kh.audit_abbreviations("KXMLBGAME", "MLB")
        return code, " ".join(str(c) for c in printed.call_args_list)

    def test_runs_on_real_shaped_tickers_without_crashing(self):
        code, text = self._audit(self.REAL_PAGE)
        self.assertIn("distinct exchange codes seen", text)
        self.assertEqual(code, 0, f"AZ is aliased, so nothing unresolved: {text}")

    def test_aliased_code_is_not_reported_unresolved(self):
        _, text = self._audit(self.REAL_PAGE)
        self.assertIn("UNRESOLVED exchange codes (0)", text)

    def test_unknown_code_is_named_so_one_run_closes_the_gap(self):
        code, text = self._audit(
            self.REAL_PAGE + [{"ticker": "KXMLBGAME-26SEP151800CHWDET-CHW"}])
        self.assertIn("CHW", text)
        self.assertEqual(code, 1, "an unresolved code must fail the audit")

    def test_malformed_tickers_are_counted_not_fatal(self):
        code, text = self._audit(self.REAL_PAGE + [{"ticker": "garbage"}])
        self.assertIn("did not fit the expected shape", text)

    def test_incomplete_enumeration_is_reported(self):
        import data.kalshi_history as kh
        with mock.patch.object(
            kh, "_iter_market_pages",
            return_value=iter([(self.REAL_PAGE, Coverage().fail("page 3 timed out"))])
        ):
            with mock.patch("builtins.print") as printed:
                code = kh.audit_abbreviations("KXMLBGAME", "MLB")
        text = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("INCOMPLETE", text)
        self.assertEqual(code, 1, "a truncated code list must not read as clean")


class BoundaryLookbackTest(unittest.TestCase):
    """A 00:30 UTC eligible game at a 60-minute lead needs the PREVIOUS day's
    23:30 snapshot. Clipping cutoffs to the study window dropped it, and
    widening --from is not a substitute: that changes the study universe."""

    def test_cutoff_before_the_window_is_still_fetched(self):
        from collect import decision_cutoffs
        game = datetime(2026, 9, 14, 0, 30, tzinfo=UTC)
        cutoffs = decision_cutoffs([game], 60)
        self.assertEqual(len(cutoffs), 1)
        self.assertLess(cutoffs[0], datetime(2026, 9, 14, tzinfo=UTC),
                        "the needed snapshot precedes --from")


class CacheKeyTimezoneTest(unittest.TestCase):
    """A cache key must name ONE instant, whatever timezone the machine is in.

    `key_for` used to stamp `at.astimezone()` -- LOCAL time -- with a literal
    "Z". On a machine observing DST the two instants of the autumn fall-back
    hour then rendered as the same wall clock and shared a key, so the second
    request silently returned the first one's snapshot. These run under
    America/Chicago because that is the only place the bug exists: on a UTC
    machine every assertion here would pass against the broken code too.
    """

    FIRST = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)     # 01:30 CDT
    SECOND = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)    # 01:30 CST
    PLAIN = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)     # no fold anywhere near

    def setUp(self):
        import os
        import time
        self._saved = os.environ.get("TZ")
        os.environ["TZ"] = "America/Chicago"
        time.tzset()
        self.addCleanup(self._restore)

    def _restore(self):
        import os
        import time
        if self._saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._saved
        time.tzset()

    def _old_key(self, at):
        """Exactly how the pre-UTC cache built its keys."""
        from data.cache import STAMP, _digest
        return _digest("NFL", at.astimezone().strftime(STAMP), "pinnacle",
                       "h2h")

    def test_the_fall_back_hour_no_longer_collides(self):
        self.assertEqual(self._old_key(self.FIRST), self._old_key(self.SECOND),
                         "the premise: the old key really did collide here")
        self.assertNotEqual(key_for("NFL", self.FIRST, "pinnacle", "h2h"),
                            key_for("NFL", self.SECOND, "pinnacle", "h2h"))

    def test_the_key_does_not_depend_on_the_machines_timezone(self):
        import os
        import time
        here = key_for("NFL", self.PLAIN, "pinnacle", "h2h")
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.assertEqual(key_for("NFL", self.PLAIN, "pinnacle", "h2h"), here)

    def test_an_entry_paid_for_under_the_old_key_is_still_served_free(self):
        """Orphaning existing caches would re-spend credits already spent."""
        from data.cache import resolve_key
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(self._old_key(self.PLAIN),
                      {"timestamp": "2026-09-27T17:00:00Z", "data": []})
            self.assertEqual(
                resolve_key(cache, "NFL", self.PLAIN, "pinnacle", "h2h"),
                self._old_key(self.PLAIN))
            led = CreditLedger(cap=0)
            result = run_study.fetch_snapshot("NFL", self.PLAIN, "K",
                                              ledger=led, cache=cache)
        self.assertEqual(led.spent_this_run, 0)
        self.assertTrue(result.coverage.complete)
        self.assertEqual(result.raw["timestamp"], "2026-09-27T17:00:00Z")

    def _interim_key(self, at):
        """Exactly how the interim cache built its keys: a UTC stamp, in the
        SAME unprefixed digest space as `_old_key`."""
        from data.cache import STAMP, _digest
        return _digest("NFL", at.astimezone(UTC).strftime(STAMP), "pinnacle",
                       "h2h")

    def test_inside_the_fold_an_old_entry_answers_only_its_own_instant(self):
        """The old key there names two instants. Its entry is served for the
        one its snapshot answers and refused for the other: a miss costs a
        credit, serving the wrong snapshot costs the study."""
        from data.cache import resolve_key
        self.assertEqual(self._old_key(self.FIRST), self._old_key(self.SECOND))
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(self._old_key(self.FIRST),
                      {"timestamp": "2026-11-01T06:30:00Z", "data": []})
            self.assertEqual(
                resolve_key(cache, "NFL", self.FIRST, "pinnacle", "h2h"),
                self._old_key(self.FIRST))
            second = resolve_key(cache, "NFL", self.SECOND, "pinnacle", "h2h")
            self.assertEqual(second,
                             key_for("NFL", self.SECOND, "pinnacle", "h2h"))
            self.assertFalse(cache.has(second))

    def test_a_utc_entry_is_never_served_for_another_instant(self):
        """The owner's reproduction on a UTC-5 clock. An entry written for
        12:00Z sat under the key a 17:00Z request fell back to, and was
        served for it."""
        from data.cache import resolve_key
        noon = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        five = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
        self.assertEqual(self._old_key(five), self._interim_key(noon),
                         "the premise: on UTC-5 these are one key")
        body = {"timestamp": "2026-09-20T12:00:00Z", "data": []}
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(self._interim_key(noon), body)
            cache.put(key_for("NFL", noon, "pinnacle", "h2h"), body)
            key = resolve_key(cache, "NFL", five, "pinnacle", "h2h")
            self.assertEqual(key, key_for("NFL", five, "pinnacle", "h2h"))
            self.assertFalse(cache.has(key))
            # and the fetch agrees: it has to buy, so a zero cap stops it
            with self.assertRaises(CreditCapReached):
                run_study.fetch_snapshot("NFL", five, "K",
                                         ledger=CreditLedger(cap=0),
                                         cache=cache)

    def test_a_namespaced_key_shares_no_name_with_either_old_scheme(self):
        """Every 15 minutes across the fall-back weekend: one key per
        instant, and none of them a name an older scheme could have used."""
        from data.cache import KEY_NAMESPACE
        start = datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
        new, old = set(), set()
        for step in range(72 * 4):
            at = start + timedelta(minutes=15 * step)
            key = key_for("NFL", at, "pinnacle", "h2h")
            self.assertTrue(key.startswith(f"{KEY_NAMESPACE}-"), key)
            new.add(key)
            old.update({self._old_key(at), self._interim_key(at)})
        self.assertEqual(len(new), 72 * 4, "one key per instant")
        self.assertFalse(new & old)

    def test_a_mixed_cache_serves_only_what_answers_the_request(self):
        """A namespaced entry, an original-scheme entry holding its own
        snapshot, and an interim entry sitting under another request's
        fallback key -- side by side, as a real cache directory has them."""
        from data.cache import resolve_key
        fresh = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
        paid_before = datetime(2026, 9, 20, 17, 5, tzinfo=UTC)
        collides = datetime(2026, 9, 20, 22, 10, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            cache.put(key_for("NFL", fresh, "pinnacle", "h2h"),
                      {"timestamp": "2026-09-20T16:59:21Z", "data": []})
            cache.put(self._old_key(paid_before),
                      {"timestamp": "2026-09-20T17:04:21Z", "data": []})
            # the interim entry for 17:10Z is under 22:10Z's local key
            self.assertEqual(self._old_key(collides),
                             self._interim_key(collides - timedelta(hours=5)))
            cache.put(self._interim_key(collides - timedelta(hours=5)),
                      {"timestamp": "2026-09-20T17:09:21Z", "data": []})
            self.assertEqual(
                resolve_key(cache, "NFL", fresh, "pinnacle", "h2h"),
                key_for("NFL", fresh, "pinnacle", "h2h"))
            self.assertEqual(
                resolve_key(cache, "NFL", paid_before, "pinnacle", "h2h"),
                self._old_key(paid_before))
            self.assertFalse(cache.has(
                resolve_key(cache, "NFL", collides, "pinnacle", "h2h")))
            self.assertEqual((cache.hits, cache.misses), (0, 0))


    def test_resolving_a_key_moves_no_statistics(self):
        """The preflight asks this to cost a run; it must not look like one."""
        from data.cache import resolve_key
        with tempfile.TemporaryDirectory() as d:
            cache = ResponseCache(Path(d))
            resolve_key(cache, "NFL", self.PLAIN, "pinnacle", "h2h")
            self.assertEqual((cache.hits, cache.misses), (0, 0))

    def test_the_network_path_carries_the_raw_body_too(self):
        """A bundle must keep an UNREADABLE body to count it as a loss, and
        the cache only ever stores readable ones."""
        from unittest import mock
        body = {"timestamp": "2026-09-27T17:00:00Z", "data": "not a list"}

        class Response:
            status = 200
            headers = {"x-requests-used": "10", "x-requests-remaining": "90"}

            def read(self):
                return json.dumps(body).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            result = run_study.fetch_snapshot("NFL", self.PLAIN, "K",
                                              ledger=CreditLedger(cap=10))
        self.assertFalse(result.coverage.complete)
        self.assertEqual(result.raw, body)


class TransportFailureTest(unittest.TestCase):
    """A historical request whose body is cut off is a failed attempt --
    reserved, retried and reported like any other -- not a traceback.
    http.client raises `IncompleteRead`, which is not an `OSError`."""

    AT = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)

    def test_a_body_cut_off_is_retried_and_reported_not_raised(self):
        from data import odds_history
        from tests import chunked, http_response
        body = json.dumps({"timestamp": "2026-09-27T17:00:00Z",
                           "data": []}).encode("utf-8")
        calls = []

        def cut(*args, **kwargs):
            calls.append(1)
            return http_response(
                200, {"Transfer-Encoding": "chunked",
                      "x-requests-used": "10", "x-requests-remaining": "90"},
                chunked(body, 2, cut_short=True))

        each = odds_history.CREDITS_PER_HISTORICAL_CALL
        ledger = CreditLedger(cap=each * odds_history.RETRIES)
        with mock.patch("urllib.request.urlopen", side_effect=cut), \
                mock.patch("time.sleep"):
            result = run_study.fetch_snapshot("NFL", self.AT, "K",
                                              ledger=ledger)
        self.assertFalse(result.coverage.complete)
        self.assertIn("IncompleteRead", " ".join(result.coverage.reasons))
        self.assertEqual(len(calls), odds_history.RETRIES)
        self.assertEqual(ledger.spent_this_run, each * odds_history.RETRIES)

    def test_the_cap_still_stops_the_retries(self):
        """Room for one attempt: the cut body is not retried past the cap."""
        from data import odds_history
        from data.cache import CreditCapReached
        from tests import chunked, http_response
        calls = []

        def cut(*args, **kwargs):
            calls.append(1)
            return http_response(200, {"Transfer-Encoding": "chunked"},
                                 chunked(b'{"data": []}', 2, cut_short=True))

        ledger = CreditLedger(cap=odds_history.CREDITS_PER_HISTORICAL_CALL)
        with mock.patch("urllib.request.urlopen", side_effect=cut), \
                mock.patch("time.sleep"):
            with self.assertRaises(CreditCapReached):
                run_study.fetch_snapshot("NFL", self.AT, "K", ledger=ledger)
        self.assertEqual(len(calls), 1)


class AnswerProblemTest(unittest.TestCase):
    """Does a snapshot body answer a request? The archive's rule, not equality.

    The archive answers with its latest snapshot AT OR BEFORE the request, so
    a body's timestamp routinely precedes the instant asked for; demanding
    equality would miss every legitimate entry. Shapes are the archive's
    documented envelope (`timestamp`, `next_timestamp`), transcribed.
    """

    AT = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)

    def problem(self, taken, following=None):
        from data.cache import answer_problem
        body = {"timestamp": taken, "data": []}
        if following is not None:
            body["next_timestamp"] = following
        return answer_problem(body, self.AT)

    def test_the_latest_snapshot_before_the_request_answers_it(self):
        self.assertIsNone(self.problem("2026-09-20T16:55:39Z"))
        self.assertIsNone(self.problem("2026-09-20T17:00:00Z"))

    def test_a_snapshot_later_than_the_request_is_another_instants(self):
        self.assertIn("LATER", self.problem("2026-09-20T17:00:01Z"))

    def test_without_next_timestamp_an_old_snapshot_is_not_trusted(self):
        from data.cache import MAX_UNPROVEN_SNAPSHOT_AGE
        edge = self.AT - MAX_UNPROVEN_SNAPSHOT_AGE
        self.assertIsNone(self.problem(edge.strftime("%Y-%m-%dT%H:%M:%SZ")))
        older = edge - timedelta(seconds=1)
        self.assertIn("before the request",
                      self.problem(older.strftime("%Y-%m-%dT%H:%M:%SZ")))

    def test_next_timestamp_proves_one_across_an_archive_gap(self):
        self.assertIsNone(self.problem("2026-09-20T16:30:00Z",
                                       "2026-09-20T17:02:00Z"))

    def test_a_next_snapshot_at_or_before_the_request_would_have_answered(self):
        self.assertIn("would have answered",
                      self.problem("2026-09-20T16:55:00Z",
                                   "2026-09-20T17:00:00Z"))

    def test_an_unreadable_body_answers_nothing(self):
        from data.cache import answer_problem
        self.assertIsNotNone(answer_problem("not a body", self.AT))
        self.assertIsNotNone(answer_problem({"data": []}, self.AT))
        self.assertIsNotNone(answer_problem(
            {"timestamp": "2026-09-20T16:59:00", "data": []}, self.AT))


if __name__ == "__main__":
    unittest.main()
