"""Outages: the monitor pauses, probes within bounds, and records the gap.

SYNTHETIC, like every session in these suites: the network and the clock
are fakes, and nothing here was observed. The failures are built the way
the standard library raises them -- a connect timeout is `URLError` around
the operating system's ETIMEDOUT, the shape the 2026-09-26 stop reported --
and each takes the time it would take on the clock the monitor runs on. No
test sleeps and no request leaves the process.

THE STORY, and what each test changes in it: the monitor's usual DAL at NYG
session (`test_shadow_monitor`), polling every 60s. Pinnacle moves NYG from
-140 to -320 at 20:05; Kalshi follows at 20:08.
"""

from __future__ import annotations

import errno
import http.client
import io
import json
import sys
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shadow_diagnostics as diag                                  # noqa: E402
import shadow_monitor                                              # noqa: E402
from data.odds_history import CreditLedger                         # noqa: E402
from tests.test_reaction_replay import EVENT                       # noqa: E402
from tests.test_shadow_monitor import (                            # noqa: E402
    KEY, MOVE_AT, NYG, T0, FakeClock, LiveNetwork, MonitorHarness, Response,
)

CADENCE = timedelta(seconds=60)
TIMEOUT = timedelta(seconds=30)


def timeout(takes: timedelta = TIMEOUT):
    """A connect timeout, as macOS reported one: it takes `takes`."""
    def fail(net, url):
        net.clock.t += takes
        raise urllib.error.URLError(OSError(errno.ETIMEDOUT,
                                            "Operation timed out"))
    return fail


def status(code: int, headers: dict | None = None, body: bytes = b""):
    def fail(net, url):
        raise urllib.error.HTTPError(url, code,
                                     http.client.responses.get(code, ""),
                                     headers or {}, io.BytesIO(body))
    return fail


def wrong_shape():
    def fail(net, url):
        return Response({"message": "not the documented list"},
                        {"x-requests-last": "1",
                         "x-requests-remaining": "9000"})
    return fail


class OutageNetwork(LiveNetwork):
    """The story's network, with odds failures scripted by the time a
    request is SENT: `script` is [(from, until, failure)]. `kalshi_down`
    windows time out every book read too, ten seconds each."""

    def __init__(self, clock, *, script=(), kalshi_down=(), **knobs):
        super().__init__(clock, **knobs)
        self.script = list(script)
        self.kalshi_down = list(kalshi_down)

    def _odds(self, url, at):
        for start, end, fail in self.script:
            if start <= at < end:
                self.odds_calls.append(at)
                return fail(self, url)
        return super()._odds(url, at)

    def _book(self, url, ticker, at):
        if any(start <= at < end for start, end in self.kalshi_down):
            self.book_calls.append((ticker, at))
            self.clock.t += timedelta(seconds=10)
            raise urllib.error.URLError(OSError(errno.ETIMEDOUT,
                                                "Operation timed out"))
        return super()._book(url, ticker, at)


class SleepyClock(FakeClock):
    """A clock that loses `naps` -- [(at, duration)] -- to a machine asleep:
    the first sleep that reaches `at` wakes `duration` later than asked."""

    def __init__(self, naps=()):
        super().__init__()
        self.naps = sorted(naps)

    def sleep(self, seconds):
        super().sleep(seconds)
        while self.naps and self.t >= self.naps[0][0]:
            _, duration = self.naps.pop(0)
            self.t += duration


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


class OutageHarness(MonitorHarness):

    def session(self, hours: float = 1.0, *, net=None, spend=None, **knobs):
        net = net or OutageNetwork(self.clock, **knobs)
        price = shadow_monitor.session_price(hours, CADENCE)
        code, text, net = self.run_monitor(
            "--hours", f"{hours:g}", "--spend", str(spend or price),
            "--api-key", KEY, net=net)
        self.code, self.text, self.net = code, text, net
        self.rows = self.records()
        return self.rows

    def status(self) -> dict:
        files = sorted(self.tmp.glob("shadow_*.status.json"))
        self.assertEqual(len(files), 1, files)
        return json.loads(files[0].read_text())

    def odds_rows(self):
        return self.kinds(self.rows, "odds")

    def failed(self):
        return [r for r in self.odds_rows() if r.get("failure")]


class BriefOutageTest(OutageHarness):
    """Three polls time out, 20:10-20:12. The fourth attempt is a probe a
    minute after the third failure, and it answers."""

    def setUp(self):
        super().setUp()
        self.session(script=[(at(10), at(13), timeout())])

    def test_it_recovers_and_runs_to_its_end(self):
        self.assertEqual(self.code, 0, self.text)
        end = self.end(self.rows)
        self.assertEqual(end["reason"], "end_of_session")
        self.assertTrue(end["ok"])
        self.assertEqual(end["status"], "recovered_with_gaps")
        self.assertEqual(len(self.kinds(self.rows, "outage_start")), 1)
        self.assertEqual(len(self.kinds(self.rows, "recovery_probe")), 1)

    def test_paid_polling_pauses_until_the_probe(self):
        third = [datetime.fromisoformat(r["sent_at"]) for r in self.failed()][2]
        probe = datetime.fromisoformat(
            self.kinds(self.rows, "recovery_probe")[0]["at"])
        ready = datetime.fromisoformat(
            self.kinds(self.rows, "outage_start")[0]["at"])
        self.assertEqual(probe - ready, shadow_monitor.RECOVERY_FIRST_BACKOFF)
        self.assertFalse([t for t in self.net.odds_calls if third < t < probe])
        # And the session's own grid resumes after it -- not a new grid from
        # the probe, which could add a poll the cap was never priced for.
        probe_poll, *after = [t for t in self.net.odds_calls if t > probe]
        self.assertNotEqual((probe_poll - self.net.odds_calls[0]) % CADENCE,
                            timedelta(0))
        self.assertGreater(len(after), 40)
        # A poll is sent after its tick's book reads, a fraction of a
        # second past the grid.
        first = self.net.odds_calls[0]
        for sent in after:
            self.assertLess((sent - first) % CADENCE, timedelta(seconds=1))
        self.assertEqual({round((b - a).total_seconds())
                          for a, b in zip(after, after[1:])}, {60})

    def test_the_gap_is_recorded_from_the_last_answer_to_the_first(self):
        gap, = self.kinds(self.rows, "gap")
        self.assertEqual(gap["cause"], "outage")
        self.assertTrue(gap["recovered"])
        self.assertEqual((gap["failed_polls"], gap["probes"]), (3, 1))
        answered = [datetime.fromisoformat(r["ready_at"])
                    for r in self.odds_rows() if not r.get("failure")]
        first_fail = datetime.fromisoformat(self.failed()[0]["sent_at"])
        before = max(t for t in answered if t < first_fail)
        after = min(t for t in answered if t > first_fail)
        self.assertEqual(datetime.fromisoformat(gap["from"]), before)
        self.assertEqual(datetime.fromisoformat(gap["to"]), after)
        self.assertEqual(gap["categories"], {"timeout": 3})

    def test_each_failure_is_described(self):
        rows = self.failed()
        self.assertEqual([r["failure"]["attempt"] for r in rows], [1, 2, 3])
        last_answer = rows[0]["failure"]["last_answer_at"]
        for row in rows:
            failure = row["failure"]
            self.assertEqual(failure["category"], "timeout")
            self.assertEqual(failure["phase"], "request")
            self.assertFalse(failure["terminal"])
            self.assertEqual(failure["errno"], errno.ETIMEDOUT)
            self.assertAlmostEqual(failure["elapsed_seconds"],
                                   TIMEOUT.total_seconds() + 0.2)
            self.assertEqual(failure["last_answer_at"], last_answer)
            self.assertIsNone(failure["probe"])

    def test_the_slate_is_rejoined_on_the_first_answer_after_it(self):
        """The hourly rejoin was not due; the outage makes it due, since a
        join older than the outage may have missed a flexed kickoff."""
        kinds = [r["kind"] for r in self.rows]
        self.assertEqual(kinds.count("join"), 2)
        # The gap closes on the probe's answer; the rejoin follows that
        # answer's moves, before the next poll.
        gap = kinds.index("gap")
        self.assertEqual(kinds[gap - 1], "odds")
        self.assertLess(kinds.index("join", gap), kinds.index("odds", gap))

    def test_every_attempt_is_reserved_against_the_one_cap(self):
        end = self.end(self.rows)
        self.assertEqual(end["credits_reserved"], len(self.net.odds_calls))
        cap = shadow_monitor.session_price(1.0, CADENCE)
        self.assertLessEqual(end["credits_reserved"], cap)
        self.assertEqual(self.status()["credits"],
                         {"cap": cap, "reserved": len(self.net.odds_calls),
                          "account_remaining": 9000})

    def test_the_end_is_the_authorized_end(self):
        start = self.kinds(self.rows, "session_start")[0]
        self.assertEqual(self.end(self.rows)["at"], start["ends"])

    def test_the_status_file(self):
        status = self.status()
        self.assertEqual(status["status"], "recovered_with_gaps")
        self.assertEqual(status["polls"]["failed"], 3)
        self.assertEqual(status["polls"]["recovery_probes"], 1)
        self.assertEqual(status["gaps"]["outages"], 1)
        self.assertEqual(status["gaps"]["outages_recovered"], 1)
        self.assertAlmostEqual(status["gaps"]["blind_seconds"],
                               self.kinds(self.rows, "gap")[0]["seconds"])
        self.assertAlmostEqual(status["actual"]["share_of_authorized"], 1.0)
        self.assertTrue(status["paths"]["records"].endswith(".jsonl"))
        self.assertFalse(list(self.tmp.glob("*.tmp")))
        self.assertIn("recovered_with_gaps", self.text)

    def test_the_report_lays_out_the_chronology(self):
        figures = shadow_monitor.report(self.rows)
        self.assertEqual(figures["session"]["status"], "recovered_with_gaps")
        run, = figures["failures"]["runs"]
        self.assertTrue(run["outage"])
        self.assertEqual(run["ended"], "answered")
        self.assertEqual([a["category"] for a in run["attempts"]],
                         ["timeout"] * 3)
        self.assertGreater(run["kalshi_reads"], 0)
        self.assertEqual(run["kalshi_failed"], 0)
        rendered = shadow_monitor.render_report(figures)
        self.assertIn("FAILED POLLS AND GAPS", rendered)
        self.assertIn("an OUTAGE", rendered)
        self.assertIn("timeout, phase request after 30.2s", rendered)
        self.assertIn("recovered_with_gaps", rendered)

    def test_the_key_is_in_no_record_and_no_output(self):
        self.assertNotIn(KEY, self.text)
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())


class PathEvidenceTest(OutageHarness):
    """Kalshi times out in the same minutes as the odds provider. The
    chronology puts the two side by side -- evidence about the path, which
    it reports without calling it a cause -- and each failed book read is
    described as the polls are."""

    def setUp(self):
        super().setUp()
        self.session(script=[(at(10), at(13), timeout())],
                     kalshi_down=[(at(10), at(13))])

    def test_the_failed_book_reads_are_described(self):
        failed = [b for b in self.kinds(self.rows, "book") if b.get("failure")]
        self.assertTrue(failed)
        for book in failed:
            self.assertEqual(book["failure"]["category"], "timeout")
            self.assertEqual(book["failure"]["errno"], errno.ETIMEDOUT)
            self.assertAlmostEqual(book["failure"]["elapsed_seconds"], 10.2)

    def test_the_chronology_shows_both(self):
        run, = shadow_monitor.report(self.rows)["failures"]["runs"]
        self.assertGreater(run["kalshi_failed"], 0)
        rendered = shadow_monitor.render_report(
            shadow_monitor.report(self.rows))
        self.assertIn(f"Kalshi reads while it lasted: {run['kalshi_reads']}, "
                      f"{run['kalshi_failed']} failed", rendered)
        self.assertIn("not a cause", rendered)

    def test_it_still_recovers(self):
        self.assertEqual(self.end(self.rows)["status"], "recovered_with_gaps")


class PersistentOutageTest(OutageHarness):
    """Nothing answers from 20:02 on, in a three-hour session: the defaults,
    unpatched, bound the outage."""

    def setUp(self):
        super().setUp()
        self.session(3.0, script=[(at(2), at(600), timeout())])

    def test_it_stops_after_its_probes_recorded_not_looping(self):
        self.assertEqual(self.code, 1, self.text)
        end = self.end(self.rows)
        self.assertEqual(end["reason"], "outage_unrecovered")
        self.assertEqual(end["status"], "stopped_early")
        self.assertIn("all 6 recovery probes failed", end["detail"])
        self.assertIn("timeout", end["detail"])
        probes = self.kinds(self.rows, "recovery_probe")
        self.assertEqual(len(probes), shadow_monitor.RECOVERY_MAX_PROBES)
        self.assertEqual(len(self.net.odds_calls[2:]),
                         shadow_monitor.OUTAGE_AFTER_FAILED_POLLS
                         + shadow_monitor.RECOVERY_MAX_PROBES)

    def test_the_backoff_doubles_to_its_cap(self):
        scheduled = self.kinds(self.rows, "recovery_scheduled")
        self.assertEqual([s["backoff_seconds"] for s in scheduled],
                         [60, 120, 240, 480, 600, 600])
        # Each probe went when it was due, measured from the failure before.
        probes = self.kinds(self.rows, "recovery_probe")
        self.assertEqual([p["at"] for p in probes],
                         [s["due"] for s in scheduled])

    def test_the_outage_ended_inside_its_bound_and_long_before_the_end(self):
        gap, = self.kinds(self.rows, "gap")
        self.assertFalse(gap["recovered"])
        first = datetime.fromisoformat(gap["first_failure_at"])
        stopped = datetime.fromisoformat(self.end(self.rows)["at"])
        self.assertLessEqual(stopped - first,
                             shadow_monitor.RECOVERY_MAX_OUTAGE)
        status = self.status()
        self.assertLess(status["actual"]["share_of_authorized"], 0.3)
        self.assertEqual(status["gaps"]["outages_recovered"], 0)

    def test_no_request_after_the_stop(self):
        stopped = datetime.fromisoformat(self.end(self.rows)["at"])
        self.assertFalse([t for t in self.net.odds_calls if t > stopped])


class BoundsTest(OutageHarness):

    def test_the_duration_bound_can_come_first(self):
        """Probes that each take as long as a machine asleep: the 45-minute
        bound stops the outage before its six probes are spent."""
        self.session(3.0, script=[(at(2), at(600), timeout(
            timedelta(minutes=9)))])
        self.assertEqual(self.code, 1, self.text)
        end = self.end(self.rows)
        self.assertEqual(end["reason"], "outage_unrecovered")
        self.assertIn("beyond the 45-minute bound", end["detail"])
        self.assertLess(len(self.kinds(self.rows, "recovery_probe")),
                        shadow_monitor.RECOVERY_MAX_PROBES)

    def test_the_default_probes_fit_inside_the_default_duration(self):
        """Six probes at the declared backoff -- plus the three failures
        that start it -- land well inside the 45 minutes, so the probe
        count is the bound in the ordinary case."""
        waits = [shadow_monitor.recovery_backoff(n, timedelta(seconds=30))
                 for n in range(shadow_monitor.RECOVERY_MAX_PROBES)]
        self.assertEqual([w.total_seconds() for w in waits],
                         [60, 120, 240, 480, 600, 600])
        worst = (sum(waits, timedelta()) + 3 * timedelta(seconds=30 + 30)
                 + shadow_monitor.RECOVERY_MAX_PROBES * TIMEOUT)
        self.assertLess(worst, shadow_monitor.RECOVERY_MAX_OUTAGE)

    def test_recovery_never_polls_faster_than_the_cadence(self):
        self.assertEqual(shadow_monitor.recovery_backoff(
            0, timedelta(minutes=5)), timedelta(minutes=5))

    def test_the_cap_is_checked_before_a_probe(self):
        """A cap with room for the session's two answered polls, its three
        failures and one probe: the second probe is never sent, or even
        scheduled -- no wait for a request that could not be paid for."""
        net = OutageNetwork(self.clock, script=[(at(2), at(600), timeout())])
        recorder = shadow_monitor.Recorder(self.tmp / "shadow_cap.jsonl")
        monitor = shadow_monitor.ShadowMonitor(
            sport="NFL", series="KXNFLGAME", api_key=KEY, cadence=CADENCE,
            hours=3.0, ledger=CreditLedger(cap=6), recorder=recorder,
            clock=self.clock)
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                mock.patch("time.sleep"):
            stop = monitor.run()
        recorder.close()
        self.assertEqual(stop.reason, "credit_cap")
        self.assertIn("no credit left for a recovery probe", stop.detail)
        self.assertEqual(len(net.odds_calls), 6)
        self.assertEqual(monitor.ledger.spent_this_run, 6)
        rows, _ = shadow_monitor.read_records(self.tmp / "shadow_cap.jsonl")
        self.assertEqual(len(self.kinds(rows, "recovery_probe")), 1)
        self.assertEqual(len(self.kinds(rows, "recovery_scheduled")), 1)
        # Stopped when the probe failed, not after waiting for the next.
        self.assertEqual(self.end(rows)["at"], self.failed_in(rows)[-1])
        status = json.loads((self.tmp / "shadow_cap.status.json").read_text())
        self.assertEqual(status["status"], "stopped_early")
        self.assertEqual(status["credits"]["reserved"], 6)

    def failed_in(self, rows):
        return [r["ready_at"] for r in self.kinds(rows, "odds")
                if r.get("failure")]

    def test_the_deadline_is_never_extended_by_an_outage(self):
        """Failures from 20:20 in a 30-minute session: the third probe would
        fall after the authorized end, so the session ends AT its end, in
        the outage, having made no request after it."""
        self.session(0.5, script=[(at(20), at(600), timeout())])
        self.assertEqual(self.code, 1, self.text)
        end = self.end(self.rows)
        start = self.kinds(self.rows, "session_start")[0]
        self.assertEqual(end["at"], start["ends"])
        self.assertEqual(end["reason"], "end_of_session")
        self.assertFalse(end["ok"])
        self.assertEqual(end["status"], "ended_in_outage")
        self.assertIn("unrecovered outage", end["detail"])
        ends = datetime.fromisoformat(start["ends"])
        self.assertFalse([t for t in self.net.odds_calls if t >= ends])
        scheduled = self.kinds(self.rows, "recovery_scheduled")
        self.assertGreater(datetime.fromisoformat(scheduled[-1]["due"]), ends)
        self.assertEqual(self.status()["status"], "ended_in_outage")


class TerminalFailureTest(OutageHarness):
    """Waiting cannot mend these, and each further poll costs a credit."""

    def assertStopsAtOnce(self, reason, fail, polls=1):
        self.session(script=[(at(2), at(600), fail)])
        self.assertEqual(self.code, 1, self.text)
        end = self.end(self.rows)
        self.assertEqual(end["reason"], reason)
        self.assertEqual(end["status"], "stopped_early")
        self.assertEqual(len(self.net.odds_calls[2:]), polls)
        self.assertFalse(self.kinds(self.rows, "recovery_probe"))
        self.assertEqual(self.status()["reason"], reason)
        return end

    def test_a_refused_key(self):
        end = self.assertStopsAtOnce("auth_refused", status(
            401, body=b'{"message": "API key is not valid"}'))
        self.assertIn("HTTP 401", end["detail"])
        self.assertIn("API key is not valid", end["detail"])

    def test_an_account_out_of_credit(self):
        self.assertStopsAtOnce("auth_refused", status(403))

    def test_a_rejected_request(self):
        self.assertStopsAtOnce("request_rejected", status(404))

    def test_an_unexpected_shape(self):
        self.assertStopsAtOnce("unexpected_shape", wrong_shape())

    def test_a_refused_key_during_an_outage_stops_the_recovery(self):
        self.session(script=[(at(2), at(5), timeout()),
                             (at(5), at(600), status(401))])
        self.assertEqual(self.end(self.rows)["reason"], "auth_refused")
        self.assertEqual(len(self.kinds(self.rows, "recovery_probe")), 1)
        self.assertEqual(self.failed()[-1]["failure"]["probe"], 1)

    def test_an_error_body_that_echoes_the_key_is_scrubbed(self):
        self.session(script=[(at(2), at(600), status(
            401, body=f'{{"message": "bad key {KEY}"}}'.encode()))])
        self.assertNotIn(KEY, self.text)
        for path in self.tmp.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY, path.read_text())


class RateLimitTest(OutageHarness):

    def test_a_retry_after_is_obeyed(self):
        """One 429 asking for five minutes: nothing is polled before them,
        and the stretch without polls is a gap like any other."""
        self.session(script=[(at(3), at(3.5), status(
            429, {"Retry-After": "300"}))])
        self.assertEqual(self.code, 0, self.text)
        limited = self.failed()[0]
        self.assertEqual(limited["failure"]["category"], "rate_limited")
        received = datetime.fromisoformat(limited["received_at"])
        later = [t for t in self.net.odds_calls if t > received]
        self.assertGreaterEqual(later[0] - received, timedelta(seconds=300))
        causes = {g["cause"] for g in self.kinds(self.rows, "gap")}
        self.assertEqual(causes, {"failed_polls", "not_polled"})

    def test_a_retry_after_beyond_the_outage_bound_stops(self):
        self.session(script=[(at(3), at(600), status(
            429, {"Retry-After": "7200"}))])
        self.assertEqual(self.end(self.rows)["reason"], "rate_limited")
        self.assertEqual(len(self.net.odds_calls[3:]), 1)


class NoMoveAcrossAnOutageTest(OutageHarness):
    """Pinnacle moves at 20:05, inside the outage. Nobody saw it move, so
    the first answer after recovery -- already moved -- is a new baseline,
    never a move measured across the blind interval."""

    def setUp(self):
        super().setUp()
        self.session(script=[(at(3), at(7), timeout())],
                     extra_pre_match=True)

    def test_the_move_inside_the_outage_is_not_a_move(self):
        self.assertEqual(self.code, 0, self.text)
        self.assertEqual([t for t in self.kinds(self.rows, "trigger")
                          if t["event_id"] == EVENT], [])
        self.assertTrue(self.net.moved_served)

    def test_the_first_answer_after_recovery_rebuilds_the_baseline(self):
        kinds = [r["kind"] for r in self.rows]
        gap = kinds.index("gap")
        after = next(r for r in self.rows[gap:] if r["kind"] == "detector")
        self.assertGreaterEqual(
            after["rejections"].get("baseline_invalidated", 0), 2)

    def test_moves_are_detected_again_after_recovery(self):
        recovered = datetime.fromisoformat(self.kinds(self.rows, "gap")[0]["to"])
        later = [t for t in self.kinds(self.rows, "trigger")
                 if datetime.fromisoformat(t["detected_at"]) > recovered]
        self.assertTrue(later)

    def test_the_offline_rebuild_agrees(self):
        figures = diag.diagnose(self.rows)
        self.assertEqual(figures["coverage"]["disagreements"], [])


class NoMoveAcrossASleepTest(OutageHarness):
    """The machine sleeps for twenty minutes across the 20:05 move, and the
    network is fine when it wakes. The first poll on waking must not be
    compared with the price from before it slept."""

    def setUp(self):
        super().setUp()
        self.clock = SleepyClock(naps=[(at(3.5), timedelta(minutes=20))])
        self.session()

    def test_the_move_across_the_sleep_is_not_a_move(self):
        self.assertEqual(self.code, 0, self.text)
        self.assertEqual(self.kinds(self.rows, "trigger"), [])

    def test_the_stretch_not_polled_is_a_gap(self):
        gap, = self.kinds(self.rows, "gap")
        self.assertEqual(gap["cause"], "not_polled")
        self.assertGreater(gap["seconds"], 20 * 60)
        self.assertEqual(gap["limit_seconds"], 180)
        self.assertTrue(self.kinds(self.rows, "books_invalidated"))
        kinds = [r["kind"] for r in self.rows]
        # Declared before the poll that ended it, so every reader meets it
        # first.
        self.assertEqual(kinds[kinds.index("gap") + 2], "odds")
        self.assertEqual(self.end(self.rows)["status"], "recovered_with_gaps")

    def test_the_slate_is_rejoined_after_it(self):
        kinds = [r["kind"] for r in self.rows]
        self.assertEqual(kinds.count("join"), 2)
        self.assertGreater(kinds.index("join", kinds.index("gap")),
                           kinds.index("gap"))

    def test_the_report_does_not_time_across_it(self):
        figures = shadow_monitor.report(self.rows)
        self.assertEqual(figures["sightings"]["unpolled_gaps"], 1)
        self.assertIn("max 60.0s", figures["provider_refresh"])
        self.assertEqual(len(figures["failures"]["unpolled"]), 1)


class SleepAfterAMoveTest(OutageHarness):
    """The move is seen at 20:05; the machine then sleeps twenty minutes.
    Every markout inside the sleep is censored, on both sides: no Kalshi
    read to exit at, and no poll to say whether the move still stood --
    never the last price carried across."""

    def setUp(self):
        super().setUp()
        self.clock = SleepyClock(naps=[(at(6.5), timedelta(minutes=20))])
        self.session()
        self.figures = diag.diagnose(self.rows)

    def markouts(self):
        item = next(a for a in self.figures["assessments"]
                    if a.get("adjustment_capture"))
        return {m["seconds"]: m
                for m in item["adjustment_capture"][0]["markouts"]}

    def test_the_move_before_the_sleep_is_still_a_move(self):
        self.assertEqual(len(self.kinds(self.rows, "trigger")), 1)

    def test_markouts_inside_the_sleep_are_censored_both_ways(self):
        gap, = self.kinds(self.rows, "gap")
        start = datetime.fromisoformat(gap["unobserved_from"])
        end = datetime.fromisoformat(gap["to"])
        inside = [m for m in self.markouts().values()
                  if start < datetime.fromisoformat(m["target"]) <= end]
        self.assertEqual(len(inside), 4)
        for markout in inside:
            self.assertEqual(markout["sharp"]["status"], "unobservable")
            self.assertEqual(markout["sharp"]["because"], "not_polled")
            self.assertIsNotNone(markout.get("censored"))

    def test_markouts_either_side_of_it_are_judged(self):
        markouts = self.markouts()
        for seconds in (30.0, 60.0, 1800.0):
            self.assertNotEqual(markouts[seconds]["sharp"].get("because"),
                                "not_polled", seconds)


class StaleBookTest(OutageHarness):
    """After an outage the decision memory is dropped: a decision whose
    fresh reads failed has no decision book, not one from before it."""

    def setUp(self):
        super().setUp()
        # Polls 20:01-20:03 time out; the probe at 20:04:3x answers. Every
        # book read from the probe until after the 20:05 move fails, so the
        # only books in memory at the move would be pre-outage ones.
        self.session(script=[(at(1), at(4), timeout())],
                     fail_books_during=(at(4), at(5.5)))

    def test_the_move_is_screened_without_a_stale_book(self):
        decisions = [d for d in self.kinds(self.rows, "decision")
                     if d.get("market_ticker")]
        self.assertTrue(decisions)
        for decision in decisions:
            self.assertEqual(decision["refusal"],
                             "no_exchange_book_at_the_trigger")
            self.assertIsNone(decision.get("book"))

    def test_the_memory_is_dropped_before_the_probes_reads(self):
        kinds = [r["kind"] for r in self.rows]
        probe = kinds.index("recovery_probe")
        self.assertEqual(kinds[probe + 1], "books_invalidated")
        self.assertGreater(self.rows[probe + 1]["dropped"], 0)

    def test_the_offline_rebuild_drops_it_too(self):
        figures = diag.diagnose(self.rows)
        self.assertEqual(figures["coverage"]["disagreements"], [])
        self.assertEqual(
            figures["coverage"]["refusals"],
            {"no_exchange_book_at_the_trigger": len(
                [d for d in self.kinds(self.rows, "decision")
                 if d.get("market_ticker")])})


class InterruptAndCrashTest(OutageHarness):

    def test_an_interrupt_writes_a_status(self):
        net = OutageNetwork(self.clock, interrupt_on_poll=5)
        self.session(net=net)
        self.assertEqual(self.status()["status"], "stopped_early")
        self.assertEqual(self.status()["reason"], "interrupted")

    def test_a_crash_writes_a_status_and_is_still_raised(self):
        net = OutageNetwork(self.clock)
        with mock.patch.object(shadow_monitor.ShadowMonitor, "follow_due",
                               side_effect=ValueError("boom")):
            with self.assertRaises(ValueError):
                self.session(net=net)
        status = self.status()
        self.assertEqual(status["reason"], "crashed")
        self.assertIn("ValueError: boom", status["detail"])
        rows, _ = shadow_monitor.read_records(
            sorted(self.tmp.glob("shadow_*.jsonl"))[0])
        self.assertEqual(self.end(rows)["reason"], "crashed")

    def test_a_complete_session_says_so(self):
        self.session(0.5)
        self.assertEqual(self.status()["status"], "complete")
        self.assertEqual(self.status()["gaps"]["count"], 0)


class LegacySessionTest(OutageHarness):
    """A session recorded before failures were classified still reports:
    its chronology from its clocks and reasons, labelled as inferred, and
    its status derived rather than invented."""

    def test_a_legacy_session_is_described_from_its_messages(self):
        self.session(script=[(at(10), at(12), timeout())])
        legacy = []
        for row in self.rows:
            if row["kind"] in ("gap", "outage_start", "recovery_probe",
                               "recovery_scheduled", "books_invalidated"):
                continue
            row = {k: v for k, v in row.items()
                   if k not in ("failure", "status", "gaps")}
            legacy.append(row)
        figures = shadow_monitor.report(legacy)
        self.assertEqual(figures["session"]["status"], "recovered_with_gaps")
        self.assertIn("derived", figures["session"]["status_source"])
        run, = figures["failures"]["runs"]
        for attempt in run["attempts"]:
            self.assertEqual(attempt["category"], "timeout")
            self.assertEqual(attempt["phase"], "request")
            self.assertTrue(attempt["inferred"])
        self.assertIn("inferred from the message",
                      shadow_monitor.render_report(figures))


if __name__ == "__main__":
    import unittest
    unittest.main()
