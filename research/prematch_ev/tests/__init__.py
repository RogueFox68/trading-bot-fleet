"""Shared test helpers."""

from __future__ import annotations

import http.client
import os
import time
from contextlib import contextmanager
from typing import Callable, Sequence


@contextmanager
def machine_timezone(name: str):
    """Run with the MACHINE's local timezone set to `name`, then restore it.

    Anything that calls `datetime.astimezone()` without an argument reads the
    machine's zone, and a suite that only ever runs on UTC -- as CI and the
    sandbox that wrote this do -- cannot see what that code does anywhere
    else. A cache that served 12:00Z for a 17:00Z request on a UTC-5 machine
    passed every run on UTC.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


class _Wire:
    """One HTTP response as a socket file hands it over: the status line and
    headers at once, then each later piece only when the reader asks for it,
    `advance` running first -- the clock moving while the piece is in
    transit. Reads block as a socket file's do: until the bytes asked for
    have arrived, or the connection has closed."""

    def __init__(self, head: bytes, pieces: Sequence[bytes],
                 advance: Callable[[], None] | None):
        self._pending = [head, *pieces]
        self._buffer = b""
        self._advance = advance
        self._started = False

    def _pull(self) -> bool:
        if not self._pending:
            return False
        if self._started and self._advance is not None:
            self._advance()
        self._started = True
        self._buffer += self._pending.pop(0)
        return True

    def readline(self, limit: int = -1) -> bytes:
        while b"\n" not in self._buffer and self._pull():
            pass
        end = self._buffer.find(b"\n")
        cut = len(self._buffer) if end < 0 else end + 1
        if limit is not None and limit >= 0:
            cut = min(cut, limit)
        line, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return line

    def read(self, amount: int | None = -1) -> bytes:
        if amount is None or amount < 0:
            while self._pull():
                pass
            amount = len(self._buffer)
        while len(self._buffer) < amount and self._pull():
            pass
        out, self._buffer = self._buffer[:amount], self._buffer[amount:]
        return out

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


def chunked(body: bytes, pieces: int, *, cut_short: bool = False
            ) -> list[bytes]:
    """`body` as a chunked transfer in `pieces` chunks, then the last-chunk
    marker. `cut_short`: the connection closes halfway through the final
    chunk instead -- no marker, and fewer bytes than its size line
    promised."""
    size = -(-len(body) // pieces)
    parts = [body[i:i + size] for i in range(0, len(body), size)] or [b""]
    wire = [b"%x\r\n" % len(part) + part + b"\r\n" for part in parts]
    if cut_short:
        last = parts[-1]
        wire[-1] = b"%x\r\n" % (len(last) + 1) + last[:len(last) // 2]
        return wire
    return wire + [b"0\r\n\r\n"]


def http_response(status: int, headers: dict[str, str],
                  pieces: Sequence[bytes], *,
                  advance: Callable[[], None] | None = None
                  ) -> http.client.HTTPResponse:
    """A REAL `http.client.HTTPResponse`, begun, over a socket that delivers
    `pieces` one at a time. What `urlopen` returns, so the code under test
    decodes a chunked body -- or fails to -- with the standard library's own
    reader, not with a fake that already knows the answer."""
    reason = http.client.responses.get(status, "")
    head = f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")
    for name, value in headers.items():
        head += f"{name}: {value}\r\n".encode("latin-1")
    wire = _Wire(head + b"\r\n", pieces, advance)

    class _Socket:
        def makefile(self, mode, *args, **kwargs):
            return wire

    response = http.client.HTTPResponse(_Socket())
    response.begin()
    return response
