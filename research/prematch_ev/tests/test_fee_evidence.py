"""`verify_fees.py`: Kalshi's dated fee record, read and judged, never applied.

THE RESPONSE SHAPES ARE TRANSCRIBED, not observed: no session that wrote
this could reach api.elections.kalshi.com. The bodies below follow the
documented shape; the parser is tested to REFUSE anything else rather than
read a fee out of it, because the owner's machine is where the first real
body will arrive, and a confident misreading there would set the study's
cost basis.
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import verify_fees
from core import fees
from core.fee_evidence import (
    entry_snippet, parse_fee_changes, parse_series_fees, verdict,
)
from core.fees import FeeScheduleEntry, entry_in_force

UTC = timezone.utc
WINDOW = (datetime(2026, 9, 24, 1, 44, 22, tzinfo=UTC),
          datetime(2026, 9, 25, 1, 44, 22, tzinfo=UTC))
NOW = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)


def changes(*items, key="series_fee_change_arr"):
    return {key: [dict(item) for item in items]}


def change(when, multiplier, series="KXNFLGAME", ident="c1",
           fee_type="quadratic"):
    return {"id": ident, "series_ticker": series, "fee_type": fee_type,
            "fee_multiplier": multiplier, "scheduled_ts": when}


def parse(payload, series="KXNFLGAME"):
    return parse_fee_changes(payload, series, source="kalshi/fee_changes",
                             observed_on="2026-09-26", observed_by="test")


class ParseTest(unittest.TestCase):

    def test_the_transcribed_shape_reads_in_date_order(self):
        entries, problems, key = parse(changes(
            change("2026-08-07T04:59:45.131Z", 0.5, ident="b"),
            change("2025-10-04T00:00:00Z", 1, ident="a")))
        self.assertEqual(problems, [])
        self.assertEqual(key, "series_fee_change_arr")
        self.assertEqual([e.multiplier for e in entries], [1.0, 0.5])
        self.assertEqual(entries[1].effective_from,
                         datetime(2026, 8, 7, 4, 59, 45, 131000, tzinfo=UTC))
        self.assertEqual(entries[1].source_id, "b")

    def test_a_numeric_string_multiplier_is_read(self):
        entries, _, _ = parse(changes(change("2026-08-07T00:00:00Z", "0.5")))
        self.assertEqual(entries[0].multiplier, 0.5)

    def test_other_series_are_counted_not_mixed_in(self):
        entries, problems, _ = parse(changes(
            change("2026-08-07T00:00:00Z", 0.5, series="KXMLBGAME")))
        self.assertEqual(entries, [])
        self.assertIn("1 change(s) for other series", problems[0])

    def test_a_change_without_a_date_or_multiplier_is_a_problem(self):
        entries, problems, _ = parse(changes(
            {"series_ticker": "KXNFLGAME", "scheduled_ts": "soon",
             "fee_multiplier": 0.5}))
        self.assertEqual(entries, [])
        self.assertIn("without a readable", problems[0])

    def test_an_unknown_shape_is_refused_with_its_keys(self):
        for body in ({"changes": "none"}, [], {"a": [{"x": 1}], "b": []},
                     {"a": [{"scheduled_ts": "x"}], "b": [{"fee_multiplier": 1}]}):
            with self.subTest(body=body):
                entries, problems, key = parse(body)
                self.assertEqual((entries, key), ([], None))
                self.assertTrue(problems)

    def test_the_list_key_is_matched_loosely_the_members_strictly(self):
        entries, problems, key = parse(changes(
            change("2026-08-07T00:00:00Z", 0.5), key="fee_changes"))
        self.assertEqual((len(entries), key), (1, "fee_changes"))

    def test_an_empty_list_is_no_changes_only_under_a_fee_change_key(self):
        entries, problems, key = parse({"series_fee_change_arr": []})
        self.assertEqual((entries, problems, key),
                         ([], [], "series_fee_change_arr"))
        entries, problems, key = parse({"cursor_list": []})
        self.assertIsNone(key)
        self.assertTrue(problems)

    def test_the_series_body_gives_the_current_fee(self):
        current, problems = parse_series_fees({"series": {
            "ticker": "KXNFLGAME", "fee_type": "quadratic",
            "fee_multiplier": 1}})
        self.assertEqual(problems, [])
        self.assertEqual(current["fee_multiplier"], 1.0)
        self.assertIsNone(parse_series_fees({"series": {"ticker": "X"}})[0])
        self.assertIsNone(parse_series_fees({"nope": 1})[0])


class VerdictTest(unittest.TestCase):

    def entries(self, *pairs):
        return parse(changes(*[change(w, m, ident=str(i))
                               for i, (w, m) in enumerate(pairs)]))[0]

    def test_the_entry_in_force_is_the_pricing_paths_own(self):
        entries = self.entries(("2025-10-04T00:00:00Z", 1),
                               ("2026-08-07T04:59:45.131Z", 0.5))
        v = verdict(entries, None, WINDOW, record_read=True)
        self.assertEqual(v["in_force_at_start"]["multiplier"], 0.5)
        self.assertIs(entry_in_force(entries, WINDOW[0]), entries[1])
        self.assertEqual(v["changes_inside_window"], [])
        self.assertIn("multiplier 0.5", v["establishes"][0])

    def test_a_change_inside_the_window_is_said(self):
        entries = self.entries(("2025-10-04T00:00:00Z", 1),
                               ("2026-09-24T12:00:00Z", 0.5))
        v = verdict(entries, None, WINDOW, record_read=True)
        self.assertEqual(len(v["changes_inside_window"]), 1)
        self.assertIn("INSIDE the window", v["establishes"][1])

    def test_a_record_starting_after_the_window_is_not_extrapolated(self):
        entries = self.entries(("2026-10-01T00:00:00Z", 0.5))
        v = verdict(entries, None, WINDOW, record_read=True)
        self.assertEqual(v["establishes"], [])
        self.assertIn("must not be extrapolated backwards",
                      v["does_not_establish"][0])

    def test_no_change_on_record_does_not_date_todays_multiplier(self):
        v = verdict([], {"fee_multiplier": 1.0, "fee_type": "quadratic"},
                    WINDOW, record_read=True)
        self.assertEqual(v["establishes"], [])
        joined = " ".join(v["does_not_establish"])
        self.assertIn("no dated fee change is on record", joined)
        self.assertIn("only if Kalshi's change history is complete", joined)

    def test_an_unread_record_is_not_an_empty_one(self):
        v = verdict([], None, WINDOW, record_read=False)
        joined = " ".join(v["does_not_establish"])
        self.assertIn("could not be read", joined)
        self.assertNotIn("no dated fee change is on record", joined)

    def test_the_route_is_never_established(self):
        v = verdict(self.entries(("2025-10-04T00:00:00Z", 1)), None, WINDOW,
                    record_read=True)
        self.assertIn("ACCOUNT ROUTE", v["does_not_establish"][-1])

    def test_the_snippet_builds_the_entry_it_describes(self):
        entries = self.entries(("2026-08-07T04:59:45.131Z", 0.5))
        text = entry_snippet("KXNFLGAME", entries, "evidence f.json")
        namespace = {"FeeScheduleEntry": FeeScheduleEntry,
                     "datetime": datetime, "timezone": timezone}
        built = eval("{" + text + "}", namespace)
        (entry,) = built["KXNFLGAME"]
        self.assertEqual(entry.effective_from, entries[0].effective_from)
        self.assertEqual(entry.multiplier, 0.5)
        self.assertIn("evidence f.json", entry.observed_by)


class FakeKalshi:
    """Answers the two fee endpoints; anything else fails the test."""

    def __init__(self, changes_body=None, series_body=None, status=200,
                 fail=False):
        self.changes_body = changes_body
        self.series_body = series_body
        self.status = status
        self.fail = fail
        self.urls: list[str] = []

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        if self.fail:
            raise urllib.error.URLError("Tunnel connection failed: 403")
        body = (self.changes_body if "/fee_changes" in url
                else self.series_body)
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "Not Found",
                                         {"Date": "Sat, 26 Sep 2026"},
                                         io.BytesIO(b'{"error":"nope"}'))
        return _Response(json.dumps(body).encode())


class _Response:
    def __init__(self, body: bytes):
        self._body, self.status = body, 200
        self.headers = {"Date": "Sat, 26 Sep 2026 15:00:00 GMT"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class VerifyCommandTest(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fees_source = Path(fees.__file__).read_text()

    def run_verify(self, net):
        out = io.StringIO()
        argv = ["--series", "KXNFLGAME", "--window", "2026-09-24T01:44:22Z",
                "2026-09-25T01:44:22Z", "--out-dir", str(self.tmp)]
        with mock.patch("urllib.request.urlopen", side_effect=net), \
                redirect_stdout(out), redirect_stderr(out):
            code = verify_fees.main(argv, now=lambda: NOW)
        return code, out.getvalue()

    def test_a_dated_record_is_judged_saved_and_offered_not_applied(self):
        net = FakeKalshi(changes(change("2025-10-04T00:00:00Z", 1)),
                         {"series": {"ticker": "KXNFLGAME",
                                     "fee_type": "quadratic",
                                     "fee_multiplier": 1}})
        code, text = self.run_verify(net)
        self.assertEqual(code, 0, text)
        self.assertEqual(len(net.urls), 2)
        (saved,) = self.tmp.glob("KXNFLGAME_*.json")
        record = json.loads(saved.read_text())
        self.assertEqual(record["verdict"]["in_force_at_start"]["multiplier"],
                         1.0)
        for read in record["reads"]:
            self.assertEqual(read["status"], 200)
            self.assertTrue(read["body"])
            self.assertEqual(len(read["sha256"]), 64)
        self.assertIn("NOT applied", text)
        self.assertIn("FeeScheduleEntry(", text)
        self.assertEqual(Path(fees.__file__).read_text(), self.fees_source,
                         "verify_fees must never edit core/fees.py")

    def test_every_request_is_a_declared_read_only_endpoint(self):
        from reaction.capture import endpoint_allowed
        net = FakeKalshi(changes(), {"series": {"fee_multiplier": 1}})
        self.run_verify(net)
        for url in net.urls:
            self.assertTrue(endpoint_allowed(url), url)

    def test_an_unreachable_kalshi_establishes_nothing_and_fails(self):
        code, text = self.run_verify(FakeKalshi(fail=True))
        self.assertEqual(code, 1)
        self.assertIn("could not be read", text)
        self.assertIn("nothing about the window", text)

    def test_an_http_error_keeps_its_status_and_body(self):
        code, text = self.run_verify(FakeKalshi(status=404))
        self.assertEqual(code, 1)
        (saved,) = self.tmp.glob("KXNFLGAME_*.json")
        read = json.loads(saved.read_text())["reads"][0]
        self.assertEqual((read["status"], read["body"]),
                         (404, '{"error":"nope"}'))
        self.assertIn("HTTP 404", text)

    def test_an_empty_record_is_unverified_not_confirmed(self):
        net = FakeKalshi(changes(), {"series": {"fee_multiplier": 1,
                                                "fee_type": "quadratic"}})
        code, text = self.run_verify(net)
        self.assertEqual(code, 1, "an empty history dates nothing")
        self.assertIn("no dated fee change is on record", text)

    def test_a_bad_window_is_a_usage_error(self):
        out = io.StringIO()
        with redirect_stderr(out):
            code = verify_fees.main(["--series", "KXNFLGAME", "--window",
                                     "2026-09-25T00:00:00Z",
                                     "2026-09-24T00:00:00Z"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
