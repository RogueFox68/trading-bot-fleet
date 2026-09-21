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
    """Stable id for one game. Date is the UTC calendar date of first pitch/kick."""
    return f"{league.upper()}_{start.strftime('%Y%m%d')}_{away.upper()}_{home.upper()}"


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


# Kalshi game tickers look like KXNFLGAME-24NOV17-BUF-KC. The abbreviations are
# read straight out of the ticker -- no fuzzy step is needed or wanted here.
_KALSHI_TICKER = re.compile(
    r"^(?P<series>KX[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})-"
    r"(?P<a>[A-Z0-9]{2,4})-(?P<b>[A-Z0-9]{2,4})$"
)


def parse_kalshi_game_ticker(ticker: str) -> dict[str, str] | None:
    """Pull (series, date, team_a, team_b) out of a Kalshi game ticker.

    Returns None on any ticker that does not fit the shape, so a format change
    surfaces as unmatched coverage rather than as mis-parsed teams.
    """
    m = _KALSHI_TICKER.match(ticker.strip().upper())
    return m.groupdict() if m else None
