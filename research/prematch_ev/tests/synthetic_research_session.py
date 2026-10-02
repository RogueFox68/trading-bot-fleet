"""SYNTHETIC: a shadow session shaped like the two behaviours the owner's
2026-10-01 audit found, for the research channels to be demonstrated on.

NOTHING IN THIS FILE WAS OBSERVED. It drives the REAL monitor -- fetchers,
join, adjacent detector, screen, research channels, recorder -- over a
scripted network, so the file it writes has every record a real session
writes, in the monitor's order and shape. The owner's raw session
(`shadow_20261001T044202Z.jsonl`) is private and not in this repository;
this is built only from the figures reported in PR #27, comment 5953266882:

  * PIT-CLE: Pinnacle PIT -143 / CLE +129 drifting to PIT -150 / CLE +135
    (Shin CLE 0.4241019282 -> 0.4127659574, 1.1336pp) within an hour, the
    last change at about 22:33 UTC, about 1.70h before kickoff. No single
    step reaches a point.
  * LAR-PHI: LAR -176 / PHI +151 at about 23:17:06 UTC; one poll later an
    HTTP 200 answer with the event present and `bookmakers: []`; one poll
    after that LAR -189 / PHI +161 -- PHI 1.5781pp lower, about 66h before
    kickoff.
  * NYG-ARI: a move about 73.8h before kickoff, beyond the 73h book
    horizon -- the owner's Arizona-NYG change was at 73.74h.

EVERYTHING ELSE IS INVENTED: every kickoff (chosen to match the reported
lead times), which team is home, the intermediate PIT-CLE prices and their
timing, the NYG-ARI prices, every Kalshi book and depth, every timestamp not
listed above, and the window -- 21:20 to 23:50 UTC, compressed from the
owner's 24 hours. It is a regression fixture and a demonstration of what
the channels record, not evidence about any book, exchange or strategy.
"""

from __future__ import annotations

import email.utils
import io
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import shadow_monitor
from tests.synthetic_session import Team, _at, _espn_event, _iso
from tests.test_schedule import board, nfl_market
from tests.test_shadow_monitor import FakeClock, Response

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 21, 20, 19, tzinfo=UTC)
HOURS = 2.5
CADENCE = timedelta(seconds=30)
PRICE = shadow_monitor.session_price(HOURS, CADENCE)
LATENCY = timedelta(milliseconds=200)
PROVIDER_LAG = timedelta(seconds=14)
KEY = "SYNTHETIC-KEY"
EARLY = datetime(2026, 9, 1, tzinfo=UTC)

PIT_CLE_KICKOFF = datetime(2026, 10, 2, 0, 15, tzinfo=UTC)     # TNF
LAR_PHI_KICKOFF = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)
NYG_ARI_KICKOFF = datetime(2026, 10, 5, 0, 20, tzinfo=UTC)

#: The drift: one invented sub-point step every six minutes, the last at
#: about 22:33, so the change accumulates inside one hour.
DRIFT_STEPS = ((datetime(2026, 10, 1, 21, 51, tzinfo=UTC), -144, 130),
               (datetime(2026, 10, 1, 21, 57, tzinfo=UTC), -145, 130),
               (datetime(2026, 10, 1, 22, 3, tzinfo=UTC), -146, 131),
               (datetime(2026, 10, 1, 22, 9, tzinfo=UTC), -147, 132),
               (datetime(2026, 10, 1, 22, 15, tzinfo=UTC), -148, 133),
               (datetime(2026, 10, 1, 22, 21, tzinfo=UTC), -149, 134),
               (datetime(2026, 10, 1, 22, 32, 50, tzinfo=UTC), -150, 135))
#: One poll answers with LAR-PHI present and no sharp quote.
EMPTY_BOOKMAKERS = (datetime(2026, 10, 1, 23, 17, 20, tzinfo=UTC),
                    datetime(2026, 10, 1, 23, 17, 50, tzinfo=UTC))
LAR_PHI_RETURN = EMPTY_BOOKMAKERS[1]
NYG_ARI_MOVE = datetime(2026, 10, 1, 22, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Game:
    event_ticker: str
    bucket: str
    kickoff: datetime
    away: Team
    home: Team
    odds_event: str
    pinnacle: tuple
    books: dict
    empty: tuple | None = None


PIT, CLE = (Team("PIT", "PIT", "Pittsburgh Steelers"),
            Team("CLE", "CLE", "Cleveland Browns"))
LAR, PHI = (Team("LAR", "LAR", "Los Angeles Rams"),
            Team("PHI", "PHI", "Philadelphia Eagles"))
NYG, ARI = (Team("NYG", "NYG", "New York Giants"),
            Team("ARI", "ARI", "Arizona Cardinals"))

GAMES = (
    Game("KXNFLGAME-26OCT01PITCLE", "20261001", PIT_CLE_KICKOFF, PIT, CLE,
         "evt-pit-cle", ((EARLY, -143, 129),) + DRIFT_STEPS,
         {"CLE": [(EARLY, 0.42, 0.43, 140.0, 120.0),
                  (datetime(2026, 10, 1, 22, 45, tzinfo=UTC),
                   0.41, 0.42, 110.0, 130.0)],
          "PIT": [(EARLY, 0.57, 0.58, 125.0, 135.0),
                  (datetime(2026, 10, 1, 22, 45, tzinfo=UTC),
                   0.58, 0.59, 120.0, 105.0)]}),
    Game("KXNFLGAME-26OCT04LARPHI", "20261004", LAR_PHI_KICKOFF, LAR, PHI,
         "evt-lar-phi", ((EARLY, -176, 151), (LAR_PHI_RETURN, -189, 161)),
         {"PHI": [(EARLY, 0.37, 0.38, 90.0, 80.0),
                  (LAR_PHI_RETURN + timedelta(minutes=7),
                   0.36, 0.37, 85.0, 95.0)],
          "LAR": [(EARLY, 0.62, 0.63, 75.0, 95.0),
                  (LAR_PHI_RETURN + timedelta(minutes=7),
                   0.63, 0.64, 90.0, 80.0)]},
         empty=EMPTY_BOOKMAKERS),
    Game("KXNFLGAME-26OCT04NYGARI", "20261004", NYG_ARI_KICKOFF, NYG, ARI,
         "evt-nyg-ari", ((EARLY, 120, -140), (NYG_ARI_MOVE, 135, -158)),
         {"ARI": [(EARLY, 0.56, 0.57, 60.0, 70.0)],
          "NYG": [(EARLY, 0.43, 0.44, 70.0, 60.0)]}),
)


class ResearchNetwork:
    """Answers the monitor's endpoints for the scripted slate at `now`."""

    def __init__(self, clock: FakeClock, games: tuple = GAMES):
        self.clock = clock
        self.games = games
        self.odds_calls: list[datetime] = []
        self.book_calls: list[tuple[str, datetime]] = []

    def __call__(self, request, *args, **kwargs):
        url = getattr(request, "full_url", request)
        parts = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parts.query)
        at = self.clock.now()
        self.clock.t = at + LATENCY
        if parts.netloc == "site.api.espn.com":
            day = (query.get("dates") or [""])[0]
            return Response(board(*[_espn_event(g) for g in self.games
                                    if g.bucket == day]))
        if parts.netloc == "api.elections.kalshi.com":
            if parts.path.endswith("/orderbook"):
                return self._book(parts.path.split("/")[-2], at)
            if parts.path.endswith("/markets"):
                markets = []
                for game in self.games:
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

    def observed_for(self, game: Game, at: datetime) -> datetime:
        """The provider's observation stamp on `game`'s quote at `at`: a
        fixed lag behind the poll, so every answer is a new observation."""
        return at - PROVIDER_LAG

    def _odds(self, at: datetime) -> Response:
        self.odds_calls.append(at)
        events = []
        for game in self.games:
            observed = self.observed_for(game, at)
            _, away_price, home_price = _at(game.pinnacle, at)
            event = {"id": game.odds_event,
                     "commence_time": _iso(game.kickoff),
                     "home_team": game.home.name,
                     "away_team": game.away.name}
            if game.empty and game.empty[0] <= at < game.empty[1]:
                # PRESENT, WITH NO SHARP QUOTE: what the owner's raw answer
                # held for LAR-PHI at 23:17:36.
                event["bookmakers"] = []
            else:
                event["bookmakers"] = [{
                    "key": "pinnacle",
                    "last_update": _iso(observed - timedelta(minutes=5)),
                    "markets": [{
                        "key": "h2h", "last_update": _iso(observed),
                        "outcomes": [
                            {"name": game.home.name, "price": home_price},
                            {"name": game.away.name, "price": away_price}]}]}]
            events.append(event)
        headers = {"Date": email.utils.format_datetime(
                       at.replace(microsecond=0), usegmt=True),
                   "x-requests-used": str(len(self.odds_calls)),
                   "x-requests-remaining": "50000",
                   "x-requests-last": "1"}
        return Response(events, headers)

    def _book(self, ticker: str, at: datetime) -> Response:
        self.book_calls.append((ticker, at))
        event, code = ticker.rsplit("-", 1)
        game = next(g for g in self.games if g.event_ticker == event)
        _, bid, ask, bid_size, ask_size = _at(game.books[code], at)
        # TRANSCRIBED from Kalshi's documentation, as the monitor's own tests
        # say of this shape: bids only, YES ask = 1 - best NO bid.
        return Response({"orderbook_fp": {
            "yes_dollars": [[f"{bid - 0.02:.4f}", "25.00"],
                            [f"{bid:.4f}", f"{bid_size:.2f}"]],
            "no_dollars": [[f"{1 - ask:.4f}", f"{ask_size:.2f}"]]}})


def run_session(out_dir: Path, *, network: type = ResearchNetwork,
                hours: float = HOURS, start: datetime = T0,
                games: tuple = GAMES) -> tuple[Path, ResearchNetwork, str]:
    """Drive the real monitor through the scripted slate; return the file."""
    clock = FakeClock(start)
    net = network(clock, games)
    price = shadow_monitor.session_price(hours, CADENCE)
    argv = ["--hours", str(hours), "--cadence-seconds",
            str(CADENCE.total_seconds()), "--out-dir", str(out_dir),
            "--spend", str(price), "--api-key", KEY]
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
