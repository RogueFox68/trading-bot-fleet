"""Entity resolution between a sportsbook feed and an exchange contract.

The originating spec proposed `rapidfuzz.process.extractOne` at score >= 85
over natural-language market titles, mapped to a canonical matchup id. That is
a silent wrong-answer generator. Fuzzy-matching whole titles will confidently
return the wrong game for a playoff rematch, a divisional pair played twice, or
any of the shared-city collisions below -- and a mismatched event means trading
the wrong game while holding a confident fair probability. Not an error, a
wrong-side-of-the-market position.

The discipline here mirrors the fleet's ownership resolver: there is no default
answer. An event that does not resolve is REJECTED with a reason and counted,
never guessed at.

Two properties do the work:

  1. FUZZY MATCHING IS SCOPED AND BOUNDED. It is used only to map one team name
     to an abbreviation within one league's closed roster (30-32 known
     candidates). It is never used to compare two event titles to each other.
  2. THE MATCH KEY IS HARD. (league, date, away_abbr, home_abbr) must agree
     exactly, and scheduled start times must agree within a tolerance. Both
     sides must resolve; one resolved team is a rejection, not half a match.

The collision hazard is real and is why `league` is part of every lookup:
MIA, BAL, CIN, CLE, PIT, HOU, KC, ATL, TB, ARI, SF, SEA, MIN, DET, PHI and CHI
are all live abbreviations in BOTH the NFL and MLB rosters, for different
franchises. A league-blind roster would resolve "Miami" to two different teams
depending on dictionary order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# Score at or above which a name is considered to have resolved.
MIN_TEAM_SCORE = 85.0
# If the runner-up scores within this of the winner, the name is AMBIGUOUS and
# is rejected. "New York" against an MLB roster hits Yankees and Mets almost
# equally; picking the higher score would be picking dictionary order.
AMBIGUITY_MARGIN = 6.0
# Scheduled start times from two sources rarely agree to the second.
START_TIME_TOLERANCE = timedelta(minutes=15)

# --- Fuzzy backend ----------------------------------------------------------
# rapidfuzz when available, stdlib difflib otherwise. Both return 0-100. The
# fallback keeps this module importable (and testable) on a box that could not
# build rapidfuzz -- the same reason the fleet imports yfinance lazily.
try:  # pragma: no cover - import shape, not logic
    from rapidfuzz import fuzz as _rf_fuzz

    def _score(a: str, b: str) -> float:
        return float(_rf_fuzz.token_sort_ratio(a, b))

    FUZZY_BACKEND = "rapidfuzz"
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def _score(a: str, b: str) -> float:
        return SequenceMatcher(None, " ".join(sorted(a.split())),
                               " ".join(sorted(b.split()))).ratio() * 100.0

    FUZZY_BACKEND = "difflib"


# --- Rosters ----------------------------------------------------------------
# abbreviation -> every spelling a feed might use for it. Abbreviations must
# match the exchange's own tickers; verify against live Kalshi series before
# trusting a study built on them (see `unverified_note`).
ROSTERS: dict[str, dict[str, tuple[str, ...]]] = {
    "NFL": {
        "BUF": ("Buffalo Bills", "Buffalo"), "MIA": ("Miami Dolphins", "Miami"),
        "NE": ("New England Patriots", "New England"), "NYJ": ("New York Jets",),
        "BAL": ("Baltimore Ravens", "Baltimore"), "CIN": ("Cincinnati Bengals", "Cincinnati"),
        "CLE": ("Cleveland Browns", "Cleveland"), "PIT": ("Pittsburgh Steelers", "Pittsburgh"),
        "HOU": ("Houston Texans", "Houston"), "IND": ("Indianapolis Colts", "Indianapolis"),
        "JAX": ("Jacksonville Jaguars", "Jacksonville"), "TEN": ("Tennessee Titans", "Tennessee"),
        "DEN": ("Denver Broncos", "Denver"), "KC": ("Kansas City Chiefs", "Kansas City"),
        "LV": ("Las Vegas Raiders", "Las Vegas", "Oakland Raiders"),
        "LAC": ("Los Angeles Chargers", "LA Chargers", "San Diego Chargers"),
        "DAL": ("Dallas Cowboys", "Dallas"), "NYG": ("New York Giants",),
        "PHI": ("Philadelphia Eagles", "Philadelphia"),
        "WAS": ("Washington Commanders", "Washington Football Team"),
        "CHI": ("Chicago Bears",), "DET": ("Detroit Lions", "Detroit"),
        "GB": ("Green Bay Packers", "Green Bay"), "MIN": ("Minnesota Vikings", "Minnesota"),
        "ATL": ("Atlanta Falcons", "Atlanta"), "CAR": ("Carolina Panthers", "Carolina"),
        "NO": ("New Orleans Saints", "New Orleans"),
        "TB": ("Tampa Bay Buccaneers", "Tampa Bay Bucs"),
        "ARI": ("Arizona Cardinals", "Arizona"), "LAR": ("Los Angeles Rams", "LA Rams", "St. Louis Rams"),
        "SF": ("San Francisco 49ers", "San Francisco", "Niners"),
        "SEA": ("Seattle Seahawks", "Seattle"),
    },
    "MLB": {
        "BAL": ("Baltimore Orioles", "Baltimore"), "BOS": ("Boston Red Sox", "Boston"),
        "NYY": ("New York Yankees",), "TB": ("Tampa Bay Rays",),
        "TOR": ("Toronto Blue Jays", "Toronto"), "CWS": ("Chicago White Sox",),
        "CLE": ("Cleveland Guardians", "Cleveland Indians"), "DET": ("Detroit Tigers", "Detroit"),
        "KC": ("Kansas City Royals", "Kansas City"), "MIN": ("Minnesota Twins", "Minnesota"),
        "HOU": ("Houston Astros", "Houston"), "LAA": ("Los Angeles Angels", "LA Angels", "Anaheim Angels"),
        "ATH": ("Athletics", "Oakland Athletics", "Oakland A's", "Sacramento Athletics"),
        "SEA": ("Seattle Mariners", "Seattle"), "TEX": ("Texas Rangers", "Texas"),
        "ATL": ("Atlanta Braves", "Atlanta"), "MIA": ("Miami Marlins", "Florida Marlins"),
        "NYM": ("New York Mets",), "PHI": ("Philadelphia Phillies", "Philadelphia"),
        "WSH": ("Washington Nationals",), "CHC": ("Chicago Cubs",),
        "CIN": ("Cincinnati Reds", "Cincinnati"), "MIL": ("Milwaukee Brewers", "Milwaukee"),
        "PIT": ("Pittsburgh Pirates", "Pittsburgh"), "STL": ("St. Louis Cardinals", "St Louis Cardinals"),
        "ARI": ("Arizona Diamondbacks", "Arizona D-backs"), "COL": ("Colorado Rockies", "Colorado"),
        "LAD": ("Los Angeles Dodgers", "LA Dodgers"), "SD": ("San Diego Padres", "San Diego"),
        "SF": ("San Francisco Giants", "SF Giants"),
    },
}


def unverified_note() -> str:
    return (
        "Roster abbreviations are conventional spellings and have NOT been "
        "verified against live Kalshi series tickers. Run "
        "`data/kalshi_history.py --audit-abbreviations` before trusting a "
        "study built on them; an abbreviation that silently never matches "
        "shows up as thin coverage, not as an error."
    )


# --- exchange code aliases --------------------------------------------------
# The exchange's ticker suffixes are NOT the same vocabulary as the canonical
# abbreviations resolved from bookmaker team names. A live smoke test dropped
# 44 contracts under `no_sharp_event_for_matchup` because the Odds API name
# "Arizona Diamondbacks" resolves to ARI here while the exchange ticker says
# AZ -- so the two participant sets never compared equal.
#
# Only codes OBSERVED in a real response belong here. An unobserved code is not
# added on a hunch: `normalise_exchange_code` returns the input unchanged and
# the join records the unresolved code by name, so one run enumerates the rest
# rather than a guess hiding them.
# HOW A LEAGUE'S SCHEDULED START IS DERIVED, per a VERIFIED payload shape.
#
# STRUCTURAL parsing is league-agnostic -- series, event ticker and YES
# participant come out of any of these. DERIVING A START IS NOT, and conflating
# the two is how NFL looked supported when it is not:
#
#   MLB  26SEP152140MIAAZ   date + HHMM + teams   -> the start is in the ticker
#   NFL  26SEP14DENKC       date + teams, NO TIME -> the ticker cannot say when
#
# NFL's kickoff is NOT invented from its date-only body, and NOT inferred from
# `close_time`, `expected_expiration_time` or `settlement_ts`: on the sampled
# KC contract those are 03:15:19Z, 03:15:00Z and 03:21:19Z on the day AFTER the
# game -- they describe the end of the contract, not the start of the match. A
# study whose lead times are measured backwards from the final whistle is
# measuring the wrong thing precisely.
#
# It comes from an EXTERNAL schedule instead (`data.espn_schedule`), which is a
# different KIND of source with a different warranty: the ticker's time is a
# fact about the contract, available at decision time; a schedule fetched today
# is the CURRENT schedule and does not establish what a kickoff was believed to
# be 72 hours earlier. `START_SOURCE_KINDS` keeps the two apart so the weaker
# warranty cannot be read as the stronger one.
START_SOURCE_TICKER = "event_ticker"
START_SOURCE_EXTERNAL = "external_schedule"

START_SOURCES: dict[str, str] = {
    "MLB": START_SOURCE_TICKER,
    "NFL": START_SOURCE_EXTERNAL,
}

# source -> (kind, historical provenance). "verified_at_decision_time" means
# the value was published by the exchange itself on the contract; "unverified"
# means it was retrieved later and may not be what was known at the decision.
START_SOURCE_KINDS: dict[str, tuple[str, str]] = {
    START_SOURCE_TICKER: ("ticker", "verified_at_decision_time"),
    START_SOURCE_EXTERNAL: ("external", "unverified"),
}


def start_source_kind(league: str) -> str | None:
    source = start_source(league)
    return START_SOURCE_KINDS.get(source, (None, None))[0] if source else None


def start_source_provenance(league: str) -> str | None:
    """How far a league's kickoffs can be trusted BACKWARDS in time.

    Reported separately from "can we get a kickoff at all" because they are
    different questions and only one of them is about whether the study can
    run. An external schedule makes NFL collectable and leaves every run over
    it exploratory.
    """
    source = start_source(league)
    return START_SOURCE_KINDS.get(source, (None, None))[1] if source else None


def start_source(league: str) -> str | None:
    """Where this league's scheduled start comes from, or None if nowhere."""
    return START_SOURCES.get((league or "").upper())


def league_has_schedule(league: str) -> bool:
    return start_source(league) is not None


def supported_leagues() -> tuple[str, ...]:
    """Leagues this code can actually resolve team identity for.

    A league with no roster cannot map a bookmaker's team NAMES onto an
    exchange's team CODES, which is the whole join. It does not fail at
    import, at argument parsing, or at preflight -- it fails at join time,
    after the snapshots have been paid for. `run_study` refuses such a sport
    before spending anything.
    """
    return tuple(sorted(ROSTERS))


def league_is_supported(league: str) -> bool:
    return (league or "").upper() in ROSTERS


EXCHANGE_CODE_ALIASES: dict[str, dict[str, str]] = {
    "MLB": {
        "AZ": "ARI",     # observed 2026-09-21, KXMLBGAME-26SEP152140MIAAZ
    },
    "NFL": {
        "JAC": "JAX",    # observed 2026-09-21, KXNFLGAME-26SEP13CLEJAC-JAC
    },
}


def normalise_exchange_code(code: str, league: str) -> str:
    """Map an exchange ticker suffix to this module's canonical abbreviation."""
    return EXCHANGE_CODE_ALIASES.get(league.upper(), {}).get(
        code.strip().upper(), code.strip().upper())


def unknown_exchange_codes(codes, league: str) -> list[str]:
    """Codes that are neither canonical nor aliased -- the audit list."""
    roster = ROSTERS.get(league.upper(), {})
    out = []
    for code in codes:
        canonical = normalise_exchange_code(code, league)
        if canonical not in roster:
            out.append(code)
    return sorted(set(out))


# --- event-body date/time ---------------------------------------------------
# A real event body is a FIXED-WIDTH date and time followed by the teams:
#
#     26SEP152140MIAAZ   ->  26SEP15 | 2140 | MIAAZ
#     26SEP131920SDSF    ->  26SEP13 | 1920 | SDSF
#
# The 11-character prefix is unambiguous. Only the TEAM tail is ambiguous
# (MIAAZ reads as MIA|AZ or MI|AAZ), and it is not needed: the YES suffixes of
# an event's markets already give the participants. So the prefix is parsed and
# the tail is deliberately ignored.
#
# THE TIMEZONE IS AN ASSUMPTION, not a verified fact. It is declared here and
# CROSS-CHECKED per game against the sharp feed's own `commence_time`; the
# collector records the agreement rate, and a wrong assumption shows up as
# systematic disagreement rather than as silently shifted timestamps.
EVENT_BODY_TIMEZONE = "America/New_York"

_EVENT_BODY = re.compile(r"^(?P<date>\d{2}[A-Z]{3}\d{2})(?P<time>\d{4})(?P<teams>[A-Z0-9]+)$")

# A DATE-ONLY body: the NFL shape. The team tail is letters only, which is what
# keeps this from also matching an MLB body -- `26SEP152140MIAAZ` leaves the
# tail `2140MIAAZ`, and the digits refuse it. The tail is still NOT decoded:
# `DALNYG` splits as DAL|NYG, DA|LNYG and DALN|YG with nothing in the string to
# choose between them, so participants keep coming from the YES suffixes.
_EVENT_BODY_DATE_ONLY = re.compile(
    r"^(?P<date>\d{2}[A-Z]{3}\d{2})(?P<teams>[A-Z]+)$")


def parse_event_body_start(event_ticker: str, tz_name: str = EVENT_BODY_TIMEZONE):
    """Scheduled start encoded in an event ticker, or None if it does not fit.

    Returns a timezone-aware datetime under the DECLARED timezone assumption.
    Callers must treat it as a candidate to be validated, not as ground truth.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    body = event_ticker.strip().upper().split("-", 1)
    if len(body) != 2:
        return None
    m = _EVENT_BODY.match(body[1])
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group("date") + m.group("time"), "%y%b%d%H%M")
    except ValueError:
        return None
    try:
        return naive.replace(tzinfo=ZoneInfo(tz_name))
    except Exception:
        return None


def parse_event_body_date(event_ticker: str):
    """The LOCAL calendar day an event body names, or None if it has none.

    Both body shapes carry a day, so this answers for MLB and NFL alike. It is
    a DAY, never a kickoff: returning a date rather than a midnight datetime is
    deliberate, because a caller cannot then accidentally use it as a start
    time. The checkpoint grid is measured in hours and a day is not one.

    The day is read under the declared `EVENT_BODY_TIMEZONE` assumption, and
    that assumption is what makes `26SEP14DENKC` the right label for a game
    kicking off at 2026-09-15T00:15Z -- 20:15 the previous evening in New York.
    """
    from datetime import datetime as _dt

    body = event_ticker.strip().upper().split("-", 1)
    if len(body) != 2:
        return None
    text = body[1]
    m = _EVENT_BODY.match(text) or _EVENT_BODY_DATE_ONLY.match(text)
    if not m:
        return None
    try:
        return _dt.strptime(m.group("date"), "%y%b%d").date()
    except ValueError:
        return None


_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalise(name: str) -> str:
    return _PUNCT.sub(" ", name.lower()).strip()


@dataclass(frozen=True)
class TeamResolution:
    abbreviation: str | None
    score: float
    runner_up: str | None
    runner_up_score: float
    reject_reason: str | None

    @property
    def resolved(self) -> bool:
        return self.abbreviation is not None


def resolve_team(name: str, league: str) -> TeamResolution:
    """Map one team name to an abbreviation within ONE league's roster.

    Rejects rather than guesses on: unknown league, empty name, no candidate
    above threshold, or two candidates too close to separate.
    """
    roster = ROSTERS.get(league.upper())
    if roster is None:
        return TeamResolution(None, 0.0, None, 0.0, f"unknown league {league!r}")
    text = normalise(name)
    if not text:
        return TeamResolution(None, 0.0, None, 0.0, "empty team name")

    scored: list[tuple[float, str]] = []
    for abbr, aliases in roster.items():
        best = max(_score(text, normalise(a)) for a in aliases)
        # An exact abbreviation is an identity, not a similarity.
        if text.upper() == abbr:
            best = 100.0
        scored.append((best, abbr))
    scored.sort(reverse=True)

    (top_score, top_abbr), (second_score, second_abbr) = scored[0], scored[1]

    if top_score < MIN_TEAM_SCORE:
        return TeamResolution(
            None, top_score, second_abbr, second_score,
            f"best candidate {top_abbr} scored {top_score:.1f} < {MIN_TEAM_SCORE}",
        )
    if top_score - second_score < AMBIGUITY_MARGIN:
        return TeamResolution(
            None, top_score, second_abbr, second_score,
            f"ambiguous: {top_abbr} ({top_score:.1f}) vs {second_abbr} "
            f"({second_score:.1f}) within {AMBIGUITY_MARGIN} margin",
        )
    return TeamResolution(top_abbr, top_score, second_abbr, second_score, None)


def canonical_event_id(league: str, start: datetime, away: str, home: str) -> str:
    """Stable id for one game, to the MINUTE.

    An earlier version keyed on the calendar date alone, which collapses a
    doubleheader: both games of a 17:05 / 20:10 pair produced the same id, so
    the join could attach one game's exchange prices to the other game's
    settlement. The time is part of the identity, not decoration.

    This remains a FALLBACK identity. When a provider supplies its own stable
    event id -- the Odds API's `id`, Kalshi's `event_ticker` -- that is the
    identity to carry, because it survives a reschedule that moves the start
    time and this does not. See `GameKey`.
    """
    return (
        f"{league.upper()}_{start.strftime('%Y%m%dT%H%MZ')}_"
        f"{away.upper()}_{home.upper()}"
    )


@dataclass(frozen=True)
class GameKey:
    """One game, identified by both providers' own ids rather than a guess.

    A date plus two team names cannot separate a doubleheader, a rematch or a
    reschedule. Both sides publish a stable id for the event; carrying both is
    what makes a join auditable after the fact -- every observation can name
    the exact provider records it came from.
    """

    league: str
    provider_event_id: str        # Odds API event id
    kalshi_event_ticker: str      # Kalshi event (shared by both team contracts)
    kalshi_market_ticker: str     # the specific YES contract
    yes_participant: str          # abbreviation the YES side pays out on
    start: datetime

    def as_id(self) -> str:
        return f"{self.provider_event_id}::{self.kalshi_market_ticker}"


@dataclass(frozen=True)
class EventMatch:
    matched: bool
    event_id: str | None
    league: str
    away: str | None
    home: str | None
    reject_reason: str | None
    away_score: float = 0.0
    home_score: float = 0.0


def match_event(
    league: str,
    away_name: str,
    home_name: str,
    start: datetime,
    exchange_start: datetime | None = None,
    tolerance: timedelta = START_TIME_TOLERANCE,
) -> EventMatch:
    """Resolve one book event to a canonical id, optionally against an exchange.

    Both teams must resolve, they must be different teams, and -- when an
    exchange start time is supplied -- the schedules must agree within
    `tolerance`. Anything else is a rejection carrying its reason.
    """
    league = league.upper()
    away = resolve_team(away_name, league)
    home = resolve_team(home_name, league)

    if not away.resolved:
        return EventMatch(False, None, league, None, None,
                          f"away {away_name!r}: {away.reject_reason}")
    if not home.resolved:
        return EventMatch(False, None, league, None, None,
                          f"home {home_name!r}: {home.reject_reason}")
    if away.abbreviation == home.abbreviation:
        return EventMatch(False, None, league, None, None,
                          f"both names resolved to {away.abbreviation}")

    if exchange_start is not None:
        drift = abs(exchange_start - start)
        if drift > tolerance:
            return EventMatch(
                False, None, league, away.abbreviation, home.abbreviation,
                f"start times disagree by {drift} (> {tolerance})",
                away.score, home.score,
            )

    return EventMatch(
        True,
        canonical_event_id(league, start, away.abbreviation, home.abbreviation),
        league, away.abbreviation, home.abbreviation, None,
        away.score, home.score,
    )


# Real Kalshi game tickers look like:
#
#     KXMLBGAME-26SEP201920MILBAL-MIL
#     KXMLBGAME-26SEP201920MILBAL-BAL
#
# i.e. SERIES - EVENT - YES_PARTICIPANT, where the two contracts of one game
# share an EVENT and differ only in the final segment, which names the team the
# YES side pays out on.
#
# An earlier version of this matched `KXNFLGAME-24NOV17-BUF-KC`: a shape
# invented from an illustrative example in the spec, never checked against a
# response. It returns None for both real tickers above, so the normal path
# discarded every actual market before the join -- and because the fixtures
# were invented from the same assumption, the tests agreed with it.
#
# The event segment is NOT decoded. `26SEP201920MILBAL` concatenates date, time
# and both abbreviations with no separator, and `MILBAL` cannot be split
# reliably (MIL|BAL and MI|LBAL are equally valid readings of the string).
# Start time and participants come from market metadata, which publishes them
# as fields. This parser extracts only what the STRUCTURE guarantees: the
# series, the event, and the YES participant.
_KALSHI_TICKER = re.compile(
    r"^(?P<series>KX[A-Z0-9]+)-(?P<event_body>[A-Z0-9]+)-(?P<yes>[A-Z0-9]{2,5})$"
)


def parse_kalshi_game_ticker(ticker: str) -> dict[str, str] | None:
    """Structural parse of a Kalshi game ticker.

    Returns series, event_ticker (series + event body, shared by both team
    contracts of one game), and yes_participant. Returns None on any ticker
    that does not fit, so a format change surfaces as unmatched coverage
    rather than as mis-parsed teams.
    """
    m = _KALSHI_TICKER.match(ticker.strip().upper())
    if not m:
        return None
    series, event_body, yes = m.group("series"), m.group("event_body"), m.group("yes")
    return {
        "series": series,
        "event_ticker": f"{series}-{event_body}",
        "yes_participant": yes,          # raw exchange code
        "ticker": ticker.strip().upper(),
    }


def kalshi_event_ticker(market: dict) -> str | None:
    """The game a market belongs to, preferring the field over the parse.

    Kalshi publishes `event_ticker` on the market object. Reading it is more
    robust than reconstructing it, and the parse stays only as a fallback.
    """
    published = market.get("event_ticker")
    if isinstance(published, str) and published.strip():
        return published.strip().upper()
    parsed = parse_kalshi_game_ticker(str(market.get("ticker", "")))
    return parsed["event_ticker"] if parsed else None
