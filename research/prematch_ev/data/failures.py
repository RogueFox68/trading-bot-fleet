"""Why a live read failed, as data: one classifier for every live fetcher.

A failed poll used to be one redacted sentence in the record. That was enough
to say THAT it failed and not enough to act on or to reconstruct: the monitor
cannot tell a refused key from a dropped connection by reading English, and
nobody reading the file afterwards can tell whether a timeout came before the
request was sent or halfway through the answer. So each failure is also
described here, as fields -- category, phase, exception, status, errno,
retry-after, elapsed -- by the one function both the odds poll and the book
reads call. The fetcher that records a category and the monitor that decides
on it read the same answer (rule 19).

WHAT RECOVERS AND WHAT STOPS
----------------------------
TRANSIENT -- waiting can mend it, so the monitor backs off and probes:

  timeout           no answer in time (the socket's own limit, the operating
                    system's ETIMEDOUT, or HTTP 408)
  connection        refused, reset, aborted, unreachable
  dns               the name did not resolve
  tls               a handshake or record-layer failure other than trust
  server_error      HTTP 5xx
  rate_limited      HTTP 429 (and 425); its Retry-After is kept, and obeyed
  incomplete_body   the answer began and was cut off
  undecodable       the body is not JSON -- a truncated or garbled answer
  protocol          any other malformed HTTP exchange
  unexpected_status a status that is neither 200 nor an error
  network           any other transport failure

TERMINAL -- waiting cannot mend it, and every further attempt costs a credit:

  auth              HTTP 401 / 403: the key was refused, or the account is out
  request_rejected  any other 4xx: the request itself is wrong -- the sport
                    key, a parameter, the endpoint
  tls_certificate   this machine does not trust the certificate it was shown:
                    an intercepting proxy, a captive portal or a broken trust
                    store, none of which a probe repairs
  unexpected_shape  a 200 whose JSON is not the documented list: the
                    transcribed shape is wrong, as the book-shape check stops
                    on

THE CATEGORY IS NOT THE CAUSE. A timeout says no answer came in time; it does
not say whether the provider, this machine's connection or something between
them failed. The phase narrows it -- `request` is connecting, the TLS
handshake or sending, `awaiting_response` is after the request went and
before any status came back, `response` is an error status, `body` is partway
through the answer -- and a Kalshi read failing in the same minute is
evidence about the path, not a verdict. Nothing here names a root cause.

NOTHING HERE MAY CARRY THE CREDENTIAL. `detail` is scrubbed of `apiKey=`-style
pairs (`cache.redact`) and of every secret the caller names, and bounded; an
`HTTPError`'s URL -- which holds the key -- is never read.
"""

from __future__ import annotations

import email.utils
import errno as errnos
import http.client
import json
import socket
import ssl
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .cache import redact

TRANSIENT = frozenset({
    "timeout", "connection", "dns", "tls", "server_error", "rate_limited",
    "incomplete_body", "undecodable", "protocol", "unexpected_status",
    "network",
})
TERMINAL = frozenset({
    "auth", "request_rejected", "tls_certificate", "unexpected_shape",
})

#: Resolving, connecting, the TLS handshake and sending the request. CPython
#: wraps an OSError from exactly this step -- and only this one -- in
#: `URLError`, so "<urlopen error ...>" is this phase.
REQUEST = "request"
#: The request was sent and no status line had arrived. The same failures
#: here -- a timeout, a reset -- surface UNWRAPPED, which is how this phase is
#: told apart from the one before it.
AWAITING = "awaiting_response"
#: A status line arrived, and it was an error (or not 200).
RESPONSE = "response"
#: The status and headers arrived; the body did not, whole.
BODY = "body"
#: The body arrived and is not JSON.
DECODE = "decode"
#: The body is JSON of the wrong shape.
SHAPE = "shape"

_CONNECTION_ERRNOS = frozenset(
    getattr(errnos, name) for name in (
        "ECONNREFUSED", "ECONNRESET", "ECONNABORTED", "EPIPE", "ENETUNREACH",
        "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", "ENETRESET")
    if hasattr(errnos, name))

DETAIL_LIMIT = 240


def scrub(text: Any, secrets: Iterable[str] = (),
          limit: int = DETAIL_LIMIT) -> str:
    """`text` fit for a record: credentials out, one line, bounded."""
    out = redact(str(text))
    for secret in secrets:
        if secret:
            out = out.replace(secret, "REDACTED")
    out = " ".join(out.split())
    return out if len(out) <= limit else out[:limit - 3] + "..."


def status_category(status: int) -> str:
    """The category of an HTTP status that is not 200."""
    if status in (401, 403):
        return "auth"
    if status == 408:
        return "timeout"
    if status in (425, 429):
        return "rate_limited"
    if 400 <= status < 500:
        return "request_rejected"
    if 500 <= status < 600:
        return "server_error"
    return "unexpected_status"


def classify(exc: BaseException) -> tuple[str, int | None, int | None]:
    """(category, HTTP status, errno) for an exception a read raised.

    `URLError` is unwrapped to its reason first: urllib wraps the connect
    step's OSError in it, so the reason is what actually happened.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return status_category(exc.code), exc.code, None
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
    if not isinstance(reason, BaseException):
        return "network", None, None
    number = getattr(reason, "errno", None)
    number = number if isinstance(number, int) else None
    # Order matters: the certificate failure is an SSLError, which is an
    # OSError; a JSON error is a ValueError; RemoteDisconnected is both a
    # ConnectionResetError and an HTTPException.
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_certificate", None, number
    if isinstance(reason, ssl.SSLError):
        return "tls", None, number
    if isinstance(reason, socket.gaierror):
        return "dns", None, number
    if isinstance(reason, TimeoutError) or number == errnos.ETIMEDOUT:
        return "timeout", None, number
    if isinstance(reason, http.client.IncompleteRead):
        return "incomplete_body", None, number
    if isinstance(reason, ConnectionError) or number in _CONNECTION_ERRNOS:
        return "connection", None, number
    if isinstance(reason, http.client.HTTPException):
        return "protocol", None, number
    if isinstance(reason, (json.JSONDecodeError, UnicodeDecodeError,
                           ValueError)):
        return "undecodable", None, number
    return "network", None, number


def retry_after_seconds(headers: Any, now: datetime) -> float | None:
    """The provider's `Retry-After`, in seconds from `now`, or None.

    Either form the standard allows: delta-seconds, or an HTTP date. A value
    that cannot be read is None -- no guidance, not a zero wait.
    """
    try:
        raw = headers.get("Retry-After") if headers is not None else None
    except AttributeError:
        return None
    if raw is None:
        return None
    text = str(raw).strip()
    if text.isdigit():
        return float(text)
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, (when.astimezone(timezone.utc) - now).total_seconds())


@dataclass(frozen=True)
class Failure:
    """One failed read, described. `terminal` follows from the category."""

    category: str
    phase: str | None
    exception: str | None
    detail: str
    elapsed_seconds: float | None = None
    status: int | None = None
    errno: int | None = None
    retry_after_seconds: float | None = None

    @property
    def terminal(self) -> bool:
        return self.category in TERMINAL

    def as_dict(self) -> dict:
        return {"category": self.category, "terminal": self.terminal,
                "phase": self.phase, "exception": self.exception,
                "status": self.status, "errno": self.errno,
                "elapsed_seconds": self.elapsed_seconds,
                "retry_after_seconds": self.retry_after_seconds,
                "detail": self.detail}


def describe(exc: BaseException, *, phase: str | None,
             elapsed_seconds: float | None = None,
             secrets: Iterable[str] = (), headers: Any = None,
             now: datetime | None = None, body: bytes | None = None
             ) -> Failure:
    """A `Failure` for an exception, with the credential scrubbed out.

    `headers` and `body` are an error response's, when there was one: the
    Retry-After is read from the headers, and a bounded, scrubbed piece of
    the body -- the provider's own words for a refusal -- joins the detail.
    """
    category, status, number = classify(exc)
    if isinstance(exc, urllib.error.HTTPError):
        # Never str(exc.url) or exc.filename: they are the request URL, and
        # the request URL carries the key.
        detail = f"HTTP {exc.code} {exc.reason}"
    else:
        detail = f"{type(exc).__name__}: {exc}"
    if body:
        detail += " -- " + body.decode("utf-8", errors="replace")
    retry = (retry_after_seconds(headers, now)
             if headers is not None and now is not None else None)
    return Failure(category=category, phase=phase,
                   exception=type(exc).__name__,
                   detail=scrub(detail, secrets),
                   elapsed_seconds=elapsed_seconds, status=status,
                   errno=number, retry_after_seconds=retry)
