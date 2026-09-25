"""Offline re-analysis of a shadow session, and what each assessment records.

THE FIXTURE IS SYNTHETIC. `tests/synthetic_session.py` drives the real
monitor over a scripted network built from the figures the owner reported for
the 2026-09-24 session (PR #27, comment 5826957047) -- nothing in it was
observed, and everything the owner did not report is invented and marked so
there. What these tests pin is that re-analysis reproduces the owner's
figures from a file with exactly the records a real session writes:

  5 moves on 4 games; 10 contract assessments; 8 screened and all 8 below the
  net-EV floor, none on spread, price band or lead time; Seattle and Green Bay
  to the digit; Houston-Indianapolis outside the 73h horizon, not a
  collection failure.

The file is analysed twice: as the current monitor writes it, and stripped
to what a session recorded before assessments existed -- the shape of the
file the owner holds.
"""

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shadow_diagnostics as diag
import shadow_monitor
from analysis.scoring import Eligibility
from data.kalshi_history import BookQuote
from reaction.adjustment import (
    HYPOTHETICAL, CapturePolicy, Read, SharpReading, capture, sharp_state,
    summarize,
)
from reaction.assessment import (
    CoverageClass, TickContext, TickRead, assess, classify_coverage,
)
from reaction.detector import MovePolicy, MoveTrigger, fair_probabilities
from tests import synthetic_session as syn
from tests.test_shadow_monitor import (
    MOVE_AT, T0 as MONITOR_T0, MonitorHarness,
)

UTC = timezone.utc
SEA = "KXNFLGAME-26SEP24SEAWAS-SEA"
WAS = "KXNFLGAME-26SEP24SEAWAS-WAS"
GB = "KXNFLGAME-26SEP27CHIGB-GB"
CHI = "KXNFLGAME-26SEP27CHIGB-CHI"
HOU = "KXNFLGAME-26SEP27HOUIND-HOU"
IND = "KXNFLGAME-26SEP27HOUIND-IND"

# The owner's figures, to the digit they were reported.
SEA_SHARP = 0.7616627132
SEA_GROSS, SEA_NET = 0.0116627132, -0.0083372868
GB_SHARP = 0.7241734676
GB_GROSS, GB_NET = 0.0241734676, 0.0041734676


def _session():
    """The synthetic session, once per process: it is ~0.7 s of monitor."""
    if not hasattr(_session, "cache"):
        tmp = Path(tempfile.mkdtemp())
        path, net, _ = syn.run_session(tmp)
        old = syn.as_recorded_before_assessments(path, tmp / "older.jsonl")
        with diag.no_network():
            rows, _ = shadow_monitor.read_records(path)
            old_rows, _ = shadow_monitor.read_records(old)
            current, older = diag.diagnose(rows), diag.diagnose(old_rows)
        _session.cache = dict(tmp=tmp, path=path, old=old, net=net,
                              rows=rows, current=current, older=older)
    return _session.cache


def tearDownModule():
    cache = getattr(_session, "cache", None)
    if cache:
        shutil.rmtree(cache["tmp"], ignore_errors=True)


def _items(figures, ticker):
    return [a for a in figures["assessments"] if a["ticker"] == ticker]


class OwnerFiguresTest(unittest.TestCase):
    """Re-analysis reproduces what the owner reported, from raw records."""

    def setUp(self):
        self.s = _session()

    def test_the_coverage_denominators(self):
        for figures in (self.s["current"], self.s["older"]):
            c = figures["coverage"]
            with self.subTest(format="current" if figures is self.s["current"]
                              else "before assessments"):
                self.assertEqual((c["moves"], c["games"],
                                  c["contract_assessments"]), (5, 4, 10))
                self.assertEqual(c["moves_on_no_joined_contract"], 0)
                self.assertEqual((c["within_horizon"], c["outside_horizon"],
                                  c["collection_failures"]), (8, 2, 0))
                self.assertEqual(c["usable"], 8)
                self.assertEqual(c["screen"], {"below_ev_floor": 8})
                self.assertEqual(c["refusals"],
                                 {"no_exchange_book_at_the_trigger": 2})
                self.assertEqual((c["admitted"], c["entries"]), (0, 0))
                self.assertEqual(c["reproduced"], 10)
                self.assertEqual(c["disagreements"], [])

    def test_no_screened_assessment_failed_spread_band_or_lead_time(self):
        screened = [a for a in self.s["older"]["assessments"]
                    if a["decision"]["refusal"] is None]
        self.assertEqual(len(screened), 8)
        for item in screened:
            gates = item["assessment"]["screen"]["gates"]
            with self.subTest(ticker=item["ticker"]):
                self.assertEqual(item["decision"]["rejection"],
                                 "below_ev_floor")
                for gate in ("price_band", "lead_time", "decision_quotes",
                             "spread"):
                    self.assertTrue(gates[gate]["passes"], gate)
                self.assertFalse(gates["net_ev"]["passes"])

    def test_seattle_to_the_digit(self):
        (item,) = _items(self.s["older"], SEA)
        a = item["assessment"]
        self.assertEqual((a["decision_book"]["bid"], a["decision_book"]["ask"]),
                         (0.74, 0.75))
        yes = a["sides"]["YES"]
        self.assertEqual(yes["decision_price"], 0.75)
        self.assertAlmostEqual(yes["win_probability"], SEA_SHARP, places=10)
        self.assertAlmostEqual(yes["gross_edge"], SEA_GROSS, places=10)
        self.assertEqual(yes["fee"], 0.02)
        self.assertAlmostEqual(yes["net_ev"], SEA_NET, places=10)
        self.assertAlmostEqual(yes["margin_to_floor"], SEA_NET - 0.01,
                               places=10)
        self.assertEqual(a["screen"]["best_side"], "YES")
        # The sensitivity says what the verdict hangs on: no fee this route
        # and size can charge lets it clear.
        ceilings = {(r["route"], r["contracts"]): r["multiplier_at_most"]
                    for r in a["fees"]["sensitivity"]["break_even_multiplier"]}
        self.assertEqual(ceilings[("non_direct", 1)], 0.0)
        self.assertAlmostEqual(ceilings[("direct", 1)], 0.0016 / 0.013125)

    def test_green_bay_to_the_digit(self):
        first = min(_items(self.s["older"], GB),
                    key=lambda a: a["assessment"]["move"]["detected_at"])
        yes = first["assessment"]["sides"]["YES"]
        self.assertEqual((first["assessment"]["decision_book"]["bid"],
                          first["assessment"]["decision_book"]["ask"]),
                         (0.69, 0.70))
        self.assertAlmostEqual(yes["win_probability"], GB_SHARP, places=10)
        self.assertAlmostEqual(yes["gross_edge"], GB_GROSS, places=10)
        self.assertEqual(yes["fee"], 0.02)
        self.assertAlmostEqual(yes["net_ev"], GB_NET, places=10)
        self.assertLess(yes["net_ev"], 0.01, "below the frozen floor")
        self.assertGreater(yes["net_ev"], 0.0, "but positive: the fee decided")
        # It would clear at a lower multiplier; the table says where.
        ceilings = {(r["route"], r["contracts"]): r["multiplier_at_most"]
                    for r in first["assessment"]["fees"]["sensitivity"]
                    ["break_even_multiplier"]}
        self.assertAlmostEqual(ceilings[("non_direct", 1)], 0.01 / 0.0147)
        self.assertAlmostEqual(ceilings[("direct", 1)], 0.0141 / 0.0147)
        # The opposite move about fifteen minutes later is on the record.
        (opposite,) = first["opposite_moves"]
        self.assertAlmostEqual(opposite["after_seconds"], 900.0, delta=1.0)
        self.assertLess(opposite["move_for_yes"], 0.0)

    def test_houston_is_outside_the_horizon_not_a_collection_failure(self):
        for ticker in (HOU, IND):
            (item,) = _items(self.s["older"], ticker)
            coverage = item["assessment"]["coverage"]
            with self.subTest(ticker=ticker):
                self.assertEqual(coverage["class"],
                                 CoverageClass.OUTSIDE_HORIZON.value)
                self.assertFalse(coverage["collection_failure"])
                self.assertFalse(coverage["within_horizon_at_tick"])
                self.assertAlmostEqual(coverage["hours_to_start_at_tick"],
                                       76.06, delta=0.01)
                self.assertEqual(coverage["horizon_hours"], 73.0)
                self.assertFalse(coverage["lead_time_gate_at_trigger"]["admits"])
                self.assertEqual(item["decision"]["refusal"],
                                 "no_exchange_book_at_the_trigger")
                self.assertIn("by design, not by failure", coverage["note"])

    def test_seattle_paid_the_ask_and_sold_the_bid_for_nothing(self):
        (item,) = _items(self.s["older"], SEA)
        (scenario,) = item["adjustment_capture"]
        self.assertEqual((scenario["kind"], scenario["side"]),
                         (HYPOTHETICAL, "YES"))
        self.assertEqual(scenario["entry"]["price"], 0.75)
        by = {m["seconds"]: m for m in scenario["markouts"]}
        # Before Kalshi's response: the bid is still 0.74.
        self.assertEqual(by[600.0]["exit"]["price"], 0.74)
        # After it: 0.75 ask paid, 0.75 bid received -- zero gross, and the
        # two fees make it a loss.
        self.assertEqual(by[900.0]["exit"]["price"], 0.75)
        self.assertEqual(by[900.0]["gross"], 0.0)
        self.assertAlmostEqual(by[900.0]["net"], -0.04)
        self.assertEqual(by[900.0]["sharp"]["status"], "persisted")

    def test_a_markout_past_the_reads_is_censored_not_filled(self):
        """Houston's follow window ends at 30 minutes and its game is outside
        the horizon, so nothing reads its book at the last markout."""
        (item,) = _items(self.s["older"], HOU)
        last = item["adjustment_capture"][0]["markouts"][-1]
        self.assertEqual(last["seconds"], 1800.0)
        self.assertNotIn("net", last)
        self.assertEqual(last["censored"]["reason"],
                         "no_usable_read_within_tolerance")

    def test_every_rejected_decision_keeps_its_diagnostics_in_the_record(self):
        decisions = [r for r in self.s["rows"] if r["kind"] == "decision"]
        self.assertEqual(len(decisions), 10)
        for row in decisions:
            with self.subTest(ticker=row["market_ticker"]):
                self.assertFalse(row["admitted"])
                a = row["assessment"]
                self.assertNotIn("error", a)
                self.assertIn("coverage", a)
                self.assertIn("provenance", a["fees"])
                if row["refusal"] is None:
                    self.assertEqual(row["rejection"], "below_ev_floor")
                    self.assertEqual(set(a["sides"]), {"YES", "NO"})
                    self.assertIsNotNone(a["fees"]["sensitivity"])

    def test_no_execution_read_waited_behind_the_hourly_rejoin(self):
        """The synthetic run found a rejoin between a move and its execution
        read, adding ~2.4s of entry delay. The rejoin now comes after."""
        for item in self.s["current"]["assessments"]:
            after = item["assessment"]["execution_book"]["after_decision_seconds"]
            with self.subTest(ticker=item["ticker"]):
                self.assertLess(after["received"], 1.0)


class RecordedAgainstRecomputedTest(unittest.TestCase):
    """One function decides what an assessment means, live and offline."""

    def setUp(self):
        self.s = _session()

    def test_the_live_record_equals_the_offline_recomputation(self):
        decisions = [r for r in self.s["rows"] if r["kind"] == "decision"]
        rebuilt = sorted(self.s["current"]["assessments"],
                         key=lambda a: a["index"])
        self.assertEqual(len(decisions), len(rebuilt))
        for row, item in zip(decisions, rebuilt):
            with self.subTest(ticker=row["market_ticker"]):
                self.assertEqual(
                    row["assessment"],
                    json.loads(json.dumps(item["assessment"],
                                          default=diag.jsonable)))

    def test_the_tick_rebuilt_from_file_order_is_the_recorded_one(self):
        c = self.s["current"]["coverage"]
        self.assertEqual(c["tick_disagreements"], [])
        for item in self.s["current"]["assessments"]:
            self.assertTrue(item["tick_agreement"]["agrees"])

    def test_a_file_from_before_assessments_yields_the_same_assessments(self):
        def comparable(figures):
            out = []
            for item in figures["assessments"]:
                a = json.loads(json.dumps(item["assessment"],
                                          default=diag.jsonable))
                a["coverage"].pop("tick_context")
                out.append((item["ticker"], a, item["adjustment_capture"]))
            return out

        self.assertEqual(comparable(self.s["current"]),
                         comparable(self.s["older"]))
        sources = {a["assessment"]["coverage"]["tick_context"]
                   for a in self.s["older"]["assessments"]}
        self.assertEqual(sources, {"reconstructed_from_file_order"})
        self.assertEqual(self.s["older"]["session"]["fee_route_source"][:12],
                         "not recorded")

    def test_a_recomputation_that_disagrees_is_reported_not_resolved(self):
        rows = [dict(r) for r in self.s["rows"]]
        for row in rows:
            if row["kind"] == "decision" and row["market_ticker"] == SEA:
                row["book"] = {**row["book"], "decision_ask": 0.74}
        with diag.no_network():
            figures = diag.diagnose(rows)
        (diff,) = figures["coverage"]["disagreements"]
        self.assertEqual(diff["ticker"], SEA)
        self.assertEqual(diff["differences"][0]["field"], "book.decision_ask")
        self.assertIn("DISAGREES", diag.render_diagnostics(figures))


class OfflineTest(unittest.TestCase):
    """The re-analysis reads the file and nothing else."""

    def test_every_connection_is_refused_inside_the_guard(self):
        originals = (socket.create_connection, urllib.request.urlopen,
                     socket.socket.connect)
        with diag.no_network():
            with self.assertRaises(diag.OfflineViolation):
                socket.create_connection(("example.com", 443))
            with self.assertRaises(diag.OfflineViolation):
                urllib.request.urlopen("https://api.elections.kalshi.com/")
            with socket.socket() as sock:
                with self.assertRaises(diag.OfflineViolation):
                    sock.connect(("127.0.0.1", 9))
        self.assertEqual((socket.create_connection, urllib.request.urlopen,
                          socket.socket.connect), originals)

    def test_the_report_command_runs_offline_and_writes_its_json(self):
        s = _session()
        out_json = s["tmp"] / "diagnostics.json"
        text = io.StringIO()
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("network used")), \
                redirect_stdout(text), redirect_stderr(text):
            code = shadow_monitor.main(["--report", str(s["old"]),
                                        "--json", str(out_json)])
        self.assertEqual(code, 0, text.getvalue())
        figures = json.loads(out_json.read_text())
        self.assertEqual(figures["schema"], diag.SCHEMA)
        self.assertEqual(figures["coverage"]["screen"], {"below_ev_floor": 8})
        self.assertEqual(figures["source"]["file"], s["old"].name)
        self.assertNotIn(str(s["tmp"]), json.dumps(figures["source"]))
        report = text.getvalue()
        for section in ("SHADOW SESSION", "SHADOW DIAGNOSTICS", "COVERAGE",
                        "ASSESSMENTS", "SETTLEMENT VALUE",
                        "ADJUSTMENT CAPTURE", "FEES", "UNRESOLVED"):
            self.assertIn(section, report)

    def test_realized_figures_are_withheld_without_settlement(self):
        value = _session()["older"]["settlement_value"]
        self.assertEqual(value["realized"]["status"], "withheld")
        self.assertEqual(value["admitted"], 0)
        self.assertAlmostEqual(value["best_margin_to_floor"]["max"],
                               GB_NET - 0.01, places=10)

    def test_a_settlements_file_must_say_zero_or_one(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bad = tmp / "s.json"
        bad.write_text(json.dumps({SEA: "yes"}))
        with self.assertRaises(ValueError):
            diag.load_settlements(bad)
        bad.write_text(json.dumps({SEA: True}))
        with self.assertRaises(ValueError):
            diag.load_settlements(bad)
        good = tmp / "g.json"
        good.write_text(json.dumps({SEA: 1, WAS: 0}))
        self.assertEqual(diag.load_settlements(good), {SEA: 1, WAS: 0})


class CollectionFailureTest(MonitorHarness):
    """A watched, in-horizon game with no book is a collection failure --
    and is named as one, on a session the real monitor recorded."""

    def test_failed_decision_reads_before_a_move_are_a_collection_failure(self):
        # Every decision read from the second tick through the move's own
        # tick fails (ticks fall 5s past the minute, after the preflight);
        # the execution reads just after the move succeed.
        code, text, _ = self.paid(fail_books_during=(
            MONITOR_T0 + timedelta(seconds=10),
            MOVE_AT + timedelta(seconds=5, milliseconds=300)))
        self.assertEqual(code, 0, text)
        files = sorted(self.tmp.glob("shadow_*.jsonl"))
        rows, _ = shadow_monitor.read_records(files[0])
        with diag.no_network():
            figures = diag.diagnose(rows)
        c = figures["coverage"]
        self.assertEqual(c["collection_failures"], 2)
        self.assertEqual(c["outside_horizon"], 0)
        reads = {}
        for item in figures["assessments"]:
            coverage = item["assessment"]["coverage"]
            self.assertEqual(coverage["class"], "collection_failure")
            self.assertIn("COLLECTION failure", coverage["note"])
            reads[item["ticker"][-3:]] = coverage["tick_read"]
        # The first read failed; the second was abandoned behind it.
        self.assertEqual(reads, {"NYG": "failed", "DAL": "abandoned"})
        # The live record said the same, before anyone re-analysed it.
        for row in self.kinds(rows, "decision"):
            self.assertEqual(row["assessment"]["coverage"]["class"],
                             "collection_failure")


def _trigger(at, before, after, *, event="evt", stream="s"):
    b, a = fair_probabilities(*before), fair_probabilities(*after)
    return MoveTrigger(
        event_id=event, stream_id=stream, detected_at=at,
        provider_observed_at=at - timedelta(seconds=14),
        book_change_earliest=at - timedelta(seconds=44),
        book_change_latest=at - timedelta(seconds=14),
        provider_to_available_seconds=14.0, age_at_decision_seconds=14.0,
        capture_age_seconds=None, fair_before_away=b[0],
        fair_before_home=b[1], fair_after_away=a[0], fair_after_home=a[1],
        delta_away=a[0] - b[0], delta_home=a[1] - b[1],
        overround_before=b[2], overround_after=a[2],
        devig_disagreement=a[3], policy=MovePolicy(book="pinnacle"),
        before_provenance={}, after_provenance={})


def _book(ts, bid, ask, *, ticker="T", yes_size=100.0, no_size=150.0,
          read=timedelta(milliseconds=200)):
    return BookQuote(ticker=ticker, ts=ts, bid_close=bid, ask_close=ask,
                     bid_size=yes_size, ask_size=no_size, yes_levels=2,
                     no_levels=1, sent_at=ts - read)


AT = datetime(2026, 9, 24, 17, 43, 55, 502234, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)


class AssessmentTest(unittest.TestCase):
    """`assess` on hand-built inputs: the record a decision now carries."""

    def assessed(self, books, *, before=(-320, 262), after=(-354, 290),
                 yes_is_home=False, delay=timedelta(milliseconds=400),
                 tick=None, series="KXNFLGAME", start=KICKOFF):
        return assess(_trigger(AT, before, after), books, market_ticker="T",
                      yes_is_home=yes_is_home, start=start, entry_delay=delay,
                      entry_tolerance=timedelta(seconds=1), series=series,
                      tick=tick)

    def test_seattle_as_the_owner_reported_it(self):
        decision, record = self.assessed([
            _book(AT - timedelta(seconds=1.5), 0.74, 0.75),
            _book(AT + timedelta(milliseconds=400), 0.74, 0.75)])
        self.assertFalse(decision.admitted)
        self.assertEqual(decision.rejection.value, "below_ev_floor")
        self.assertEqual(record["screen"]["rejection"], "below_ev_floor")
        yes = record["sides"]["YES"]
        self.assertAlmostEqual(yes["gross_edge"], SEA_GROSS, places=10)
        self.assertAlmostEqual(yes["net_ev"], SEA_NET, places=10)
        self.assertEqual(yes["execution"]["latency_cost"], 0.0)
        self.assertEqual(yes["depth_at_decision"], 150.0,
                         "YES entry takes the resting NO bids")
        self.assertEqual(record["sides"]["NO"]["depth_at_decision"], 100.0)
        age = record["decision_book"]["age_at_decision_seconds"]
        self.assertAlmostEqual(age["since_received"], 1.5)
        self.assertAlmostEqual(age["since_sent"], 1.7)

    def test_a_missing_execution_book_is_named_not_priced_at_the_decision(self):
        decision, record = self.assessed(
            [_book(AT - timedelta(seconds=1.5), 0.74, 0.75)])
        for side in ("YES", "NO"):
            row = record["sides"][side]
            self.assertTrue(row["priced"], "the decision price is still known")
            self.assertIsNone(row["execution"])
            self.assertIn("no fill", row["execution_missing_because"])
        self.assertEqual(decision.rejection.value, "no_executable_side")
        self.assertTrue(any(m.startswith("execution_book")
                            for m in record["missing"]))

    def test_the_fee_provenance_says_what_is_assumed(self):
        _, record = self.assessed([_book(AT - timedelta(seconds=1), 0.74, 0.75),
                                   _book(AT + timedelta(milliseconds=400),
                                         0.74, 0.75)])
        provenance = record["fees"]["provenance"]
        self.assertEqual(provenance["basis"], "generic_coefficient")
        self.assertEqual(provenance["multiplier"], 1.0)
        self.assertFalse(provenance["rounding"]["route_resolved"])
        joined = " ".join(provenance["unresolved"])
        for topic in ("series_schedule", "account_route", "rounding_source",
                      "order_size"):
            self.assertIn(topic, joined)
        labels = {s["multiplier_basis"] for s in
                  record["fees"]["sensitivity"]["scenarios"]}
        self.assertIn("as priced: generic coefficient, ASSUMED", labels)
        self.assertTrue(any("not known to apply" in label for label in labels))

    def test_a_dated_schedule_is_named_as_one(self):
        from core.fees import fee_provenance
        dated = fee_provenance("KXMLBGAME", AT)
        self.assertEqual((dated["basis"], dated["multiplier"]),
                         ("dated_series_schedule", 0.5))
        self.assertTrue(any(u.startswith("conflict:")
                            for u in dated["unresolved"]))
        early = fee_provenance("KXMLBGAME",
                               datetime(2025, 1, 1, tzinfo=UTC))
        self.assertEqual(early["basis"], "unresolved")
        self.assertIsNone(early["multiplier"])
        self.assertIn("precedes", early["error"])

    def test_a_crossed_decision_book_is_refused_with_its_reason(self):
        crossed = replace(_book(AT - timedelta(seconds=1), 0.76, 0.75))
        decision, record = self.assessed([crossed])
        self.assertEqual(decision.refusal.value,
                         "crossed_exchange_book_at_the_trigger")
        self.assertEqual(record["screen"]["outcome"], "not_screened")
        self.assertIsNone(record["sides"])


class CoverageClassTest(unittest.TestCase):
    """Why a book was missing: never due, a market state, or a failure."""

    E = Eligibility()
    START = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)

    def classify(self, *, tick_at, reads=None, joined=("T",), abandoned=None,
                 present=False, horizon=timedelta(hours=73), detected=None,
                 eligibility=None):
        tick = TickContext(at=tick_at, horizon=horizon,
                           joined=frozenset(joined), reads=reads or {},
                           abandoned_after=abandoned)
        return classify_coverage(
            ticker="T", start=self.START, tick=tick,
            decision_book_present=present,
            detected_at=detected or tick_at + timedelta(seconds=2),
            eligibility=eligibility or self.E)

    def inside(self):
        return self.START - timedelta(hours=40)

    def test_each_cause_of_a_missing_book_is_named(self):
        cases = [
            (dict(reads={"T": "failed"}), "collection_failure", "failed"),
            (dict(reads={"U": "failed"}, abandoned="U"), "collection_failure",
             "abandoned"),
            (dict(joined=()), "collection_failure", "not_joined_at_tick"),
            (dict(reads={"T": "malformed"}), "collection_failure",
             "malformed"),
            (dict(reads={"T": "one_sided"}), "no_two_sided_book", "one_sided"),
        ]
        for kwargs, klass, read in cases:
            with self.subTest(read=read):
                out = self.classify(tick_at=self.inside(), **kwargs)
                self.assertEqual((out["class"], out["tick_read"]),
                                 (klass, read))
                self.assertEqual(out["collection_failure"],
                                 klass == "collection_failure")

    def test_a_stale_book_after_a_failed_read_is_observed_and_says_so(self):
        out = self.classify(tick_at=self.inside(), reads={"T": "failed"},
                            present=True)
        self.assertEqual(out["class"], "observed")
        self.assertIn("earlier one", out["note"])

    def test_outside_the_horizon_is_never_a_failure(self):
        far = self.START - timedelta(hours=76)
        out = self.classify(tick_at=far, reads={})
        self.assertEqual(out["class"], CoverageClass.OUTSIDE_HORIZON.value)
        self.assertFalse(out["collection_failure"])
        self.assertEqual(out["tick_read"], TickRead.NOT_ATTEMPTED.value)
        self.assertAlmostEqual(out["hours_beyond_horizon"], 3.0)
        with_book = self.classify(tick_at=far, present=True)
        self.assertEqual(with_book["class"], "observed_outside_horizon")

    def test_a_horizon_narrower_than_the_screen_says_what_it_cost(self):
        out = self.classify(tick_at=self.START - timedelta(hours=40),
                            horizon=timedelta(hours=24))
        self.assertEqual(out["class"], "outside_observation_horizon")
        self.assertTrue(out["lead_time_gate_at_trigger"]["admits"])
        self.assertIn("WOULD have admitted", out["note"])

    def test_no_tick_context_is_unknown_not_observed(self):
        out = classify_coverage(ticker="T", start=self.START, tick=None,
                                decision_book_present=True,
                                detected_at=self.inside(),
                                eligibility=self.E)
        self.assertEqual(out["class"], CoverageClass.UNKNOWN.value)
        self.assertIsNone(out["collection_failure"])


def _reads(*books, failed=()):
    out = [Read(b.sent_at, b.ts, "ok", b) for b in books]
    out += [Read(at - timedelta(milliseconds=200), at, "failed", None)
            for at in failed]
    return sorted(out, key=lambda r: r.received)


class CaptureTest(unittest.TestCase):
    """Executable round trips: the synthetic cases the owner asked for."""

    MOVE = datetime(2026, 9, 24, 17, 0, tzinfo=UTC)
    START = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)
    ONE = CapturePolicy(markouts=(timedelta(seconds=60),))

    def run_capture(self, reads, side="YES", policy=None, **kwargs):
        options = dict(kind=HYPOTHETICAL, side=side, reads=reads,
                       detected_at=self.MOVE, start=self.START,
                       session_end=self.MOVE + timedelta(hours=2),
                       series="KXNFLGAME", policy=policy or self.ONE)
        options.update(kwargs)
        return capture(**options)

    def at(self, seconds):
        return self.MOVE + timedelta(seconds=seconds)

    def test_seattle_ask_to_bid_is_zero_gross_and_a_loss_after_fees(self):
        result = self.run_capture(_reads(_book(self.at(0.4), 0.74, 0.75),
                                         _book(self.at(61), 0.75, 0.76)))
        (m,) = result["markouts"]
        self.assertEqual((result["entry"]["price"], m["exit"]["price"]),
                         (0.75, 0.75))
        self.assertEqual(m["gross"], 0.0)
        self.assertAlmostEqual(m["net"], -0.04)
        direct = self.run_capture(_reads(_book(self.at(0.4), 0.74, 0.75),
                                         _book(self.at(61), 0.75, 0.76)),
                                  route="direct")
        self.assertAlmostEqual(direct["markouts"][0]["net"], -0.0264)

    def test_no_enters_at_one_minus_bid_and_exits_at_one_minus_ask(self):
        result = self.run_capture(_reads(_book(self.at(0.4), 0.40, 0.42),
                                         _book(self.at(61), 0.35, 0.37)),
                                  side="NO")
        (m,) = result["markouts"]
        self.assertAlmostEqual(result["entry"]["price"], 0.60)
        self.assertAlmostEqual(m["exit"]["price"], 0.63)
        self.assertAlmostEqual(m["gross"], 0.03)
        self.assertEqual(result["entry"]["depth"], 100.0,
                         "NO entry takes the resting YES bids")
        self.assertEqual(m["exit"]["depth"], 150.0,
                         "NO exit hits the resting NO bids")

    def test_each_leg_pays_its_own_fee_at_its_own_price(self):
        result = self.run_capture(_reads(_book(self.at(0.4), 0.49, 0.50),
                                         _book(self.at(61), 0.89, 0.90)))
        (m,) = result["markouts"]
        self.assertEqual(result["entry"]["fee"], 0.02)       # 0.0175 raw
        self.assertEqual(m["exit"]["fee"], 0.01)             # 0.0063 raw
        self.assertAlmostEqual(m["net"], 0.39 - 0.03)

    def test_delayed_execution_waits_for_the_next_read_never_an_earlier(self):
        reads = _reads(_book(self.at(-5), 0.60, 0.61),
                       _book(self.at(12), 0.70, 0.71),
                       _book(self.at(75), 0.72, 0.73))
        result = self.run_capture(reads)
        self.assertEqual(result["entry"]["price"], 0.71)
        self.assertAlmostEqual(result["entry"]["delay_after_move_seconds"], 12)
        (m,) = result["markouts"]
        self.assertEqual(m["exit"]["price"], 0.72)
        self.assertAlmostEqual(m["exit"]["late_by_seconds"], 3.0)

    def test_no_read_soon_enough_after_the_move_is_no_entry(self):
        reads = _reads(_book(self.at(-5), 0.60, 0.61),
                       _book(self.at(45), 0.70, 0.71), failed=[self.at(8)])
        result = self.run_capture(reads)
        self.assertNotIn("markouts", result)
        self.assertIn("no usable read within 30s", result["entry_missing_because"])
        self.assertIn("failed", result["entry_missing_because"])

    def test_unobserved_depth_is_flagged_and_short_depth_censors(self):
        blind = replace(_book(self.at(0.4), 0.74, 0.75), ask_size=None)
        result = self.run_capture(_reads(blind, _book(self.at(61), 0.75, 0.76)))
        self.assertFalse(result["entry"]["depth_observed"])
        self.assertFalse(result["markouts"][0]["depth_observed"])
        self.assertIn("net", result["markouts"][0])
        thin = replace(_book(self.at(61), 0.75, 0.76), bid_size=0.5)
        result = self.run_capture(_reads(_book(self.at(0.4), 0.74, 0.75), thin))
        (m,) = result["markouts"]
        self.assertEqual(m["censored"]["reason"], "exit_unusable")
        self.assertIn("below the 1 contract", m["censored"]["detail"])

    def test_a_stale_or_missing_exit_is_censored_never_filled(self):
        entry = _book(self.at(0.4), 0.74, 0.75)
        stale = _book(self.at(55), 0.80, 0.81)        # before the markout
        late = _book(self.at(95), 0.80, 0.81)         # past its tolerance
        result = self.run_capture(_reads(entry, stale, late,
                                         failed=[self.at(70)]))
        (m,) = result["markouts"]
        self.assertNotIn("net", m)
        self.assertEqual(m["censored"]["reason"],
                         "no_usable_read_within_tolerance")
        self.assertEqual(m["censored"]["reads_in_tolerance"], {"failed": 1})

    def test_session_end_and_kickoff_censor_the_markout(self):
        reads = _reads(_book(self.at(0.4), 0.74, 0.75),
                       _book(self.at(61), 0.75, 0.76))
        ended = self.run_capture(reads, session_end=self.at(30))
        self.assertEqual(ended["markouts"][0]["censored"]["reason"],
                         "session_ended")
        kicked = self.run_capture(reads, start=self.at(45))
        self.assertEqual(kicked["markouts"][0]["censored"]["reason"],
                         "game_started")

    def test_the_best_exit_is_hindsight_and_never_a_markout(self):
        reads = _reads(_book(self.at(0.4), 0.74, 0.75),
                       _book(self.at(30), 0.80, 0.81),
                       _book(self.at(61), 0.74, 0.75))
        result = self.run_capture(reads)
        self.assertEqual(result["markouts"][0]["exit"]["price"], 0.74)
        best = result["hindsight"]["max_favourable"]
        self.assertEqual(best["exit_price"], 0.80)
        self.assertIn("HINDSIGHT", result["hindsight"]["label"])

    def test_the_sharp_state_uses_only_polls_read_by_then(self):
        before, after = 0.74, 0.76
        readings = [
            SharpReading(self.at(0), "answered", fair_home=0.24,
                         fair_away=0.76),
            SharpReading(self.at(30), "answered", fair_home=0.255,
                         fair_away=0.745),
            SharpReading(self.at(60), "poll_unanswered"),
            SharpReading(self.at(90), "answered", fair_home=0.24,
                         fair_away=0.76)]
        kwargs = dict(readings=readings, detected_at=self.at(0),
                      yes_is_home=False, fair_before_yes=before,
                      fair_after_yes=after, min_move=0.01, session_end=None)
        self.assertEqual(sharp_state(at=self.at(10), **kwargs)["status"],
                         "persisted")
        reversed_ = sharp_state(at=self.at(45), **kwargs)
        self.assertEqual(reversed_["status"], "reversed")
        self.assertAlmostEqual(reversed_["retraced"], 0.015)
        self.assertFalse(reversed_["full_reversal"])
        blind = sharp_state(at=self.at(70), **kwargs)
        self.assertEqual((blind["status"], blind["because"]),
                         ("unobservable", "poll_unanswered"))
        self.assertEqual(sharp_state(at=self.at(95), **kwargs)["status"],
                         "persisted")
        # A reversal read at +30s is invisible at +29s.
        self.assertEqual(sharp_state(at=self.at(29), **kwargs)["status"],
                         "persisted")

    def test_the_summary_counts_games_beside_rows_and_names_censoring(self):
        policy = CapturePolicy(markouts=(timedelta(seconds=60),
                                         timedelta(seconds=120)))
        reads = _reads(_book(self.at(0.4), 0.74, 0.75),
                       _book(self.at(61), 0.75, 0.76))
        a = self.run_capture(reads, policy=policy)
        b = self.run_capture(reads, side="NO", policy=policy)
        summary = summarize([("g1", a), ("g1", b)], policy)[HYPOTHETICAL]
        first, second = summary["markouts"]
        self.assertEqual((first["priced"], first["games"]), (2, 1))
        self.assertEqual(second["censored"],
                         {"no_usable_read_within_tolerance": 2})

    def test_the_policy_refuses_an_unordered_or_empty_markout_set(self):
        for bad in ((), (timedelta(seconds=60), timedelta(seconds=30)),
                    (timedelta(0),)):
            with self.subTest(markouts=bad):
                with self.assertRaises(ValueError):
                    CapturePolicy(markouts=bad)


class PlanHorizonTest(MonitorHarness):
    """The plan lists which games a proposed session would watch, free."""

    def test_starts_at_lists_each_game_and_its_watched_hours(self):
        code, text, net = self.run_monitor(
            "--starts-at", "2026-09-13T21:00:00Z")
        self.assertEqual(code, 0, text)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("KXNFLGAME-26SEP13DALNYG", text)
        self.assertIn("watched  0.5h of the session", text)

    def test_starts_at_is_refused_on_a_paid_session(self):
        code, text, net = self.paid("--starts-at", "2026-09-13T21:00:00Z")
        self.assertEqual(code, 2, text)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("plan-only", text)

    def test_a_non_default_horizon_is_announced_and_recorded(self):
        code, text, _ = self.run_monitor("--book-horizon-hours", "96")
        self.assertEqual(code, 0, text)
        self.assertIn("is NOT the screen's 73h", text)
        code, text, _ = self.paid("--book-horizon-hours", "96")
        self.assertEqual(code, 0, text)
        start = self.kinds(self.records(), "session_start")[0]
        self.assertEqual(start["book_horizon_seconds"], 96 * 3600)
        self.assertEqual(start["fee_route"], "non_direct")
        self.assertEqual(start["entry_tolerance_seconds"], 1.0)

    def test_bad_values_are_usage_errors(self):
        for extra in (("--book-horizon-hours", "0"),
                      ("--starts-at", "2026-09-13 21:00"),
                      ("--json", "x.json")):
            with self.subTest(extra=extra):
                code, _, _ = self.run_monitor(*extra)
                self.assertEqual(code, 2)


class ProposalTest(unittest.TestCase):
    """SHADOW_NEXT_SESSION.md gets pasted too: its commands must parse, and
    its numbers must be the ones the tools compute."""

    TEXT = (Path(__file__).resolve().parent.parent
            / "SHADOW_NEXT_SESSION.md").read_text()

    def test_every_command_in_the_proposal_parses(self):
        from tests.test_reaction_capture import check_documented_commands
        self.assertGreaterEqual(check_documented_commands(self, self.TEXT), 5)

    def test_the_proposed_spend_is_the_computed_price(self):
        price = shadow_monitor.session_price(24, timedelta(seconds=30))
        self.assertEqual(price, 2881)
        self.assertIn("--spend 2881", self.TEXT)
        self.assertIn("2,881 polls, 2,881 credits", self.TEXT)

    def test_the_proposed_window_starts_on_a_friday_at_14_utc(self):
        start = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
        self.assertEqual(start.strftime("%A"), "Friday")
        self.assertIn("--starts-at 2026-10-02T14:00:00Z", self.TEXT)
        # The horizon arithmetic it quotes -- which this test corrected once:
        # the first draft put Sunday night a day late and Monday night
        # outside the session.
        end = start + timedelta(hours=24)
        horizon = shadow_monitor.BOOK_HORIZON
        self.assertEqual(datetime(2026, 10, 4, 17, tzinfo=UTC) - horizon,
                         datetime(2026, 10, 1, 16, tzinfo=UTC))
        snf = datetime(2026, 10, 5, 0, 20, tzinfo=UTC)
        self.assertEqual(snf - horizon, datetime(2026, 10, 1, 23, 20,
                                                 tzinfo=UTC))
        self.assertLess(snf - horizon, start)
        mnf = datetime(2026, 10, 6, 0, 15, tzinfo=UTC)
        self.assertEqual(mnf - horizon, datetime(2026, 10, 2, 23, 15,
                                                 tzinfo=UTC))
        self.assertAlmostEqual((end - (mnf - horizon)).total_seconds() / 3600,
                               14.75)
        self.assertIn("watched for the last 14.75 hours", self.TEXT)

    def test_it_is_still_a_proposal(self):
        self.assertIn("Nothing here is started or authorised", self.TEXT)


if __name__ == "__main__":
    unittest.main()
