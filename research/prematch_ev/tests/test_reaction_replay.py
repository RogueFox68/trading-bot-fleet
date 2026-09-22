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

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaction.episodes import DataRole, HoldoutViolation            # noqa: E402
from reaction.replay import (                                       # noqa: E402
    BUNDLE_SCHEMA, BundleError, load_bundle, render_report, replay,
    replay_file,
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


def bundle(*, games=None, schema=BUNDLE_SCHEMA, **game_overrides) -> dict:
    """A minimal valid bundle: one game, two contracts, one detected move."""
    if games is None:
        game = {
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
        self.assertIn("*** PARSE INCOMPLETE", render_report(report))
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
        self.assertNotIn("*** PARSE INCOMPLETE", render_report(report))


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


class ReplayCliTest(unittest.TestCase):
    """Drive the real entry point.

    Round 5 shipped two crashes in entry points nothing drove, while 171
    tests passed. An exit code nothing exercises is a decoration.
    """

    def _run(self, argv):
        from unittest import mock
        import run_reaction
        chunks: list[str] = []
        with mock.patch("builtins.print", side_effect=lambda *a, **k:
                        chunks.append(" ".join(str(x) for x in a))):
            code = run_reaction.main(argv)
        return code, "\n".join(chunks)

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
        self.assertIn("PARSE INCOMPLETE", text)

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
        self.assertTrue(payload["bundle"]["parse_complete"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
