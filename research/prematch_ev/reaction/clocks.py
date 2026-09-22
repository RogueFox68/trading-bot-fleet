"""Separate clocks, immutable provenance, and the no-future-data rule.

FOUR CLOCKS, NEVER COLLAPSED
----------------------------
    source_update_time      when the SOURCE says the value changed
    provider_snapshot_time  when the provider captured or closed the record
    local_receipt_time      when WE received it  (live capture only; historical
                            replay has none, and none may be invented)
    scheduled_start         kickoff

A reaction study lives or dies on keeping these apart, because the interesting
quantity -- how long an executable price stayed behind -- is a difference
between two of them, and picking the wrong pair silently answers a different
question. Two specific ways that goes wrong, both guarded here:

1. **Measuring from the source's own update time.** A book move stamped
   `last_update=T` was knowable to us only once a snapshot carrying it was
   taken. `available_at()` is the one function that answers "when could our
   system have acted on this", and it never returns `source_update_time` for a
   historical record. Using the finer stamp would credit the strategy with
   information it did not have.

2. **Reading a fresh envelope as a fresh price.** The archive returns a
   snapshot every five minutes whether or not the book moved, so an envelope
   captured at 14:00 routinely carries a price the book last touched at 11:20.
   That envelope is fresh and its CONTENT is nearly three hours old.
   `content_age_seconds()` reports the second, `is_new_content_versus()`
   decides whether anything actually changed, and the detector triggers on
   content, never on arrival (see `detector`).

WHAT PROVENANCE IS FOR
----------------------
Every envelope carries the ids and the payload hash needed to re-derive it
from the source months later. A lag figure whose inputs cannot be located
again is not a finding, it is an anecdote -- and this study has already had to
throw away conclusions that came from fixtures nobody could point at.

UNKNOWN STAYS UNKNOWN
---------------------
`local_receipt_time=None` means we were not listening. It is not zero, not the
snapshot time, and not "close enough". Anything that needs a receipt time and
does not have one must say so and stop, which is the same rule the fleet
applies to a failed position read (rule 17).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Sequence

from .capability import (
    Clock, Resolution, SourceCapability, earliest_observable_at, source as
    capability_for,
)


class Origin(str, Enum):
    """How a record reached us. Decides which clock is executable."""

    HISTORICAL_REPLAY = "historical_replay"   # fetched after the fact
    LIVE_CAPTURE = "live_capture"             # observed as it happened


class FutureDataError(RuntimeError):
    """A join tried to read a record stamped after the decision instant.

    Raised, never warned. Lookahead is the one defect that makes every
    downstream number look better and none of them true, and this study has
    already shipped it twice -- a snapshot captured after the decision, and an
    execution quote written into the decision book. Both were found in review
    rather than by the code, which is why this is an exception.
    """


class ReceiptTimeUnknown(RuntimeError):
    """Something needed a local receipt time that historical data cannot have."""


@dataclass(frozen=True)
class Provenance:
    """Everything needed to find this record again at the source."""

    source: str
    event_id: str                    # provider's stable event id
    market_id: str                   # provider's stable market/contract id
    payload_sha256: str
    request_url: str = ""
    sequence: int | None = None      # provider ordering where one exists

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "event_id": self.event_id,
            "market_id": self.market_id,
            "payload_sha256": self.payload_sha256,
            "request_url": self.request_url,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class SourceEnvelope:
    """One observation from one source, with its clocks kept apart.

    `payload` is the already-parsed value (a quote, a candle). The envelope
    carries WHEN and WHERE FROM; what it means is the payload's business.
    """

    provenance: Provenance
    origin: Origin
    provider_snapshot_time: datetime
    payload: Any
    source_update_time: datetime | None = None
    local_receipt_time: datetime | None = None
    request_sent_at: datetime | None = None
    response_received_at: datetime | None = None
    resolution_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("provider_snapshot_time", "source_update_time",
                     "local_receipt_time", "request_sent_at",
                     "response_received_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
                # A naive stamp is not an instant. Attaching UTC would be
                # inventing the offset, and an evening kickoff is exactly where
                # that lands on the wrong day.
                raise ValueError(f"{name} is not timezone-aware: {value!r}")
        if self.origin is Origin.HISTORICAL_REPLAY and self.local_receipt_time:
            raise ValueError(
                "a historical record cannot carry a local receipt time; we "
                "were not listening when it happened")

    # --- the executable clock ---
    def available_at(self, snapshot_times: Sequence[datetime] | None = None
                     ) -> tuple[datetime | None, str]:
        """When our system could FIRST have acted on this record.

        LIVE capture: the receipt time. That is literally when we had it.

        HISTORICAL replay: the snapshot that carried it -- never
        `source_update_time`, however finely that is stamped. `snapshot_times`
        is accepted for signature symmetry with the series-level helpers and
        does not change this answer; see `earliest_availability` for "when did
        we FIRST see this value", which is a different question.
        """
        if self.origin is Origin.LIVE_CAPTURE:
            if self.local_receipt_time is None:
                return None, "live record with no receipt time recorded"
            return self.local_receipt_time, "local receipt time"
        # HISTORICAL: this record demonstrably appeared in THIS snapshot, so
        # that is when a poller holding this snapshot had it. `snapshot_times`
        # is deliberately NOT used to move the answer earlier -- if the same
        # value appeared in an earlier snapshot the replay has an envelope for
        # that snapshot, and `earliest_availability` is what finds it. Folding
        # both questions into one method is how a per-record answer starts
        # depending on which other records happen to be in scope.
        return self.provider_snapshot_time, (
            "provider snapshot that carried this record (historical replay "
            "has no receipt time)")

    def require_receipt_time(self) -> datetime:
        if self.local_receipt_time is None:
            raise ReceiptTimeUnknown(
                f"{self.provenance.source}: no local receipt time. Historical "
                "replay cannot supply one -- a prospective recorder can.")
        return self.local_receipt_time

    # --- content vs arrival ---
    def content_age_seconds(self) -> float | None:
        """How old the VALUE was when the provider captured it.

        None when the source published no update time: unknown, which is not
        fresh and must not be read as fresh.
        """
        if self.source_update_time is None:
            return None
        return (self.provider_snapshot_time
                - self.source_update_time).total_seconds()

    def content_key(self) -> str:
        """Identity of the CONTENT, so one value seen twice is seen once.

        A five-minutely archive re-serves an unchanged price every snapshot,
        and a live feed re-sends on reconnect. Both are the same observation
        arriving again. Keying on the content -- market, the source's own
        update stamp, and the payload hash -- is what lets a reconnect be
        recognised instead of counted as a move.
        """
        stamp = (self.source_update_time.isoformat()
                 if self.source_update_time else "unknown")
        return "|".join([
            self.provenance.source, self.provenance.market_id, stamp,
            self.provenance.payload_sha256,
        ])

    def is_new_content_versus(self, other: "SourceEnvelope | None") -> bool:
        """Did the underlying value change since `other`?

        False for a re-served identical record, whatever its arrival time.
        """
        if other is None:
            return True
        return self.content_key() != other.content_key()

    # --- reporting ---
    def clocks_as_dict(self) -> dict:
        return {
            "origin": self.origin.value,
            "source_update_time": _iso(self.source_update_time),
            "provider_snapshot_time": _iso(self.provider_snapshot_time),
            "local_receipt_time": _iso(self.local_receipt_time),
            "request_sent_at": _iso(self.request_sent_at),
            "response_received_at": _iso(self.response_received_at),
            "content_age_seconds": self.content_age_seconds(),
            "resolution_seconds": self.resolution_seconds,
        }

    def as_dict(self, snapshot_times: Sequence[datetime] | None = None) -> dict:
        available, reason = self.available_at(snapshot_times)
        out = self.provenance.as_dict()
        out.update(self.clocks_as_dict())
        out["available_at"] = _iso(available)
        out["available_at_reason"] = reason
        return out


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def payload_hash(payload: Any) -> str:
    """Stable digest of a record, for provenance."""
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               default=str)
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- the no-future-data rule ------------------------------------------------

def assert_no_future_data(decision_at: datetime,
                          envelope: SourceEnvelope,
                          snapshot_times: Sequence[datetime] | None = None,
                          what: str = "input") -> None:
    """Refuse a record that was not available at the decision instant.

    Checks the EXECUTABLE clock, not the source's own stamp: a book move whose
    `last_update` precedes the decision is still unusable if the snapshot that
    revealed it came afterwards.
    """
    available, reason = envelope.available_at(snapshot_times)
    if available is None:
        raise FutureDataError(
            f"{what} from {envelope.provenance.source} has no establishable "
            f"availability time ({reason}), so it cannot be shown to have been "
            f"usable at {decision_at.isoformat()}")
    if available > decision_at:
        raise FutureDataError(
            f"{what} from {envelope.provenance.source} "
            f"({envelope.provenance.market_id}) first became available at "
            f"{available.isoformat()} ({reason}), which is AFTER the decision "
            f"at {decision_at.isoformat()}. Using it would be lookahead.")


def usable_at(decision_at: datetime,
              envelopes: Iterable[SourceEnvelope],
              snapshot_times: Sequence[datetime] | None = None
              ) -> list[SourceEnvelope]:
    """Every record available at or before `decision_at`, oldest first.

    The filtering counterpart to `assert_no_future_data`: use this to build a
    decision book, and the assertion to prove a specific input was legitimate.
    """
    out = []
    for envelope in envelopes:
        available, _ = envelope.available_at(snapshot_times)
        if available is not None and available <= decision_at:
            out.append((available, envelope))
    out.sort(key=lambda pair: pair[0])
    return [envelope for _, envelope in out]


def earliest_availability(envelopes: Iterable[SourceEnvelope],
                          snapshot_times: Sequence[datetime] | None = None
                          ) -> dict[str, datetime]:
    """content_key -> the earliest snapshot that carried that value.

    The five-minutely archive re-serves an unchanged price every snapshot, so
    one book move appears in many envelopes. Our system knew it from the
    FIRST of them; crediting a later one would understate how long we had the
    information, and crediting the book's own `last_update` would overstate it.
    This is the series-level answer the per-record `available_at` deliberately
    does not try to give.
    """
    out: dict[str, datetime] = {}
    for envelope in envelopes:
        available, _ = envelope.available_at(snapshot_times)
        if available is None:
            continue
        key = envelope.content_key()
        if key not in out or available < out[key]:
            out[key] = available
    return out


def detection_blindness_seconds(envelope: SourceEnvelope,
                                earliest: dict[str, datetime] | None = None
                                ) -> float | None:
    """How long we were blind to this value after the source changed it.

    `source_update_time` -> first snapshot that carried it. None when the
    source published no update stamp, because the interval is then unknown
    rather than zero.
    """
    if envelope.source_update_time is None:
        return None
    first = (earliest or {}).get(envelope.content_key())
    if first is None:
        first, _ = envelope.available_at()
    if first is None:
        return None
    return (first - envelope.source_update_time).total_seconds()


def latest_usable(decision_at: datetime,
                  envelopes: Iterable[SourceEnvelope],
                  snapshot_times: Sequence[datetime] | None = None
                  ) -> SourceEnvelope | None:
    """The newest record our system could have held at `decision_at`."""
    usable = usable_at(decision_at, envelopes, snapshot_times)
    return usable[-1] if usable else None


def first_usable_at_or_after(decision_at: datetime,
                             envelopes: Iterable[SourceEnvelope],
                             max_wait: timedelta,
                             snapshot_times: Sequence[datetime] | None = None
                             ) -> tuple[SourceEnvelope | None, str]:
    """The first record available at/after a delayed decision, within a bound.

    This is the delayed-entry rule: a delay means waiting for the NEXT quote,
    not reaching back for the one that existed before the delay. `max_wait`
    bounds it, because a quote arriving two hours later is not the fill a
    ten-second delay would have got -- and returning it would quietly convert
    a missed opportunity into a taken one.
    """
    deadline = decision_at + max_wait
    candidates: list[tuple[datetime, SourceEnvelope]] = []
    for envelope in envelopes:
        available, _ = envelope.available_at(snapshot_times)
        if available is not None and decision_at <= available <= deadline:
            candidates.append((available, envelope))
    if not candidates:
        return None, (
            f"no record became available in the {max_wait.total_seconds():.0f}s "
            f"after {decision_at.isoformat()}: right-censored, not a fill")
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1], "first record available at or after the delay"


# --- builders ---------------------------------------------------------------

def envelope_for_sharp_quote(quote: Any, *, request_url: str = "",
                             sequence: int | None = None) -> SourceEnvelope:
    """Wrap a `data.odds_history.SharpQuote` with its clocks kept apart.

    `snapshot` is the provider capture; `last_update` is the book's own move
    stamp. The mapping is deliberately explicit here rather than guessed by
    attribute name, because getting it backwards is defect (1) in the module
    docstring and it does not look wrong in a diff.
    """
    capability = capability_for("the_odds_api_historical")
    payload = {
        "away_name": getattr(quote, "away_name", None),
        "home_name": getattr(quote, "home_name", None),
        "away_price": getattr(quote, "away_price", None),
        "home_price": getattr(quote, "home_price", None),
        "book": getattr(quote, "book", None),
        "commence_time": _iso(getattr(quote, "commence_time", None)),
    }
    return SourceEnvelope(
        provenance=Provenance(
            source="the_odds_api_historical",
            event_id=str(getattr(quote, "provider_event_id", "")),
            market_id=f"{getattr(quote, 'provider_event_id', '')}:h2h",
            payload_sha256=payload_hash(payload),
            request_url=request_url,
            sequence=sequence,
        ),
        origin=Origin.HISTORICAL_REPLAY,
        provider_snapshot_time=getattr(quote, "snapshot"),
        source_update_time=getattr(quote, "last_update", None),
        payload=quote,
        resolution_seconds=(capability.resolution_seconds if capability else None),
    )


def envelope_for_candle(candle: Any, market_ticker: str, *,
                        request_url: str = "",
                        sequence: int | None = None) -> SourceEnvelope:
    """Wrap a `data.kalshi_history.Candle`.

    A candle's `ts` is its period CLOSE, so it is the provider snapshot time.
    `source_update_time` stays None: the candle does not say when inside its
    period the quote moved, and inventing a point inside would manufacture the
    very precision this study is trying not to claim.
    """
    capability = capability_for("kalshi_candlesticks_historical")
    payload = {
        "bid_close": getattr(candle, "bid_close", None),
        "ask_close": getattr(candle, "ask_close", None),
        "ts": _iso(getattr(candle, "ts", None)),
    }
    return SourceEnvelope(
        provenance=Provenance(
            source="kalshi_candlesticks_historical",
            event_id=market_ticker.rsplit("-", 1)[0],
            market_id=market_ticker,
            payload_sha256=payload_hash(payload),
            request_url=request_url,
            sequence=sequence,
        ),
        origin=Origin.HISTORICAL_REPLAY,
        provider_snapshot_time=getattr(candle, "ts"),
        source_update_time=None,
        payload=candle,
        resolution_seconds=(capability.resolution_seconds if capability else None),
    )
