"""NFL kickoff times from the public ESPN scoreboard, as an external source.

WHY THIS EXISTS
---------------
A Kalshi NFL event body carries a DATE and no kickoff:

    MLB  26SEP152140MIAAZ   date + HHMM + teams   -> the start is in the ticker
    NFL  26SEP14DENKC       date + teams, NO TIME -> the ticker cannot say when

The checkpoint grid is measured in hours, so a day is not enough, and the
contract-lifecycle fields are not a substitute: on the sampled KC contract
`close_time`, `expected_expiration_time` and `settlement_ts` are 03:15:19Z,
03:15:00Z and 03:21:19Z on the day AFTER kickoff. Counting lead times back
from those would measure the wrong thing precisely. This module supplies the
missing fact from a free, external, public endpoint instead of inferring it.

WHAT THIS IS NOT
----------------
**A schedule fetched now does not prove what was known 72 hours before
kickoff.** This adapter retrieves the schedule as it stands at retrieval time.
For a game that was flexed or rescheduled, the kickoff it returns is the FINAL
one, not the one a trader would have been counting back from three days out.
Every snapshot therefore carries `historical_schedule_as_of = "unverified"`
(`SCHEDULE_PROVENANCE`) and that label rides into the run manifest and the
readiness report.

The consequence is a scope limit, not a caveat to be waved through: this
supports an exploratory, retrospective availability and cost assessment. It
**cannot certify a point-in-time backtest or a live strategy.** A prospective
or holdout run needs schedule snapshots recorded BEFORE the decisions they
date, which is a different artifact that this module does not produce.

NOTHING HERE WAS FETCHED IN-SESSION
-----------------------------------
`site.api.espn.com` is refused by the egress proxy from the sessions this was
written in (gateway answered 403 to CONNECT). The payload shapes below, and
the fixtures in `tests/test_schedule.py`, are TRANSCRIBED from two responses
the repository owner fetched on 2026-09-21 and pasted into review. They are
labelled as transcribed wherever they are relied on, exactly as the Kalshi fee
schedules are, because a shape nobody re-read in-session is not a verified
shape. The fourth defect in this study was an invented ticker fixture; the
rule that came out of it is that a fixture is copied from a real response or
it is not evidence.

THE OBSERVED PROJECTION
-----------------------
    {"id": "401872930",
     "date": "2026-09-14T00:20Z",
     "name": "Dallas Cowboys at New York Giants",
     "competitions": [
        {"date": "2026-09-14T00:20Z",
         "timeValid": true,
         "competitors": [
            {"homeAway": "home", "team": {"id": "19", "abbreviation": "NYG",
                                          "displayName": "New York Giants"}},
            {"homeAway": "away", "team": {"id": "6",  "abbreviation": "DAL",
                                          "displayName": "Dallas Cowboys"}}]}]}

THE DATE TRAP THIS IS BUILT AROUND
----------------------------------
The Kalshi ticker day is NOT the UTC kickoff day, and equating them silently
shifts every lead time by up to a day:

    KXNFLGAME-26SEP13DALNYG  ticker day 2026-09-13  kickoff 2026-09-14T00:20Z
    KXNFLGAME-26SEP14DENKC   ticker day 2026-09-14  kickoff 2026-09-15T00:15Z

Both are US-evening games whose UTC instant lands after midnight. The ticker
day is a LOCAL (US Eastern) calendar day, so that is what candidates are
compared on -- see `SCHEDULE_TIMEZONE` and `local_date`. A one-day tolerance
is allowed around it for the international slate, but the match must still be
UNIQUE, and the offset that was used is recorded on every resolution so a
systematically wrong timezone assumption shows up as a population of non-zero
offsets rather than as quietly shifted timestamps.

VERSION SENSITIVITY
-------------------
This is an undocumented public endpoint. It can change shape without notice,
and the failure that matters is not a crash but a plausible wrong answer, so
every structural expectation is asserted rather than assumed:

  * a missing `timeValid` is a REJECTION, not an assumed-valid kickoff. The
    flag is the only thing separating a real kickoff from a placeholder, and a
    placeholder that enters the study makes every lead time on that game wrong
    while looking exactly like a real one.
  * `event.date` and `competitions[0].date` must agree.
  * exactly one competition, exactly two competitors, exactly one home and one
    away, two DIFFERENT teams.
  * timestamps must parse AND be timezone-aware.
  * a status block is read strictly when present and never invented when
    absent: the observed projection carries none, so its absence is not
    evidence of a scheduled game and is not treated as one either way.

SCORES ARE DELIBERATELY NOT CARRIED. The scoreboard endpoint serves results,
and this study's decisions must never see them. `ScheduleEvent` has no field
for a score and `parse_scoreboard` never reads one, so a result cannot reach a
feature or a checkpoint through this path even by accident.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from core.matcher import ROSTERS, resolve_team

# --- provider constants -----------------------------------------------------

SCHEDULE_SOURCE = "espn_nfl_scoreboard"
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
    "?dates={bucket}"
)
# The endpoint is free and unauthenticated. It carries no credential, so
# nothing here needs the redaction discipline the paid cache has -- and
# nothing here may ever acquire one.
REQUEST_TIMEOUT = 20
RETRIES = 3
BACKOFF_SECONDS = (1, 3)

# The retrieved schedule is the CURRENT schedule, not the schedule as it stood
# at any earlier decision. See the module docstring.
SCHEDULE_PROVENANCE = "unverified"

# The ticker day and the ESPN date bucket are both US-Eastern calendar days.
# This is a DECLARED ASSUMPTION, cross-checked by `day_offset` on every
# resolution rather than trusted.
SCHEDULE_TIMEZONE = "America/New_York"

# How far either side of the ticker day a candidate may sit. One day covers
# the international slate (a 09:30 ET London kickoff is the same local day, but
# a future fixture in another hemisphere need not be) without ever admitting a
# second candidate: two NFL teams do not play each other twice inside a
# three-day span, so uniqueness still does the separating.
DAY_TOLERANCE = 1

# Daily buckets fetched either side of the requested window. A game on the
# first day of the window can be listed in the previous day's bucket if the
# provider ever buckets by UTC rather than local day; a buffer costs one free
# request and removes the question.
BUCKET_BUFFER_DAYS = 1

# ESPN abbreviation -> this module's canonical roster abbreviation, for the
# cases where the two vocabularies diverge. EMPTY BY POLICY: only codes
# OBSERVED in a real response belong here. An unobserved guess would silently
# bind a team to the wrong roster entry, and the name path below already
# resolves a divergent abbreviation through the closed-roster matcher, which
# rejects rather than guesses. The resolution PATH is recorded per team, so a
# league resolving by name rather than by code is visible in the report and
# enumerable into this map from one run.
ESPN_CODE_ALIASES: dict[str, dict[str, str]] = {
    "NFL": {},
}

# Status values that mean the game did not happen as scheduled. Read only when
# the payload actually carries a status block.
NOT_PLAYED_STATES = {
    "STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED",
    "STATUS_SUSPENDED", "STATUS_ABANDONED",
}

# --- named failures ---------------------------------------------------------
# Every one of these is a REASON, never an inferred kickoff. They are the
# vocabulary the ledger and the coverage matrix report, so a schedule that
# cannot be resolved fails loudly and by name.

FAIL_FETCH = "schedule_fetch_failed"
FAIL_PAYLOAD = "schedule_payload_malformed"
FAIL_EVENT = "schedule_event_malformed"
FAIL_MISSING_ID = "schedule_event_id_missing"
FAIL_KICKOFF_MISSING = "schedule_kickoff_missing"
FAIL_KICKOFF_UNPARSEABLE = "schedule_kickoff_unparseable"
FAIL_KICKOFF_NAIVE = "schedule_kickoff_not_timezone_aware"
FAIL_TIME_TBD = "schedule_kickoff_tbd"
FAIL_TIME_VALID_MISSING = "schedule_time_valid_missing"
FAIL_DATE_MISMATCH = "schedule_event_competition_date_mismatch"
FAIL_COMPETITION_COUNT = "schedule_competition_count"
FAIL_COMPETITOR_COUNT = "schedule_competitor_count"
FAIL_HOMEAWAY = "schedule_home_away_invalid"
FAIL_TEAM_UNRESOLVED = "schedule_team_unresolved"
FAIL_TEAM_DUPLICATE = "schedule_team_duplicate"
FAIL_NOT_PLAYED = "schedule_game_not_played"
FAIL_EVENT_CONFLICT = "schedule_event_conflict"

# resolution-side failures
FAIL_NO_MATCH = "schedule_no_event_for_matchup"
FAIL_AMBIGUOUS = "schedule_ambiguous_matchup"
FAIL_NO_TICKER_DAY = "schedule_ticker_day_unreadable"
FAIL_NO_SNAPSHOT = "schedule_snapshot_absent"


class ScheduleFetchError(RuntimeError):
    """A bucket could not be retrieved. Silence, never an empty slate.

    The fleet's rule 17 one layer out: a failed read is not a zero. An empty
    schedule and an unreachable provider both produce no games, and only one
    of them means there were none.
    """


def local_date(moment: datetime, tz_name: str = SCHEDULE_TIMEZONE) -> date:
    """The US-Eastern calendar day an instant falls on.

    This is the whole point of the module's date handling: 2026-09-15T00:15Z
    is the evening of 2026-09-14 in New York, which is the day the ticker
    `26SEP14DENKC` names.
    """
    return moment.astimezone(ZoneInfo(tz_name)).date()


def _parse_instant(raw: Any) -> tuple[datetime | None, str | None]:
    """Parse one provider timestamp. Returns (value, failure-reason)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, FAIL_KICKOFF_MISSING
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None, FAIL_KICKOFF_UNPARSEABLE
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        # A naive timestamp is not an instant. Attaching UTC to it would be
        # inventing the offset, which on an evening kickoff is exactly the
        # one-day error this module exists to avoid.
        return None, FAIL_KICKOFF_NAIVE
    return parsed.astimezone(timezone.utc), None


def payload_hash(payload: Any) -> str:
    """Stable digest of a raw payload, for the run manifest.

    Sorted keys so the same response hashes the same however it was decoded.
    """
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- records ----------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleFailure:
    """One named reason a game did not become a usable schedule entry."""

    reason: str
    detail: str = ""
    provider_event_id: str | None = None

    def __str__(self) -> str:
        who = f" [{self.provider_event_id}]" if self.provider_event_id else ""
        return f"{self.reason}{who}: {self.detail}" if self.detail else \
            f"{self.reason}{who}"


@dataclass(frozen=True)
class ScheduleEvent:
    """One scheduled game, with the provenance needed to audit it later.

    Carries NO score, NO result and NO status beyond "this was scheduled and
    not cancelled". The endpoint serves results; the study must not see them.
    """

    provider_event_id: str
    kickoff: datetime                 # tz-aware, UTC
    away: str                         # canonical roster abbreviation
    home: str
    league: str
    source_url: str
    retrieved_at: datetime
    payload_sha256: str
    date_bucket: str                  # the YYYYMMDD actually queried
    away_resolved_by: str = "exact_abbreviation"
    home_resolved_by: str = "exact_abbreviation"

    @property
    def matchup(self) -> frozenset[str]:
        return frozenset({self.away, self.home})

    @property
    def local_day(self) -> date:
        return local_date(self.kickoff)

    @property
    def resolved_by_name(self) -> bool:
        """Either side needed the fuzzy name path rather than a code match."""
        return (self.away_resolved_by != "exact_abbreviation"
                or self.home_resolved_by != "exact_abbreviation")

    def provenance(self) -> dict[str, Any]:
        """Everything needed to re-derive this kickoff from the source."""
        return {
            "schedule_source": SCHEDULE_SOURCE,
            "source_url": self.source_url,
            "provider_event_id": self.provider_event_id,
            "retrieved_at": self.retrieved_at.astimezone(timezone.utc).isoformat(),
            "payload_sha256": self.payload_sha256,
            "date_bucket": self.date_bucket,
            "kickoff": self.kickoff.astimezone(timezone.utc).isoformat(),
            "historical_schedule_as_of": SCHEDULE_PROVENANCE,
            "away_resolved_by": self.away_resolved_by,
            "home_resolved_by": self.home_resolved_by,
        }


@dataclass(frozen=True)
class StartResolution:
    """The outcome of asking the schedule for one contract's kickoff."""

    kickoff: datetime | None
    event: ScheduleEvent | None
    reason: str | None
    detail: str = ""
    day_offset: int = 0

    @property
    def resolved(self) -> bool:
        return self.kickoff is not None

    def provenance(self) -> dict[str, Any]:
        if self.event is None:
            return {"schedule_source": SCHEDULE_SOURCE,
                    "resolution_failure": self.reason,
                    "detail": self.detail}
        out = self.event.provenance()
        out["ticker_day_offset"] = self.day_offset
        return out


# --- parsing ----------------------------------------------------------------


def _resolve_side(team: Any, league: str) -> tuple[str | None, str, str]:
    """(abbreviation, how-it-resolved, detail) for one competitor's team.

    The ESPN abbreviation vocabulary is not guaranteed to be this module's, so
    a code match is tried first and the closed-roster NAME matcher second. The
    name matcher rejects on a low score or an ambiguous pair rather than
    guessing, and the path that succeeded is returned so a league resolving by
    name rather than by code is visible rather than silent.
    """
    if not isinstance(team, dict):
        return None, "", "competitor carries no team object"
    roster = ROSTERS.get(league.upper(), {})
    raw = str(team.get("abbreviation", "") or "").strip().upper()
    code = ESPN_CODE_ALIASES.get(league.upper(), {}).get(raw, raw)
    if code and code in roster:
        return code, "exact_abbreviation", ""

    name = str(team.get("displayName", "") or "").strip()
    if not name:
        return None, "", f"abbreviation {raw!r} is not in the {league} roster " \
                         "and no displayName was supplied"
    resolution = resolve_team(name, league)
    if not resolution.resolved:
        return None, "", (f"abbreviation {raw!r} is not in the {league} roster "
                          f"and name {name!r} did not resolve: "
                          f"{resolution.reject_reason}")
    return (resolution.abbreviation,
            f"name_fuzzy:{resolution.score:.1f}",
            f"ESPN code {raw!r} is not canonical; resolved via {name!r}")


def _competition_status_blocks_play(competition: dict) -> str | None:
    """A cancellation/postponement, when the payload says so. Never inferred.

    The observed projection carries no status block. Its ABSENCE is therefore
    not evidence that a game was played as scheduled, and is not treated as
    evidence of the opposite either -- it simply yields no finding here, and
    the game stands or falls on the rest of its fields.
    """
    status = competition.get("status")
    if not isinstance(status, dict):
        return None
    kind = status.get("type")
    if not isinstance(kind, dict):
        return None
    for key in ("name", "state"):
        value = str(kind.get(key, "") or "").strip().upper()
        if value in NOT_PLAYED_STATES:
            return value
    return None


def parse_scoreboard(
    payload: Any,
    *,
    league: str,
    source_url: str,
    retrieved_at: datetime,
    date_bucket: str,
    sha256: str | None = None,
) -> tuple[list[ScheduleEvent], list[ScheduleFailure]]:
    """Turn one scoreboard response into events and named failures.

    Every structural expectation in the module docstring is checked here. A
    game that fails any of them produces a ScheduleFailure and NO event: there
    is no partial entry and no inferred kickoff.
    """
    digest = sha256 if sha256 is not None else payload_hash(payload)
    events: list[ScheduleEvent] = []
    failures: list[ScheduleFailure] = []

    if not isinstance(payload, dict):
        return [], [ScheduleFailure(FAIL_PAYLOAD,
                                    f"top level is {type(payload).__name__}, "
                                    "expected an object")]
    raw_events = payload.get("events")
    if not isinstance(raw_events, list):
        return [], [ScheduleFailure(
            FAIL_PAYLOAD,
            f"'events' is {type(raw_events).__name__}, expected a list -- the "
            "endpoint shape has changed")]

    for raw in raw_events:
        if not isinstance(raw, dict):
            failures.append(ScheduleFailure(
                FAIL_EVENT, f"event is {type(raw).__name__}, expected an object"))
            continue

        event_id = str(raw.get("id", "") or "").strip()
        if not event_id:
            failures.append(ScheduleFailure(
                FAIL_MISSING_ID, "event carries no id, so it cannot be "
                                 "deduplicated across buckets"))
            continue

        competitions = raw.get("competitions")
        if not isinstance(competitions, list) or len(competitions) != 1:
            count = len(competitions) if isinstance(competitions, list) else "none"
            failures.append(ScheduleFailure(
                FAIL_COMPETITION_COUNT,
                f"{count} competitions, expected exactly 1", event_id))
            continue
        competition = competitions[0]
        if not isinstance(competition, dict):
            failures.append(ScheduleFailure(
                FAIL_EVENT, "competition is not an object", event_id))
            continue

        not_played = _competition_status_blocks_play(competition)
        if not_played:
            failures.append(ScheduleFailure(
                FAIL_NOT_PLAYED,
                f"status {not_played}; a game that did not happen as "
                "scheduled has no usable kickoff for a lead-time grid",
                event_id))
            continue

        # `timeValid` separates a real kickoff from a placeholder. Absent is a
        # rejection, not an assumption -- see the module docstring.
        if "timeValid" not in competition:
            failures.append(ScheduleFailure(
                FAIL_TIME_VALID_MISSING,
                "competition has no 'timeValid' flag; without it a TBD "
                "placeholder is indistinguishable from a scheduled kickoff",
                event_id))
            continue
        if not competition.get("timeValid"):
            failures.append(ScheduleFailure(
                FAIL_TIME_TBD, "timeValid is false: the kickoff is TBD",
                event_id))
            continue

        event_at, event_fail = _parse_instant(raw.get("date"))
        if event_fail:
            failures.append(ScheduleFailure(
                event_fail, f"event.date={raw.get('date')!r}", event_id))
            continue
        comp_at, comp_fail = _parse_instant(competition.get("date"))
        if comp_fail:
            failures.append(ScheduleFailure(
                comp_fail, f"competitions[0].date={competition.get('date')!r}",
                event_id))
            continue
        if event_at != comp_at:
            failures.append(ScheduleFailure(
                FAIL_DATE_MISMATCH,
                f"event.date {event_at.isoformat()} != competition.date "
                f"{comp_at.isoformat()}; the payload disagrees with itself "
                "about when this game starts",
                event_id))
            continue

        competitors = competition.get("competitors")
        if not isinstance(competitors, list) or len(competitors) != 2:
            count = len(competitors) if isinstance(competitors, list) else "none"
            failures.append(ScheduleFailure(
                FAIL_COMPETITOR_COUNT,
                f"{count} competitors, expected exactly 2", event_id))
            continue

        sides: dict[str, dict] = {}
        bad_side = False
        for competitor in competitors:
            if not isinstance(competitor, dict):
                bad_side = True
                break
            where = str(competitor.get("homeAway", "") or "").strip().lower()
            if where not in ("home", "away") or where in sides:
                bad_side = True
                break
            sides[where] = competitor
        if bad_side or set(sides) != {"home", "away"}:
            failures.append(ScheduleFailure(
                FAIL_HOMEAWAY,
                "competitors do not form exactly one home and one away side",
                event_id))
            continue

        away, away_by, away_detail = _resolve_side(sides["away"].get("team"), league)
        home, home_by, home_detail = _resolve_side(sides["home"].get("team"), league)
        if away is None or home is None:
            failures.append(ScheduleFailure(
                FAIL_TEAM_UNRESOLVED,
                "; ".join(d for d in (away_detail, home_detail) if d),
                event_id))
            continue
        if away == home:
            failures.append(ScheduleFailure(
                FAIL_TEAM_DUPLICATE,
                f"both sides resolved to {away}", event_id))
            continue

        events.append(ScheduleEvent(
            provider_event_id=event_id,
            kickoff=event_at,
            away=away,
            home=home,
            league=league.upper(),
            source_url=source_url,
            retrieved_at=retrieved_at,
            payload_sha256=digest,
            date_bucket=date_bucket,
            away_resolved_by=away_by,
            home_resolved_by=home_by,
        ))

    return events, failures


# --- snapshot ---------------------------------------------------------------


@dataclass
class ScheduleSnapshot:
    """A FROZEN schedule for one run.

    Built once, before eligibility is applied, and then reused verbatim by
    targeting, collection and the join. That is deliberate: a schedule
    re-fetched mid-run could move a kickoff between the moment a contract was
    judged eligible and the moment its checkpoints were priced, and the study
    would have no single answer to "when did this game start".
    """

    league: str
    events: dict[str, ScheduleEvent] = field(default_factory=dict)
    failures: list[ScheduleFailure] = field(default_factory=list)
    buckets: list[str] = field(default_factory=list)
    retrieved_at: datetime | None = None
    offsets_used: dict[int, int] = field(default_factory=dict)

    # --- construction ---
    def add(self, event: ScheduleEvent) -> None:
        """Insert, deduplicating by provider event id.

        Two buckets legitimately carry the same game. Two buckets carrying the
        same game at DIFFERENT kickoffs do not: that is the provider changing
        its answer inside one retrieval, and picking either one silently would
        be choosing which lead times to be wrong about.
        """
        existing = self.events.get(event.provider_event_id)
        if existing is None:
            self.events[event.provider_event_id] = event
            return
        if existing.kickoff != event.kickoff:
            self.failures.append(ScheduleFailure(
                FAIL_EVENT_CONFLICT,
                f"bucket {existing.date_bucket} says "
                f"{existing.kickoff.isoformat()} and bucket "
                f"{event.date_bucket} says {event.kickoff.isoformat()}",
                event.provider_event_id))
            self.events.pop(event.provider_event_id, None)

    @property
    def resolved_by_name(self) -> int:
        return sum(1 for e in self.events.values() if e.resolved_by_name)

    def by_matchup(self) -> dict[frozenset[str], list[ScheduleEvent]]:
        out: dict[frozenset[str], list[ScheduleEvent]] = {}
        for event in self.events.values():
            out.setdefault(event.matchup, []).append(event)
        return out

    # --- resolution ---
    def resolve(self, matchup: frozenset[str], ticker_day: date | None,
                tolerance: int = DAY_TOLERANCE) -> StartResolution:
        """The kickoff for one exchange event, or a named reason there is none.

        `matchup` comes from the contracts' YES suffixes, never from splitting
        the concatenated team tail of an event body -- `MILBAL` has no unique
        reading and `DALNYG` has several. `ticker_day` narrows the candidates;
        identity decides the match, and the match must be UNIQUE.
        """
        if ticker_day is None:
            return StartResolution(
                None, None, FAIL_NO_TICKER_DAY,
                "the event body carried no readable calendar day")

        candidates = [
            event for event in self.events.values()
            if event.matchup == matchup
            and abs((event.local_day - ticker_day).days) <= tolerance
        ]
        if not candidates:
            return StartResolution(
                None, None, FAIL_NO_MATCH,
                f"{'/'.join(sorted(matchup))} on {ticker_day.isoformat()} "
                f"+-{tolerance}d is not in the retrieved schedule "
                f"({len(self.events)} games over {len(self.buckets)} buckets)")
        if len(candidates) > 1:
            listed = ", ".join(
                f"{c.provider_event_id}@{c.kickoff.isoformat()}"
                for c in sorted(candidates, key=lambda c: c.kickoff))
            return StartResolution(
                None, None, FAIL_AMBIGUOUS,
                f"{len(candidates)} schedule entries match "
                f"{'/'.join(sorted(matchup))} near {ticker_day.isoformat()}: "
                f"{listed}")

        event = candidates[0]
        offset = (event.local_day - ticker_day).days
        self.offsets_used[offset] = self.offsets_used.get(offset, 0) + 1
        return StartResolution(event.kickoff, event, None, day_offset=offset)

    # --- reporting ---
    def failure_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for failure in self.failures:
            out[failure.reason] = out.get(failure.reason, 0) + 1
        return dict(sorted(out.items()))

    def manifest(self) -> dict[str, Any]:
        """What the run records about where its kickoffs came from."""
        return {
            "schedule_source": SCHEDULE_SOURCE,
            "league": self.league,
            "historical_schedule_as_of": SCHEDULE_PROVENANCE,
            "provenance_note": (
                "The schedule was retrieved at run time and is the CURRENT "
                "schedule. It does not establish what the kickoff was "
                "believed to be at any earlier decision. A flexed or "
                "rescheduled game carries its FINAL kickoff here. This "
                "supports an exploratory retrospective assessment and cannot "
                "certify a point-in-time backtest or a live strategy."),
            "retrieved_at": (self.retrieved_at.astimezone(timezone.utc).isoformat()
                             if self.retrieved_at else None),
            "date_buckets": list(self.buckets),
            "games": len(self.events),
            "failures": self.failure_counts(),
            "resolved_by_name": self.resolved_by_name,
            "ticker_day_offsets": {str(k): v for k, v
                                   in sorted(self.offsets_used.items())},
            "timezone_assumption": SCHEDULE_TIMEZONE,
            "events": {
                event_id: event.provenance()
                for event_id, event in sorted(self.events.items())
            },
        }


# --- retrieval --------------------------------------------------------------


def date_buckets(start: date, end: date,
                 buffer_days: int = BUCKET_BUFFER_DAYS) -> list[str]:
    """Every YYYYMMDD to fetch for a window, with a boundary buffer.

    DAILY buckets, deliberately. The endpoint accepts a date RANGE, but that
    behaviour is undocumented and an incomplete range would present as a
    thinner slate rather than as an error -- the same shape as the truncated
    bar windows that sat unnoticed in the fleet for months. One free request
    per day is a cheap way not to depend on it.
    """
    if end < start:
        start, end = end, start
    first = start - timedelta(days=buffer_days)
    last = end + timedelta(days=buffer_days)
    out: list[str] = []
    cursor = first
    while cursor <= last:
        out.append(cursor.strftime("%Y%m%d"))
        cursor += timedelta(days=1)
    return out


def _default_transport(url: str) -> Any:
    """One GET with retry/backoff. Raises rather than returning an empty slate."""
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            request = urllib.request.Request(
                url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                if response.status != 200:
                    raise ScheduleFetchError(f"HTTP {response.status} from {url}")
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                OSError, ScheduleFetchError) as exc:
            last = exc
            if attempt < RETRIES - 1:
                import time
                time.sleep(BACKOFF_SECONDS[attempt])
    raise ScheduleFetchError(f"{url} failed after {RETRIES} attempts: {last}")


def fetch_schedule(
    league: str,
    start: date,
    end: date,
    *,
    transport: Callable[[str], Any] | None = None,
    buffer_days: int = BUCKET_BUFFER_DAYS,
    retrieved_at: datetime | None = None,
    cache_dir: Path | str | None = None,
) -> ScheduleSnapshot:
    """Build a frozen schedule snapshot for a window. FREE -- no credits.

    `transport` is injectable so the whole adapter is testable, and so an
    operator can replay saved payloads offline. `cache_dir`, when given, stores
    each bucket's raw JSON so a later run (or a debugging session with no
    network) reads the same bytes the study was built on.
    """
    send = transport if transport is not None else _default_transport
    now = retrieved_at or datetime.now(timezone.utc)
    snapshot = ScheduleSnapshot(league=league.upper(), retrieved_at=now)
    root = Path(cache_dir) if cache_dir else None

    for bucket in date_buckets(start, end, buffer_days):
        url = SCOREBOARD_URL.format(bucket=bucket)
        snapshot.buckets.append(bucket)
        payload: Any = None
        cached = root / f"{bucket}.json" if root else None
        if cached is not None and cached.exists():
            try:
                payload = json.loads(cached.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                # A corrupt entry is a miss, never a silent empty slate.
                snapshot.failures.append(ScheduleFailure(
                    FAIL_PAYLOAD, f"unreadable cached bucket {bucket}: {exc}"))
                payload = None
        if payload is None:
            try:
                payload = send(url)
            except Exception as exc:          # transport-shaped, by contract
                snapshot.failures.append(ScheduleFailure(
                    FAIL_FETCH, f"bucket {bucket}: {exc}"))
                continue
            if root is not None:
                try:
                    root.mkdir(parents=True, exist_ok=True)
                    tmp = (root / f"{bucket}.json").with_suffix(".tmp")
                    tmp.write_text(json.dumps(payload), encoding="utf-8")
                    tmp.replace(root / f"{bucket}.json")
                except (OSError, TypeError) as exc:
                    snapshot.failures.append(ScheduleFailure(
                        FAIL_PAYLOAD, f"could not cache bucket {bucket}: {exc}"))

        events, failures = parse_scoreboard(
            payload, league=league, source_url=url, retrieved_at=now,
            date_bucket=bucket)
        snapshot.failures.extend(failures)
        for event in events:
            snapshot.add(event)

    return snapshot


def snapshot_from_payloads(
    league: str,
    payloads: Sequence[tuple[str, Any]],
    *,
    retrieved_at: datetime | None = None,
) -> ScheduleSnapshot:
    """Build a snapshot from `(bucket, payload)` pairs already in hand.

    The offline path: an operator who can reach the endpoint saves the raw
    responses, and a session that cannot reach it -- this one -- builds the
    identical snapshot from them. Same parser, same failures, same manifest.
    """
    now = retrieved_at or datetime.now(timezone.utc)
    snapshot = ScheduleSnapshot(league=league.upper(), retrieved_at=now)
    for bucket, payload in payloads:
        url = SCOREBOARD_URL.format(bucket=bucket)
        snapshot.buckets.append(bucket)
        events, failures = parse_scoreboard(
            payload, league=league, source_url=url, retrieved_at=now,
            date_bucket=bucket)
        snapshot.failures.extend(failures)
        for event in events:
            snapshot.add(event)
    return snapshot


def snapshot_from_directory(
    league: str,
    directory: Path | str,
    *,
    retrieved_at: datetime | None = None,
) -> ScheduleSnapshot:
    """Build a snapshot from a directory of `YYYYMMDD.json` raw payloads."""
    root = Path(directory)
    payloads: list[tuple[str, Any]] = []
    unreadable: list[ScheduleFailure] = []
    for path in sorted(root.glob("*.json")):
        try:
            payloads.append((path.stem, json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            unreadable.append(ScheduleFailure(
                FAIL_PAYLOAD, f"unreadable schedule file {path.name}: {exc}"))
    snapshot = snapshot_from_payloads(league, payloads, retrieved_at=retrieved_at)
    snapshot.failures.extend(unreadable)
    return snapshot


def schedule_leagues() -> tuple[str, ...]:
    """Leagues this adapter can supply a kickoff for."""
    return ("NFL",)


def free_command_hint(start: date, end: date, series: str = "KXNFLGAME") -> str:
    """The exact free command that exercises this path end to end."""
    return (
        "python3 run_study.py --preflight --sport NFL "
        f"--series {series} --from {start.isoformat()} --to {end.isoformat()} "
        "--lead-grid 72h,48h,24h,12h,6h,3h"
    )


def describe_buckets(buckets: Iterable[str]) -> str:
    listed = list(buckets)
    if not listed:
        return "none"
    return f"{len(listed)} daily bucket(s) {listed[0]}..{listed[-1]}"
