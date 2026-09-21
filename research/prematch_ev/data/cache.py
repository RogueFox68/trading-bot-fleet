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
from datetime import datetime
from pathlib import Path
from typing import Any

_SECRET = re.compile(r"(apiKey|api_key|token|secret)=([^&\s\"']+)", re.I)


def redact(text: str) -> str:
    """Scrub credentials from anything destined for a log or an error."""
    return _SECRET.sub(lambda m: f"{m.group(1)}=REDACTED", str(text))


def key_for(sport: str, at: datetime, bookmakers: str, markets: str) -> str:
    """A stable key for one request's MEANING.

    Deliberately takes the fields, not a URL: a URL carries the credential and
    a hash of it would silently bind the cache to one key while also embedding
    the secret in a filename.
    """
    canonical = "|".join([
        sport.upper(),
        at.astimezone().strftime("%Y-%m-%dT%H:%M:%SZ"),
        (bookmakers or "").lower(),
        (markets or "").lower(),
    ])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


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
