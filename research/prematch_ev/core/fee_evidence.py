"""Reading Kalshi's dated fee record into the study's own schedule type. Pure.

The response shapes here are TRANSCRIBED from Kalshi's API documentation --
this repository has never received one, because the sessions that wrote it
cannot reach api.elections.kalshi.com. So the parser accepts only what the
documentation describes and REFUSES anything else, naming the keys it found;
`verify_fees.py` saves the raw body either way, so a refusal costs a re-read
of a file, not a guess at a fee.

Transcribed shapes:

    GET /series/fee_changes?series_ticker=S&show_historical=true
        {"series_fee_change_arr": [{"id": "...", "series_ticker": "S",
                                    "fee_type": "quadratic",
                                    "fee_multiplier": 1,
                                    "scheduled_ts": "2026-08-07T04:59:45.131Z"}]}
    GET /series/S
        {"series": {"ticker": "S", "fee_type": "quadratic",
                    "fee_multiplier": 1, ...}}

The list's KEY is matched loosely -- any top-level list whose members all
carry `scheduled_ts` or `fee_multiplier` -- because that is the part of the
transcription least worth trusting, and a strict match would refuse a correct
body over a name. The MEMBERS are matched strictly: a change without a date
or a multiplier is not a fee schedule. An EMPTY list is the one exception to
the loose key: "no changes" is an answer only under a key that says it is a
list of fee changes, or any stray empty list would read as a clean record.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Sequence

from .fees import FeeScheduleEntry, entry_in_force

REQUIRED = ("scheduled_ts", "fee_multiplier")


def _moment(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_fee_changes(payload: Any, series: str, *, source: str,
                      observed_on: str, observed_by: str
                      ) -> tuple[list[FeeScheduleEntry], list[str], str | None]:
    """(entries for `series`, problems, the list key used).

    Entries for OTHER series are dropped and counted; an entry for this one
    that lacks a readable date or multiplier is a problem, never skipped
    quietly -- a schedule with a hole in it would resolve the wrong entry.
    """
    problems: list[str] = []
    if not isinstance(payload, dict):
        kind = type(payload).__name__
        return [], [f"body was {kind}, expected an object"], None
    def names_fee_changes(key: str) -> bool:
        lowered = key.lower()
        return "fee" in lowered and "change" in lowered

    candidates = [key for key, value in payload.items()
                  if isinstance(value, list)
                  and all(isinstance(item, dict) for item in value)
                  and (all(any(k in item for k in REQUIRED) for item in value)
                       if value else names_fee_changes(key))]
    if len(candidates) != 1:
        return [], [f"expected exactly one list of fee changes; found "
                    f"{len(candidates)} candidate(s) among keys "
                    f"{sorted(payload)} -- refusing to guess"], None
    key = candidates[0]
    entries, others = [], 0
    for item in payload[key]:
        owner = item.get("series_ticker")
        if owner is not None and owner != series:
            others += 1
            continue
        when, multiplier = (_moment(item.get("scheduled_ts")),
                            _number(item.get("fee_multiplier")))
        if when is None or multiplier is None:
            problems.append(f"a change for {series} without a readable "
                            f"scheduled_ts/fee_multiplier: {item}")
            continue
        entries.append(FeeScheduleEntry(
            effective_from=when, multiplier=multiplier,
            fee_type=str(item.get("fee_type") or "unknown"), source=source,
            source_id=(str(item["id"]) if item.get("id") is not None
                       else None),
            observed_on=observed_on, observed_by=observed_by))
    if others:
        problems.append(f"{others} change(s) for other series were ignored")
    return sorted(entries, key=lambda e: e.effective_from), problems, key


def parse_series_fees(payload: Any) -> tuple[dict | None, list[str]]:
    """The series' CURRENT fee fields, or None and why."""
    if not isinstance(payload, dict) or not isinstance(payload.get("series"),
                                                       dict):
        keys = sorted(payload) if isinstance(payload, dict) else []
        return None, [f"no 'series' object in the body (keys: {keys})"]
    series = payload["series"]
    multiplier = _number(series.get("fee_multiplier"))
    if multiplier is None:
        return None, [f"the series object carries no readable fee_multiplier "
                      f"(keys: {sorted(series)})"]
    return {"ticker": series.get("ticker"), "fee_type": series.get("fee_type"),
            "fee_multiplier": multiplier}, []


def verdict(entries: Sequence[FeeScheduleEntry], current: dict | None,
            window: tuple[datetime, datetime], *, record_read: bool) -> dict:
    """What the record establishes about a window -- and what it does not.

    The entry in force is judged by `core.fees.entry_in_force`, the pricing
    path's own rule, so the answer printed here is the one the study would
    price with once the entry is recorded. `record_read` is whether the
    dated record ANSWERED: an unread record is not an empty one, and
    reporting "no change on record" for a read that failed would be the
    failed-read-as-zero error in its most expensive place.
    """
    start, end = window
    at_start, at_end = entry_in_force(entries, start), entry_in_force(entries,
                                                                      end)
    inside = [e for e in entries if start < e.effective_from <= end]
    establishes, open_questions = [], []
    if at_start is not None:
        establishes.append(
            f"multiplier {at_start.multiplier:g} ({at_start.fee_type}) was in "
            f"force at the window's start, by a change dated "
            f"{at_start.effective_from.isoformat()}")
        if at_end is not at_start:
            establishes.append(
                f"it changed INSIDE the window: {at_end.multiplier:g} from "
                f"{at_end.effective_from.isoformat()}")
    elif not record_read:
        open_questions.append(
            "the dated fee record could not be read, so NOTHING about the "
            "window's multiplier is established -- not even that no change "
            "is on record")
    elif entries:
        open_questions.append(
            f"the earliest dated change "
            f"({entries[0].effective_from.isoformat()}) "
            f"is AFTER the window's start: what applied before it is not on "
            f"record, and the oldest entry must not be extrapolated backwards")
    else:
        open_questions.append(
            "no dated fee change is on record for this series, so nothing "
            "here dates the multiplier the window was priced at")
        if current is not None:
            open_questions.append(
                f"the series currently reports multiplier "
                f"{current['fee_multiplier']:g} ({current.get('fee_type')}). "
                f"That is today's value; it establishes the window's only if "
                f"Kalshi's change history is complete, which this record "
                f"does not state")
    open_questions.append(
        "the ACCOUNT ROUTE (direct $0.0001 vs non-direct $0.01 alignment) is "
        "a property of the account, not the series; no public endpoint "
        "answers it")
    return {"dated_changes": len(entries),
            "in_force_at_start": _entry(at_start),
            "in_force_at_end": _entry(at_end),
            "changes_inside_window": [_entry(e) for e in inside],
            "current_series_fee": current,
            "establishes": establishes,
            "does_not_establish": open_questions}


def _entry(entry: FeeScheduleEntry | None) -> dict | None:
    if entry is None:
        return None
    return {"effective_from": entry.effective_from.isoformat(),
            "multiplier": entry.multiplier, "fee_type": entry.fee_type,
            "source_id": entry.source_id}


def entry_snippet(series: str, entries: Sequence[FeeScheduleEntry],
                  evidence: str) -> str:
    """A `KALSHI_SERIES_SCHEDULES` entry for a PERSON to review and paste.

    Printed, never applied. The evidence file and its hash go into
    `observed_by`, so the recorded entry points at the body it came from.
    """
    lines = [f'    "{series}": (']
    for entry in entries:
        e = replace(entry, observed_by=f"verify_fees.py; {evidence}")
        when = e.effective_from.astimezone(timezone.utc)
        lines += [
            "        FeeScheduleEntry(",
            f"            effective_from=datetime({when.year}, {when.month}, "
            f"{when.day}, {when.hour}, {when.minute}, {when.second}, "
            f"{when.microsecond}, tzinfo=timezone.utc),",
            f"            multiplier={e.multiplier!r},",
            f"            fee_type={e.fee_type!r},",
            f"            source={e.source!r},",
            f"            source_id={e.source_id!r},",
            f"            observed_on={e.observed_on!r},",
            f"            observed_by={e.observed_by!r},",
            "        ),"]
    lines.append("    ),")
    return "\n".join(lines)
