"""SYNTHETIC: a shadow session shaped like the owner's 2026-09-24 run.

NOTHING IN THIS FILE WAS OBSERVED. It drives the REAL monitor -- the live
fetchers, the join, the detector, the screen, the recorder -- over a scripted
network, so the session file it writes has every record the monitor writes,
in the monitor's own order and shape. The owner's raw session is private and
is not in this repository; this is built only from the figures the owner
reported in PR #27 (comment 5826957047), and reproduces those:

  * Seattle-Washington: Pinnacle -354 Seattle / +290 Washington after the
    move (Shin: Seattle 0.7616627132), unchanged at the response; Kalshi's
    Seattle YES 0.74 bid / 0.75 ask at the move, 0.75/0.76 about 615s later.
  * Green Bay: sharp 0.7241734676 (Pinnacle -287 / +241), YES 0.69/0.70, and
    an opposite sharp move about 15 minutes later.
  * Houston-Indianapolis: detected 2026-09-24 12:56:24 UTC, kickoff
    2026-09-27 17:00 UTC -- about 76h out, beyond the 73h book horizon.
  * 5 moves on 4 games; 10 contract assessments; 8 screened, all below the
    net-EV floor, none refused on spread, price band or lead time.

EVERYTHING ELSE IS INVENTED to make those facts reachable, and says so here:
the fourth game (Tennessee at Jacksonville) and all its prices; Green Bay's
opponent (Chicago) and kickoff; Seattle's kickoff and which side is home; the
Washington, Chicago, Houston and Indianapolis books; every pre-move price;
every depth; and every timestamp not listed above. The session is also
COMPRESSED to 12:40-18:25 UTC rather than the owner's 24 hours: nothing
before or after contributes a move.

What it is for: re-analysis must reproduce the owner's figures from a file
with exactly the records a real session writes. It is a regression fixture,
not evidence about Kalshi, Pinnacle or the strategy.
"""

from __future__ import annotations

import email.utils
import io
import json
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shadow_monitor
from tests.test_shadow_monitor import FakeClock, Response
from tests.test_schedule import board, nfl_market

UTC = timezone.utc
T0 = datetime(2026, 9, 24, 12, 40, 19, tzinfo=UTC)
HOURS = "5.75"
CADENCE = "30"
PRICE = shadow_monitor.session_price(5.75, timedelta(seconds=30))
LATENCY = timedelta(milliseconds=200)
#: The owner's reported median age of a newly observed quote.
PROVIDER_LAG = timedelta(seconds=14)
KEY = "SYNTHETIC-KEY"

TNF = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)          # invented
SUNDAY = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)       # Houston's, reported

HOU_MOVE = datetime(2026, 9, 24, 12, 56, 20, tzinfo=UTC)
GB_MOVE = datetime(2026, 9, 24, 16, 40, 20, tzinfo=UTC)
GB_BACK = GB_MOVE + timedelta(minutes=15)
JAX_MOVE = datetime(2026, 9, 24, 17, 10, 20, tzinfo=UTC)
SEA_MOVE = datetime(2026, 9, 24, 17, 43, 50, tzinfo=UTC)
SEA_RESPONSE = SEA_MOVE + timedelta(seconds=620)

EARLY = datetime(2026, 9, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Team:
    kalshi: str
    espn: str
    name: str


@dataclass(frozen=True)
class Game:
    event_ticker: str
    bucket: str
    kickoff: datetime
    away: Team
    home: Team
    odds_event: str
    #: [(from, away_price, home_price)], American odds, the provider's
    pinnacle: tuple
    #: {kalshi code: [(from, yes_bid, yes_ask, yes_bid_size, no_bid_size)]}
    books: dict


SEA, WAS = (Team("SEA", "SEA", "Seattle Seahawks"),
            Team("WAS", "WSH", "Washington Commanders"))
CHI, GB = (Team("CHI", "CHI", "Chicago Bears"),
           Team("GB", "GB", "Green Bay Packers"))
HOU, IND = (Team("HOU", "HOU", "Houston Texans"),
            Team("IND", "IND", "Indianapolis Colts"))
TEN, JAX = (Team("TEN", "TEN", "Tennessee Titans"),
            Team("JAX", "JAX", "Jacksonville Jaguars"))

GAMES = (
    Game("KXNFLGAME-26SEP24SEAWAS", "20260924", TNF, SEA, WAS, "evt-sea-was",
         ((EARLY, -320, 262), (SEA_MOVE, -354, 290)),
         {"SEA": [(EARLY, 0.74, 0.75, 120.0, 180.0),
                  (SEA_RESPONSE, 0.75, 0.76, 90.0, 140.0)],
          "WAS": [(EARLY, 0.25, 0.26, 175.0, 110.0),
                  (SEA_RESPONSE, 0.24, 0.25, 130.0, 95.0)]}),
    Game("KXNFLGAME-26SEP27CHIGB", "20260927", SUNDAY, CHI, GB, "evt-chi-gb",
         ((EARLY, 215, -260), (GB_MOVE, 241, -287), (GB_BACK, 215, -260)),
         {"GB": [(EARLY, 0.69, 0.70, 210.0, 160.0)],
          "CHI": [(EARLY, 0.30, 0.31, 150.0, 200.0)]}),
    Game("KXNFLGAME-26SEP27HOUIND", "20260927", SUNDAY, HOU, IND, "evt-hou-ind",
         ((EARLY, 130, -150), (HOU_MOVE, 145, -170)),
         {"IND": [(EARLY, 0.58, 0.60, 80.0, 95.0),
                  (HOU_MOVE + timedelta(seconds=300), 0.60, 0.62, 70.0, 85.0)],
          "HOU": [(EARLY, 0.40, 0.42, 90.0, 85.0),
                  (HOU_MOVE + timedelta(seconds=300), 0.38, 0.40, 85.0, 70.0)]}),
    Game("KXNFLGAME-26SEP27TENJAX", "20260927", SUNDAY, TEN, JAX, "evt-ten-jax",
         ((EARLY, 100, -120), (JAX_MOVE, 118, -140)),
         {"JAX": [(EARLY, 0.55, 0.57, 60.0, 75.0)],
          "TEN": [(EARLY, 0.43, 0.45, 70.0, 65.0)]}),
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _at(path, at):
    current = path[0]
    for step in path:
        if at >= step[0]:
            current = step
    return current


def _espn_event(game: Game) -> dict:
    return {"id": f"syn-{game.odds_event}", "date": _iso(game.kickoff),
            "name": f"{game.away.name} at {game.home.name}",
            "competitions": [{
                "date": _iso(game.kickoff), "timeValid": True,
                "competitors": [
                    {"homeAway": "home", "team": {
                        "id": game.home.espn, "abbreviation": game.home.espn,
                        "displayName": game.home.name}},
                    {"homeAway": "away", "team": {
                        "id": game.away.espn, "abbreviation": game.away.espn,
                        "displayName": game.away.name}}]}]}


class SlateNetwork:
    """Answers the monitor's endpoints for the synthetic slate at `now`."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.odds_calls: list[datetime] = []
        self.book_calls: list[tuple[str, datetime]] = []
        self.urls: list[str] = []

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        self.urls.append(url)
        parts = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parts.query)
        at = self.clock.now()
        self.clock.t = at + LATENCY
        if parts.netloc == "site.api.espn.com":
            day = (query.get("dates") or [""])[0]
            return Response(board(*[_espn_event(g) for g in GAMES
                                    if g.bucket == day]))
        if parts.netloc == "api.elections.kalshi.com":
            if parts.path.endswith("/orderbook"):
                return self._book(parts.path.split("/")[-2], at)
            if parts.path.endswith("/markets"):
                markets = []
                for game in GAMES:
                    for team in (game.away, game.home):
                        market = nfl_market(game.event_ticker, team.kalshi,
                                            game.kickoff)
                        del market["result"]
                        market["status"] = "active"
                        markets.append(market)
                return Response({"markets": markets, "cursor": ""})
        if parts.netloc == "api.the-odds-api.com":
            return self._odds(at)
        raise AssertionError(f"no answer for {url}")

    def _odds(self, at: datetime) -> Response:
        self.odds_calls.append(at)
        observed = at - PROVIDER_LAG
        events = []
        for game in GAMES:
            _, away_price, home_price = _at(game.pinnacle, at)
            events.append({
                "id": game.odds_event, "commence_time": _iso(game.kickoff),
                "home_team": game.home.name, "away_team": game.away.name,
                "bookmakers": [{
                    "key": "pinnacle",
                    "last_update": _iso(observed - timedelta(minutes=5)),
                    "markets": [{
                        "key": "h2h", "last_update": _iso(observed),
                        "outcomes": [
                            {"name": game.home.name, "price": home_price},
                            {"name": game.away.name, "price": away_price}]}]}]})
        headers = {"Date": email.utils.format_datetime(
                       at.replace(microsecond=0), usegmt=True),
                   "x-requests-used": str(len(self.odds_calls)),
                   "x-requests-remaining": "50000",
                   "x-requests-last": "1"}
        return Response(events, headers)

    def _book(self, ticker: str, at: datetime) -> Response:
        self.book_calls.append((ticker, at))
        event, code = ticker.rsplit("-", 1)
        game = next(g for g in GAMES if g.event_ticker == event)
        _, bid, ask, bid_size, ask_size = _at(game.books[code], at)
        # TRANSCRIBED from Kalshi's documentation, as the monitor's own
        # tests say of this shape: bids only, YES ask = 1 - best NO bid.
        return Response({"orderbook_fp": {
            "yes_dollars": [[f"{bid - 0.02:.4f}", "25.00"],
                            [f"{bid:.4f}", f"{bid_size:.2f}"]],
            "no_dollars": [[f"{1 - ask:.4f}", f"{ask_size:.2f}"]]}})


def run_session(out_dir: Path) -> tuple[Path, SlateNetwork, str]:
    """Drive the real monitor through the synthetic slate; return the file."""
    clock = FakeClock(T0)
    net = SlateNetwork(clock)
    argv = ["--hours", HOURS, "--cadence-seconds", CADENCE,
            "--out-dir", str(out_dir), "--spend", str(PRICE),
            "--api-key", KEY]
    out = io.StringIO()
    with mock.patch("urllib.request.urlopen", side_effect=net), \
            mock.patch("time.sleep"), \
            mock.patch.object(shadow_monitor.signal, "signal"), \
            redirect_stdout(out), redirect_stderr(out):
        code = shadow_monitor.main(argv, clock=clock)
    if code != 0:
        raise AssertionError(f"synthetic session failed ({code}):\n"
                             f"{out.getvalue()}")
    files = sorted(out_dir.glob("shadow_*.jsonl"))
    if len(files) != 1:
        raise AssertionError(f"expected one session file, found {files}")
    return files[0], net, out.getvalue()


#: What a session recorded before assessments existed (eaea5df) did not
#: write. Stripping them turns a current file into that older shape, so the
#: offline path is tested on the file the owner actually holds.
LATER_DECISION_FIELDS = ("assessment", "tick", "rejection")
LATER_START_FIELDS = ("book_memory_seconds", "entry_tolerance_seconds",
                      "fee_route", "code")


def as_recorded_before_assessments(path: Path, out: Path) -> Path:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") == "decision":
            for key in LATER_DECISION_FIELDS:
                row.pop(key, None)
        elif row.get("kind") == "session_start":
            for key in LATER_START_FIELDS:
                row.pop(key, None)
        elif row.get("kind") == "session_end":
            (row.get("counts") or {}).pop("assessment_errors", None)
        lines.append(json.dumps(row, separators=(",", ":")))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
