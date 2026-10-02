"""On-disk cache for paid API responses.

A live smoke test spent 200 credits and lost its diagnostics to a crash in the
reporting path. Every credit bought a response that was thrown away, so each
attempt to debug a purely LOCAL problem -- a join, a parser, a report writer --
cost money again.

Two rules:

1. **THE CREDENTIAL NEVER ENTERS A KEY, A PATH OR A LOG.** The cache key is
   built from the request's *meaning* (sport, instant, book, market), never
   from the URL, because the URL carries `apiKey`. `key_for` takes the fields
   explicitly rather than a URL so a credential cannot be passed in by
   accident, and `redact` scrubs anything that reaches a message.
2. **Only successful, parseable responses are stored.** Caching a failure
   would make a transient outage permanent for every later replay.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

STAMP = "%Y-%m-%dT%H:%M:%SZ"

_SECRET = re.compile(r"(apiKey|api_key|token|secret)=([^&\s\"']+)", re.I)

#: The namespace every key is now written in, in its digest AND its filename.
#: Keys from before it are LEGACY, and a legacy key cannot say which instant
#: it holds -- see `legacy_keys_for`.
KEY_NAMESPACE = "v2"

#: How far before its request a snapshot may be when the body carries no
#: `next_timestamp` to prove it is the one the archive would answer with.
#: The archive answers with its latest snapshot AT OR BEFORE the request, on
#: a 5-minute grid; ten minutes allows one missing snapshot. It must stay
#: under the smallest gap between two instants one legacy key can confuse --
#: the difference between two UTC offsets, 15 minutes at the least (+5:30 and
#: +5:45) and usually an hour or more -- and it does.
MAX_UNPROVEN_SNAPSHOT_AGE = timedelta(minutes=10)


def redact(text: str) -> str:
    """Scrub credentials from anything destined for a log or an error."""
    return _SECRET.sub(lambda m: f"{m.group(1)}=REDACTED", str(text))


def parse_stamp(value: Any) -> datetime | None:
    """An archive timestamp, or None. The ONE parser for them: the cache
    verifies an entry with the same reading the snapshot parser uses."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _digest(sport: str, stamp: str, bookmakers: str, markets: str,
            namespace: str = "") -> str:
    fields = [sport.upper(), stamp, (bookmakers or "").lower(),
              (markets or "").lower()]
    if namespace:
        fields.insert(0, namespace)
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()[:32]


def key_for(sport: str, at: datetime, bookmakers: str, markets: str) -> str:
    """The key for one request's MEANING: namespaced, and stamped in UTC.

    Deliberately takes the fields, not a URL: a URL carries the credential and
    a hash of it would silently bind the cache to one key while also embedding
    the secret in a filename.

    THE STAMP IS UTC, EXPLICITLY. It used to be `at.astimezone()` -- the
    MACHINE'S local time -- formatted with a literal "Z". On a machine that
    observes DST the two instants of the autumn fall-back hour render as the
    same wall clock, so they shared a key and the second request silently
    returned the FIRST one's snapshot: on US Central time, 06:30Z and 07:30Z on
    2026-11-01 hashed identically.

    AND THE KEY IS NAMESPACED, because stamping in UTC was not enough. The
    UTC keys went into the SAME digest space as the local-time keys before
    them, and a local key read as a fallback then found UTC entries: on a
    UTC-5 machine the local key for 17:00Z is exactly the UTC key for 12:00Z,
    and a request for 17:00Z was served the 12:00Z snapshot. That is every
    request on a machine not on UTC, not only the DST hour. A namespaced key
    shares no digest and no filename with either scheme before it.
    """
    stamp = at.astimezone(timezone.utc).strftime(STAMP)
    return (f"{KEY_NAMESPACE}-"
            f"{_digest(sport, stamp, bookmakers, markets, KEY_NAMESPACE)}")


def legacy_keys_for(sport: str, at: datetime, bookmakers: str,
                    markets: str) -> list[str]:
    """Every pre-namespace key this instant may have been stored under.

    The original scheme's (this machine's LOCAL time with a literal "Z") and
    the interim one's (UTC). Every entry under them was paid for, so they are
    still read -- but a file under one is a CANDIDATE, never an answer. The
    two schemes share one digest space, so the file may be another instant's
    snapshot: the local key for 17:00Z on a UTC-5 machine is the interim key
    for 12:00Z, and inside a fall-back hour a local stamp names two instants.
    `resolve_key` serves a candidate only once `answer_problem` finds
    that its own snapshot stamps answer this request.
    """
    utc = at.astimezone(timezone.utc).strftime(STAMP)
    local = at.astimezone().strftime(STAMP)
    keys = [_digest(sport, utc, bookmakers, markets)]
    original = _digest(sport, local, bookmakers, markets)
    if original not in keys:
        keys.append(original)
    return keys


def answer_problem(payload: Any, at: datetime) -> str | None:
    """Why a snapshot body does NOT verifiably answer a request for `at`, or
    None when it does. The cache asks it of a legacy entry before serving
    one, and the replay asks it of every snapshot a bundle recorded the
    request instant of: one question, one answer (rule 19).

    The archive answers a request with its latest snapshot at or before it,
    so a body answers `at` exactly when `timestamp <= at < next_timestamp`.
    Equality with the request is NOT required -- the snapshot routinely
    precedes it -- and demanding it would miss every legitimate entry. A
    body without `next_timestamp` cannot prove the second half, and is held
    instead to at most `MAX_UNPROVEN_SNAPSHOT_AGE` before the request, which
    no other instant a legacy key can be confused with can satisfy. (An
    archive gap longer than that fails it too, and costs a re-purchase: the
    price of never serving a snapshot that cannot be shown to be the one.)

    Anything else is refused: a miss costs one credit, a snapshot served for
    the wrong instant costs the study.
    """
    if not isinstance(payload, dict):
        return "unreadable cache entry"
    taken = parse_stamp(payload.get("timestamp"))
    if taken is None or taken.tzinfo is None:
        return "no readable snapshot timestamp"
    if taken > at:
        return (f"snapshot {taken.isoformat()} is LATER than the request "
                f"{at.isoformat()}: another instant's")
    following = parse_stamp(payload.get("next_timestamp"))
    if following is not None and following.tzinfo is not None:
        if at >= following:
            return (f"the archive's next snapshot, {following.isoformat()}, "
                    f"is at or before the request {at.isoformat()}, so it "
                    f"would have answered instead")
        return None
    if at - taken > MAX_UNPROVEN_SNAPSHOT_AGE:
        return (f"snapshot {taken.isoformat()} is "
                f"{(at - taken).total_seconds():,.0f}s before the request, "
                f"beyond the {MAX_UNPROVEN_SNAPSHOT_AGE.total_seconds():.0f}s "
                f"a snapshot without next_timestamp may be trusted across")
    return None


def resolve_key(cache: "ResponseCache", sport: str, at: datetime,
                bookmakers: str, markets: str) -> str:
    """THE key to read this instant from.

    The namespaced key if it is on disk; else a legacy key whose entry
    VERIFIABLY answers this instant; else the namespaced key again, which
    then misses and is bought and stored there. The fetch and the preflight
    both go through here, so the run and the estimate of what it will cost
    cannot disagree about what is cached (rule 19). Reads with `has()` and
    `peek()`, which move no statistics.
    """
    key = key_for(sport, at, bookmakers, markets)
    if cache.has(key):
        return key
    for legacy in legacy_keys_for(sport, at, bookmakers, markets):
        if (cache.has(legacy)
                and answer_problem(cache.peek(legacy), at) is None):
            return legacy
    return key


@dataclass
class ResponseCache:
    """Read-through cache. Disabled when `root` is None."""

    root: Path | None = None
    hits: int = 0
    misses: int = 0
    writes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def has(self, key: str) -> bool:
        """Is this response already on disk? Does NOT count a hit or a miss.

        The preflight asks this to cost a run, and a preflight that moved the
        cache statistics would make the run it predicts look different from the
        run that happens.
        """
        return bool(self.enabled and self._path(key).exists())

    def peek(self, key: str) -> Any | None:
        """The stored body, read WITHOUT counting a hit or a miss -- for
        verifying an entry before deciding to serve it. None when absent or
        unreadable."""
        if not self.enabled:
            return None
        try:
            return json.loads(self._path(key).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt entry is a miss, never a crash and never a silent empty
            # response -- the latter would look like a market with no games.
            self.errors.append(redact(f"unreadable cache entry {key}: {exc}"))
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, key: str, payload: Any) -> None:
        if not self.enabled:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self._path(key).with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._path(key))
            self.writes += 1
        except (OSError, TypeError) as exc:
            self.errors.append(redact(f"could not cache {key}: {exc}"))

    def __str__(self) -> str:
        if not self.enabled:
            return "cache disabled"
        return (f"cache {self.hits} hit / {self.misses} miss / {self.writes} written"
                + (f" ({len(self.errors)} errors)" if self.errors else ""))


class CreditCapReached(RuntimeError):
    """Raised when a run would exceed its declared paid-request budget.

    A cap that merely warns is not a cap. This stops collection so the caller
    persists partial diagnostics rather than discovering the overspend after
    the fact.
    """
