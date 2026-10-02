"""The research channels in the live monitor and in `--report`.

SYNTHETIC, like every session in these suites: the network and the clock
are fakes, and nothing here was observed. The development session
(`tests/synthetic_research_session.py`) is shaped on the owner's 2026-10-01
audit -- PIT-CLE drifting 1.1336pp in sub-point steps, LAR-PHI coming back
1.5781pp away after an HTTP 200 answer with `bookmakers: []`, NYG-ARI moving
beyond the 73h horizon -- and everything else in it is invented.

What these pin, beyond the channels' own rules (`test_research_channels`):

* the ADJACENT path is untouched: every record it writes is identical, byte
  for byte, with the channels on and off -- on the 2026-09-24 synthetic
  session and on this one, whose research reads number in the hundreds;
* a research read is never a decision book, never leaves at or after the
  authorized end, never starts within its guard of the next paid request,
  and costs no credit;
* the declared read bounds hold when a whole slate returns at once;
* a failure inside the channels stops the channels, never the session;
* `--report` rebuilds the live signals from the raw polls, says when a
  session never ran the channels, and censors research markouts at kickoff
  and at the session's end.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shadow_monitor                                              # noqa: E402
from reaction.research import DRIFT, RETURN, ResearchChannels      # noqa: E402
from shadow_diagnostics import replay as diagnostics_replay       # noqa: E402
from shadow_research import analyse, render_research               # noqa: E402
from tests import synthetic_research_session as dev                # noqa: E402
from tests import synthetic_session                                # noqa: E402

UTC = timezone.utc
RESEARCH_KINDS = {"research_signal", "research_execution", "research_error"}


def rows_of(path: Path) -> list[dict]:
    rows, bad = shadow_monitor.read_records(path)
    assert bad == 0
    return rows


def adjacent_records(rows: list[dict]) -> list[dict]:
    """The records the adjacent path writes, as it would write them with
    the channels off: research records and keys removed, and the commit
    (which differs between checkouts, not between runs) set aside."""
    out = []
    for row in rows:
        row = json.loads(json.dumps(row))
        if row["kind"] in RESEARCH_KINDS:
            continue
        if (row["kind"] == "book"
                and row.get("purpose") in shadow_monitor.RESEARCH_PURPOSES):
            continue
        row.pop("research", None)
        out.append(row)
    return out


def report_text(path: Path) -> str:
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = shadow_monitor.main(["--report", str(path)])
    assert code == 0, out.getvalue()
    return out.getvalue()


class SessionCase(unittest.TestCase):
    """Runs each scripted session once per class, on and off."""

    run_off = False
    session = staticmethod(dev.run_session)

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.path, cls.net, _ = cls.session(cls.tmp / "on")
        cls.rows = rows_of(cls.path)
        if cls.run_off:
            with mock.patch.object(shadow_monitor, "RESEARCH_ENABLED", False):
                cls.off_path, cls.off_net, _ = cls.session(cls.tmp / "off")
            cls.off_rows = rows_of(cls.off_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def kinds(self, kind: str, rows: list[dict] | None = None) -> list[dict]:
        return [r for r in (rows if rows is not None else self.rows)
                if r["kind"] == kind]

    def research_books(self) -> list[dict]:
        return [r for r in self.kinds("book")
                if r.get("purpose") in shadow_monitor.RESEARCH_PURPOSES]


class AdjacentUnchangedOn0924Test(SessionCase):
    """The 2026-09-24 synthetic session: five adjacent moves, every one of
    which the drift channel also sees -- and shares the reads of."""

    run_off = True

    @staticmethod
    def session(out_dir):
        return synthetic_session.run_session(out_dir)

    def test_every_adjacent_record_is_identical(self):
        self.assertEqual(adjacent_records(self.rows),
                         adjacent_records(self.off_rows))
        self.assertEqual(self.net.book_calls, self.off_net.book_calls)
        self.assertEqual(self.net.odds_calls, self.off_net.odds_calls)

    def test_the_overlapping_episodes_cost_no_read(self):
        signals = self.kinds("research_signal")
        self.assertEqual(len(signals), 5)
        self.assertTrue(all(s["adjacent_trigger_on_this_quote"]
                            for s in signals))
        self.assertEqual(self.research_books(), [])

    def test_the_adjacent_report_is_unchanged_but_for_the_file_itself(self):
        on = report_text(self.path).split("\nRESEARCH CHANNELS")[0]
        off = report_text(self.off_path).split("\nRESEARCH CHANNELS")[0]

        def stable(text: str) -> list[str]:
            # The source line names the file's own record count and hash.
            return [line for line in text.splitlines()
                    if not line.strip().startswith(("source ", "code "))]

        self.assertEqual(stable(on), stable(off))


class DevelopmentSessionTest(SessionCase):
    """The two behaviours the 2026-10-01 audit found, on the live monitor."""

    run_off = True

    def signal(self, channel: str, event: str) -> dict:
        found = [s for s in self.kinds("research_signal")
                 if s["channel"] == channel and s["event_id"] == event]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_every_adjacent_record_is_identical_despite_the_reads(self):
        self.assertGreater(len(self.research_books()), 100)
        self.assertEqual(adjacent_records(self.rows),
                         adjacent_records(self.off_rows))

    def test_the_drift_is_seen_without_any_adjacent_move(self):
        drift = self.signal(DRIFT, "evt-pit-cle")
        self.assertAlmostEqual(drift["delta"]["home"], -0.0113359708,
                               places=9)
        self.assertLess(drift["window"]["largest_single_step"], 0.01)
        self.assertEqual([t for t in self.kinds("trigger")
                          if t["event_id"] == "evt-pit-cle"], [])

    def test_the_return_after_an_empty_bookmakers_answer(self):
        signal = self.signal(RETURN, "evt-lar-phi")
        self.assertAlmostEqual(signal["delta"]["home"], -0.0157813457,
                               places=9)
        self.assertEqual(signal["gap"]["first_cause"],
                         "declared:absent_from_answer")
        # The answer it was absent from was an HTTP 200 carrying the event,
        # with no sharp quote in it.
        empty = [r for r in self.kinds("odds")
                 if any(e.get("id") == "evt-lar-phi"
                        and e.get("bookmakers") == []
                        for e in r.get("payload") or [])]
        self.assertEqual(len(empty), 1)
        self.assertEqual(empty[0]["status"], 200)
        self.assertEqual([t for t in self.kinds("trigger")
                          if t["event_id"] == "evt-lar-phi"], [])
        # The adjacent detector re-anchored, as it must.
        refused = {}
        for row in self.kinds("detector"):
            for key, n in (row.get("rejections") or {}).items():
                refused[key] = refused.get(key, 0) + n
        self.assertGreaterEqual(refused.get("baseline_invalidated", 0), 1)

    def test_research_is_never_an_entry_or_a_decision(self):
        self.assertEqual(self.kinds("decision"), self.kinds("decision",
                                                            self.off_rows))
        decided = {d["market_ticker"] for d in self.kinds("decision")}
        self.assertTrue(all("NYGARI" in t for t in decided), decided)
        self.assertEqual(self.kinds("session_end")[0]["counts"]["entries"], 0)

    def test_a_fresh_quote_is_read_after_the_signal_was_ready(self):
        for signal in self.kinds("research_signal"):
            reads = [b for b in self.research_books()
                     if b.get("research_episode") == signal["episode"]["id"]]
            if signal["event_id"] == "evt-nyg-ari":
                self.assertEqual(reads, [])     # beyond the horizon
                continue
            self.assertEqual(len(reads), 2)
            for read in reads:
                self.assertGreaterEqual(read["sent_at"],
                                        signal["detected_at"])

    def test_beyond_the_horizon_nothing_is_read_for_research(self):
        execution = [r for r in self.kinds("research_execution")
                     if r["event_id"] == "evt-nyg-ari"]
        self.assertEqual(len(execution), 1)
        self.assertEqual(set(execution[0]["reads"].values()),
                         {"outside_observation_horizon"})
        self.assertTrue(execution[0]["follow"].startswith("none"))

    def test_no_research_read_is_ever_a_decision_book(self):
        memory = diagnostics_replay(self.rows).memory
        research_times = {(b["ticker"], b["received_at"])
                          for b in self.research_books()}
        for ticker, books in memory.items():
            for book in books:
                self.assertNotIn((ticker, book.ts.isoformat()),
                                 research_times)

    def test_no_research_read_reaches_the_next_paid_request(self):
        polls = sorted(r["sent_at"] for r in self.kinds("odds"))
        guard = shadow_monitor.RESEARCH_READ_GUARD
        for read in self.research_books():
            sent = datetime.fromisoformat(read["sent_at"])
            later = [p for p in polls if p > read["sent_at"]]
            if later:
                self.assertGreaterEqual(
                    datetime.fromisoformat(later[0]) - sent, guard, read)

    def test_research_costs_no_credit(self):
        on, off = self.kinds("session_end")[0], self.kinds(
            "session_end", self.off_rows)[0]
        self.assertEqual(on["credits_reserved"], off["credits_reserved"])
        self.assertEqual(len(self.net.odds_calls), len(self.off_net.odds_calls))

    def test_the_load_is_disclosed(self):
        start = self.kinds("session_start")[0]
        bounds = start["research"]["reads"]
        self.assertEqual(bounds["max_games_per_answer"], 4)
        self.assertEqual(bounds["max_followed_games"], 4)
        status = json.loads(self.path.with_suffix(".status.json").read_text())
        self.assertEqual(status["research"]["counts"]["book_reads"],
                         len(self.research_books()))
        self.assertEqual(status["polls"], json.loads(
            self.off_path.with_suffix(".status.json").read_text())["polls"])

    def test_the_report_rebuilds_what_the_session_recorded(self):
        figures = analyse(self.rows)
        self.assertTrue(figures["inputs_check"]["agrees"])
        self.assertTrue(figures["recorded_check"]["agrees"])
        self.assertEqual(figures["recorded_check"]["recorded"], 3)
        self.assertEqual(figures[DRIFT]["episodes"], 2)
        self.assertEqual(figures[RETURN]["episodes"], 1)
        self.assertEqual(figures[DRIFT]["overlapping_adjacent_trigger"], 1)
        text = render_research(figures)
        self.assertIn("recorded signals   3 recorded, 3 rebuilt: agree", text)

    def test_each_episode_is_one_game_with_its_mirror_beside_it(self):
        figures = analyse(self.rows)
        [item] = figures[RETURN]["items"]
        roles = sorted(c["role"] for c in item["contracts"])
        self.assertEqual(roles, ["mirror", "primary"])
        summary = figures[RETURN]["capture_primary"]["research_return"]
        self.assertEqual((summary["assessments"], summary["games"]), (1, 1))
        primary = next(c for c in item["contracts"] if c["role"] == "primary")
        self.assertEqual(primary["favoured_side"], "YES")
        self.assertTrue(primary["ticker"].endswith("-LAR"))
        # Entered on the fresh quote: the research read, after the signal.
        entry = primary["capture"]["entry"]
        self.assertGreaterEqual(entry["delay_after_move_seconds"], 0.0)
        self.assertLess(entry["delay_after_move_seconds"], 1.0)
        self.assertIsNotNone(entry["depth"])
        # The counterfactual is labelled and is never an admission.
        screened = primary["counterfactual_screen"]
        self.assertIn("COUNTERFACTUAL", screened["label"])
        self.assertEqual(screened["priced_at"], entry["at"])
        self.assertIn("includes the unobserved interval",
                      primary["exchange_before"]["note"])

    def test_without_the_channels_the_report_still_finds_both(self):
        """A session recorded before the channels existed -- the owner's
        2026-10-01 file is one -- gets them from its raw polls, as
        DEVELOPMENT evidence, entered at its decision reads."""
        figures = analyse(self.off_rows)
        self.assertFalse(figures["ran_live"])
        self.assertIn("DEVELOPMENT", figures["evidence"])
        self.assertEqual(
            [(i["signal"]["channel"], i["signal"]["event_id"])
             for c in (DRIFT, RETURN) for i in figures[c]["items"]],
            [(DRIFT, "evt-nyg-ari"), (DRIFT, "evt-pit-cle"),
             (RETURN, "evt-lar-phi")])
        live = analyse(self.rows)
        for channel in (DRIFT, RETURN):
            self.assertEqual(
                [i["signal"]["delta"] for i in figures[channel]["items"]],
                [i["signal"]["delta"] for i in live[channel]["items"]])
        [item] = figures[RETURN]["items"]
        primary = next(c for c in item["contracts"] if c["role"] == "primary")
        self.assertGreater(primary["capture"]["entry"]
                           ["delay_after_move_seconds"], 10.0)


class EndOfSessionTest(SessionCase):
    """The session ends four minutes after the return: research follow
    reads stop before the end, and the markouts past it are censored."""

    @staticmethod
    def session(out_dir):
        hours = ((dev.LAR_PHI_RETURN + timedelta(minutes=4) - dev.T0)
                 .total_seconds() / 3600)
        return dev.run_session(out_dir, hours=hours)

    def test_no_research_read_at_or_after_the_end(self):
        start = self.kinds("session_start")[0]
        ends = start["ends"]
        books = self.research_books()
        self.assertTrue(books)
        self.assertTrue(all(b["sent_at"] < ends for b in books))
        end = self.kinds("session_end")[0]
        self.assertEqual(end["deadline"]["paid_requests_sent_at_or_after_end"],
                         0)
        # Stopped by the research path's own check, never by the backstop.
        self.assertEqual(self.kinds("dispatch_refused"), [])
        self.assertEqual(end["reason"], "end_of_session")

    def test_markouts_past_the_end_are_censored(self):
        figures = analyse(self.rows)
        [item] = figures[RETURN]["items"]
        primary = next(c for c in item["contracts"] if c["role"] == "primary")
        reasons = {m["seconds"]: (m.get("censored") or {}).get("reason")
                   for m in primary["capture"]["markouts"]}
        self.assertIsNone(reasons[30.0])
        for seconds in (300.0, 600.0, 900.0, 1800.0):
            self.assertEqual(reasons[seconds], "session_ended")


class KickoffCensoringTest(SessionCase):
    """A drift twelve minutes before kickoff: the markouts after kickoff are
    censored `game_started`, and nothing is read for research after it."""

    @staticmethod
    def session(out_dir):
        kickoff = dev.DRIFT_STEPS[-1][0] + timedelta(minutes=12)
        games = (dev.Game(
            "KXNFLGAME-26OCT01PITCLE", "20261001", kickoff, dev.PIT, dev.CLE,
            "evt-pit-cle", dev.GAMES[0].pinnacle, dev.GAMES[0].books),)
        return dev.run_session(out_dir, hours=2.0, games=games)

    def test_markouts_after_kickoff_are_censored(self):
        figures = analyse(self.rows)
        [item] = figures[DRIFT]["items"]
        primary = next(c for c in item["contracts"] if c["role"] == "primary")
        reasons = {m["seconds"]: (m.get("censored") or {}).get("reason")
                   for m in primary["capture"]["markouts"]}
        self.assertIsNone(reasons[300.0])
        for seconds in (900.0, 1800.0):
            self.assertEqual(reasons[seconds], "game_started")
        kickoff = datetime.fromisoformat(primary["start"])
        for read in self.research_books():
            self.assertLess(datetime.fromisoformat(read["sent_at"]), kickoff)


class OutageReturnTest(SessionCase):
    """LAR-PHI's quote lost to three failed polls and an outage instead of
    an empty answer: the return names both causes."""

    @staticmethod
    def session(out_dir):
        window = (dev.EMPTY_BOOKMAKERS[0] - timedelta(seconds=40),
                  dev.EMPTY_BOOKMAKERS[1] + timedelta(seconds=30))

        class Failing(dev.ResearchNetwork):
            def _odds(self, at):
                if window[0] <= at < window[1]:
                    self.odds_calls.append(at)
                    raise urllib.error.HTTPError(
                        "https://api.the-odds-api.com", 503, "Unavailable",
                        {}, None)
                return super()._odds(at)

        games = tuple(dev.Game(g.event_ticker, g.bucket, g.kickoff, g.away,
                               g.home, g.odds_event, g.pinnacle, g.books)
                      for g in dev.GAMES)
        return dev.run_session(out_dir, network=Failing, games=games)

    def test_the_return_names_the_failed_polls_and_the_outage(self):
        self.assertTrue(self.kinds("outage_start"))
        [signal] = [s for s in self.kinds("research_signal")
                    if s["channel"] == RETURN]
        causes = [c["cause"] for c in signal["gap"]["causes"]]
        self.assertEqual(causes, ["declared:failed_poll", "declared:outage"])
        self.assertLessEqual(signal["gap"]["seconds"], 300)
        figures = analyse(self.rows)
        self.assertTrue(figures["inputs_check"]["agrees"])
        self.assertTrue(figures["recorded_check"]["agrees"])


class SlateReturnsAtOnceTest(SessionCase):
    """Every game missing from one answer, then back a point and more
    away: one answer opens six return episodes. Execution reads go to four
    games, the largest moves first, and four are followed."""

    @staticmethod
    def session(out_dir):
        hole = (datetime(2026, 10, 1, 21, 40, 5, tzinfo=UTC),
                datetime(2026, 10, 1, 21, 40, 35, tzinfo=UTC))
        teams = [("BUF", "Buffalo Bills"), ("MIA", "Miami Dolphins"),
                 ("NE", "New England Patriots"), ("NYJ", "New York Jets"),
                 ("BAL", "Baltimore Ravens"), ("CIN", "Cincinnati Bengals"),
                 ("HOU", "Houston Texans"), ("IND", "Indianapolis Colts"),
                 ("TEN", "Tennessee Titans"), ("JAX", "Jacksonville Jaguars"),
                 ("DEN", "Denver Broncos"), ("KC", "Kansas City Chiefs")]
        kickoff = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
        games = []
        for n in range(6):
            (ac, an), (hc, hn) = teams[2 * n], teams[2 * n + 1]
            away, home = dev.Team(ac, ac, an), dev.Team(hc, hc, hn)
            after = (-150 - 10 * n, 130 + 9 * n)
            games.append(dev.Game(
                f"KXNFLGAME-26OCT04{ac}{hc}", "20261004", kickoff, away, home,
                f"evt-{ac.lower()}-{hc.lower()}",
                ((dev.EARLY, -120, 100), (hole[1], *after)),
                {ac: [(dev.EARLY, 0.52, 0.53, 50.0, 60.0)],
                 hc: [(dev.EARLY, 0.47, 0.48, 60.0, 50.0)]},
                empty=hole))
        return dev.run_session(out_dir, hours=0.5, games=tuple(games))

    def test_the_read_bounds_hold(self):
        signals = [s for s in self.kinds("research_signal")
                   if s["channel"] == RETURN]
        self.assertEqual(len(signals), 6)
        executions = self.kinds("research_execution")
        read = [e for e in executions
                if "read" in set(e["reads"].values())]
        capped = [e for e in executions
                  if all(v.startswith("not sent: 4 game(s)")
                         for v in e["reads"].values())]
        self.assertEqual((len(read), len(capped)), (4, 2))
        # The largest moves were read first.
        sizes = {s["episode"]["id"]: abs(s["delta"]["home"]) for s in signals}
        self.assertGreaterEqual(min(sizes[e["episode"]] for e in read),
                                max(sizes[e["episode"]] for e in capped))
        follows = [e["follow"] for e in executions]
        self.assertEqual(follows.count("started"), 4)
        self.assertEqual(sum(1 for f in follows if f.startswith("refused")),
                         2)


class ErrorIsolationTest(SessionCase):
    """A failure inside the channels stops them, and only them."""

    run_off = True

    @classmethod
    def setUpClass(cls):
        calls = {"n": 0}
        real = ResearchChannels.on_answer

        def flaky(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 40:
                raise ZeroDivisionError("a research bug")
            return real(self, *args, **kwargs)

        with mock.patch.object(ResearchChannels, "on_answer", flaky):
            super().setUpClass()

    def test_the_session_runs_to_its_end_unchanged(self):
        self.assertEqual(adjacent_records(self.rows),
                         adjacent_records(self.off_rows))
        end = self.kinds("session_end")[0]
        self.assertEqual(end["reason"], "end_of_session")
        self.assertIn("ZeroDivisionError", end["research"]["stopped_by_error"])
        [error] = self.kinds("research_error")
        self.assertIn("a research bug", error["error"])
        self.assertEqual(self.kinds("research_signal"), [])
        self.assertEqual(self.research_books(), [])

    def test_the_report_rebuilds_the_channels_anyway(self):
        figures = analyse(self.rows)
        self.assertEqual(figures[RETURN]["episodes"], 1)
        check = figures["recorded_check"]
        self.assertFalse(check["agrees"])
        self.assertTrue(check["stopped_by_error"])


class ReplayExclusionTest(unittest.TestCase):
    """The offline rebuild keeps research reads out of everything the
    adjacent path reports -- its decision memory, its reads, its counts --
    and hands them to the research capture alone."""

    def rows(self) -> list[dict]:
        book = {"orderbook_fp": {"yes_dollars": [["0.4000", "10.00"]],
                                 "no_dollars": [["0.5800", "12.00"]]}}
        ticker = "KXNFLGAME-26OCT04LARPHI-PHI"

        def read(purpose: str, second: int) -> dict:
            at = datetime(2026, 10, 1, 21, 0, second, tzinfo=UTC)
            return {"kind": "book", "ticker": ticker, "purpose": purpose,
                    "sent_at": at.isoformat(),
                    "received_at": (at + timedelta(milliseconds=200))
                    .isoformat(), "payload": book, "coverage": []}

        return [{"kind": "session_start", "at": "2026-10-01T20:59:00+00:00"},
                read("decision", 1), read("research_execution", 2),
                read("research_follow", 3), read("follow", 4)]

    def test_the_rebuild(self):
        replayed = diagnostics_replay(self.rows())
        ticker = "KXNFLGAME-26OCT04LARPHI-PHI"
        self.assertEqual(len(replayed.reads[ticker]), 2)
        self.assertEqual(len(replayed.all_reads[ticker]), 4)
        self.assertEqual(len(replayed.memory[ticker]), 2)

    def test_the_report(self):
        rows = self.rows()
        self.assertEqual([r["purpose"] for r in
                          shadow_monitor.adjacent_books(rows)],
                         ["decision", "follow"])
        chronology = shadow_monitor.failure_chronology(rows)
        self.assertEqual(chronology["book_reads"], 2)


class ReplayInputsTest(SessionCase):
    """What the rebuild may and may not feed the channels."""

    def cut(self, last_odds: int) -> list[dict]:
        """The development session, stopped after its `last_odds`-th poll."""
        out, seen = [], 0
        for row in self.rows:
            if row["kind"] == "session_end":
                break
            out.append(row)
            if row["kind"] == "odds":
                seen += 1
                if seen == last_odds:
                    break
        return out

    def test_an_answer_the_session_stopped_on_was_never_observed(self):
        rows = self.cut(4)
        rows.append({"kind": "session_end", "at": rows[-1]["received_at"],
                     "reason": "clock_skew"})
        from shadow_research import replay_research
        replayed = replay_research(rows)
        [skipped] = replayed.skipped
        self.assertIn("clock_skew", skipped["why"])
        # Three games on each of the three answers it did observe.
        self.assertEqual(
            sum(replayed.channels.counts["observations"].values()), 9)

    def test_the_tail_a_session_ended_inside_is_not_a_hole(self):
        """The monitor declares no gap for its last stretch -- the end
        censors it -- so the rebuild must not open one either."""
        rows = self.cut(20)
        last = rows[-1]["received_at"]
        rows += [{"kind": "gap", "cause": "not_polled", "from": last,
                  "to": "2026-10-01T22:00:00+00:00", "recovered": False},
                 {"kind": "session_end", "at": "2026-10-01T22:00:00+00:00",
                  "reason": "end_of_session"}]
        from shadow_research import replay_research
        replayed = replay_research(rows)
        self.assertEqual(replayed.channels.returns, [])


class ResearchReadTest(unittest.TestCase):

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.clock = dev.FakeClock(dev.T0)
        self.net = dev.ResearchNetwork(self.clock)
        recorder = shadow_monitor.Recorder(tmp / "s.jsonl")
        self.addCleanup(recorder.close)
        self.monitor = shadow_monitor.ShadowMonitor(
            sport="NFL", series="KXNFLGAME", api_key="x", cadence=dev.CADENCE,
            hours=1, ledger=mock.Mock(), recorder=recorder, clock=self.clock)
        self.contracts = [shadow_monitor.Watched(
            ticker=f"KXNFLGAME-26OCT04LARPHI-{code}",
            event_ticker="KXNFLGAME-26OCT04LARPHI",
            provider_event_id="evt-lar-phi", yes_is_home=code == "PHI",
            start=dev.LAR_PHI_KICKOFF) for code in ("LAR", "PHI")]

    def reads(self, due):
        with mock.patch("urllib.request.urlopen", side_effect=self.net):
            return self.monitor._research_reads(
                self.contracts, "research_follow", {}, due)

    def test_nothing_is_read_at_or_after_the_end(self):
        self.monitor.ends = self.clock.now()
        outcomes = self.reads(None)
        self.assertEqual(set(outcomes.values()),
                         {"not sent: the authorized end had passed"})
        self.assertEqual(self.net.book_calls, [])

    def test_nothing_is_read_within_the_guard_of_the_next_paid_request(self):
        outcomes = self.reads(self.clock.now() + timedelta(seconds=9))
        self.assertEqual(set(outcomes.values()),
                         {"not sent: the next paid request was due within "
                          "10s"})
        self.assertEqual(self.net.book_calls, [])
        outcomes = self.reads(self.clock.now() + timedelta(seconds=11))
        self.assertEqual(set(outcomes.values()), {"read"})

    def test_one_failed_read_abandons_the_rest(self):
        with mock.patch.object(self.net, "_book",
                               side_effect=urllib.error.URLError("down")):
            outcomes = self.reads(None)
        self.assertEqual(list(outcomes.values()),
                         ["failed", "not sent: an earlier research read "
                                    "failed"])

    def test_a_research_read_is_counted_apart_and_never_remembered(self):
        monitor, watched, net = self.monitor, self.contracts[1], self.net
        with mock.patch("urllib.request.urlopen", side_effect=net):
            book = monitor.read_book(watched, "research_execution")
            self.assertIsNotNone(book)
            self.assertEqual(monitor.books, {})
            self.assertEqual(monitor.counts["book_reads"], 0)
            self.assertEqual(monitor.research_counts["book_reads"], 1)
            self.assertIsNone(monitor.last_read_status)
            monitor.read_book(watched, "decision")
            self.assertEqual(len(monitor.books[watched.ticker]), 1)
            self.assertEqual(monitor.counts["book_reads"], 1)


if __name__ == "__main__":
    unittest.main()
