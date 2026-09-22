"""Separate clocks, immutable provenance, and the no-future-data rule.

FOUR CLOCKS, NEVER COLLAPSED
----------------------------
    provider_observed_at    when the PROVIDER last saw this value from the
                            source. NOT when the source changed it.
    provider_snapshot_time  when the provider captured or closed the record
    local_receipt_time      when WE received it  (live capture only; historical
                            replay has none, and none may be invented)
    scheduled_start         kickoff

WHAT `last_update` ACTUALLY MEANS  (corrected 2026-09-22, PR #27 review)
------------------------------------------------------------------------
The Odds API's market `last_update` is **the last time the provider's system
saw odds for that market from the bookmaker** -- not the moment the bookmaker
changed the price. (Bookmaker-level `last_update` is deprecated. Source:
https://the-odds-api.com/liveapi/guides/v4/, "More info" beneath GET odds.)

An earlier version of this module asserted the opposite, inheriting the claim
from `SharpQuote`'s docstring, and named the detector's output
`book_moved_at`. That was wrong, and wrong in the direction that matters: it
put a confident timestamp on an event nobody observed.

**The true book-change instant is not in this data at all.** What IS available
is a BRACKET: the price changed somewhere after the previous provider
observation of an unchanged value and at or before the observation that
carried the new one. `book_change_bracket` reports that interval; nothing
reports a point.

THREE CLOCKS, THREE DIFFERENT QUESTIONS
---------------------------------------
    provider_observed_at -> provider_snapshot_time     capture age
    provider_observed_at -> available_at               age at the decision
    previous observation -> provider_observed_at       the change bracket

Collapsing any pair answers a different question than the one asked. The
interesting quantity -- how long an executable price stayed behind -- is a
difference between two of them, and picking the wrong pair is silent.

`available_at` IS AN ASSUMPTION, NOT A MEASUREMENT
--------------------------------------------------
For a historical record it returns the provider snapshot time, which asserts
**zero delivery delay**: that a live system polling this archive would have
had the record the instant the provider stamped it. That is a replay
assumption, labelled `ZERO_DELIVERY_DELAY_ASSUMED` on every answer, not proof
of when our system could actually have received anything. Real delivery lag is
unmeasurable here (see `capability`) and only a prospective recorder with a
real `local_receipt_time` can bound it.

TWO SPECIFIC FAILURES THIS GUARDS
---------------------------------
1. **Reading a fresh envelope as a fresh price.** The archive returns a
   snapshot every five minutes whether or not the price moved, so an envelope
   captured at 14:00 routinely carries a value the provider last saw at 11:20.
   That envelope is fresh and its CONTENT is nearly three hours old.
   `content_age_seconds()` reports the latter, `is_new_content_versus()`
   decides whether anything changed, and the detector triggers on content.

2. **Clocks out of order.** `provider_observed_at` after
   `provider_snapshot_time` is impossible -- a provider cannot capture a value
   it has not yet seen -- and it yields a NEGATIVE age that sails through any
   upper-bound-only freshness test. `clock_order_problem()` names it, and the
   detector refuses the record rather than triggering on it.

WHAT PROVENANCE IS FOR
----------------------
Every envelope carries the ids and the payload hash needed to re-derive it
from the source months later. A lag figure whose inputs cannot be located
again is not a finding, it is an anecdote.

STREAM IDENTITY INCLUDES THE BOOK
---------------------------------
`market_id` carries provider, book, event and market, because state keyed on
the event alone lets two bookmakers' quotes overwrite each other -- and a
second book's different price then reads as the first book moving.

UNKNOWN STAYS UNKNOWN
---------------------
`local_receipt_time=None` means we were not listening. It is not zero, not the
snapshot time, and not "close enough" (rule 17).
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


# The label every historical availability answer carries. It is an ASSUMPTION
# -- that a live poller would have held the record the instant the provider
# stamped it -- and it is stated on the answer so no reader can mistake it for
# a measurement of delivery lag, which this data cannot provide.
ZERO_DELIVERY_DELAY_ASSUMED = (
    "provider snapshot that carried this record; ZERO DELIVERY DELAY ASSUMED "
    "(historical replay has no receipt time, so real delivery lag is "
    "unmeasurable here)")

# A provider cannot capture a value it has not yet seen, so
# provider_observed_at must not follow provider_snapshot_time. A small
# tolerance is allowed for clock skew between the provider's own systems;
# beyond it the record is refused rather than trusted, because the resulting
# age is NEGATIVE and a negative age passes every upper-bound freshness test
# ever written.
CLOCK_SKEW_TOLERANCE = timedelta(seconds=2)


def clock_order_problem(envelope: "SourceEnvelope",
                        tolerance: timedelta = CLOCK_SKEW_TOLERANCE
                        ) -> str | None:
    """Name an impossible clock ordering, or None when the record is coherent.

    Checked BEFORE any freshness bound, because an out-of-order pair produces
    a negative age that an upper-bound test reads as extremely fresh.
    """
    observed = envelope.provider_observed_at
    snapshot = envelope.provider_snapshot_time
    if observed is not None and observed > snapshot + tolerance:
        drift = (observed - snapshot).total_seconds()
        return (f"provider_observed_at is {drift:.0f}s AFTER "
                f"provider_snapshot_time: a provider cannot capture a value it "
                f"has not yet seen, and the resulting age is negative")
    receipt = envelope.local_receipt_time
    if receipt is not None and receipt + tolerance < snapshot:
        drift = (snapshot - receipt).total_seconds()
        return (f"local_receipt_time is {drift:.0f}s BEFORE "
                f"provider_snapshot_time: we cannot have received a record "
                f"before it was captured")
    if (envelope.request_sent_at and envelope.response_received_at
            and envelope.response_received_at + tolerance
            < envelope.request_sent_at):
        return "response_received_at precedes request_sent_at"
    # Both of these are OUR clock, so no skew allowance: acting on a response
    # before the whole of it has arrived is impossible, not imprecise.
    if (receipt is not None and envelope.response_received_at is not None
            and receipt < envelope.response_received_at):
        return ("local_receipt_time precedes response_received_at: a record "
                "cannot be acted on before its response has fully arrived")
    return None


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
    provider_observed_at: datetime | None = None
    local_receipt_time: datetime | None = None
    request_sent_at: datetime | None = None
    response_received_at: datetime | None = None
    resolution_seconds: float | None = None

    def __post_init__(self) -> None:
        for name in ("provider_snapshot_time", "provider_observed_at",
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
        `provider_observed_at`, however finely that is stamped. `snapshot_times`
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
        return self.provider_snapshot_time, ZERO_DELIVERY_DELAY_ASSUMED

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
        if self.provider_observed_at is None:
            return None
        return (self.provider_snapshot_time
                - self.provider_observed_at).total_seconds()

    def age_at_decision_seconds(self,
                                snapshot_times: Sequence[datetime] | None = None
                                ) -> float | None:
        """How old the VALUE was when our system could act on it.

        THE freshness clock. `content_age_seconds` measures to the provider's
        CAPTURE, which for a live record can be long before we received it: a
        record captured at 12:05 and received at 12:25 is 20 minutes old when
        it becomes actionable, and a capture-age test calls it fresh. The
        bound belongs on the clock the decision runs on.

        None when either end is unknown -- which is not fresh (rule 17).
        """
        if self.provider_observed_at is None:
            return None
        available, _ = self.available_at(snapshot_times)
        if available is None:
            return None
        return (available - self.provider_observed_at).total_seconds()

    def content_key(self) -> str:
        """Identity of the CONTENT, so one value seen twice is seen once.

        A five-minutely archive re-serves an unchanged price every snapshot,
        and a live feed re-sends on reconnect. Both are the same observation
        arriving again. Keying on the content -- market, the source's own
        update stamp, and the payload hash -- is what lets a reconnect be
        recognised instead of counted as a move.
        """
        stamp = (self.provider_observed_at.isoformat()
                 if self.provider_observed_at else "unknown")
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
            "provider_observed_at": _iso(self.provider_observed_at),
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

    `provider_observed_at` -> first snapshot that carried it. None when the
    source published no update stamp, because the interval is then unknown
    rather than zero.
    """
    if envelope.provider_observed_at is None:
        return None
    first = (earliest or {}).get(envelope.content_key())
    if first is None:
        first, _ = envelope.available_at()
    if first is None:
        return None
    return (first - envelope.provider_observed_at).total_seconds()


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

def orientation_key(away_name: str, home_name: str) -> str:
    """Away/home orientation, as part of identity.

    A quote whose sides are swapped relative to the baseline is not the same
    stream: comparing them would read a re-labelling as a large price move.
    """
    return f"{(away_name or '?').strip()}@{(home_name or '?').strip()}"


def stream_id(*, source: str, book: str, event_id: str, market: str,
              orientation: str = "") -> str:
    """The identity a detector keys its state on. Book included, always."""
    parts = [source, book.lower(), event_id, market]
    if orientation:
        parts.append(orientation)
    return "|".join(parts)


def envelope_for_sharp_quote(quote: Any, *, request_url: str = "",
                             sequence: int | None = None) -> SourceEnvelope:
    """Wrap a `data.odds_history.SharpQuote` with its clocks kept apart.

    `snapshot` is the provider capture; `last_update` is the book's own move
    stamp. The mapping is deliberately explicit here rather than guessed by
    attribute name, because getting it backwards is defect (1) in the module
    docstring and it does not look wrong in a diff.
    """
    capability = capability_for("the_odds_api_historical")
    return _sharp_envelope(
        quote, source="the_odds_api_historical",
        origin=Origin.HISTORICAL_REPLAY, request_url=request_url,
        sequence=sequence,
        resolution_seconds=(capability.resolution_seconds
                            if capability else None))


#: The live feed's source name. A different source from the archive on
#: purpose: the stream id carries it, so a live quote can never be compared
#: against an archived one as though the two were one feed.
LIVE_SHARP_SOURCE = "the_odds_api_live"


def envelope_for_live_sharp_quote(quote: Any, *, received_at: datetime,
                                  sent_at: datetime | None = None,
                                  ready_at: datetime | None = None,
                                  request_url: str = "",
                                  resolution_seconds: float | None = None,
                                  sequence: int | None = None
                                  ) -> SourceEnvelope:
    """Wrap a quote from a LIVE poll. The executable clock is OUR READINESS.

    `quote.snapshot` is the provider's own clock for the response (its HTTP
    `Date`) when it sent one; `last_update` is still the provider's
    observation, never the book's change. `received_at` is when the WHOLE
    response had arrived, and `ready_at` when it had been read and parsed:
    the first instant a live system could act on it -- the clock the archive
    can only assume. They are kept apart (`response_received_at` and
    `local_receipt_time`), so the time spent on the body is recorded as a
    delay rather than credited as time available to trade. Without
    `ready_at` the receipt is both. `resolution_seconds` is the POLL cadence:
    the archive's five-minute grid does not describe a live poll, and
    borrowing it would understate what the live feed can resolve.
    """
    return _sharp_envelope(
        quote, source=LIVE_SHARP_SOURCE, origin=Origin.LIVE_CAPTURE,
        request_url=request_url, sequence=sequence,
        resolution_seconds=resolution_seconds, received_at=received_at,
        sent_at=sent_at, ready_at=ready_at)


def _sharp_envelope(quote: Any, *, source: str, origin: Origin,
                    request_url: str, sequence: int | None,
                    resolution_seconds: float | None,
                    received_at: datetime | None = None,
                    sent_at: datetime | None = None,
                    ready_at: datetime | None = None) -> SourceEnvelope:
    """The one body both sharp-quote envelopes are built from, so live and
    archived quotes cannot differ in stream identity or payload hashing."""
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
            source=source,
            event_id=str(getattr(quote, "provider_event_id", "")),
            # STREAM IDENTITY INCLUDES THE BOOK. Keyed on the event alone, two
            # bookmakers' quotes overwrite each other in the detector's state
            # and the second book's different price reads as the first book
            # moving. Provider + book + event + market is the stream.
            market_id=stream_id(
                source=source,
                book=str(getattr(quote, "book", "") or "unknown"),
                event_id=str(getattr(quote, "provider_event_id", "")),
                market="h2h",
                orientation=orientation_key(
                    getattr(quote, "away_name", ""),
                    getattr(quote, "home_name", ""))),
            payload_sha256=payload_hash(payload),
            request_url=request_url,
            sequence=sequence,
        ),
        origin=origin,
        provider_snapshot_time=getattr(quote, "snapshot"),
        provider_observed_at=getattr(quote, "last_update", None),
        local_receipt_time=ready_at or received_at,
        request_sent_at=sent_at,
        response_received_at=received_at,
        payload=quote,
        resolution_seconds=resolution_seconds,
    )


def envelope_for_candle(candle: Any, market_ticker: str, *,
                        request_url: str = "",
                        sequence: int | None = None) -> SourceEnvelope:
    """Wrap a `data.kalshi_history.Candle`.

    A candle's `ts` is its period CLOSE, so it is the provider snapshot time.
    `provider_observed_at` stays None: the candle does not say when inside its
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
        provider_observed_at=None,
        payload=candle,
        resolution_seconds=(capability.resolution_seconds if capability else None),
    )
