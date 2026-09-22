"""The shadow monitor, driven end to end on a fake network and a fake clock.

WHAT IS FAKE AND WHAT IS REAL
-----------------------------
The network and the clock. Everything between them is the code that will
run: the live odds fetcher with its reservation and headers, Kalshi's
listing and order-book readers, the ESPN adapter, the study's join, its move
detector on the receipt clock, its screen, the endpoint gate and the
recorder. The clock only moves when the monitor sleeps or a request takes
time, so a thirty-minute session runs in milliseconds and every instant in it
is exact.

THE SHAPES
----------
The odds events are the replay suite's observed wire format -- a live body is
the archive's `data` array without its envelope. The scoreboard event is the
owner's observed DAL@NYG fetch. The KALSHI ORDER BOOK IS NOT OBSERVED: it is
the documented shape, transcribed, exactly as `data.kalshi_history` says of
its own parser. That is why the monitor reads one real book before its first
paid poll, and why a test here pins that a shape it cannot read stops the
session for free.

THE STORY
---------
Sunday 20:00Z, DAL at NYG kicks off 00:20Z. Pinnacle moves NYG from -140 to
-320 at 20:05; Kalshi's book follows at 20:08. The monitor should see the
move on the 20:05 poll, price the shadow entry at the book it could actually
have hit, record one entry for the game, and time Kalshi's follow to within
its own poll spacing.
"""

from __future__ import annotations

import email.utils
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
import shadow_monitor                                               # noqa: E402
from data.odds_history import CreditLedger                         # noqa: E402
from reaction.capture import endpoint_allowed                      # noqa: E402
from tests.test_reaction_replay import EVENT, START, odds_payload  # noqa: E402
from tests.test_schedule import OBSERVED_DAL_NYG, board, nfl_market  # noqa: E402

UTC = timezone.utc
KICKOFF = START                                   # 2026-09-14T00:20Z
T0 = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)     # the session starts
MOVE_AT = T0 + timedelta(minutes=5)               # Pinnacle moves
FOLLOW_AT = T0 + timedelta(minutes=8)             # Kalshi follows
LATENCY = timedelta(milliseconds=200)             # every request takes this
PROVIDER_LAG = timedelta(seconds=20)              # last_update is this old
EVENT_TICKER = "KXNFLGAME-26SEP13DALNYG"
NYG, DAL = f"{EVENT_TICKER}-NYG", f"{EVENT_TICKER}-DAL"
KEY = "SECRET-KEY-VALUE"
HOURS = "0.5"
PRICE = 31                                        # 30 minutes at 60s: 31 polls


class FakeClock:
    def __init__(self, start: datetime = T0):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.t += timedelta(seconds=seconds)


def _open_markets() -> list[dict]:
    markets = []
    for yes in ("NYG", "DAL"):
        market = nfl_market(EVENT_TICKER, yes, KICKOFF)
        del market["result"]                  # open: nothing has settled
        market["status"] = "active"
        markets.append(market)
    return markets


class Response:
    """`on_read`, when given, runs as the body is read: a slow body is one
    whose reading moves the clock."""

    def __init__(self, body, headers=None, on_read=None):
        self._body = json.dumps(body).encode("utf-8")
        self.status = 200
        self.headers = headers or {}
        self._on_read = on_read

    def read(self):
        if self._on_read is not None:
            self._on_read()
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _inside(window, at) -> bool:
    return window is not None and window[0] <= at < window[1]


class LiveNetwork:
    """Answers the monitor's endpoints as they would stand at `clock.now()`.

    Knobs, each for one failure the monitor must survive or stop on:
      odds_status      every odds request fails with this HTTP status
      charged          the provider's `x-requests-last`; None omits it
      skew             the provider's clock minus ours
      remaining        what the account reports left
      book_shape_ok    False answers the order book in a shape nobody knows
      fail_books       the next N order-book requests fail (500)
      fail_books_during  order-book requests in this window -- or in any of
                       a list of windows -- fail (500)
      absent_at        odds polls in this window omit the game entirely
      listing_status   the open-market listing fails with this status
      listing_fail_after  the listing answers this many times, then fails
      extra_in_play    add a game already under way, re-priced every poll
      extra_pre_match  add a second, later game, re-priced every poll, with
                       no Kalshi market to join
      follow_on_execution  Kalshi re-prices the instant the move is served:
                       the decision read sees the old book, the execution
                       read the new one
      odds_fail_at     odds polls in this window fail (503)
      undated_at       odds polls in this window carry no observation stamp
      older_copy_at    odds polls in this window are answered from a copy
                       made before the move: the old price, an older stamp
      refresh_every    the provider re-observes on this grid rather than on
                       every request, so polls in between repeat it
      follow_at        when Kalshi re-prices (default FOLLOW_AT); None:
                       never
      odds_body_delay  each odds body takes this long to arrive after its
                       headers; `odds_bodies_done` records when each did
      book_body_delay  the same for every order-book body
    """

    def __init__(self, clock: FakeClock, **knobs):
        self.clock = clock
        self.odds_status = knobs.get("odds_status")
        self.charged = knobs.get("charged", 1)
        self.skew = knobs.get("skew", timedelta(0))
        self.remaining = knobs.get("remaining", 9000)
        self.book_shape_ok = knobs.get("book_shape_ok", True)
        self.fail_books = knobs.get("fail_books", 0)
        self.fail_books_after_move = knobs.get("fail_books_after_move", 0)
        self.absent_at = knobs.get("absent_at")
        self.listing_status = knobs.get("listing_status")
        self.listing_fail_after = knobs.get("listing_fail_after")
        during = knobs.get("fail_books_during")
        self.fail_books_during = ([] if during is None else
                                  [during] if isinstance(during[0], datetime)
                                  else list(during))
        self.interrupt_on_poll = knobs.get("interrupt_on_poll")
        self.follow_on_execution = knobs.get("follow_on_execution", False)
        self.odds_fail_at = knobs.get("odds_fail_at")
        self.undated_at = knobs.get("undated_at")
        self.older_copy_at = knobs.get("older_copy_at")
        self.refresh_every = knobs.get("refresh_every")
        self.follow_at = knobs.get("follow_at", FOLLOW_AT)
        self.odds_body_delay = knobs.get("odds_body_delay")
        self.book_body_delay = knobs.get("book_body_delay")
        self.odds_bodies_done: list[datetime] = []
        self.book_bodies_done: list[tuple[str, datetime]] = []
        self.listings_served = 0
        self.extra_in_play = knobs.get("extra_in_play", False)
        self.extra_pre_match = knobs.get("extra_pre_match", False)
        self.odds_calls: list[datetime] = []
        self.book_calls: list[tuple[str, datetime]] = []
        self.urls: list[str] = []
        self.moved_served = False

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        parts = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parts.query)
        at = self.clock.now()
        self.clock.t = at + LATENCY
        if parts.netloc == "site.api.espn.com":
            return Response(board(OBSERVED_DAL_NYG)
                            if query["dates"] == ["20260913"] else board())
        if parts.netloc == "api.elections.kalshi.com":
            if parts.path.endswith("/orderbook"):
                return self._book(url, parts.path.split("/")[-2], at)
            if parts.path.endswith("/markets"):
                if self.listing_status or (
                        self.listing_fail_after is not None
                        and self.listings_served >= self.listing_fail_after):
                    raise urllib.error.HTTPError(url, 503, "Unavailable",
                                                 {}, None)
                self.listings_served += 1
                return Response({"markets": _open_markets(), "cursor": ""})
        if parts.netloc == "api.the-odds-api.com":
            return self._odds(url, at)
        raise AssertionError(f"no answer for {url}")

    def _odds(self, url, at):
        self.odds_calls.append(at)
        if self.interrupt_on_poll == len(self.odds_calls):
            raise KeyboardInterrupt
        if self.odds_status:
            raise urllib.error.HTTPError(url, self.odds_status, "Unauthorized",
                                         {}, None)
        if _inside(self.odds_fail_at, at):
            raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
        observed = at - PROVIDER_LAG
        if self.refresh_every:
            observed = T0 + self.refresh_every * (
                (observed - T0) // self.refresh_every)
        # The provider shows the move once it has OBSERVED the book after it.
        moved = observed + PROVIDER_LAG >= MOVE_AT
        if _inside(self.older_copy_at, at):
            observed = MOVE_AT - PROVIDER_LAG - timedelta(minutes=1)
            moved = False
        events = []
        if not _inside(self.absent_at, at):
            events = odds_payload(at, 260 if moved else 120,
                                  -320 if moved else -140, observed,
                                  event_id=EVENT)["data"]
            if _inside(self.undated_at, at):
                book = events[0]["bookmakers"][0]
                del book["last_update"]
                del book["markets"][0]["last_update"]
            self.moved_served = self.moved_served or moved
        extras = []
        if self.extra_in_play:
            extras.append(("evt-in-play", "2026-09-13T17:00:00Z"))
        if self.extra_pre_match:
            extras.append(("evt-later", "2026-09-14T17:00:00Z"))
        for event_id, commence in extras:
            other = odds_payload(at, 150 + (at.minute % 7) * 40,
                                 -170 - (at.minute % 5) * 60, observed,
                                 event_id=event_id)["data"][0]
            other["commence_time"] = commence
            other["home_team"], other["away_team"] = (
                "Kansas City Chiefs", "Denver Broncos")
            for outcome in other["bookmakers"][0]["markets"][0]["outcomes"]:
                outcome["name"] = ("Kansas City Chiefs"
                                   if outcome["name"] == "New York Giants"
                                   else "Denver Broncos")
            events.append(other)
        headers = {"Date": email.utils.format_datetime(
                       (at + self.skew).replace(microsecond=0), usegmt=True),
                   "x-requests-used": str(len(self.odds_calls)),
                   "x-requests-remaining": str(self.remaining)}
        if self.charged is not None:
            headers["x-requests-last"] = str(self.charged)
        return Response(events, headers, on_read=self._odds_body)

    def _odds_body(self):
        if self.odds_body_delay:
            self.clock.t += self.odds_body_delay
        self.odds_bodies_done.append(self.clock.t)

    def _book(self, url, ticker, at):
        self.book_calls.append((ticker, at))
        if any(_inside(window, at) for window in self.fail_books_during):
            raise urllib.error.HTTPError(url, 500, "Error", {}, None)
        if self.fail_books > 0:
            self.fail_books -= 1
            raise urllib.error.HTTPError(url, 500, "Error", {}, None)
        if self.moved_served and self.fail_books_after_move > 0:
            self.fail_books_after_move -= 1
            raise urllib.error.HTTPError(url, 500, "Error", {}, None)
        if not self.book_shape_ok:
            return Response({"book": {"bids": [[56, 100]]}})
        followed = ((self.follow_at is not None and at >= self.follow_at)
                    or (self.follow_on_execution and self.moved_served))
        yes_bid, no_bid = (0.70, 0.28) if followed else (0.56, 0.42)
        if ticker == DAL:
            yes_bid, no_bid = no_bid, yes_bid
        # TRANSCRIBED from Kalshi's documentation, not observed.
        return Response({"orderbook_fp": {
            "yes_dollars": [[f"{yes_bid - 0.02:.4f}", "40.00"],
                            [f"{yes_bid:.4f}", "100.00"]],
            "no_dollars": [[f"{no_bid:.4f}", "150.00"]]}},
            on_read=lambda: self._book_body(ticker))

    def _book_body(self, ticker):
        if self.book_body_delay:
            self.clock.t += self.book_body_delay
        self.book_bodies_done.append((ticker, self.clock.t))


class MonitorHarness(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clock = FakeClock()

    def run_monitor(self, *extra, net=None, **knobs):
        net = net or LiveNetwork(self.clock, **knobs)
        argv = ["--hours", HOURS, "--cadence-seconds", "60",
                "--out-dir", str(self.tmp), *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                mock.patch("time.sleep"), \
                mock.patch.object(shadow_monitor.signal, "signal") as hooked, \
                redirect_stdout(out), redirect_stderr(err):
            code = shadow_monitor.main(argv, clock=self.clock)
        self.hooked = hooked
        return code, out.getvalue() + err.getvalue(), net

    def paid(self, *extra, **knobs):
        return self.run_monitor("--spend", str(PRICE), "--api-key", KEY,
                                *extra, **knobs)

    def records(self) -> list[dict]:
        files = sorted(self.tmp.glob("shadow_*.jsonl"))
        self.assertEqual(len(files), 1, files)
        rows, bad = shadow_monitor.read_records(files[0])
        self.assertEqual(bad, 0)
        return rows

    def kinds(self, rows, kind):
        return [r for r in rows if r["kind"] == kind]

    def end(self, rows):
        return self.kinds(rows, "session_end")[-1]


class PlanFirstTest(MonitorHarness):
    """Money moves only on an explicit confirmation of the quoted price."""

    def test_without_spend_it_prices_the_session_and_buys_nothing(self):
        code, text, net = self.run_monitor()
        self.assertEqual(code, 0, text)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("31 poll(s), 31 credit(s)", text)
        self.assertIn(f"--spend {PRICE}", text)
        self.assertFalse(list(self.tmp.glob("*.jsonl")))

    def test_the_plan_reads_one_real_book_for_free(self):
        """The book's shape is transcribed. The plan -- free -- reads one
        real book through the preflight's own reader, so the shape can be
        checked without buying anything."""
        code, text, net = self.run_monitor()
        self.assertEqual(code, 0, text)
        self.assertEqual(net.odds_calls, [])
        self.assertEqual(len(net.book_calls), 1)
        self.assertIn("the transcribed shape parses", text)

    def test_a_book_the_parser_refuses_fails_the_plan(self):
        code, text, net = self.run_monitor(book_shape_ok=False)
        self.assertEqual(code, 1, text)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("could not be read", text)
        self.assertIn("Nothing was spent", text)

    def test_a_paid_session_reads_the_shape_once_in_its_preflight(self):
        """Not twice: the plan's read is for a plan; a session's preflight
        is the one that stops it."""
        code, text, net = self.paid()
        self.assertEqual(code, 0, text)
        shape = self.kinds(self.records(), "shape_check")
        self.assertEqual(len(shape), 1)
        first_odds = net.odds_calls[0]
        # The preflight's read and nothing else: the plan's own read is not
        # repeated, and the first tick has joined nothing yet to read.
        self.assertEqual(sum(1 for _, at in net.book_calls if at < first_odds),
                         1)

    def test_a_spend_below_the_price_is_refused(self):
        code, text, net = self.run_monitor("--spend", str(PRICE - 1),
                                           "--api-key", KEY)
        self.assertEqual(code, 2)
        self.assertEqual(net.odds_calls, [])

    def test_no_key_no_purchase(self):
        with mock.patch.dict("os.environ", {"ODDS_API_KEY": ""}):
            code, _, net = self.run_monitor("--spend", str(PRICE))
        self.assertEqual(code, 2)
        self.assertEqual(net.odds_calls, [])

    def test_a_cadence_below_the_floor_is_refused(self):
        code, text, net = self.run_monitor("--cadence-seconds", "5")
        self.assertEqual(code, 2)
        self.assertEqual(net.odds_calls, [])

    def test_an_incomplete_slate_buys_nothing(self):
        code, text, net = self.paid(listing_status=503)
        self.assertEqual(code, 1)
        self.assertEqual(net.odds_calls, [])
        self.assertIn("nothing was bought", text)

    def test_session_price_is_an_upper_bound_on_the_polls_made(self):
        code, text, net = self.paid()
        self.assertEqual(code, 0, text)
        self.assertLessEqual(len(net.odds_calls), PRICE)
        self.assertEqual(shadow_monitor.session_price(0.5,
                                                      timedelta(seconds=60)),
                         PRICE)


class SessionTest(MonitorHarness):
    """The whole story, read back from the records."""

    def setUp(self):
        super().setUp()
        self.code, self.text, self.net = self.paid()
        self.rows = self.records()

    def test_it_runs_to_the_end_of_the_session(self):
        self.assertEqual(self.code, 0, self.text)
        end = self.end(self.rows)
        self.assertEqual(end["reason"], "end_of_session")
        self.assertTrue(end["ok"])
        self.assertEqual(len(self.net.odds_calls), 30)

    def test_one_move_is_detected_on_the_poll_that_carried_it(self):
        """Detected at the RECEIPT of the first poll after the move -- not
        earlier (nobody had it), not a poll later (it was in that answer)."""
        triggers = self.kinds(self.rows, "trigger")
        self.assertEqual(len(triggers), 1)
        detected = datetime.fromisoformat(triggers[0]["detected_at"])
        carried = min(at for at in self.net.odds_calls if at >= MOVE_AT)
        self.assertEqual(detected, carried + LATENCY)

    def test_the_detector_ran_on_the_receipt_clock(self):
        """The move is actionable when WE had it, not when the provider saw
        it: detected_at is the receipt, 20s after `last_update`."""
        trigger = self.kinds(self.rows, "trigger")[0]
        detected = datetime.fromisoformat(trigger["detected_at"])
        observed = datetime.fromisoformat(trigger["provider_observed_at"])
        carried = min(at for at in self.net.odds_calls if at >= MOVE_AT)
        # The provider stamps whole seconds, as the observed payloads do.
        served = (carried - PROVIDER_LAG).replace(microsecond=0)
        self.assertEqual(observed, served)
        self.assertEqual(detected - observed, carried + LATENCY - served)
        self.assertGreaterEqual(detected - observed, PROVIDER_LAG)

    def test_the_shadow_entry_is_priced_at_the_book_it_could_hit(self):
        decisions = [d for d in self.kinds(self.rows, "decision")
                     if d.get("market_ticker")]
        self.assertEqual(len(decisions), 2)
        entered = [d for d in decisions if d["entered"]]
        self.assertEqual(len(entered), 1, "one entry per game")
        entry = entered[0]
        self.assertTrue(entry["admitted"])
        self.assertGreater(entry["entry_delay_seconds"], 0)
        self.assertLess(entry["entry_delay_seconds"], 2)
        predicted = entry["predicted"]
        # Kalshi had not followed yet: decided and paid at the pre-move book.
        self.assertAlmostEqual(predicted["entry_price"], 0.58, places=6)
        self.assertGreater(predicted["predicted_ev_per_contract"], 0.01)
        self.assertAlmostEqual(predicted["latency_cost"], 0.0, places=9)

    def test_decision_books_were_read_before_the_poll_that_moved(self):
        trigger = self.kinds(self.rows, "trigger")[0]
        detected = datetime.fromisoformat(trigger["detected_at"])
        decisions = [b for b in self.kinds(self.rows, "book")
                     if b["purpose"] == "decision"]
        before = [b for b in decisions
                  if datetime.fromisoformat(b["received_at"]) <= detected
                  and datetime.fromisoformat(b["received_at"])
                  > detected - timedelta(seconds=5)]
        self.assertEqual({b["ticker"] for b in before}, {NYG, DAL})
        executions = [b for b in self.kinds(self.rows, "book")
                      if b["purpose"] == "execution"]
        self.assertEqual({b["ticker"] for b in executions}, {NYG, DAL})
        for book in executions:
            self.assertGreater(datetime.fromisoformat(book["received_at"]),
                               detected)

    def test_the_game_is_followed_every_ten_seconds(self):
        follows = [datetime.fromisoformat(b["received_at"])
                   for b in self.kinds(self.rows, "book")
                   if b["purpose"] == "follow" and b["ticker"] == NYG]
        self.assertGreater(len(follows), 100)
        gaps = [(b - a).total_seconds() for a, b in zip(follows, follows[1:])]
        self.assertTrue(all(9 <= g <= 11 for g in gaps), gaps[:5])

    def test_the_report_times_kalshis_follow_inside_the_poll_spacing(self):
        figures = shadow_monitor.report(self.rows)
        followed = [r for r in figures["responses"] if r["ticker"] == NYG]
        self.assertEqual(len(followed), 1)
        low, high = followed[0]["lag_seconds"]
        self.assertEqual(followed[0]["outcome"], "responded")
        self.assertFalse(followed[0]["session_ended_inside_window"] is None)
        # Kalshi moved at 20:08:00, 179.4s after the 20:05:00.6 detection.
        self.assertLessEqual(low, 179.4)
        self.assertGreaterEqual(high, 179.4)
        self.assertLessEqual(high - low, 11)

    def test_the_report_measures_age_and_refresh(self):
        figures = shadow_monitor.report(self.rows)
        self.assertIn("median 20.", figures["age_when_received"])
        self.assertIn("median 60.0s", figures["provider_refresh"])
        rendered = shadow_monitor.render_report(figures)
        self.assertIn("moves detected     1", rendered)
        self.assertIn("No orders were placed", rendered)

    def test_the_key_is_in_no_record_and_no_output(self):
        self.assertNotIn(KEY, self.text)
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())

    def test_every_request_was_a_declared_one(self):
        self.assertTrue(self.net.urls)
        for url in self.net.urls:
            self.assertTrue(endpoint_allowed(url), url.split("?")[0])

    def test_the_book_shape_was_checked_before_the_first_paid_poll(self):
        kinds = [r["kind"] for r in self.rows]
        self.assertLess(kinds.index("shape_check"), kinds.index("odds"))


class StopTest(MonitorHarness):
    """Each stop, and that it stops before spending what it would waste."""

    def assertStopped(self, reason, *, polls=None, **knobs):
        code, text, net = self.paid(**knobs)
        self.assertEqual(code, 1, text)
        rows = self.records()
        self.assertEqual(self.end(rows)["reason"], reason)
        if polls is not None:
            self.assertEqual(len(net.odds_calls), polls)
        self.assertNotIn(KEY, text)
        return text, rows, net

    def test_a_refused_key_stops_after_three_polls_and_says_why(self):
        text, rows, _ = self.assertStopped("failed_polls", polls=3,
                                           odds_status=401)
        self.assertIn("HTTP Error 401", self.end(rows)["detail"])

    def test_a_price_per_call_other_than_quoted_stops_at_once(self):
        self.assertStopped("cost_mismatch", polls=1, charged=2)

    def test_an_unverifiable_price_stops_at_once(self):
        self.assertStopped("cost_unverifiable", polls=1, charged=None)

    def test_a_skewed_clock_stops_at_once(self):
        _, rows, _ = self.assertStopped("clock_skew", polls=1,
                                        skew=timedelta(seconds=30))
        self.assertIn("NTP", self.end(rows)["detail"])

    def test_a_nearly_empty_account_stops(self):
        self.assertStopped("quota_floor", polls=1, remaining=10)

    def test_an_undated_feed_stops_on_its_first_answer(self):
        """The provider's observation stamp is transcribed too. The detector
        refuses an undated price, so a feed that sends none would be
        watched, and paid for, with nothing able to trigger."""
        _, rows, _ = self.assertStopped(
            "undated_quotes", polls=1,
            undated_at=(T0, T0 + timedelta(days=1)))
        self.assertIn("observation time", self.end(rows)["detail"])

    def test_only_a_pre_match_quote_can_vouch_for_the_stamps(self):
        """A dated quote for a game already under way proves nothing: the
        monitor never shows one to the detector."""
        self.assertStopped("undated_quotes", polls=1, extra_in_play=True,
                           undated_at=(T0, T0 + timedelta(days=1)))

    def test_one_dated_pre_match_quote_is_enough(self):
        """Only a feed with NO observation time stops. One dated game shows
        the stamp exists; the undated one is the detector's to refuse."""
        code, text, _ = self.paid(extra_pre_match=True,
                                  undated_at=(T0, T0 + timedelta(days=1)))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.end(self.records())["reason"], "end_of_session")

    def test_a_book_shape_nobody_knows_stops_before_any_purchase(self):
        text, rows, _ = self.assertStopped("book_unreadable", polls=0,
                                           book_shape_ok=False)
        self.assertIn("nothing was bought", self.end(rows)["detail"])

    def test_the_trading_scan_runs_before_the_first_purchase(self):
        from reaction.capture import TradingCapabilityPresent
        net = LiveNetwork(self.clock)
        with mock.patch.object(collect_reaction, "assert_read_only",
                               side_effect=TradingCapabilityPresent("x")):
            with self.assertRaises(TradingCapabilityPresent):
                self.paid(net=net)
        self.assertEqual(net.odds_calls, [])

    def test_an_undeclared_request_ends_the_session_unsent(self):
        """A live poll pointed at a path nobody declared is refused before it
        leaves the process, and the session records why it ended."""
        from data import odds_live
        with mock.patch.object(odds_live, "LIVE_ODDS_PATH",
                               "/sports/{sport_key}/scores"):
            _, rows, net = self.assertStopped("undeclared_request", polls=0)
        self.assertIn("not one of the declared", self.end(rows)["detail"])

    def test_the_cap_is_enforced_per_poll(self):
        """Driven directly, with a cap below the session's length."""
        net = LiveNetwork(self.clock)
        recorder = shadow_monitor.Recorder(self.tmp / "capped.jsonl")
        monitor = shadow_monitor.ShadowMonitor(
            sport="NFL", series="KXNFLGAME", api_key=KEY,
            cadence=timedelta(seconds=60), hours=0.5,
            ledger=CreditLedger(cap=3), recorder=recorder, clock=self.clock)
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                mock.patch("time.sleep"):
            stop = monitor.run()
        recorder.close()
        self.assertEqual(stop.reason, "credit_cap")
        self.assertEqual(len(net.odds_calls), 3)


class HonestyTest(MonitorHarness):
    """The ways a live feed can manufacture a move, or a fill, that was not."""

    def test_a_failed_execution_read_is_not_a_fill_at_the_decision_price(self):
        """Both execution reads fail. Pricing the entry at the decision book
        instead would record a fill that could not have happened."""
        code, text, _ = self.paid(fail_books_after_move=6)
        self.assertEqual(code, 0, text)
        decisions = [d for d in self.kinds(self.records(), "decision")
                     if d.get("market_ticker")]
        self.assertEqual(len(decisions), 2)
        for decision in decisions:
            self.assertFalse(decision["admitted"])
            self.assertFalse(decision["entered"])
            self.assertIsNone(decision["predicted"])

    def test_a_book_that_moves_before_execution_is_paid_not_hidden(self):
        """Kalshi re-prices between the decision read and the execution read.
        The decision is still judged on what the bot KNEW -- the old book --
        and what it would have PAID is the new one. Pricing the fill at the
        decision book would report an edge the latency had already taken."""
        code, text, _ = self.paid(follow_on_execution=True)
        self.assertEqual(code, 0, text)
        entry = [d for d in self.kinds(self.records(), "decision")
                 if d.get("entered")][0]
        predicted = entry["predicted"]
        self.assertTrue(entry["admitted"])
        self.assertAlmostEqual(entry["book"]["decision_ask"]
                               if entry["market_ticker"] == NYG
                               else 1 - entry["book"]["decision_bid"], 0.58,
                               places=6)
        self.assertAlmostEqual(predicted["entry_price"], 0.58, places=6)
        self.assertGreater(predicted["paid_at_execution"],
                           predicted["entry_price"] + predicted["fee"] + 0.1)
        self.assertGreater(predicted["latency_cost"], 0.1)
        rendered = shadow_monitor.render_report(
            shadow_monitor.report(self.records()))
        self.assertIn("would have paid 0.7", rendered)

    def test_an_older_copy_re_served_is_not_a_second_move(self):
        """The two polls after the move are answered from a copy made
        before it, then the newer copy returns. One book move, one trigger:
        read as content, the older copy is a move back and the newer one's
        return, three minutes after the first trigger, a second move."""
        window = (MOVE_AT + timedelta(seconds=60),
                  MOVE_AT + timedelta(seconds=180))
        code, text, _ = self.paid(older_copy_at=window)
        self.assertEqual(code, 0, text)
        rows = self.records()
        self.assertEqual(len(self.kinds(rows, "trigger")), 1)
        figures = shadow_monitor.report(rows)
        self.assertEqual(figures["detector"]["regressed_content"], 2)
        self.assertEqual(figures["sightings"]["older_copy"], 2)

    def test_a_stamp_lost_mid_session_is_counted_not_a_stop(self):
        """The stamp check is on the first answer. After it, a quote with
        no stamp is one the detector refuses, and the report counts it --
        once: the next two undated polls carry the same content, which the
        detector deduplicates like any re-served record."""
        code, text, _ = self.paid(
            undated_at=(MOVE_AT, MOVE_AT + timedelta(minutes=3)))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.end(self.records())["reason"], "end_of_session")
        figures = shadow_monitor.report(self.records())
        self.assertEqual(figures["detector"]["unknown_content_age"], 1)
        # and the undated stretch is a hole in the provider's timing too
        self.assertIn("max 60.0s", figures["provider_refresh"])
        self.assertEqual(figures["sightings"]["first"], 2)

    def test_a_game_absent_from_a_poll_is_a_hole_not_a_move(self):
        """The game vanishes on the 20:05 poll and returns moved on 20:06:
        nobody saw it move, so nothing may be measured across the hole."""
        code, text, _ = self.paid(absent_at=(MOVE_AT, MOVE_AT
                                             + timedelta(seconds=30)))
        self.assertEqual(code, 0, text)
        self.assertEqual(self.kinds(self.records(), "trigger"), [])

    def test_a_failed_slate_refresh_keeps_the_last_join(self):
        """A failed read is not an empty slate. The refresh at 20:03 fails;
        the move at 20:05 is still judged against the games joined before.
        (The listing answers the plan, the preflight and the first join --
        three times -- and fails from then on.)"""
        with mock.patch.object(shadow_monitor, "REJOIN_EVERY",
                               timedelta(minutes=3)):
            code, text, _ = self.paid(listing_fail_after=3)
        self.assertEqual(code, 0, text)
        rows = self.records()
        self.assertTrue(self.kinds(rows, "rejoin_skipped"))
        self.assertEqual(sum(1 for d in self.kinds(rows, "decision")
                             if d.get("entered")), 1)

    def test_a_game_already_under_way_is_never_a_move(self):
        code, text, _ = self.paid(extra_in_play=True)
        self.assertEqual(code, 0, text)
        rows = self.records()
        triggers = self.kinds(rows, "trigger")
        self.assertEqual({t["event_id"] for t in triggers}, {EVENT})
        self.assertGreater(self.end(rows)["counts"]["in_play_skipped"], 0)


class ReceiptClockTest(MonitorHarness):
    """A move is dated by when its answer was usable, never before.

    Three clocks per poll: the headers arriving, the whole body arriving,
    and the body read and parsed. The first used to be the only one: the
    receipt was stamped when `urlopen` returned, before the body was read,
    so a body that took eight seconds was credited to us eight seconds
    early -- and every lag measured from that instant ran eight seconds
    long, as time a bot could have traded in.
    """

    BODY = timedelta(seconds=8)

    def carrying(self, net):
        """The odds call that first served the move, and when its body was
        all in."""
        index = next(i for i, at in enumerate(net.odds_calls) if at >= MOVE_AT)
        return net.odds_calls[index], net.odds_bodies_done[index]

    def test_a_move_is_dated_after_its_body_has_arrived(self):
        code, text, net = self.paid(odds_body_delay=self.BODY)
        self.assertEqual(code, 0, text)
        rows = self.records()
        _, done = self.carrying(net)
        trigger = self.kinds(rows, "trigger")[0]
        self.assertGreaterEqual(datetime.fromisoformat(trigger["detected_at"]),
                                done)
        for decision in self.kinds(rows, "decision"):
            if decision.get("decision_at"):
                self.assertGreaterEqual(
                    datetime.fromisoformat(decision["decision_at"]), done)
        poll = next(r for r in self.kinds(rows, "odds")
                    if datetime.fromisoformat(r["received_at"]) == done)
        headers = datetime.fromisoformat(poll["headers_at"])
        self.assertGreaterEqual(done - headers, self.BODY)
        self.assertLessEqual(done, datetime.fromisoformat(poll["ready_at"]))

    def test_a_slow_body_is_not_clock_skew(self):
        """`Date` marks the response STARTING, so skew is measured against
        the headers. Against the finished body, eight seconds of transfer
        would read as a clock eight seconds off and stop the session."""
        code, text, _ = self.paid(odds_body_delay=self.BODY)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.end(self.records())["reason"], "end_of_session")

    def test_processing_is_recorded_and_not_credited(self):
        """Two seconds spent reading the answer: the move is dated after
        them, and they are recorded as processing."""
        real = shadow_monitor.parse_snapshot

        def slow(body):
            self.clock.t += timedelta(seconds=2)
            return real(body)

        with mock.patch.object(shadow_monitor, "parse_snapshot",
                               side_effect=slow):
            code, text, net = self.paid()
        self.assertEqual(code, 0, text)
        rows = self.records()
        trigger = self.kinds(rows, "trigger")[0]
        detected = datetime.fromisoformat(trigger["detected_at"])
        poll = next(r for r in self.kinds(rows, "odds")
                    if datetime.fromisoformat(r["ready_at"]) == detected)
        self.assertEqual(poll["processing_seconds"], 2.0)
        received = datetime.fromisoformat(poll["received_at"])
        self.assertEqual(detected - received, timedelta(seconds=2))
        after = trigger["after"]
        self.assertEqual(datetime.fromisoformat(after["response_received_at"]),
                         received)
        self.assertEqual(datetime.fromisoformat(after["local_receipt_time"]),
                         detected)
        figures = shadow_monitor.report(rows)
        self.assertIn("median 2.0s", figures["polls"]["processing"])

    def test_a_slow_book_read_is_dated_by_its_completion(self):
        """The execution book is the book when its answer was complete; the
        entry delay runs to there."""
        code, text, net = self.paid(book_body_delay=timedelta(seconds=3))
        self.assertEqual(code, 0, text)
        rows = self.records()
        executions = [b for b in self.kinds(rows, "book")
                      if b["purpose"] == "execution"]
        self.assertTrue(executions)
        for book in executions:
            sent = datetime.fromisoformat(book["sent_at"])
            received = datetime.fromisoformat(book["received_at"])
            self.assertGreaterEqual(received - sent, timedelta(seconds=3))
        entered = [d for d in self.kinds(rows, "decision") if d["entered"]]
        self.assertGreaterEqual(entered[0]["entry_delay_seconds"], 3.0)


class LongSessionTest(MonitorHarness):
    """What a session left running for days must not do."""

    def test_each_poll_records_its_own_refusals(self):
        """Counted once the poll's quotes have been through the detector,
        so the first poll carries the baseline IT set. Counted before, every
        record described the poll ahead of it and the last one's were lost."""
        code, text, _ = self.paid()
        self.assertEqual(code, 0, text)
        rows = self.records()
        polls = self.kinds(rows, "odds")
        counted = self.kinds(rows, "detector")
        self.assertEqual(len(counted), len(polls))
        for poll, refusals in zip(polls, counted):
            self.assertEqual(refusals["received_at"], poll["received_at"])
        self.assertEqual(counted[0]["rejections"], {"first_observation": 1})
        self.assertTrue(any("below_threshold" in c["rejections"]
                            for c in counted[1:]), counted[:3])
        self.assertIn("detector", self.end(rows))

    def test_the_detector_holds_one_polls_worth_of_rejections(self):
        net = LiveNetwork(self.clock)
        recorder = shadow_monitor.Recorder(self.tmp / "long.jsonl")
        monitor = shadow_monitor.ShadowMonitor(
            sport="NFL", series="KXNFLGAME", api_key=KEY,
            cadence=timedelta(seconds=60), hours=0.5,
            ledger=CreditLedger(cap=PRICE), recorder=recorder,
            clock=self.clock)
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                mock.patch("time.sleep"):
            monitor.run()
        recorder.close()
        # Each poll's are counted and dropped; the session_end takes the rest.
        self.assertEqual(monitor.detector.result.rejections, [])

    def test_a_stalled_book_read_abandons_the_tick_not_the_poll(self):
        """Book reads fail across the 20:05 tick's decision reads. The rest
        of that tick's reads are abandoned and the paid poll still runs on
        time, so the move is still seen."""
        # Ticks run a few seconds after the minute (the free preflight comes
        # first), so the window covers the whole of the tick after MOVE_AT.
        window = (MOVE_AT, MOVE_AT + timedelta(seconds=30))
        code, text, net = self.paid(fail_books_during=window)
        self.assertEqual(code, 0, text)
        rows = self.records()
        self.assertTrue(self.kinds(rows, "decision_reads_abandoned"))
        self.assertEqual(len(net.odds_calls), 30)
        self.assertEqual(len(self.kinds(rows, "trigger")), 1)
        # One attempt per live read, all failing inside the window: the
        # abandoned decision read, two execution reads, and two follow rounds
        # of two contracts -- seven. Retried like the archive readers, 21.
        inside = [t for t, at in net.book_calls if window[0] <= at < window[1]]
        self.assertEqual(len(inside), 7, inside)

    def test_an_interrupt_ends_the_session_with_a_record(self):
        code, text, net = self.paid(interrupt_on_poll=5)
        self.assertEqual(code, 1)
        self.assertEqual(self.end(self.records())["reason"], "interrupted")

    def test_a_service_stop_is_an_interrupt(self):
        self.paid()
        self.hooked.assert_called_once_with(shadow_monitor.signal.SIGTERM,
                                            shadow_monitor._interrupt)
        with self.assertRaises(KeyboardInterrupt):
            shadow_monitor._interrupt(15, None)


class ReportTest(MonitorHarness):

    def test_a_torn_last_line_is_counted_not_fatal(self):
        code, _, _ = self.paid()
        path = sorted(self.tmp.glob("shadow_*.jsonl"))[0]
        with path.open("a") as handle:
            handle.write('{"kind": "odds", "trunc')
        out = io.StringIO()
        with redirect_stdout(out):
            code = shadow_monitor.main(["--report", str(path)])
        self.assertEqual(code, 0)
        self.assertIn("1 unreadable line(s) skipped", out.getvalue())
        self.assertIn("moves detected     1", out.getvalue())

    def test_a_provider_that_refreshes_every_poll_gives_a_ceiling(self):
        """The story's provider re-observes on every request, so every poll
        finds something new, and the 60s the report times is this session's
        spacing, not the provider's. It says so."""
        self.paid()
        figures = shadow_monitor.report(self.records())
        self.assertEqual(figures["sightings"]["repeat"], 0)
        self.assertGreater(figures["sightings"]["new"], 20)
        rendered = shadow_monitor.render_report(figures)
        self.assertIn("60s poll spacing", rendered)
        self.assertIn("a ceiling on the provider's interval", rendered)

    def test_a_provider_slower_than_the_polls_is_measured(self):
        code, text, _ = self.paid(refresh_every=timedelta(seconds=150))
        self.assertEqual(code, 0, text)
        figures = shadow_monitor.report(self.records())
        self.assertIn("median 150.0s", figures["provider_refresh"])
        self.assertIn("max 150.0s", figures["provider_refresh"])
        self.assertGreater(figures["sightings"]["repeat"], 0)
        rendered = shadow_monitor.render_report(figures)
        self.assertNotIn("ceiling", rendered)
        self.assertIn("buys more of those", rendered)

    def test_a_game_missing_from_a_poll_is_not_timed_across(self):
        """Three polls without the game: observations may have come and
        gone inside the hole, so none is timed across it."""
        self.paid(absent_at=(T0 + timedelta(minutes=2),
                             T0 + timedelta(minutes=5)))
        figures = shadow_monitor.report(self.records())
        self.assertIn("max 60.0s", figures["provider_refresh"])
        self.assertEqual(figures["sightings"]["first"], 2)

    def test_an_unanswered_poll_is_not_timed_across(self):
        code, text, _ = self.paid(odds_fail_at=(T0 + timedelta(minutes=2),
                                                T0 + timedelta(minutes=4)))
        self.assertEqual(code, 0, text)
        figures = shadow_monitor.report(self.records())
        self.assertIn("max 60.0s", figures["provider_refresh"])
        self.assertEqual(figures["sightings"]["unanswered_polls"], 2)
        self.assertEqual(figures["sightings"]["first"], 2)

    def test_only_an_observation_first_seen_new_is_aged(self):
        """A first sighting may predate our watching; its age is not a
        delivery delay."""
        self.paid()
        figures = shadow_monitor.report(self.records())
        self.assertTrue(figures["age_when_received"].startswith(
            f"n={figures['sightings']['new']} "), figures)

    def test_refusals_are_summed_from_every_poll_and_the_remainder(self):
        rows = [{"kind": "session_start", "at": "2026-09-13T20:00:00+00:00",
                 "cadence_seconds": 60},
                {"kind": "detector", "rejections": {"below_threshold": 2}},
                {"kind": "detector", "rejections": {"below_threshold": 1,
                                                    "declared_gap": 1}},
                {"kind": "session_end", "at": "2026-09-13T20:30:00+00:00",
                 "reason": "interrupted",
                 "detector": {"unknown_content_age": 4}}]
        figures = shadow_monitor.report(rows)
        self.assertEqual(figures["detector"], {"below_threshold": 3,
                                               "declared_gap": 1,
                                               "unknown_content_age": 4})
        rendered = shadow_monitor.render_report(figures)
        self.assertLess(rendered.index("unknown_content_age"),
                        rendered.index("below_threshold"))

    def test_a_follow_past_the_window_is_censored_not_counted(self):
        code, _, _ = self.paid()
        rows = self.records()
        figures = shadow_monitor.report(rows, window=timedelta(seconds=60))
        nyg = [r for r in figures["responses"] if r["ticker"] == NYG][0]
        self.assertEqual(nyg["outcome"], "no_response_in_window")
        self.assertGreater(nyg["reads_in_window"], 0)
        self.assertFalse(nyg["session_ended_inside_window"])


class ResponseCoverageTest(MonitorHarness):
    """What the report may call Kalshi's response, and what it may not.

    The report is the replay's own `measure_response` over the session's
    reads. A response is `responded` only when it is located entirely after
    the move was actionable; a window is `no_response_in_window` only when
    the reads covered it to its end. Everything short of that says why.
    """

    def responses(self, rows, **overrides):
        figures = shadow_monitor.report(rows, **overrides)
        return {r["ticker"]: r for r in figures["responses"]}, figures

    def test_a_window_watched_to_its_end_without_a_move(self):
        """The successful non-response: reads every 10s to the window's end."""
        code, text, _ = self.paid(follow_at=None)
        self.assertEqual(code, 0, text)
        rows = self.records()
        by, _ = self.responses(rows, window=timedelta(minutes=5))
        self.assertEqual(by[NYG]["outcome"], "no_response_in_window")
        self.assertFalse(by[NYG]["session_ended_inside_window"])
        self.assertEqual(by[NYG]["unreadable_in_window"], 0)

    def test_a_window_the_session_ended_inside_is_censored_not_quiet(self):
        """The move at 20:05 has a window to 20:35; the session ends at
        20:30. Five minutes nobody watched is not five minutes of nothing."""
        code, text, _ = self.paid(follow_at=None)
        self.assertEqual(code, 0, text)
        rows = self.records()
        by, figures = self.responses(rows)
        self.assertEqual(by[NYG]["outcome"], "blind_interval")
        self.assertTrue(by[NYG]["session_ended_inside_window"])
        self.assertIn("outlived the session",
                      shadow_monitor.render_report(figures))

    def test_an_interrupted_session_is_censored_not_quiet(self):
        code, text, _ = self.paid(follow_at=None, interrupt_on_poll=8)
        self.assertEqual(code, 1, text)
        rows = self.records()
        self.assertEqual(self.end(rows)["reason"], "interrupted")
        by, _ = self.responses(rows, window=timedelta(minutes=5))
        self.assertEqual(by[NYG]["outcome"], "blind_interval")
        self.assertTrue(by[NYG]["session_ended_inside_window"])

    def test_failed_reads_inside_the_window_leave_a_hole(self):
        """A minute of follow reads lost: the window was not watched."""
        window = (MOVE_AT + timedelta(seconds=60),
                  MOVE_AT + timedelta(seconds=120))
        code, text, _ = self.paid(follow_at=None, fail_books_during=window)
        self.assertEqual(code, 0, text)
        by, figures = self.responses(self.records(),
                                     window=timedelta(minutes=5))
        self.assertEqual(by[NYG]["outcome"], "blind_interval")
        self.assertGreater(by[NYG]["unreadable_in_window"], 0)
        self.assertIn("gave no usable book",
                      shadow_monitor.render_report(figures))

    def test_the_coverage_counts_are_of_the_reads_the_outcome_used(self):
        """Beside every outcome the report prints how many reads covered its
        window and how many of those failed. They are counted by the
        measurement's own rule -- answered after the decision, requested no
        later than the window's end -- so a read in flight at the deadline
        is in, and reads before the decision or after the window, failed
        ones included, are not."""
        before = (T0 + timedelta(minutes=1), T0 + timedelta(minutes=3))
        # From the first follow read after the fifth minute of the window:
        # that read is in flight when the window below ends, and it fails.
        after = (T0 + timedelta(minutes=10, seconds=6),
                 T0 + timedelta(minutes=12))
        code, text, _ = self.paid(follow_at=None,
                                  fail_books_during=[before, after])
        self.assertEqual(code, 0, text)
        rows = self.records()
        decided = datetime.fromisoformat(next(
            d for d in self.kinds(rows, "decision")
            if d.get("market_ticker") == NYG)["decision_at"])
        reads = [b for b in self.kinds(rows, "book") if b["ticker"] == NYG]

        def sent(b):
            return datetime.fromisoformat(b["sent_at"])

        def got(b):
            return datetime.fromisoformat(b["received_at"])

        straddling = next(b for b in reads if sent(b) >= after[0])
        self.assertIsNone(straddling["payload"])
        deadline = sent(straddling) + (got(straddling) - sent(straddling)) / 2
        by, _ = self.responses(rows, window=deadline - decided)

        inside = [b for b in reads
                  if got(b) > decided and sent(b) <= deadline]
        outside = [b for b in reads if b not in inside]
        # Something to exclude on every side, failed and successful alike.
        for side in (lambda b: got(b) <= decided,
                     lambda b: sent(b) > deadline):
            self.assertTrue(any(side(b) and b["payload"] is None
                                for b in outside))
            self.assertTrue(any(side(b) and b["payload"] is not None
                                for b in outside))
        self.assertIn(straddling, inside)
        self.assertEqual(by[NYG]["window_ends"], deadline.isoformat())
        self.assertEqual(by[NYG]["reads_in_window"],
                         sum(1 for b in inside if b["payload"] is not None))
        self.assertEqual(by[NYG]["unreadable_in_window"], 1)
        self.assertEqual(by[NYG]["outcome"], "no_response_in_window")

    def test_a_slow_read_widens_the_response_to_its_request(self):
        """Each book takes three seconds to answer. The change is bracketed
        from the REQUEST of the last read that did not show it: the book it
        reported may be from anywhere after that request went out."""
        code, text, _ = self.paid(book_body_delay=timedelta(seconds=3))
        self.assertEqual(code, 0, text)
        rows = self.records()
        by, _ = self.responses(rows)
        low, high = by[NYG]["lag_seconds"]
        detected = datetime.fromisoformat(by[NYG]["decision_at"])
        reads = [b for b in self.kinds(rows, "book") if b["ticker"] == NYG
                 and datetime.fromisoformat(b["received_at"]) > detected]
        # The first read whose book shows Kalshi's new price (a 0.70 bid).
        changed = next(i for i, b in enumerate(reads) if b["payload"]
                       and b["payload"]["orderbook_fp"]["yes_dollars"][-1][0]
                       == "0.7000")
        before = reads[changed - 1]
        self.assertAlmostEqual(
            low, (datetime.fromisoformat(before["sent_at"])
                  - detected).total_seconds(), places=6)
        self.assertAlmostEqual(
            high, (datetime.fromisoformat(reads[changed]["received_at"])
                   - detected).total_seconds(), places=6)

    def test_the_session_is_judged_by_the_limit_it_recorded(self):
        """The same lost minute, under a session that declared it could go
        200s unseen: then it is inside the limit, and the window was watched.
        A report must use the policy the session ran with, not today's."""
        window = (MOVE_AT + timedelta(seconds=60),
                  MOVE_AT + timedelta(seconds=120))
        self.paid(follow_at=None, fail_books_during=window)
        rows = self.records()
        start = self.kinds(rows, "session_start")[0]
        self.assertEqual(
            start["reaction_policy"]["max_unobserved_seconds"],
            shadow_monitor.MAX_UNOBSERVED.total_seconds())
        start["reaction_policy"]["max_unobserved_seconds"] = 200.0
        by, _ = self.responses(rows, window=timedelta(minutes=5))
        self.assertEqual(by[NYG]["outcome"], "no_response_in_window")

    def test_kalshi_moving_before_the_move_was_actionable_is_already_priced(
            self):
        """Kalshi re-prices at 20:03, two minutes before the sharp move
        reached us: the decision books already show it."""
        code, text, _ = self.paid(follow_at=MOVE_AT - timedelta(minutes=2))
        self.assertEqual(code, 0, text)
        by, _ = self.responses(self.records())
        self.assertEqual(by[NYG]["outcome"], "exchange_moved_before_trigger")

    def test_a_change_first_seen_after_detection_is_not_a_reaction(self):
        """Kalshi re-prices as the move is served: after the decision read,
        before we could act, first seen on the execution read. Clamped, that
        read as a follow within a second."""
        code, text, _ = self.paid(follow_on_execution=True)
        self.assertEqual(code, 0, text)
        by, figures = self.responses(self.records())
        self.assertEqual(by[NYG]["outcome"], "moved_around_trigger")
        low, high = by[NYG]["lag_seconds"]
        self.assertLess(low, 0)
        self.assertGreater(high, 0)


class LiveOddsUrlTest(unittest.TestCase):

    def test_a_recorded_url_carries_no_key(self):
        from data.odds_live import live_odds_url, recorded_url
        url = live_odds_url("NFL", KEY)
        self.assertIn(KEY, url)
        self.assertNotIn(KEY, recorded_url(url))
        self.assertEqual(recorded_url(url),
                         "https://api.the-odds-api.com/v4/sports/"
                         "americanfootball_nfl/odds")

    def test_the_live_poll_asks_for_the_archives_book_and_market(self):
        """One parser reads both only if both ask for the same thing."""
        from data.odds_history import build_snapshot_url
        from data.odds_live import live_odds_url
        live = urllib.parse.parse_qs(urllib.parse.urlparse(
            live_odds_url("NFL", KEY)).query)
        archive = urllib.parse.parse_qs(urllib.parse.urlparse(
            build_snapshot_url("NFL", T0, KEY)).query)
        for field in ("bookmakers", "markets", "oddsFormat"):
            self.assertEqual(live[field], archive[field], field)


class LiveFetchClockTest(unittest.TestCase):
    """`fetch_live_odds` keeps the request's clocks apart."""

    def test_the_receipt_waits_for_the_body(self):
        from data import odds_live
        clock = FakeClock()

        def slow():
            clock.t += timedelta(seconds=8)

        headers = {"Date": email.utils.format_datetime(T0, usegmt=True),
                   "x-requests-last": "1"}
        response = Response([], headers, on_read=slow)
        with mock.patch("urllib.request.urlopen", return_value=response):
            live = odds_live.fetch_live_odds(
                "NFL", KEY, ledger=CreditLedger(cap=1), now=clock.now)
        self.assertTrue(live.ok, live.coverage.reasons)
        self.assertLessEqual(live.sent_at, live.headers_at)
        self.assertEqual(live.received_at - live.headers_at,
                         timedelta(seconds=8))


class OrderBookParserTest(unittest.TestCase):
    """The transcribed shape, read strictly."""

    AT = datetime(2026, 9, 13, 20, tzinfo=UTC)

    def parse(self, payload):
        from data.kalshi_history import parse_orderbook
        return parse_orderbook(payload, ticker="T", received_at=self.AT)

    def test_the_yes_ask_is_one_minus_the_best_no_bid(self):
        book, coverage = self.parse({"orderbook_fp": {
            "yes_dollars": [["0.4400", "10"], ["0.4500", "120.00"]],
            "no_dollars": [["0.5000", "5"], ["0.5300", "80.00"]]}})
        self.assertTrue(coverage.complete)
        self.assertEqual((book.bid_close, book.ask_close), (0.45, 0.47))
        self.assertEqual((book.bid_size, book.ask_size), (120.0, 80.0))

    def test_an_empty_side_is_an_answer_and_leaves_no_price(self):
        book, coverage = self.parse({"orderbook_fp": {"yes_dollars": [],
                                                      "no_dollars": None}})
        self.assertTrue(coverage.complete)
        self.assertIsNone(book.mid)

    def test_an_unreadable_level_fails_coverage(self):
        book, coverage = self.parse({"orderbook_fp": {
            "yes_dollars": [["1.40", "10"]]}})
        self.assertFalse(coverage.complete)
        self.assertTrue(book.has_malformed_price)

    def test_an_unknown_shape_is_refused_not_read_as_empty(self):
        book, coverage = self.parse({"bids": []})
        self.assertIsNone(book)
        self.assertFalse(coverage.complete)

    def test_the_integer_cent_form_reads_the_same(self):
        book, coverage = self.parse({"orderbook": {"yes": [[45, 120]],
                                                   "no": [[53, 80]]}})
        self.assertTrue(coverage.complete)
        self.assertEqual((book.bid_close, book.ask_close), (0.45, 0.47))


if __name__ == "__main__":
    unittest.main()
