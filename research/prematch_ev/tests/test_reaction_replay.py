"""Offline replay: the bundle reader refuses rather than guesses.

WHAT IS OBSERVED HERE AND WHAT IS SYNTHETIC
-------------------------------------------
The payload SHAPES are the observed wire format, taken from the same
structures `test_data.py` parses -- `*_dollars` as a fixed-point STRING,
`end_period_ts` as the candle's period CLOSE, the odds market's own
`last_update` preferred over the bookmaker envelope's. That matters because
an earlier candle fixture used numbers instead of strings, having been
invented rather than observed, so every real bid decoded to None and the
parser could only ever agree with the fixture.

The VALUES -- ids, team names, prices, timestamps -- are synthetic controlled
inputs, as everywhere else in these suites.

Crucially the bundle goes through the REAL parsers. `load_bundle` calls
`data.odds_history.parse_snapshot` and `data.kalshi_history.parse_candles`,
which is why these tests can assert on a bundle at all: the thing under test
is the reader's validation and the chain's wiring, not a second parser.

THE REFUSALS ARE THE POINT
--------------------------
A replay that silently skips a malformed game reports thinner coverage, and
thinner coverage reads downstream as a market with less activity. That is the
opposite of what it means, and it is the confusion this study's whole
coverage ledger exists to prevent. So every structural defect raises
`BundleError`, and every parse failure is recorded as a LOSS.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaction.episodes import DataRole, HoldoutViolation            # noqa: E402
from reaction.measure import usable_candle                          # noqa: E402
from reaction.replay import (                                       # noqa: E402
    BUNDLE_SCHEMA, BUNDLE_SCHEMA_POOLED, BundleError, load_bundle,
    render_report, replay, replay_file,
)

UTC = timezone.utc
START = datetime(2026, 9, 14, 0, 20, tzinfo=UTC)
EVENT, TICKER = "evt-nfl-1", "KXNFLGAME-26SEP13DALNYG"
AWAY, HOME = "Dallas Cowboys", "New York Giants"


def at(minutes: float) -> datetime:
    return START - timedelta(minutes=minutes)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def odds_payload(snapshot: datetime, away_price: float, home_price: float,
                 observed: datetime, event_id: str = EVENT) -> dict:
    """The observed odds-archive shape. Market `last_update` is the PROVIDER'S
    observation stamp, and it takes precedence over the book envelope's."""
    return {
        "timestamp": iso(snapshot),
        "data": [{
            "id": event_id,
            "commence_time": iso(START),
            "home_team": HOME, "away_team": AWAY,
            "bookmakers": [{
                "key": "pinnacle",
                "last_update": iso(observed - timedelta(minutes=5)),
                "markets": [{
                    "key": "h2h",
                    "last_update": iso(observed),
                    "outcomes": [{"name": HOME, "price": home_price},
                                 {"name": AWAY, "price": away_price}],
                }],
            }],
        }],
    }


def candle_payload(rows) -> dict:
    """The observed candlesticks shape: fixed-point STRINGS, not numbers."""
    return {"candlesticks": [{
        "end_period_ts": int(ts.timestamp()),
        "yes_bid": {"close_dollars": f"{bid:.4f}"},
        "yes_ask": {"close_dollars": f"{ask:.4f}"},
        "price": {"close_dollars": f"{(bid + ask) / 2:.4f}",
                  "mean_dollars": f"{(bid + ask) / 2:.4f}"},
        "volume_fp": "120", "open_interest_fp": 900,
    } for ts, bid, ask in rows]}


def flat_candles(first=210.0, last=160.0, bid=0.56, ask=0.58):
    return [(at(m), bid, ask) for m in range(int(first), int(last) - 1, -1)]


def _base_game() -> dict:
    """The one healthy game every bundle here is built from."""
    return {
        "provider_event_id": EVENT,
        "event_ticker": TICKER,
        "start": iso(START),
        "start_source": "external_schedule",
        "historical_schedule_as_of": "unverified",
        "odds_snapshots": [
            odds_payload(at(200), 120, -140, at(201)),
            odds_payload(at(195), 160, -190, at(196)),
        ],
        "contracts": [
            {"market_ticker": f"{TICKER}-NYG", "yes_is_home": True,
             "settled_yes": 1,
             "candlesticks": [candle_payload(flat_candles())]},
            {"market_ticker": f"{TICKER}-DAL", "yes_is_home": False,
             "settled_yes": 0,
             "candlesticks": [candle_payload(flat_candles())]},
        ],
    }


def bundle(*, games=None, schema=BUNDLE_SCHEMA, **game_overrides) -> dict:
    """A minimal valid bundle: one game, two contracts, one detected move."""
    if games is None:
        game = _base_game()
        game.update(game_overrides)
        games = [game]
    return {"schema": schema, "games": games}


def write(payload: dict) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(payload, handle)
    handle.close()
    return Path(handle.name)


class RoundTripTest(unittest.TestCase):
    """Raw payloads in, a full ledger out, through the real parsers."""

    def test_a_valid_bundle_replays_end_to_end(self):
        games, report = load_bundle(write(bundle()))
        self.assertEqual(report.games, 1)
        self.assertEqual(report.contracts, 2)
        self.assertEqual(report.snapshots_parsed, 2)
        self.assertEqual(report.quotes, 2)
        self.assertEqual(report.candles, 102, "51 candles x 2 contracts")
        self.assertTrue(report.complete)
        ledger = replay(games)
        self.assertEqual(ledger.stage("moves detected").total, 1)
        self.assertEqual(ledger.stage("contract rows screened").total, 2)
        self.assertEqual(len(ledger.episodes), 1)

    def test_the_real_parsers_decode_the_fixed_point_strings(self):
        """The defect an invented fixture could not catch.

        `*_dollars` is a STRING. A bundle reader with its own parser written
        against numbers would decode every bid to None here and report a
        market with no quotes.
        """
        games, _ = load_bundle(write(bundle()))
        candles = games[0].contracts[0].candles
        self.assertAlmostEqual(candles[0].bid_close, 0.56, places=8)
        self.assertAlmostEqual(candles[0].ask_close, 0.58, places=8)
        self.assertAlmostEqual(candles[0].mid, 0.57, places=8)

    def test_the_market_stamp_is_what_reaches_the_detector(self):
        """Bookmaker-level `last_update` is deprecated; the market's wins."""
        games, _ = load_bundle(write(bundle()))
        self.assertEqual(games[0].quotes[0].last_update, at(201))
        self.assertEqual(games[0].quotes[0].snapshot, at(200))

    def test_replay_file_is_the_one_entry_point_a_cli_needs(self):
        ledger, report = replay_file(write(bundle()))
        self.assertEqual(report.games, 1)
        self.assertEqual(ledger.stage("moves detected").total, 1)

    def test_nothing_in_the_replay_path_reaches_the_network(self):
        """Source-level: a fetch here would spend credits on a replay."""
        source = (Path(__file__).resolve().parent.parent
                  / "reaction" / "replay.py").read_text()
        for forbidden in ("fetch_snapshot", "fetch_candlesticks", "requests",
                          "urlopen", "apiKey", "http://", "https://"):
            self.assertNotIn(forbidden, source,
                             f"{forbidden!r} in the replay path")

    def test_the_bundle_is_json_serialisable_and_the_ledger_too(self):
        ledger, report = replay_file(write(bundle()))
        json.loads(json.dumps(ledger.as_dict()))
        json.loads(json.dumps(report.as_dict()))


class RefusalTest(unittest.TestCase):
    """Every structural defect raises. None is skipped."""

    def test_an_unknown_schema_is_refused_not_guessed_at(self):
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(bundle(schema="reaction_replay_bundle/99")))
        self.assertIn("refusing to guess", str(caught.exception))

    def test_an_empty_bundle_is_a_collection_failure(self):
        """Not a quiet market. The whole publish-guard lesson, one layer on."""
        with self.assertRaises(BundleError) as caught:
            load_bundle(write({"schema": BUNDLE_SCHEMA, "games": []}))
        self.assertIn("not a quiet market", str(caught.exception))

    def test_a_kickoff_with_no_declared_source_is_refused(self):
        """The two sources carry different warranties (`START_SOURCES`)."""
        for source in (None, "", "close_time", "guessed"):
            with self.subTest(start_source=source):
                with self.assertRaises(BundleError) as caught:
                    load_bundle(write(bundle(start_source=source)))
                self.assertIn("start_source", str(caught.exception))

    def test_a_naive_kickoff_is_refused(self):
        """A naive stamp is not an instant, and UTC would be invented.

        This is the ticker-day-versus-UTC-day failure at its root: attaching
        an offset lands an evening kickoff on the wrong DAY.
        """
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(bundle(start="2026-09-14T00:20:00")))
        self.assertIn("not timezone-aware", str(caught.exception))

    def test_an_unparseable_kickoff_is_refused(self):
        for value in ("not a date", 1758400000, None):
            with self.subTest(start=value):
                with self.assertRaises(BundleError):
                    load_bundle(write(bundle(start=value)))

    def test_an_unoriented_contract_is_refused_never_defaulted(self):
        """An unoriented probability is an INVERTED signal, not a smaller one."""
        payload = bundle()
        del payload["games"][0]["contracts"][0]["yes_is_home"]
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(payload))
        self.assertIn("silently INVERTED", str(caught.exception))

    def test_a_non_boolean_orientation_is_refused(self):
        for value in ("true", 1, 0, None):
            with self.subTest(yes_is_home=value):
                payload = bundle()
                payload["games"][0]["contracts"][0]["yes_is_home"] = value
                with self.assertRaises(BundleError):
                    load_bundle(write(payload))

    def test_a_bogus_settlement_value_is_refused(self):
        for value in (2, -1, "yes", 0.5):
            with self.subTest(settled_yes=value):
                payload = bundle()
                payload["games"][0]["contracts"][0]["settled_yes"] = value
                with self.assertRaises(BundleError):
                    load_bundle(write(payload))

    def test_a_game_with_no_contracts_is_a_join_failure(self):
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(bundle(contracts=[])))
        self.assertIn("join failure", str(caught.exception))

    def test_a_missing_identifier_is_refused(self):
        for field in ("provider_event_id", "event_ticker"):
            with self.subTest(field=field):
                payload = bundle()
                del payload["games"][0][field]
                with self.assertRaises(BundleError):
                    load_bundle(write(payload))

    def test_a_root_that_is_not_an_object_is_refused(self):
        with self.assertRaises(BundleError):
            load_bundle(write([{"schema": BUNDLE_SCHEMA}]))


class LossReportingTest(unittest.TestCase):
    """An unread payload is a LOSS, not a quiet zero."""

    def test_an_unparseable_candle_payload_is_recorded_as_incomplete(self):
        payload = bundle()
        payload["games"][0]["contracts"][0]["candlesticks"] = [
            {"error": "unauthorized"}]
        games, report = load_bundle(write(payload))
        self.assertFalse(report.complete)
        self.assertEqual(len(report.incomplete_candles), 1)
        self.assertIn("*** COVERAGE INCOMPLETE", render_report(report))
        self.assertIn("not quiet", render_report(report))

    def test_an_unparseable_snapshot_is_recorded_and_not_counted_as_parsed(
            self):
        payload = bundle()
        payload["games"][0]["odds_snapshots"] = [
            odds_payload(at(200), 120, -140, at(201)), {"data": "nonsense"}]
        games, report = load_bundle(write(payload))
        self.assertEqual(report.snapshot_payloads, 2)
        self.assertEqual(report.snapshots_parsed, 1)
        self.assertFalse(report.complete)
        self.assertEqual(len(report.incomplete_snapshots), 1)

    def test_a_clean_bundle_prints_no_loss_warning(self):
        """Or the warning is noise on every run (rule 10)."""
        _, report = load_bundle(write(bundle()))
        self.assertTrue(report.complete)
        self.assertNotIn("*** COVERAGE INCOMPLETE", render_report(report))


class PointInTimeTest(unittest.TestCase):
    """A schedule retrieved after the fact cannot certify a backtest."""

    def test_an_external_schedule_is_not_point_in_time(self):
        games, report = load_bundle(write(bundle()))
        self.assertFalse(games[0].point_in_time)
        self.assertEqual(report.point_in_time_unverified, [EVENT])
        text = render_report(report)
        self.assertIn("NOT knowable at the", text)
        self.assertIn("may not be", text)

    def test_a_ticker_published_kickoff_is_point_in_time(self):
        """MLB's case: the exchange published the time on the contract."""
        games, report = load_bundle(write(bundle(
            start_source="event_ticker", historical_schedule_as_of="n/a")))
        self.assertTrue(games[0].point_in_time)
        self.assertEqual(report.point_in_time_unverified, [])
        self.assertNotIn("NOT knowable", render_report(report))

    def test_an_unverified_ticker_kickoff_is_still_not_point_in_time(self):
        """Both conditions, not either: the source AND the verification."""
        games, _ = load_bundle(write(bundle(
            start_source="event_ticker",
            historical_schedule_as_of="unverified")))
        self.assertFalse(games[0].point_in_time)

    def test_the_start_sources_are_counted_by_kind(self):
        _, report = load_bundle(write(bundle()))
        self.assertEqual(report.start_sources, {"external_schedule": 1})
        self.assertIn("kickoff from external_schedule", render_report(report))


class SettlementTest(unittest.TestCase):
    """Absent settlement is a withheld figure, not a zero."""

    def test_a_bundle_without_settlement_still_replays(self):
        payload = bundle()
        for contract in payload["games"][0]["contracts"]:
            contract.pop("settled_yes")
        games, report = load_bundle(write(payload))
        self.assertEqual(report.games_without_settlement, [EVENT])
        ledger = replay(games)
        for row in ledger.episodes[0].as_dict()["screened"]:
            self.assertFalse(row["settlement_known"])
            self.assertIsNone(row["realized"])
        self.assertIn("Predicted EV is still", render_report(report))

    def test_predicted_ev_is_identical_with_and_without_settlement(self):
        """Because predicted EV never reads the outcome."""
        with_settlement = replay(load_bundle(write(bundle()))[0])
        payload = bundle()
        for contract in payload["games"][0]["contracts"]:
            contract.pop("settled_yes")
        without = replay(load_bundle(write(payload))[0])
        self.assertEqual(
            [r["predicted"]["predicted_ev_per_contract"]
             for r in with_settlement.episodes[0].as_dict()["screened"]
             if r["predicted"]],
            [r["predicted"]["predicted_ev_per_contract"]
             for r in without.episodes[0].as_dict()["screened"]
             if r["predicted"]])


class ReplayWiringTest(unittest.TestCase):
    """The chain is wired once, and the ledger accounts for all of it."""

    def test_the_detector_is_shared_across_games(self):
        """So a stream spanning two games cannot gain a free trigger."""
        second = json.loads(json.dumps(bundle()["games"][0]))
        second["provider_event_id"] = "evt-nfl-2"
        second["event_ticker"] = "KXNFLGAME-26SEP14DENKC"
        second["odds_snapshots"] = [
            odds_payload(at(200), 120, -140, at(201), event_id="evt-nfl-2"),
            odds_payload(at(195), 160, -190, at(196), event_id="evt-nfl-2")]
        second["contracts"] = [
            {"market_ticker": "KXNFLGAME-26SEP14DENKC-KC",
             "yes_is_home": True, "settled_yes": 1,
             "candlesticks": [candle_payload(flat_candles())]}]
        games, report = load_bundle(write(bundle(
            games=[bundle()["games"][0], second])))
        self.assertEqual(report.games, 2)
        ledger = replay(games)
        self.assertEqual(ledger.stage("moves detected").total, 2)
        self.assertEqual(len(ledger.episodes), 2)
        self.assertEqual(ledger.stage("streams that produced a move").total, 2,
                         "two games are two streams, not one")

    def test_only_this_games_quotes_reach_this_games_detector(self):
        """A snapshot carries every event; the bundle filters by event id."""
        payload = bundle()
        extra = odds_payload(at(197), 300, -400, at(198), event_id="other-evt")
        payload["games"][0]["odds_snapshots"].append(extra)
        games, _ = load_bundle(write(payload))
        self.assertEqual(len(games[0].quotes), 2,
                         "a foreign event's quote was attributed to this game")
        self.assertTrue(all(q.provider_event_id == EVENT
                            for q in games[0].quotes))

    def test_an_entry_delay_reaches_the_screen(self):
        games, _ = load_bundle(write(bundle()))
        prompt = replay(games)
        delayed = replay(games, entry_delay=timedelta(minutes=10))
        for row in delayed.episodes[0].as_dict()["screened"]:
            self.assertEqual(row["entry_delay_seconds"], 600.0)
        self.assertEqual(
            [r["entry_delay_seconds"]
             for r in prompt.episodes[0].as_dict()["screened"]], [0.0, 0.0])

    def test_a_development_window_replay_cannot_be_declared_a_holdout(self):
        """The guard reaches through the replay, not just the ledger."""
        games, _ = load_bundle(write(bundle()))
        with self.assertRaises(HoldoutViolation):
            replay(games, declared_role=DataRole.HOLDOUT)
        ledger = replay(games)
        self.assertTrue(ledger.window.exploratory)
        self.assertIs(ledger.window.effective_role, DataRole.DEVELOPMENT)

    def test_the_declared_policies_travel_into_the_ledger(self):
        ledger, _ = replay_file(write(bundle()))
        declared = ledger.as_dict()["declared_policies"]
        self.assertEqual(declared["move_detection"]["devig_method"], "shin")
        self.assertFalse(declared["reaction_measurement"]["tuned_on_outcomes"])


class _CliDriver:
    """Run the real CLI in-process and capture everything it printed."""

    def _run(self, argv):
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(argv)
        return code, "\n".join(chunks)


class ReplayCliTest(_CliDriver, unittest.TestCase):
    """Drive the real entry point.

    Round 5 shipped two crashes in entry points nothing drove, while 171
    tests passed. An exit code nothing exercises is a decoration.
    """

    def test_a_clean_replay_exits_zero(self):
        code, text = self._run(["--replay", str(write(bundle()))])
        self.assertEqual(code, 0)
        self.assertIn("REACTION EPISODE LEDGER", text)
        self.assertIn("No orders", text)

    def test_zero_entries_is_a_result_not_a_failure(self):
        """rule 27: the checkpoint study's published finding IS zero entries.

        An exit code that called that broken would put a permanent red line
        on the honest outcome and burn the signal a real defect needs.
        """
        payload = bundle()
        # a threshold nothing can clear -- the screen refuses every row
        for contract in payload["games"][0]["contracts"]:
            contract["candlesticks"] = [candle_payload(
                flat_candles(bid=0.97, ask=0.99))]
        code, text = self._run(["--replay", str(write(payload))])
        self.assertEqual(code, 0, "a zero-entry run is not a failing run")
        self.assertIn("entries selected", text)

    def test_an_unreadable_bundle_exits_one(self):
        code, _ = self._run(["--replay", str(write(
            bundle(schema="reaction_replay_bundle/99")))])
        self.assertEqual(code, 1)

    def test_a_mislabelled_holdout_exits_one(self):
        code, _ = self._run(["--replay", str(write(bundle())),
                             "--declare-role", "holdout"])
        self.assertEqual(code, 1)

    def test_an_incomplete_parse_exits_one(self):
        """An unread payload is a loss, and a loss is a fixable defect."""
        payload = bundle()
        payload["games"][0]["contracts"][0]["candlesticks"] = [
            {"error": "unauthorized"}]
        code, text = self._run(["--replay", str(write(payload))])
        self.assertEqual(code, 1)
        self.assertIn("COVERAGE INCOMPLETE", text)

    def test_json_output_carries_the_bundle_report_and_the_ledger(self):
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(["--replay", str(write(bundle())),
                                      "--json"])
        self.assertEqual(code, 0)
        payload = json.loads("\n".join(chunks))
        self.assertEqual(payload["ledger"]["schema"],
                         "reaction_episode_ledger/1")
        self.assertTrue(payload["bundle"]["coverage_complete"])
        self.assertTrue(payload["ledger"]["all_stages_reconcile"])
        self.assertFalse(payload["ledger"]["window"]["holdout_available"])

    def test_the_entry_delay_flag_reaches_the_screen(self):
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            run_reaction.main(["--replay", str(write(bundle())), "--json",
                               "--entry-delay", "600"])
        payload = json.loads("\n".join(chunks))
        rows = payload["ledger"]["episodes"][0]["screened"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["entry_delay_seconds"], 600.0)

    def test_the_replay_path_never_prints_a_credential(self):
        _, text = self._run(["--replay", str(write(bundle()))])
        for secret in ("apiKey", "api_key", "Bearer", "SECRET"):
            self.assertNotIn(secret, text)


class MissingTargetObservationTest(unittest.TestCase):
    """A snapshot that came back WITHOUT our event is a hole, not a non-event.

    `load_bundle` used to flatten every parsed quote into one list and filter
    it to the target event. A snapshot the provider genuinely returned, in
    which our event does not appear, then left NO trace at all -- so the two
    surviving quotes either side of it looked adjacent, sat inside
    `max_gap`, and the detector closed a delta across an interval nobody had
    observed. The trigger, the bracket and every lag derived from it were
    measured over a window containing an unread hole.

    `MoveDetector.note_gap` existed for exactly this and nothing called it.

    These drive the RAW BUNDLE, not the detector, because that is the layer
    the defect lived in: the detector was already correct and the caller
    never told it.
    """

    def _replay(self, snapshots, **overrides):
        games, report = load_bundle(write(bundle(odds_snapshots=snapshots,
                                                 **overrides)))
        ledger = replay(games)
        stages = {stage.stage: stage for stage in ledger.stages}
        return ledger, report, stages

    # the reviewer's own reproduction, verbatim
    HOLE = [
        lambda: odds_payload(at(200), 120, -140, at(201)),
        lambda: {"timestamp": iso(at(195)), "data": []},
        lambda: odds_payload(at(190), 160, -190, at(191)),
    ]

    def test_a_move_is_not_attributed_across_a_missing_snapshot(self):
        ledger, report, stages = self._replay([f() for f in self.HOLE])
        self.assertEqual(stages["moves detected"].total, 0,
                         "a move was closed across an interval in which the "
                         "target event was explicitly absent")

    def test_the_hole_is_named_in_the_detector_outcomes(self):
        """Invisible correctness is indistinguishable from a quiet market."""
        _, _, stages = self._replay([f() for f in self.HOLE])
        breakdown = stages["detector outcomes"].breakdown
        self.assertEqual(breakdown.get("declared_gap"), 1)
        self.assertEqual(breakdown.get("baseline_invalidated"), 1)

    def test_the_ledger_still_reconciles_with_a_declared_gap_in_it(self):
        """A gap has no observation behind it, so the outcome total exceeds
        the envelope count -- by exactly the number of declared absences."""
        ledger, _, stages = self._replay([f() for f in self.HOLE])
        self.assertTrue(ledger.reconciles)
        self.assertEqual(stages["detector outcomes"].total,
                         stages["provider observations fed"].total + 1)

    def test_the_absence_is_counted_in_the_bundle_report(self):
        _, report, _ = self._replay([f() for f in self.HOLE])
        self.assertEqual(report.snapshots_with_target, 2)
        self.assertEqual(len(report.snapshots_without_target), 1)
        self.assertIn("snapshot[1]", report.snapshots_without_target[0])

    def test_a_missing_snapshot_is_not_a_coverage_failure(self):
        """rule 27: the design exists to survive a briefly absent market.

        Failing the run on it would fail every real run for doing exactly
        what it was built to do. It is counted and rendered, not graded."""
        _, report, _ = self._replay([f() for f in self.HOLE])
        self.assertTrue(report.complete)
        self.assertEqual(report.coverage_failures(), [])
        self.assertIn("did NOT carry the target event", render_report(report))

    def test_the_returning_quote_rebaselines_and_the_next_move_triggers(self):
        """The acceptance criterion's second half: suppression must not be
        permanent. A hole costs the move that spans it, and nothing after."""
        ledger, _, stages = self._replay([
            odds_payload(at(200), 120, -140, at(201)),
            {"timestamp": iso(at(195)), "data": []},
            odds_payload(at(190), 160, -190, at(191)),   # re-baseline
            odds_payload(at(185), 260, -320, at(186)),   # a real move off it
        ])
        self.assertEqual(stages["moves detected"].total, 1)
        self.assertTrue(ledger.reconciles)

    def test_the_same_quotes_with_no_hole_do_trigger(self):
        """The control. Without it, a change that broke detection outright
        would pass every assertion above."""
        _, _, stages = self._replay([
            odds_payload(at(200), 120, -140, at(201)),
            odds_payload(at(190), 160, -190, at(191)),
        ])
        self.assertEqual(stages["moves detected"].total, 1)

    def test_a_snapshot_the_parser_could_not_read_is_also_a_hole(self):
        """An unreadable payload is BOTH a parse loss and a break in
        continuity. Recording only the first left the second invisible."""
        ledger, report, stages = self._replay([
            odds_payload(at(200), 120, -140, at(201)),
            {"timestamp": iso(at(195)), "data": "not a list"},
            odds_payload(at(190), 160, -190, at(191)),
        ])
        self.assertEqual(stages["moves detected"].total, 0)
        self.assertEqual(
            stages["detector outcomes"].breakdown.get("declared_gap"), 1)
        self.assertFalse(report.complete)          # the parse loss, separately
        self.assertTrue(ledger.reconciles)

    def test_an_undateable_hole_keeps_its_place_in_the_sequence(self):
        """A hole with no readable timestamp must not be sorted to the end.

        The first version of the fix ordered snapshots by
        `(snapshot is None, snapshot, index)`, which is not a neutral
        tie-break: it moved every undateable record PAST the quote that
        closes the delta across it, so the gap was declared after the move
        it existed to prevent and the trigger came back anyway. The fix was
        reintroducing its own bug through the sort meant to tidy it.
        """
        ledger, report, stages = self._replay([
            odds_payload(at(200), 120, -140, at(201)),
            {"timestamp": "not-a-time", "data": []},
            odds_payload(at(190), 160, -190, at(191)),
        ])
        self.assertEqual(stages["moves detected"].total, 0)
        self.assertEqual(
            stages["detector outcomes"].breakdown.get("declared_gap"), 1)
        self.assertTrue(ledger.reconciles)

    def test_another_games_absence_does_not_invalidate_this_games_stream(self):
        """Gaps are scoped to the game whose snapshots they came from.

        A provider response is per-sport, so one game's market vanishing says
        nothing about another's. Invalidating every stream on any absence
        would suppress real moves across a whole slate.
        """
        other_id, other_ticker = "evt-nfl-2", "KXNFLGAME-26SEP13SEASF"
        healthy = dict(_base_game())
        broken = dict(_base_game())
        broken.update({
            "provider_event_id": other_id,
            "event_ticker": other_ticker,
            "odds_snapshots": [
                odds_payload(at(200), 120, -140, at(201), event_id=other_id),
                {"timestamp": iso(at(195)), "data": []},
                odds_payload(at(190), 160, -190, at(191), event_id=other_id),
            ],
            "contracts": [
                {"market_ticker": f"{other_ticker}-SF", "yes_is_home": True,
                 "settled_yes": 1,
                 "candlesticks": [candle_payload(flat_candles())]},
            ],
        })
        games, _ = load_bundle(write(bundle(games=[healthy, broken])))
        ledger = replay(games)
        stages = {stage.stage: stage for stage in ledger.stages}
        # The healthy game's move survives; only the holed game loses its.
        self.assertEqual(stages["moves detected"].total, 1)
        self.assertEqual(
            stages["detector outcomes"].breakdown.get("declared_gap"), 1)
        self.assertTrue(ledger.reconciles)


class EmptyTargetCollectionTest(unittest.TestCase):
    """No target data is a COLLECTION FAILURE, never a quiet market.

    `load_bundle(bundle(odds_snapshots=[]))` parsed cleanly, reported
    `complete=True`, and replayed to zero quotes and zero moves -- which is
    byte-for-byte what a real slate with no qualifying move looks like. The
    study's whole coverage ledger exists to stop exactly that confusion, and
    the replay had a hole in it.

    Syntactic parse success and usable target coverage are different facts.
    """

    def _report(self, **overrides):
        _, report = load_bundle(write(bundle(**overrides)))
        return report

    def test_an_empty_snapshot_collection_fails_coverage(self):
        report = self._report(odds_snapshots=[])
        self.assertFalse(report.complete)
        self.assertTrue(any("NO usable sharp quote" in reason
                            for reason in report.coverage_failures()))

    def test_snapshots_that_never_carry_the_target_fail_coverage(self):
        """Well-formed, parseable, and empty of the thing being studied."""
        report = self._report(odds_snapshots=[
            {"timestamp": iso(at(200)), "data": []},
            {"timestamp": iso(at(195)), "data": []},
        ])
        self.assertFalse(report.complete)
        self.assertEqual(report.snapshots_with_target, 0)
        self.assertIn(EVENT, report.games_without_target_quotes)

    def test_a_contract_with_no_usable_candle_fails_coverage(self):
        """Every reaction on it would read BLIND_INTERVAL -- the same false
        quiet, one source over."""
        report = self._report(contracts=[
            {"market_ticker": f"{TICKER}-NYG", "yes_is_home": True,
             "settled_yes": 1, "candlesticks": []},
        ])
        self.assertFalse(report.complete)
        self.assertIn(f"{TICKER}-NYG", report.contracts_without_usable_candles)

    def test_a_healthy_bundle_reports_complete(self):
        """The control: none of the above may fire on a good bundle."""
        report = self._report()
        self.assertTrue(report.complete)
        self.assertEqual(report.coverage_failures(), [])


class EmptyTargetCollectionCliTest(_CliDriver, unittest.TestCase):
    """The exit code is the part a human or a CI job actually reads."""

    @staticmethod
    def _failure_block(text: str) -> str:
        """Only what the CLI itself said, after its own failure header.

        The bundle report prints the same reasons earlier in the run, so an
        assertion over the WHOLE output cannot tell whether the CLI named
        the failure or merely let the report do it. A first version of this
        test could not, and a mutation that deleted the CLI's own loop
        survived it.
        """
        _, marker, tail = text.partition("coverage is incomplete")
        return tail if marker else ""

    def test_an_empty_target_collection_exits_nonzero_and_names_itself(self):
        code, text = self._run(
            ["--replay", str(write(bundle(odds_snapshots=[])))])
        self.assertEqual(code, 1)
        block = self._failure_block(text)
        self.assertTrue(block, "the CLI printed no failure header")
        self.assertIn("NOT be read as a quiet market", block)
        self.assertIn("NO usable sharp quote", block)

    def test_a_contract_with_no_candles_exits_nonzero_and_names_itself(self):
        code, text = self._run(["--replay", str(write(bundle(contracts=[
            {"market_ticker": f"{TICKER}-NYG", "yes_is_home": True,
             "settled_yes": 1, "candlesticks": []}])))])
        self.assertEqual(code, 1)
        self.assertIn("NO usable exchange quote",
                      self._failure_block(text))

    def test_a_genuine_zero_trigger_run_still_exits_zero(self):
        """rule 27. A run with real observations and no qualifying move is a
        RESULT, and it must not be dragged into the failure bucket by the
        checks above -- that would burn the exit code a real defect needs."""
        code, text = self._run(["--replay", str(write(bundle(odds_snapshots=[
            odds_payload(at(200), 120, -140, at(201)),
            odds_payload(at(195), 120, -140, at(196)),   # unchanged: no move
        ])))])
        self.assertEqual(code, 0)
        self.assertIn("REACTION EPISODE LEDGER", text)

    def test_a_run_with_a_missing_snapshot_still_exits_zero(self):
        """The hole is a tolerated condition, so it may not fail the run."""
        code, _ = self._run(["--replay", str(write(bundle(odds_snapshots=[
            odds_payload(at(200), 120, -140, at(201)),
            {"timestamp": iso(at(195)), "data": []},
            odds_payload(at(190), 160, -190, at(191)),
        ])))])
        self.assertEqual(code, 0)


def responding_bundle(response_minutes: float, *, move_at: int = 195,
                      first: int = 210, last: int = 100) -> dict:
    """A bundle whose exchange responds `response_minutes` after the move.

    One contract, continuous 1-minute candles, a single detected book move.
    The exchange holds 0.56/0.58 and then steps to 0.70/0.72.
    """
    responds_at = move_at - response_minutes
    rows = []
    for minute in range(first, last - 1, -1):
        moved = minute <= responds_at
        bid, ask = (0.70, 0.72) if moved else (0.56, 0.58)
        rows.append((at(minute), bid, ask))
    return bundle(
        odds_snapshots=[odds_payload(at(200), 120, -140, at(201)),
                        odds_payload(at(move_at), 260, -320, at(move_at + 1))],
        contracts=[{"market_ticker": f"{TICKER}-NYG", "yes_is_home": True,
                    "settled_yes": 1,
                    "candlesticks": [candle_payload(rows)]}])


class UnusableCandleCoverageTest(unittest.TestCase):
    """A PARSED candle is not a USABLE exchange quote.

    The coverage check counted rows. A payload can carry well-formed
    candles -- timestamps, volume, trade prices -- with no bid or ask at
    all, and `usable_candle` (the measurement's own predicate, which needs a
    two-sided quote to form a mid) rejects every one. 102 candles parsed,
    zero usable, `complete=True`, and every reaction came back
    `no_exchange_baseline`, which reads as an exchange that did not move.
    """

    @staticmethod
    def _quoteless() -> dict:
        payload = copy.deepcopy(bundle())
        for contract in payload["games"][0]["contracts"]:
            for body in contract["candlesticks"]:
                for row in body["candlesticks"]:
                    row.pop("yes_bid", None)
                    row.pop("yes_ask", None)
        return payload

    def test_candles_that_parse_but_carry_no_quote_fail_coverage(self):
        _, report = load_bundle(write(self._quoteless()))
        self.assertGreater(report.candles, 0, "the fixture parsed nothing")
        self.assertEqual(report.usable_candles, 0)
        self.assertFalse(report.complete)
        self.assertTrue(any("NO usable exchange quote" in reason
                            for reason in report.coverage_failures()))

    def test_the_usability_verdict_is_the_measurements_own(self):
        """rule 26: take the predicate the measurement uses, never a second
        reading of what a good candle looks like."""
        games, report = load_bundle(write(bundle()))
        counted = sum(1 for contract in games[0].contracts
                      for candle in contract.candles
                      if usable_candle(candle))
        self.assertEqual(report.usable_candles, counted)
        self.assertTrue(report.complete)

    def test_the_cli_exits_nonzero_and_names_it(self):
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(
                ["--replay", str(write(self._quoteless()))])
        text = "\n".join(chunks)
        self.assertEqual(code, 1)
        _, _, tail = text.partition("coverage is incomplete")
        self.assertIn("NO usable exchange quote", tail)


class FeasibilityReachableEndToEndTest(unittest.TestCase):
    """The declared rule must be satisfiable by an actual replay.

    The rule it replaces required a lag interval starting beyond 1,800s,
    while the policy's ceiling is `max_wait - candle_period` = 1,740s. These
    drive whole bundles so the claim is about the chain, not the arithmetic.
    """

    def _verdict(self, ledger):
        return ledger.feasibility

    def test_a_response_inside_the_window_orders_book_first(self):
        """The positive case: a real book-led reaction, end to end."""
        games, report = load_bundle(write(responding_bundle(5)))
        ledger = replay(games)
        self.assertTrue(report.complete)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["reactions measured"].breakdown,
                         {"responded": 1})
        verdict = self._verdict(ledger)
        self.assertEqual(verdict.book_led, 1)
        self.assertEqual(verdict.determinate, 1)

    def test_the_ceiling_is_what_the_policy_says_it_is(self):
        """A response AT the deadline reports `max_wait - candle_period`,
        which is the number the old rule sat 60s above."""
        from reaction.clocks import envelope_for_sharp_quote
        from reaction.detector import MoveDetector
        from reaction.measure import ReactionPolicy, measure_reaction
        policy = ReactionPolicy()
        games, _ = load_bundle(write(responding_bundle(30)))
        game = games[0]
        # The same two steps replay takes, so the number under test is the
        # one a run would report rather than a re-derivation of it.
        detector = MoveDetector()
        triggers = [t for t in
                    (detector.observe(envelope_for_sharp_quote(q))
                     for q in game.quotes) if t is not None]
        self.assertEqual(len(triggers), 1)
        reaction = measure_reaction(
            triggers[0], game.contracts[0].candles,
            market_ticker=game.contracts[0].market_ticker,
            yes_is_home=game.contracts[0].yes_is_home, policy=policy)
        self.assertEqual(reaction.outcome.value, "responded")
        self.assertEqual(reaction.lag_earliest_seconds,
                         policy.max_reportable_lag_seconds)
        self.assertLess(reaction.lag_earliest_seconds, 1800.0)

    def test_a_response_past_the_window_is_censored_not_absent(self):
        """The negative case, and it is NOT `no reaction happened`."""
        games, _ = load_bundle(write(responding_bundle(45)))
        ledger = replay(games)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["reactions measured"].breakdown,
                         {"no_response_in_window": 1})
        verdict = self._verdict(ledger)
        self.assertEqual(verdict.determinate, 0)
        self.assertEqual(
            verdict.contract_rows_by_outcome["no_response_in_window"], 1)
        self.assertEqual(verdict.verdict.value,
                         "insufficient_observable_events")

    def test_widening_max_wait_recovers_the_censored_response(self):
        """Censoring is a property of the declared window, not the market --
        so the CLI has to expose it, and it does."""
        from reaction.measure import ReactionPolicy
        games, _ = load_bundle(write(responding_bundle(45)))
        wide = replay(games, reaction_policy=ReactionPolicy(
            max_wait=timedelta(hours=1)))
        stages = {s.stage: s for s in wide.stages}
        self.assertEqual(stages["reactions measured"].breakdown,
                         {"responded": 1})
        self.assertEqual(wide.feasibility.book_led, 1)

    def test_the_unreachable_rule_is_refused_rather_than_returning_zero(self):
        """The exact rule that shipped, through the whole chain."""
        from reaction.episodes import FeasibilityRule
        games, _ = load_bundle(write(responding_bundle(5)))
        ledger = replay(games,
                        feasibility_rule=FeasibilityRule(
                            min_lag_seconds=1800.0))
        self.assertEqual(ledger.feasibility.verdict.value,
                         "rule_unreachable_against_policy")
        self.assertIn("1,740s", ledger.feasibility.detail)


class MaxWaitCliTest(_CliDriver, unittest.TestCase):
    """`--max-wait` exists because a declared horizon must be reachable."""

    def test_the_flag_changes_the_censoring_horizon(self):
        path = str(write(responding_bundle(45)))
        censored, text = self._run(["--replay", path])
        self.assertEqual(censored, 0)
        self.assertIn("no_response_in_window", text)
        code, widened = self._run(["--replay", path, "--max-wait", "3600"])
        self.assertEqual(code, 0)
        self.assertIn("responded", widened)

    def test_the_verdict_is_printed_with_what_it_excluded(self):
        _, text = self._run(["--replay", str(write(responding_bundle(45)))])
        self.assertIn("FEASIBILITY RULE", text)
        self.assertIn("declared before any data", text)
        self.assertIn("never folded in", text)
        self.assertIn("insufficient_observable_events", text)


def mirrored_bundle(response_minutes: float = 5, move_at: int = 195) -> dict:
    """One book move, BOTH contracts, each showing the mirrored response."""
    responds_at = move_at - response_minutes
    home, away = [], []
    for minute in range(210, 99, -1):
        moved = minute <= responds_at
        home.append((at(minute), 0.70, 0.72) if moved
                    else (at(minute), 0.56, 0.58))
        away.append((at(minute), 0.28, 0.30) if moved
                    else (at(minute), 0.42, 0.44))
    return bundle(
        odds_snapshots=[odds_payload(at(200), 120, -140, at(201)),
                        odds_payload(at(move_at), 260, -320,
                                     at(move_at + 1))],
        contracts=[{"market_ticker": f"{TICKER}-NYG", "yes_is_home": True,
                    "settled_yes": 1, "candlesticks": [candle_payload(home)]},
                   {"market_ticker": f"{TICKER}-DAL", "yes_is_home": False,
                    "settled_yes": 0,
                    "candlesticks": [candle_payload(away)]}])


class FeasibilityUnitEndToEndTest(unittest.TestCase):
    """The verdict's unit, through a whole replay rather than a hand-built
    list -- because the unit error lived in how replay's rows were counted."""

    def test_one_move_measured_on_both_contracts_orders_once(self):
        games, _ = load_bundle(write(mirrored_bundle()))
        ledger = replay(games)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["moves detected"].total, 1)
        self.assertEqual(stages["reactions measured"].total, 2)
        self.assertEqual(ledger.feasibility.book_moves, 1)
        self.assertEqual(ledger.feasibility.determinate, 1)

    def test_one_kalshi_step_after_two_book_moves_counts_once(self):
        """Default max_wait (30 min) over moves 5 min apart: the windows
        overlap and both moves claim the same exchange step."""
        rows = [(at(m), 0.70, 0.72) if m <= 180 else (at(m), 0.56, 0.58)
                for m in range(210, 99, -1)]
        games, _ = load_bundle(write(bundle(
            odds_snapshots=[odds_payload(at(200), 120, -140, at(201)),
                            odds_payload(at(195), 160, -190, at(196)),
                            odds_payload(at(190), 260, -320, at(191))],
            contracts=[{"market_ticker": f"{TICKER}-NYG",
                        "yes_is_home": True, "settled_yes": 1,
                        "candlesticks": [candle_payload(rows)]}])))
        ledger = replay(games)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["moves detected"].total, 2)
        self.assertEqual(stages["reactions measured"].breakdown,
                         {"responded": 2})
        self.assertEqual(ledger.feasibility.book_led, 1)
        self.assertEqual(ledger.feasibility.shared_response, 1)


class AbsenceLabelTest(unittest.TestCase):
    """A reason is a claim, and it must not name a cause it cannot see."""

    def test_a_missing_book_is_not_reported_as_a_missing_event(self):
        snap = odds_payload(at(195), 160, -190, at(196))
        snap["data"][0]["bookmakers"][0]["key"] = "draftkings"
        games, _ = load_bundle(write(bundle(odds_snapshots=[
            odds_payload(at(200), 120, -140, at(201)), snap])))
        absences = [o.absence for o in games[0].snapshots if o.absence]
        self.assertEqual(len(absences), 1)
        self.assertNotIn("target event is absent", absences[0])
        self.assertIn("sharp book", absences[0])

    def test_it_is_still_a_hole(self):
        """The label changed; the behaviour must not have."""
        snap = odds_payload(at(195), 160, -190, at(196))
        snap["data"][0]["bookmakers"][0]["key"] = "draftkings"
        games, _ = load_bundle(write(bundle(odds_snapshots=[
            odds_payload(at(200), 120, -140, at(201)), snap,
            odds_payload(at(190), 260, -320, at(191))])))
        stages = {s.stage: s for s in replay(games).stages}
        self.assertEqual(stages["moves detected"].total, 0)
        self.assertEqual(
            stages["detector outcomes"].breakdown.get("declared_gap"), 1)


class FeasibilityCliTest(_CliDriver, unittest.TestCase):
    """What the exit code says about a verdict, and about a bad flag."""

    def _exit(self, argv):
        """argparse reports usage errors by raising SystemExit."""
        from unittest import mock
        import run_reaction
        with mock.patch("sys.stderr"):
            try:
                return run_reaction.main(argv)
            except SystemExit as stop:
                return stop.code

    def test_a_bad_max_wait_is_a_usage_error_not_a_data_defect(self):
        """Exit 1 means a defect in the DATA. A typo is not one, and a
        traceback reporting it as one sends someone hunting a bad bundle."""
        path = str(write(bundle()))
        for bad in ("-5", "0", "nan", "inf", "soon"):
            with self.subTest(value=bad):
                self.assertEqual(
                    self._exit(["--replay", path, "--max-wait", bad]), 2)

    def test_an_unreachable_rule_exits_one(self):
        """`stop` and `insufficient` are results (exit 0); a rule nothing
        could satisfy is a defect -- the decision was never being made."""
        from unittest import mock
        import reaction.episodes as episodes
        real = episodes.judge_feasibility
        with mock.patch.object(
                episodes, "judge_feasibility",
                side_effect=lambda rows, rule, policy: real(
                    rows, episodes.FeasibilityRule(min_lag_seconds=1800.0),
                    policy)):
            code, text = self._run(["--replay", str(write(bundle()))])
        self.assertEqual(code, 1)
        self.assertIn("cannot be satisfied", text)

    def test_an_insufficient_verdict_still_exits_zero(self):
        code, text = self._run(["--replay", str(write(bundle()))])
        self.assertEqual(code, 0)
        self.assertIn("insufficient_observable_events", text)


def after_kickoff(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


class InPlayExclusionTest(unittest.TestCase):
    """A snapshot after kickoff is IN-PLAY, and this is a PREGAME study.

    The parser keeps every event in a response, games under way included, and
    nothing downstream asked whether a quote was pregame. A score-driven swing
    after kickoff was detected as a book move, measured against the
    exchange's own in-play repricing, and entered the feasibility verdict --
    with coverage reported clean.
    """

    INPLAY = [
        lambda: odds_payload(at(20), 120, -140, at(21)),
        lambda: odds_payload(at(10), 120, -140, at(11)),
        lambda: odds_payload(after_kickoff(10), 400, -600, after_kickoff(9)),
        lambda: odds_payload(after_kickoff(15), 900, -2000, after_kickoff(14)),
    ]

    def _run(self, snapshots):
        games, report = load_bundle(write(bundle(odds_snapshots=snapshots)))
        ledger = replay(games)
        return ledger, report, {s.stage: s for s in ledger.stages}

    def test_a_swing_after_kickoff_is_never_detected(self):
        ledger, report, stages = self._run([f() for f in self.INPLAY])
        self.assertEqual(stages["moves detected"].total, 0)
        self.assertEqual(ledger.feasibility.book_moves, 0)
        self.assertEqual(report.in_play_excluded, 2)

    def test_the_same_swing_before_kickoff_is_detected(self):
        """The control: the exclusion must be about TIME, not about size."""
        _, _, stages = self._run([
            odds_payload(at(20), 120, -140, at(21)),
            odds_payload(at(10), 400, -600, at(11))])
        self.assertEqual(stages["moves detected"].total, 1)

    def test_the_kickoff_instant_itself_is_the_closing_pregame_sample(self):
        """Matching the manifest's window, closed at both ends."""
        _, report, stages = self._run([
            odds_payload(at(10), 120, -140, at(11)),
            odds_payload(START, 400, -600, at(1))])
        self.assertEqual(report.in_play_excluded, 0)
        self.assertEqual(stages["moves detected"].total, 1)

    def test_an_exclusion_is_counted_and_rendered_not_graded(self):
        """In-play data is expected in any real pull; it is not a loss."""
        _, report, _ = self._run([f() for f in self.INPLAY])
        self.assertTrue(report.complete)
        self.assertIn("in-play pairs excluded", render_report(report))
        self.assertEqual(report.as_dict()["in_play_pairs_excluded"], 2)


def slate_payload(snapshot: datetime, events: list[tuple]) -> dict:
    """One sport-wide snapshot: every listed game in a single response.

    `events` is (event_id, away_price, home_price, observed, commence).
    """
    data = []
    for event_id, away, home, observed, commence in events:
        body = odds_payload(snapshot, away, home, observed, event_id=event_id)
        body["data"][0]["commence_time"] = iso(commence)
        data.extend(body["data"])
    return {"timestamp": iso(snapshot), "data": data}


LATE_EVENT, LATE_TICKER = "evt-nfl-late", "KXNFLGAME-26SEP13LVDEN"
LATE_START = START + timedelta(hours=3, minutes=25)


def pooled_bundle(pool: list, *, games=None) -> dict:
    """A v2 bundle: one root pool, two games in different kickoff clusters."""
    early = _base_game()
    early.pop("odds_snapshots")
    late = _base_game()
    late.pop("odds_snapshots")
    late.update({"provider_event_id": LATE_EVENT, "event_ticker": LATE_TICKER,
                 "start": iso(LATE_START)})
    for contract in late["contracts"]:
        contract["market_ticker"] = contract["market_ticker"].replace(
            TICKER, LATE_TICKER)
    return {"schema": BUNDLE_SCHEMA_POOLED, "odds_snapshots": pool,
            "games": games if games is not None else [early, late]}


class PooledBundleTest(unittest.TestCase):
    """v2: one shared pool, each game windowed to its own pregame span.

    A snapshot is sport-wide, so v1's per-game lists stored each payload
    once per game -- ~90 MB of duplicated JSON for a 5-minute NFL Sunday. And
    a shared pool is only safe with the in-play exclusion: after the early
    kickoff, every snapshot carries the early game IN PLAY.
    """

    def _pool(self):
        """Early game quiet until kickoff then swinging in play; late game
        quiet throughout. Five instants spanning both kickoffs."""
        instants = [START - timedelta(minutes=20), START - timedelta(minutes=10),
                    START + timedelta(minutes=30), START + timedelta(hours=2),
                    LATE_START]
        early_prices = [(120, -140), (120, -140), (400, -600), (900, -2000),
                        (900, -2000)]
        pool = []
        for moment, (away, home) in zip(instants, early_prices):
            events = [(EVENT, away, home, moment - timedelta(minutes=1), START),
                      (LATE_EVENT, 120, -140, moment - timedelta(minutes=1),
                       LATE_START)]
            pool.append(slate_payload(moment, events))
        return pool

    def test_a_shared_payload_is_counted_once_however_many_games_read_it(self):
        _, report = load_bundle(write(pooled_bundle(self._pool())))
        self.assertEqual(report.snapshot_payloads, 5)
        self.assertEqual(report.snapshots_parsed, 5)
        self.assertEqual(report.games, 2)

    def test_each_game_sees_only_its_own_pregame_window(self):
        games, report = load_bundle(write(pooled_bundle(self._pool())))
        by_id = {g.provider_event_id: g for g in games}
        self.assertEqual(len(by_id[EVENT].quotes), 2)       # before 00:20
        self.assertEqual(len(by_id[LATE_EVENT].quotes), 5)  # all pregame
        self.assertEqual(report.in_play_excluded, 3)

    def test_an_early_games_in_play_swing_cannot_trigger_from_the_pool(self):
        games, _ = load_bundle(write(pooled_bundle(self._pool())))
        ledger = replay(games)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["moves detected"].total, 0)

    def test_observe_from_bounds_the_window_from_below(self):
        payload = pooled_bundle(self._pool())
        payload["games"][1]["observe_from"] = iso(START + timedelta(hours=1))
        games, report = load_bundle(write(payload))
        late = [g for g in games if g.provider_event_id == LATE_EVENT][0]
        self.assertEqual(len(late.quotes), 2)
        self.assertEqual(report.before_window_excluded, 3)

    def test_a_game_may_not_bring_its_own_odds_into_a_pooled_bundle(self):
        payload = pooled_bundle(self._pool())
        payload["games"][0]["odds_snapshots"] = []
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(payload))
        self.assertIn("may not carry their own", str(caught.exception))

    def test_a_pooled_bundle_without_a_pool_is_refused(self):
        payload = pooled_bundle(self._pool())
        del payload["odds_snapshots"]
        with self.assertRaises(BundleError):
            load_bundle(write(payload))

    def test_a_window_that_opens_after_kickoff_is_refused(self):
        payload = pooled_bundle(self._pool())
        payload["games"][0]["observe_from"] = iso(START + timedelta(hours=1))
        with self.assertRaises(BundleError) as caught:
            load_bundle(write(payload))
        self.assertIn("no pregame window", str(caught.exception))

    def test_a_pooled_bundle_replays_through_the_cli(self):
        from unittest import mock
        import run_reaction
        with mock.patch("builtins.print"):
            code = run_reaction.main(
                ["--replay", str(write(pooled_bundle(self._pool())))])
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
