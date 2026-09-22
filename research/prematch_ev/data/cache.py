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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STAMP = "%Y-%m-%dT%H:%M:%SZ"

_SECRET = re.compile(r"(apiKey|api_key|token|secret)=([^&\s\"']+)", re.I)


def redact(text: str) -> str:
    """Scrub credentials from anything destined for a log or an error."""
    return _SECRET.sub(lambda m: f"{m.group(1)}=REDACTED", str(text))


def _digest(sport: str, stamp: str, bookmakers: str, markets: str) -> str:
    canonical = "|".join([sport.upper(), stamp, (bookmakers or "").lower(),
                          (markets or "").lower()])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def key_for(sport: str, at: datetime, bookmakers: str, markets: str) -> str:
    """A stable key for one request's MEANING, stamped in UTC.

    Deliberately takes the fields, not a URL: a URL carries the credential and
    a hash of it would silently bind the cache to one key while also embedding
    the secret in a filename.

    THE STAMP IS UTC, EXPLICITLY. It used to be `at.astimezone()` -- the
    MACHINE'S local time -- formatted with a literal "Z". On a machine that
    observes DST the two instants of the autumn fall-back hour render as the
    same wall clock, so they shared a key and the second request silently
    returned the FIRST one's snapshot: on US Central time, 06:30Z and 07:30Z on
    2026-11-01 hashed identically. An NFL Sunday straddles that change, and a
    5-minute collector puts twelve requests inside the hour. A cache key that
    can name two instants is a cache that can serve the wrong one.
    """
    return _digest(sport, at.astimezone(timezone.utc).strftime(STAMP),
                   bookmakers, markets)


def legacy_key_for(sport: str, at: datetime, bookmakers: str,
                   markets: str) -> str | None:
    """The key a pre-UTC cache stored this instant under, IF that is safe.

    Existing caches were written with local-time stamps, and every entry in
    them was paid for. Orphaning them would re-spend credits on a machine
    that is not on UTC. So an instant may still be READ from its legacy key --
    but never when that key is ambiguous: inside a fall-back fold the legacy
    stamp names two instants and the entry may belong to the other one. There
    the answer is None, the read misses, and the request is made again. A
    wasted credit is recoverable; a silently substituted snapshot is not.
    """
    local = at.astimezone()
    wall = local.replace(tzinfo=None)
    if (wall.replace(fold=0).astimezone(timezone.utc)
            != wall.replace(fold=1).astimezone(timezone.utc)):
        return None                        # a fold: the stamp names two instants
    legacy = _digest(sport, local.strftime(STAMP), bookmakers, markets)
    return None if legacy == key_for(sport, at, bookmakers, markets) else legacy


def resolve_key(cache: "ResponseCache", sport: str, at: datetime,
                bookmakers: str, markets: str) -> str:
    """THE key to read this instant from: current if present, else legacy.

    The fetch and the preflight both go through here, so the run and the
    estimate of what it will cost cannot disagree about what is cached
    (rule 19). Uses `has()`, which moves no statistics.
    """
    key = key_for(sport, at, bookmakers, markets)
    if cache.has(key):
        return key
    legacy = legacy_key_for(sport, at, bookmakers, markets)
    if legacy is not None and cache.has(legacy):
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
