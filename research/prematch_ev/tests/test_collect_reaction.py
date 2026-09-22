"""The reaction collector, driven end to end against a fake network.

WHAT IS FAKE AND WHAT IS REAL
-----------------------------
Only the network. Every request goes through the REAL fetchers --
`odds_history.fetch_snapshot` with its per-attempt credit reservation, retries
and raw cache, `kalshi_history`'s enumeration and candle routing, the ESPN
adapter -- and then through the collector's endpoint gate, and reaches a
`urlopen` that answers from recorded shapes instead of a socket.

The SHAPES are observed, not composed: the scoreboard event is the owner's
2026-09-21 fetch (`tests.test_schedule.OBSERVED_DAL_NYG`), the settled market
is `nfl_market` with the sampled KC contract's close/settlement pattern, the
odds snapshot and candle bodies are the replay suite's observed wire formats
(fixed-point STRING prices, `end_period_ts` as the candle's close). Only the
VALUES -- prices, the minute the book moves, the minute Kalshi follows -- are
chosen, because they are the controlled input. The two contracts of the game
mirror each other, as the two sides of one market do.

THE CLAIMS UNDER TEST
---------------------
It spends nothing unless told to, and never more than the manifest prices. A
re-run pays only for what it has not got. The bundle it writes is one the
replay accepts and reads correctly. The key reaches no file and no output. A
request outside the allow-list stops the run. A snapshot that never arrived is
a counted hole, not a silently shorter pool -- and a RUN of them stops the
purchase and says why. A candle response that stops short is completed, not
trusted.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import collect_reaction                                             # noqa: E402
from data.kalshi_history import parse_candles                      # noqa: E402
from reaction.capture import CaptureRefused, endpoint_allowed      # noqa: E402
from reaction.measure import ReactionPolicy                        # noqa: E402
from reaction.replay import load_bundle, replay                    # noqa: E402
from tests.test_reaction_replay import (                           # noqa: E402
    EVENT, START, candle_payload, odds_payload,
)
from tests.test_schedule import OBSERVED_DAL_NYG, board, nfl_market  # noqa: E402

UTC = timezone.utc
DAY = "2026-09-13"                       # the ticker day of KXNFLGAME-26SEP13DALNYG
KICKOFF = START                          # 2026-09-14T00:20Z, as ESPN reports it
MOVE_AT = KICKOFF - timedelta(minutes=30)      # the book moves at 23:50Z
FOLLOW_AT = KICKOFF - timedelta(minutes=22)    # Kalshi follows at 23:58Z
NOW = datetime(2026, 9, 21, tzinfo=UTC)
KEY = "SECRET-KEY-VALUE"
EVENT_TICKER = "KXNFLGAME-26SEP13DALNYG"
NYG, DAL = f"{EVENT_TICKER}-NYG", f"{EVENT_TICKER}-DAL"

# 23:20..00:20 at 5 minutes is 13 instants; a 10% reserve adds 2.
PRICE = 150

# The candle span: the window opens at 23:20, and the measurement's lookback
# plus one candle period reaches 31 minutes before that. 22:49..00:20 closes
# 92 one-minute candles.
CANDLES_FROM = datetime(2026, 9, 13, 22, 49, tzinfo=UTC)
CANDLE_MINUTES = 92


def _markets() -> list[dict]:
    home = nfl_market(EVENT_TICKER, "NYG", KICKOFF)
    away = nfl_market(EVENT_TICKER, "DAL", KICKOFF)
    away["result"] = "no"                # one side of a game settles NO
    return [home, away]


class Response:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8")
        self.status = 200
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeNetwork:
    """Answers each declared endpoint from recorded shapes, and counts.

    `odds_failures` maps an instant to how many of its attempts fail before
    one succeeds; `odds_status` makes every odds request fail with that HTTP
    status; `commence_offset` moves the provider's kickoff off ESPN's.
    `candle_cap` returns at most that many candles per response, oldest
    first -- a truncating endpoint that says nothing about it.
    """

    def __init__(self, *, odds_failures=None, odds_status=None,
                 candle_cap=None, commence_offset=timedelta(0)):
        self.odds_calls: list[datetime] = []
        self.candle_calls: list[tuple[str, datetime, datetime]] = []
        self.urls: list[str] = []
        self.used = 0
        self.odds_failures = dict(odds_failures or {})
        self.odds_status = odds_status
        self.candle_cap = candle_cap
        self.commence_offset = commence_offset

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        parts = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parts.query)
        host, path = parts.netloc, parts.path
        if host == "site.api.espn.com":
            return Response(board(OBSERVED_DAL_NYG)
                            if query["dates"] == ["20260913"] else board())
        if host == "api.elections.kalshi.com":
            if path.endswith("/historical/cutoff"):
                return Response({"market_settled_ts": "2026-06-01T00:00:00Z"})
            if path.endswith("/candlesticks"):
                return Response(self.candles(path.split("/")[-2], query))
            if path.endswith("/historical/markets"):
                return Response({"markets": [], "cursor": ""})
            if path.endswith("/markets"):
                return Response({"markets": _markets(), "cursor": ""})
        if host == "api.the-odds-api.com":
            return self._odds(url, query)
        raise AssertionError(f"the fake network has no answer for {url}")

    def _odds(self, url, query):
        at = datetime.fromisoformat(query["date"][0].replace("Z", "+00:00"))
        self.odds_calls.append(at)
        if self.odds_status is not None:
            raise urllib.error.HTTPError(url, self.odds_status, "Unauthorized",
                                         {}, None)
        if self.odds_failures.get(at, 0) > 0:
            self.odds_failures[at] -= 1
            raise OSError("connection reset")
        self.used += 10
        moved = at >= MOVE_AT
        body = odds_payload(at, 260 if moved else 120, -320 if moved else -140,
                            at - timedelta(minutes=1), event_id=EVENT)
        body["data"][0]["commence_time"] = (
            (KICKOFF + self.commence_offset).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return Response(body, {"x-requests-used": str(self.used),
                               "x-requests-remaining": str(20000 - self.used)})

    def candles(self, ticker, query):
        """One candle per minute closing inside [start_ts, end_ts]. NYG's
        quote steps up when Kalshi follows; DAL's is its mirror image."""
        since = datetime.fromtimestamp(int(query["start_ts"][0]), tz=UTC)
        until = datetime.fromtimestamp(int(query["end_ts"][0]), tz=UTC)
        self.candle_calls.append((ticker, since, until))
        rows = []
        minute = since.replace(second=0) + (timedelta(minutes=1)
                                            if since.second else timedelta(0))
        while minute <= until:
            bid, ask = (0.70, 0.72) if minute >= FOLLOW_AT else (0.56, 0.58)
            if ticker == DAL:
                bid, ask = round(1 - ask, 2), round(1 - bid, 2)
            rows.append((minute, bid, ask))
            minute += timedelta(minutes=1)
        if self.candle_cap is not None:
            rows = rows[:self.candle_cap]
        return candle_payload(rows)


class CollectorHarness(unittest.TestCase):
    """Run `collect_reaction.main` in-process with the fake network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "odds"
        self.net = FakeNetwork()

    def run_collector(self, *extra, net=None, now=NOW, cache=None):
        net = net or self.net
        argv = ["--day", DAY, "--lead-hours", "1", "--cadence-minutes", "5",
                "--cache-dir", str(cache or self.cache),
                "--schedule-cache", "", *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                mock.patch("time.sleep"), \
                redirect_stdout(out), redirect_stderr(err):
            code = collect_reaction.main(argv, now=now)
        return code, out.getvalue() + err.getvalue()

    def collect(self, *extra, net=None, spend=PRICE):
        """A paid run into a fresh bundle path."""
        out = self.tmp / "bundle.json"
        code, text = self.run_collector("--spend", str(spend), "--api-key",
                                        KEY, "--out", str(out), *extra,
                                        net=net)
        return code, text, out


class PlanFirstTest(CollectorHarness):
    """The default is a plan. Money moves only on an explicit confirmation."""

    def test_without_spend_nothing_is_bought(self):
        code, text = self.run_collector()
        self.assertEqual(code, 0)
        self.assertEqual(self.net.odds_calls, [])
        self.assertIn("Nothing was spent", text)
        self.assertIn(f"--spend {PRICE}", text)

    def test_a_spend_below_the_price_is_refused_before_any_purchase(self):
        code, text = self.run_collector("--spend", str(PRICE - 1),
                                        "--api-key", KEY)
        self.assertEqual(code, 2)
        self.assertEqual(self.net.odds_calls, [])
        self.assertIn(f"below the {PRICE}", text)

    def test_a_window_reaching_past_now_is_refused_before_any_purchase(self):
        """The archive cannot hold a snapshot that has not happened -- and the
        dangerous window is the one that STRADDLES now, whose first instants
        would buy perfectly well."""
        code, text = self.run_collector(
            "--spend", str(PRICE), "--api-key", KEY,
            now=KICKOFF - timedelta(minutes=30))
        self.assertEqual(code, 2)
        self.assertEqual(self.net.odds_calls, [])
        self.assertIn("have not happened", text)

    def test_a_purchase_without_a_key_is_refused(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            code, _ = self.run_collector("--spend", str(PRICE))
        self.assertEqual(code, 2)
        self.assertEqual(self.net.odds_calls, [])

    def test_an_incomplete_slate_buys_nothing(self):
        """The exchange's archive partition does not answer: the listing may
        be missing games, so the manifest may be the wrong one."""
        net = FakeNetwork()
        answer = net.__call__

        def archive_down(request, *args, **kwargs):
            url = getattr(request, "full_url", request)
            if "/historical/markets?" in url:
                net.urls.append(url)
                raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
            return answer(request, *args, **kwargs)

        code, text = self.run_collector("--spend", str(PRICE), "--api-key",
                                        KEY, net=archive_down)
        self.assertEqual(code, 1)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("slate retrieval incomplete", text)
        self.assertIn("Nothing was bought", text)
        code, _ = self.run_collector(net=archive_down)       # plan mode too
        self.assertEqual(code, 1)

    def test_a_day_with_nothing_settled_says_why(self):
        code, text = self.run_collector("--day", "2026-09-27")
        self.assertEqual(code, 1)
        self.assertIn("listed once SETTLED", text)

    def test_a_cadence_finer_than_the_archive_grid_is_refused(self):
        """Instants between the archive's own snapshots buy duplicates."""
        code, text = self.run_collector("--cadence-minutes", "1")
        self.assertEqual(code, 2)
        self.assertIn("DUPLICATE", text)


class FullRunTest(CollectorHarness):
    """A bundle the replay accepts, reads correctly, and measures."""

    def test_the_bundle_round_trips_through_the_replay(self):
        code, text, out = self.collect()
        self.assertEqual(code, 0, text)
        games, report = load_bundle(out)
        self.assertTrue(report.complete, report.coverage_failures())
        self.assertEqual(report.snapshot_payloads, 13)
        self.assertEqual(len(games), 1)
        game = games[0]
        self.assertEqual(game.provider_event_id, EVENT)
        self.assertEqual(game.start, KICKOFF)
        self.assertEqual(game.start_source, "external_schedule")
        self.assertFalse(game.point_in_time)

    def test_orientation_is_the_shared_yes_side(self):
        """NYG is home in both the scoreboard and the odds event."""
        _, _, out = self.collect()
        contracts = {c.market_ticker: c
                     for c in load_bundle(out)[0][0].contracts}
        self.assertTrue(contracts[NYG].yes_is_home)
        self.assertFalse(contracts[DAL].yes_is_home)
        self.assertEqual(contracts[NYG].settled_yes, 1)
        self.assertEqual(contracts[DAL].settled_yes, 0)

    def test_the_replay_finds_the_move_and_the_book_leading(self):
        """One book move, followed on both contracts: ONE book-led move."""
        _, _, out = self.collect()
        games, _ = load_bundle(out)
        ledger = replay(games)
        stages = {s.stage: s for s in ledger.stages}
        self.assertEqual(stages["moves detected"].total, 1)
        self.assertEqual(ledger.feasibility.book_led, 1)
        self.assertEqual(
            ledger.feasibility.contract_rows_by_outcome, {"responded": 2})

    def test_the_recommended_horizon_keeps_a_slow_follower(self):
        """Kalshi follows eight minutes after the 5-minute poller sees the
        move. One cadence interval as the horizon -- the rule when the grid
        was 30 minutes -- would right-censor exactly that follower."""
        code, text, out = self.collect()
        self.assertIn("--max-wait 1800", text)
        games, _ = load_bundle(out)
        kept = replay(games, reaction_policy=ReactionPolicy(
            max_wait=timedelta(seconds=1800)))
        self.assertEqual(kept.feasibility.book_led, 1)
        censored = replay(games, reaction_policy=ReactionPolicy(
            max_wait=timedelta(seconds=300)))
        self.assertEqual(censored.feasibility.book_led, 0)
        self.assertEqual(censored.feasibility.contract_rows_by_outcome,
                         {"no_response_in_window": 2})

    def test_the_window_opens_where_the_manifest_opened_it(self):
        _, _, out = self.collect()
        raw = json.loads(out.read_text())
        self.assertEqual(raw["games"][0]["observe_from"],
                         "2026-09-13T23:20:00Z")
        self.assertEqual(raw["collection"]["manifest"]["requests"], 13)

    def test_it_spends_exactly_what_it_bought(self):
        self.collect()
        self.assertEqual(len(self.net.odds_calls), 13)
        self.assertEqual(self.net.used, 130)

    def test_a_rerun_reads_the_cache_and_buys_nothing(self):
        self.collect()
        second = FakeNetwork()
        code, text = self.run_collector("--spend", "0", "--api-key", KEY,
                                        "--out", str(self.tmp / "again.json"),
                                        net=second)
        self.assertEqual(code, 0, text)
        self.assertEqual(second.odds_calls, [])
        self.assertRegex(text, r"already cached\s+13\s")
        self.assertRegex(text, r"THIS RUN MAY SPEND\s+0 credits")

    def test_the_key_reaches_no_file_and_no_output(self):
        code, text, out = self.collect()
        self.assertEqual(code, 0, text)
        self.assertNotIn(KEY, text)
        self.assertNotIn(KEY, out.read_text())
        files = [p for p in self.cache.rglob("*") if p.is_file()]
        self.assertTrue(files)
        for path in self.cache.rglob("*"):
            self.assertNotIn(KEY, path.name)
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())


class CandleChunkTest(CollectorHarness):
    """A minute stored twice is counted twice; a minute never fetched is a
    hole the measurement reports as blindness, not as a truncation."""

    def minutes(self, out, ticker):
        raw = json.loads(out.read_text())
        rows = [c for c in raw["games"][0]["contracts"]
                if c["market_ticker"] == ticker]
        return [candle.ts for body in rows[0]["candlesticks"]
                for candle in parse_candles(body)[0]]

    def calls_for(self, net, ticker):
        return [(a, b) for t, a, b in net.candle_calls if t == ticker]

    def test_chunks_tile_the_span_without_overlap(self):
        with mock.patch.object(collect_reaction, "CANDLE_CHUNK",
                               timedelta(minutes=20)):
            code, text, out = self.collect()
        self.assertEqual(code, 0, text)
        for ticker in (NYG, DAL):
            spans = self.calls_for(self.net, ticker)
            self.assertGreater(len(spans), 1)
            self.assertEqual(spans[0][0], CANDLES_FROM)
            self.assertEqual(spans[-1][1], KICKOFF)
            # Boundaries sit on since + k * chunk, not drifting a second a
            # chunk, and every-minute candles need no follow-up request.
            self.assertEqual(
                [end for _, end in spans[:-1]],
                [CANDLES_FROM + timedelta(minutes=20 * k)
                 for k in range(1, len(spans))])
            for (_, end), (start, _) in zip(spans, spans[1:]):
                self.assertEqual(start, end + timedelta(seconds=1))
            minutes = self.minutes(out, ticker)
            self.assertEqual(len(minutes), len(set(minutes)))
            self.assertEqual(len(minutes), CANDLE_MINUTES)

    def test_a_response_that_stops_short_is_completed(self):
        """Seven candles per response, oldest first, and nothing to say so."""
        net = FakeNetwork(candle_cap=7)
        with mock.patch.object(collect_reaction, "MAX_CONTINUATIONS", 20):
            code, text, out = self.collect(net=net)
        self.assertEqual(code, 0, text)
        for ticker in (NYG, DAL):
            minutes = self.minutes(out, ticker)
            self.assertEqual(len(minutes), len(set(minutes)))
            self.assertEqual(len(minutes), CANDLE_MINUTES)
        raw = json.loads(out.read_text())
        # 92 candles at 7 per response is 14 responses per contract, and the
        # last 13 of them each prove the one before stopped short.
        self.assertEqual(raw["collection"]["candle_responses_cut_short"], 26)
        self.assertIn("stopped short", text)
        games, _ = load_bundle(out)
        self.assertEqual(replay(games).feasibility.book_led, 1)

    def test_a_span_that_never_finishes_is_incomplete_not_assumed(self):
        net = FakeNetwork(candle_cap=1)
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertIn("not known to be complete", text)
        raw = json.loads(out.read_text())
        self.assertTrue(any("not known to be complete" in warning
                            for warning in raw["collection"]["candle_warnings"]))
        self.assertEqual(len(self.calls_for(net, NYG)),
                         1 + collect_reaction.MAX_CONTINUATIONS)

    def test_a_quiet_ending_costs_one_empty_follow_up_and_no_warning(self):
        """No candle after 00:00: the market went quiet, nothing was cut."""
        net = FakeNetwork()
        full = net.candles
        last = int((KICKOFF - timedelta(minutes=20)).timestamp())

        def quiet(ticker, query):
            body = full(ticker, query)
            body["candlesticks"] = [c for c in body["candlesticks"]
                                    if c["end_period_ts"] <= last]
            return body

        net.candles = quiet
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 0, text)
        raw = json.loads(out.read_text())
        self.assertEqual(raw["collection"]["candle_responses_cut_short"], 0)
        self.assertEqual(raw["collection"]["candle_warnings"], [])
        self.assertEqual(len(self.calls_for(net, NYG)), 2)

    def test_an_endpoint_ignoring_the_span_is_not_read_as_quiet(self):
        """Every follow-up answered with the span's FIRST seven candles: an
        empty-looking follow-up that is really a repeat."""
        net = FakeNetwork(candle_cap=7)
        capped = net.candles

        def ignores_start(ticker, query):
            query = dict(query, start_ts=[str(int(CANDLES_FROM.timestamp()))])
            return capped(ticker, query)

        net.candles = ignores_start
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertIn("outside it", text)
        for ticker in (NYG, DAL):
            minutes = self.minutes(out, ticker)
            self.assertEqual(len(minutes), 7)

    def test_a_candle_returned_twice_is_reported(self):
        """An endpoint whose span includes the candle closing one period
        before `start_ts` hands every chunk its predecessor's last minute."""
        net = FakeNetwork()
        exact = net.candles

        def inclusive(ticker, query):
            early = int(query["start_ts"][0]) - 60
            return exact(ticker, dict(query, start_ts=[str(early)]))

        net.candles = inclusive
        with mock.patch.object(collect_reaction, "CANDLE_CHUNK",
                               timedelta(minutes=20)):
            code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertIn("outside it", text)
        self.assertIn("returned in two chunks", text)

    def test_an_unreadable_body_is_kept_and_reported(self):
        """Dropped, it would read as a quiet span. Kept, the replay's own
        parser reports it too."""
        net = FakeNetwork()
        net.candles = lambda ticker, query: (
            {"candlesticks": "unavailable"} if ticker == DAL
            else FakeNetwork.candles(net, ticker, query))
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertIn("expected list", text)
        raw = json.loads(out.read_text())
        dal = [c for c in raw["games"][0]["contracts"]
               if c["market_ticker"] == DAL][0]
        self.assertEqual(dal["candlesticks"], [{"candlesticks": "unavailable"}])
        _, report = load_bundle(out)
        self.assertFalse(report.complete)


class FailureTest(CollectorHarness):
    """What a collector does when the world does not cooperate."""

    def test_a_snapshot_that_never_arrived_is_a_counted_hole(self):
        """Left out, it would make two neighbours look adjacent."""
        lost = KICKOFF - timedelta(minutes=40)
        code, _, out = self.collect(net=FakeNetwork(odds_failures={lost: 99}))
        self.assertEqual(code, 1)
        raw = json.loads(out.read_text())
        self.assertEqual(len(raw["odds_snapshots"]), 13)
        self.assertEqual(raw["odds_snapshots"][4]["timestamp"],
                         "2026-09-13T23:40:00Z")
        self.assertIn("collection_failure", raw["odds_snapshots"][4])
        games, report = load_bundle(out)
        self.assertEqual(len(report.incomplete_snapshots), 1)
        self.assertFalse(report.complete)

    LOST = [KICKOFF - timedelta(minutes=m) for m in (40, 35, 30)]
    # A wide reserve, so that the CAP is not what stops either run.
    WIDE = ("--retry-fraction", "0.9")

    def test_two_lost_in_a_row_are_still_holes(self):
        net = FakeNetwork(odds_failures={at: 99 for at in self.LOST[:2]})
        code, text, out = self.collect(*self.WIDE, spend=250, net=net)
        self.assertEqual(code, 1)
        raw = json.loads(out.read_text())
        self.assertEqual(len(raw["odds_snapshots"]), 13)
        self.assertEqual(
            [s["timestamp"] for s in raw["odds_snapshots"]
             if "collection_failure" in s],
            ["2026-09-13T23:40:00Z", "2026-09-13T23:45:00Z"])
        self.assertNotIn("consecutive instants failed", text)

    def test_three_lost_apart_are_holes_not_an_outage(self):
        """The count is of CONSECUTIVE losses: a success in between resets it."""
        apart = [KICKOFF - timedelta(minutes=m) for m in (50, 35, 20)]
        net = FakeNetwork(odds_failures={at: 99 for at in apart})
        code, text, out = self.collect(*self.WIDE, spend=250, net=net)
        self.assertEqual(code, 1)
        raw = json.loads(out.read_text())
        self.assertEqual(
            sum("collection_failure" in s for s in raw["odds_snapshots"]), 3)
        self.assertNotIn("consecutive instants failed", text)

    def test_three_lost_in_a_row_stop_the_purchase(self):
        net = FakeNetwork(odds_failures={at: 99 for at in self.LOST})
        code, text, out = self.collect(*self.WIDE, spend=250, net=net)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("3 consecutive instants failed", text)
        self.assertIn("No bundle written", text)
        self.assertIn("2026-09-13T23:50:00Z: snapshot", text)
        # Nothing after the third loss was bought.
        self.assertEqual(max(net.odds_calls), self.LOST[2])

    def test_a_refused_key_says_so_instead_of_budget_reached(self):
        """Every attempt is refused, and the REASON is what gets printed."""
        net = FakeNetwork(odds_status=401)
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("HTTP Error 401", text)
        self.assertIn("consecutive instants failed", text)
        self.assertEqual(len(net.odds_calls), 9)     # 3 instants x 3 attempts
        self.assertNotIn(KEY, text)

    def test_a_cap_stop_still_names_the_failures_behind_it(self):
        """The probe carries no reserve, so the CAP stops a refused key
        first -- and the message still has to say why, and say reserved."""
        net = FakeNetwork(odds_status=401)
        code, text = self.run_collector("--probe", "--spend", "40",
                                        "--api-key", KEY, net=net)
        self.assertEqual(code, 1)
        self.assertIn("reserved of a 40 cap", text)
        self.assertIn("HTTP Error 401", text)
        self.assertNotIn("spent of a", text)

    def test_retries_past_the_reserve_stop_the_run_with_no_bundle(self):
        """The cap is enforced per ATTEMPT. A run that cannot finish inside
        what was approved writes nothing replayable; what it bought stays
        cached for the re-run."""
        stubborn = {KICKOFF - timedelta(minutes=5 * i): 2 for i in range(13)}
        net = FakeNetwork(odds_failures=stubborn)
        code, text, out = self.collect(net=net)
        self.assertEqual(code, 1)
        self.assertFalse(out.exists())
        self.assertIn("No bundle written", text)
        self.assertEqual(len(net.odds_calls), PRICE // 10)

    def test_the_trading_scan_runs_before_the_first_purchase(self):
        from reaction.capture import TradingCapabilityPresent
        with mock.patch.object(collect_reaction, "assert_read_only",
                               side_effect=TradingCapabilityPresent("x")):
            with self.assertRaises(TradingCapabilityPresent):
                self.run_collector("--spend", str(PRICE), "--api-key", KEY)
        self.assertEqual(self.net.odds_calls, [])


class EndpointGateTest(CollectorHarness):
    """Every request in the process must be a declared one."""

    def test_an_undeclared_request_is_refused_before_it_is_sent(self):
        import urllib.request
        sent = []
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda *a, **k: sent.append(a)):
            with collect_reaction.only_declared_endpoints():
                with self.assertRaises(CaptureRefused):
                    urllib.request.urlopen(
                        "https://api.elections.kalshi.com/trade-api/v2/"
                        "exchange/status")
        self.assertEqual(sent, [])

    def test_the_gate_is_removed_afterwards(self):
        import urllib.request
        before = urllib.request.urlopen
        with collect_reaction.only_declared_endpoints():
            self.assertIsNot(urllib.request.urlopen, before)
        self.assertIs(urllib.request.urlopen, before)

    def test_a_full_run_never_leaves_the_allow_list(self):
        code, text, _ = self.collect()
        self.assertEqual(code, 0, text)
        self.assertTrue(self.net.urls)
        for url in self.net.urls:
            self.assertTrue(endpoint_allowed(url), url.split("?")[0])


class ReplayHorizonTest(unittest.TestCase):
    """The horizon printed for the replay: never shorter than declared."""

    def test_a_fine_grid_keeps_the_declared_horizon(self):
        self.assertEqual(collect_reaction.replay_max_wait_seconds(
            timedelta(minutes=5)), 1800)

    def test_a_coarse_grid_widens_it_to_one_interval(self):
        self.assertEqual(collect_reaction.replay_max_wait_seconds(
            timedelta(minutes=60)), 3600)


class ProbeTest(CollectorHarness):
    """Four instants that decide how much of the horizon is worth buying."""

    def test_the_probe_prices_four_instants_and_reports_coverage(self):
        code, text = self.run_collector("--probe")
        self.assertEqual(code, 0)
        self.assertIn("--spend 40", text)
        code, text = self.run_collector("--probe", "--spend", "40",
                                        "--api-key", KEY)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.net.odds_calls), 4)
        self.assertIn("COVERAGE PROBE", text)
        self.assertIn("1 of 1 game(s) quoted", text)

    def test_the_probe_matches_kickoffs_with_the_joins_tolerance(self):
        """The provider's kickoff two minutes off ESPN's is the same game --
        to the join, and so to the probe."""
        net = FakeNetwork(commence_offset=timedelta(minutes=2))
        code, text = self.run_collector("--probe", "--spend", "40",
                                        "--api-key", KEY, net=net)
        self.assertEqual(code, 0, text)
        self.assertIn("1 of 1 game(s) quoted", text)

    def test_the_probes_snapshots_are_reused_by_the_full_run(self):
        """They fall on the 5-minute grid, so the full run gets them free."""
        self.run_collector("--probe", "--spend", "40", "--api-key", KEY)
        full = FakeNetwork()
        code, text = self.run_collector("--lead-hours", "72", net=full)
        self.assertEqual(code, 0)
        self.assertRegex(text, r"already cached\s+4\s")


if __name__ == "__main__":
    unittest.main()
