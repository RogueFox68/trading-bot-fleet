"""The failure classifier, and the live fetchers' use of it.

Every exception here is one the standard library really raises in that
place, constructed the way it constructs it: `urlopen` wraps an OSError from
connecting or sending in `URLError`, raises `HTTPError` for an error status,
and lets a failure while waiting for the status line -- or reading the body
-- through unwrapped. The phase the fetcher records rests on exactly those
differences, so the fakes reproduce them rather than simplify them.

SYNTHETIC: no response here was observed. The timeout is built as macOS
reports one (`[Errno 60] Operation timed out` on 2026-09-26) but with this
platform's own ETIMEDOUT, since that is what the classifier compares with.
"""

from __future__ import annotations

import errno
import http.client
import io
import json
import socket
import ssl
import sys
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import failures, kalshi_history, odds_live              # noqa: E402
from data.odds_history import CreditLedger                         # noqa: E402

UTC = timezone.utc
AT = datetime(2026, 9, 26, 1, 37, 2, tzinfo=UTC)
KEY = "SECRET-KEY-VALUE"
URL = f"https://api.the-odds-api.com/v4/sports/x/odds?apiKey={KEY}"


def os_timeout() -> OSError:
    return OSError(errno.ETIMEDOUT, "Operation timed out")


def http_error(status: int, headers: dict | None = None,
               body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, status, http.client.responses.get(
        status, "Error"), headers or {}, io.BytesIO(body))


class ClassifyTest(unittest.TestCase):

    def category(self, exc):
        return failures.classify(exc)[0]

    def test_the_operating_systems_timeout_is_a_timeout(self):
        category, status, number = failures.classify(
            urllib.error.URLError(os_timeout()))
        self.assertEqual((category, status, number),
                         ("timeout", None, errno.ETIMEDOUT))

    def test_the_sockets_own_timeout_is_a_timeout(self):
        self.assertEqual(self.category(urllib.error.URLError(
            socket.timeout("timed out"))), "timeout")
        self.assertEqual(self.category(TimeoutError("timed out")), "timeout")

    def test_each_status_class(self):
        for status, category in ((401, "auth"), (403, "auth"),
                                 (400, "request_rejected"),
                                 (404, "request_rejected"),
                                 (422, "request_rejected"),
                                 (408, "timeout"), (429, "rate_limited"),
                                 (500, "server_error"),
                                 (503, "server_error"),
                                 (204, "unexpected_status")):
            with self.subTest(status=status):
                if status < 300:
                    self.assertEqual(failures.status_category(status),
                                     category)
                    continue
                self.assertEqual(self.category(http_error(status)), category)

    def test_what_recovers_and_what_stops(self):
        self.assertEqual(failures.TRANSIENT & failures.TERMINAL, set())
        for category in ("auth", "request_rejected", "tls_certificate",
                         "unexpected_shape"):
            self.assertIn(category, failures.TERMINAL)
        for category in ("timeout", "connection", "dns", "server_error",
                         "rate_limited", "incomplete_body"):
            self.assertIn(category, failures.TRANSIENT)

    def test_transport_failures(self):
        cases = [
            (socket.gaierror(socket.EAI_NONAME, "not known"), "dns"),
            (ConnectionRefusedError(errno.ECONNREFUSED, "refused"),
             "connection"),
            (OSError(errno.ENETUNREACH, "Network is unreachable"),
             "connection"),
            (ssl.SSLCertVerificationError(1, "certificate verify failed"),
             "tls_certificate"),
            (ssl.SSLError(1, "record layer failure"), "tls"),
        ]
        for reason, category in cases:
            with self.subTest(reason=type(reason).__name__):
                self.assertEqual(self.category(urllib.error.URLError(reason)),
                                 category)

    def test_http_client_failures(self):
        self.assertEqual(self.category(http.client.IncompleteRead(b"x", 9)),
                         "incomplete_body")
        self.assertEqual(self.category(http.client.RemoteDisconnected("x")),
                         "connection")
        self.assertEqual(self.category(http.client.BadStatusLine("x")),
                         "protocol")
        self.assertEqual(self.category(json.JSONDecodeError("x", "{", 0)),
                         "undecodable")

    def test_retry_after_in_both_forms(self):
        self.assertEqual(failures.retry_after_seconds(
            {"Retry-After": "120"}, AT), 120.0)
        later = "Sat, 26 Sep 2026 01:42:02 GMT"
        self.assertEqual(failures.retry_after_seconds(
            {"Retry-After": later}, AT), 300.0)
        self.assertIsNone(failures.retry_after_seconds(
            {"Retry-After": "soon"}, AT))
        self.assertIsNone(failures.retry_after_seconds({}, AT))

    def test_no_detail_carries_the_key(self):
        """An error body that echoes the key, and a URL that holds it: the
        detail keeps the provider's words and loses the key."""
        exc = http_error(401, body=f'{{"message": "bad key {KEY}", "url": '
                                   f'"{URL}"}}'.encode())
        failure = failures.describe(exc, phase=failures.RESPONSE,
                                    secrets=(KEY,), body=exc.read(512))
        self.assertNotIn(KEY, json.dumps(failure.as_dict()))
        self.assertIn("HTTP 401", failure.detail)
        self.assertIn("bad key", failure.detail)
        self.assertTrue(failure.terminal)

    def test_a_detail_is_bounded(self):
        self.assertLessEqual(len(failures.scrub("x" * 5000)),
                             failures.DETAIL_LIMIT)


class LiveOddsPhaseTest(unittest.TestCase):
    """`fetch_live_odds` says where in the request each failure came."""

    def fetch(self, opener):
        clock = {"t": AT}

        def now():
            return clock["t"]

        def advance(seconds):
            clock["t"] += timedelta(seconds=seconds)

        ledger = CreditLedger(cap=5)
        with mock.patch("urllib.request.urlopen",
                        side_effect=lambda *a, **k: opener(advance)):
            live = odds_live.fetch_live_odds("NFL", KEY, ledger=ledger,
                                             now=now)
        return live, ledger

    def test_a_connect_timeout_is_the_request_phase(self):
        def opener(advance):
            advance(30)
            raise urllib.error.URLError(os_timeout())
        live, ledger = self.fetch(opener)
        failure = live.failure
        self.assertEqual((failure.category, failure.phase, failure.errno),
                         ("timeout", "request", errno.ETIMEDOUT))
        self.assertEqual(failure.elapsed_seconds, 30.0)
        # Reserved before the attempt, answer or no answer.
        self.assertEqual(ledger.spent_this_run, 1)

    def test_a_timeout_waiting_for_the_status_is_its_own_phase(self):
        def opener(advance):
            advance(30)
            raise TimeoutError("timed out")
        live, _ = self.fetch(opener)
        self.assertEqual((live.failure.category, live.failure.phase),
                         ("timeout", "awaiting_response"))

    def test_a_timeout_reading_the_body_is_the_body_phase(self):
        class Slow:
            status = 200
            headers = {"x-requests-last": "1"}

            def read(self):
                raise TimeoutError("timed out")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        live, _ = self.fetch(lambda advance: Slow())
        self.assertEqual((live.failure.category, live.failure.phase),
                         ("timeout", "body"))

    def test_an_error_status_keeps_the_providers_guidance(self):
        def opener(advance):
            raise http_error(429, {"Retry-After": "90",
                                   "x-requests-remaining": "4321"})
        live, ledger = self.fetch(opener)
        self.assertEqual((live.failure.category, live.failure.phase,
                          live.failure.status),
                         ("rate_limited", "response", 429))
        self.assertEqual(live.failure.retry_after_seconds, 90.0)
        self.assertEqual(live.remaining, 4321)
        self.assertEqual(ledger.remaining, 4321)

    def test_the_wrong_shape_is_terminal(self):
        class Wrong:
            status = 200
            headers = {}

            def read(self):
                return b'{"message": "not a list"}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        live, _ = self.fetch(lambda advance: Wrong())
        self.assertFalse(live.ok)
        self.assertEqual((live.failure.category, live.failure.phase),
                         ("unexpected_shape", "shape"))
        self.assertTrue(live.failure.terminal)

    def test_an_answer_has_no_failure(self):
        class Good:
            status = 200
            headers = {}

            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        live, _ = self.fetch(lambda advance: Good())
        self.assertTrue(live.ok)
        self.assertIsNone(live.failure)


class BookFailureTest(unittest.TestCase):

    def test_a_failed_book_read_carries_its_classified_cause(self):
        def opener(*args, **kwargs):
            raise urllib.error.URLError(os_timeout())
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            payload, coverage = kalshi_history.fetch_orderbook_payload("T")
        self.assertIsNone(payload)
        self.assertEqual(coverage.failure.category, "timeout")
        self.assertEqual(coverage.failure.errno, errno.ETIMEDOUT)
        # The sentence is unchanged; the fields are beside it.
        self.assertIn("failed after 1 attempts", " ".join(coverage.reasons))

    def test_an_error_status_on_a_book_read(self):
        def opener(*args, **kwargs):
            raise urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=opener):
            _, coverage = kalshi_history.fetch_orderbook_payload("T")
        self.assertEqual((coverage.failure.category, coverage.failure.status),
                         ("server_error", 503))

    def test_merge_keeps_the_first_cause(self):
        whole = kalshi_history.Coverage()
        first = kalshi_history.Coverage().fail("a", "first")
        whole.merge(first).merge(kalshi_history.Coverage().fail("b", "second"))
        self.assertEqual(whole.failure, "first")
        self.assertEqual(whole.reasons, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
