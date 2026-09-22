"""Offline replay: run the whole chain over RAW payloads saved to disk.

WHY THE BUNDLE HOLDS RAW PAYLOADS
---------------------------------
A replay bundle stores the provider responses EXACTLY as they arrived, and
this module runs them through `data.odds_history.parse_snapshot` and
`data.kalshi_history.parse_candles` -- the same pure parsers the live
collector uses.

The alternative -- storing already-parsed quotes and candles -- means a second
parser, written here, against my reading of the wire format. That reading has
already been wrong once in this study: Kalshi serialises `*_dollars` as a
fixed-point STRING (`"0.5600"`), an earlier parser accepted only numbers, and
every real bid and ask decoded to None. The fixtures did not catch it because
they were written from an assumption about the format rather than copied from
a response. Raw payloads plus the shared parser means a parser fix reaches
the replay automatically, and a replay can never disagree with a live run
about what a byte sequence means (rule 19).

WHAT A BUNDLE MUST SUPPLY, AND WHAT IT MAY NOT
----------------------------------------------
It must carry, per game: the provider event id, the exchange event ticker, a
kickoff WITH the source that produced it, the odds snapshot payloads, and per
contract the candlestick payloads and which side YES is.

It may not supply a kickoff without saying where it came from. `START_SOURCES`
in `core.matcher` declares that per league because the two sources carry
different warranties: an MLB kickoff is published on the contract itself and
was available at decision time, while an NFL kickoff comes from an external
schedule retrieved after the fact and therefore cannot certify a
point-in-time backtest. A bundle that lost that distinction would let an
exploratory run be read as a historical one.

Settlement is OPTIONAL and its absence is not zero. `reaction.screen` already
withholds every realized figure when settlement is unknown, so a bundle
without it still produces a complete predicted-EV run.

NOTHING HERE FETCHES
--------------------
No network, no credential, no credits. `load_bundle` reads a file and
`replay` computes. Producing a bundle is the collection machine's job, and
`capture.py` declares the interface it must satisfy -- read-only, bounded,
and incapable of placing an order.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import Eligibility, EntryPolicy             # noqa: E402
from core.matcher import START_SOURCE_KINDS                      # noqa: E402
from data.kalshi_history import parse_candles                    # noqa: E402
from data.odds_history import parse_snapshot                     # noqa: E402
from .clocks import envelope_for_sharp_quote                     # noqa: E402
from .detector import MoveDetector, MovePolicy                   # noqa: E402
from .episodes import DataRole, EpisodeLedger, build_ledger      # noqa: E402
from .measure import ReactionPolicy, measure_reaction            # noqa: E402
from .screen import ScreenResult, screen_reaction                # noqa: E402

BUNDLE_SCHEMA = "reaction_replay_bundle/1"


class BundleError(ValueError):
    """The bundle is not usable, and this module will not guess at it.

    Raised rather than degraded. A replay that silently skips a malformed
    game reports thinner coverage and looks like a market with less
    activity -- the exact confusion this study's coverage ledger exists to
    prevent.
    """


@dataclass
class BundleReport:
    """What the bundle contained, and what could not be read from it."""

    games: int = 0
    contracts: int = 0
    snapshots_parsed: int = 0
    snapshot_payloads: int = 0
    candle_payloads: int = 0
    quotes: int = 0
    candles: int = 0
    incomplete_snapshots: list[str] = field(default_factory=list)
    incomplete_candles: list[str] = field(default_factory=list)
    games_without_settlement: list[str] = field(default_factory=list)
    start_sources: dict[str, int] = field(default_factory=dict)
    point_in_time_unverified: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """A parser that could not read a payload is a LOSS, not a quiet zero."""
        return not self.incomplete_snapshots and not self.incomplete_candles

    def as_dict(self) -> dict:
        return {
            "games": self.games,
            "contracts": self.contracts,
            "snapshot_payloads": self.snapshot_payloads,
            "snapshots_parsed": self.snapshots_parsed,
            "candle_payloads": self.candle_payloads,
            "quotes_parsed": self.quotes,
            "candles_parsed": self.candles,
            "parse_complete": self.complete,
            "incomplete_snapshots": self.incomplete_snapshots,
            "incomplete_candles": self.incomplete_candles,
            "games_without_settlement": self.games_without_settlement,
            "start_sources": dict(sorted(self.start_sources.items())),
            "point_in_time_unverified": self.point_in_time_unverified,
            "note": ("an incomplete parse is a LOSS: it reads downstream as "
                     "a market with less activity, which is the opposite of "
                     "what it means"),
        }


@dataclass(frozen=True)
class ReplayContract:
    market_ticker: str
    yes_is_home: bool
    candles: tuple
    settled_yes: int | None


@dataclass(frozen=True)
class ReplayGame:
    provider_event_id: str
    event_ticker: str
    start: datetime
    start_source: str
    historical_schedule_as_of: str
    quotes: tuple
    contracts: tuple[ReplayContract, ...]

    @property
    def point_in_time(self) -> bool:
        """Was the kickoff knowable AT the decision?

        `event_ticker` means the exchange published it on the contract, so
        yes. `external_schedule` means it was retrieved after the fact and
        carries the FINAL time -- a flexed game's schedule cannot say what
        was believed 72h earlier. Such a run is exploratory by construction
        and may not be quoted as a point-in-time backtest.
        """
        return (self.start_source == "event_ticker"
                and self.historical_schedule_as_of != "unverified")


def _time(value: Any, what: str) -> datetime:
    if not isinstance(value, str):
        raise BundleError(f"{what}: expected an ISO-8601 string, got "
                          f"{type(value).__name__}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BundleError(f"{what}: {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        # A naive stamp is not an instant, and attaching UTC would be
        # inventing the offset -- which lands on the wrong DAY for an evening
        # kickoff, the exact failure the ticker-day-vs-UTC-day work fixed.
        raise BundleError(f"{what}: {value!r} is not timezone-aware")
    return parsed


def load_bundle(path: str | Path) -> tuple[list[ReplayGame], BundleReport]:
    """Read and VALIDATE a replay bundle. Raises rather than guessing."""
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise BundleError(f"bundle root is {type(payload).__name__}, "
                          f"expected an object")
    schema = payload.get("schema")
    if schema != BUNDLE_SCHEMA:
        raise BundleError(f"bundle schema is {schema!r}, expected "
                          f"{BUNDLE_SCHEMA!r} -- refusing to guess at the "
                          f"shape of an unknown version")
    raw_games = payload.get("games")
    if not isinstance(raw_games, list) or not raw_games:
        raise BundleError("bundle has no 'games' list; an empty bundle is a "
                          "collection failure, not a quiet market")

    report = BundleReport()
    games: list[ReplayGame] = []
    for index, raw in enumerate(raw_games):
        where = f"games[{index}]"
        if not isinstance(raw, dict):
            raise BundleError(f"{where} is {type(raw).__name__}, expected "
                              f"an object")
        event_id = raw.get("provider_event_id")
        ticker = raw.get("event_ticker")
        for name, value in (("provider_event_id", event_id),
                            ("event_ticker", ticker)):
            if not isinstance(value, str) or not value:
                raise BundleError(f"{where}: {name} is missing or not a string")
        start_source = raw.get("start_source")
        if start_source not in START_SOURCE_KINDS:
            raise BundleError(
                f"{where}: start_source {start_source!r} is not one of "
                f"{sorted(START_SOURCE_KINDS)}. A kickoff with no declared "
                f"source cannot be judged for point-in-time validity, and "
                f"the two sources carry different warranties")
        start = _time(raw.get("start"), f"{where}.start")
        as_of = raw.get("historical_schedule_as_of", "unverified")

        quotes: list = []
        for snap_index, body in enumerate(raw.get("odds_snapshots") or []):
            report.snapshot_payloads += 1
            result = parse_snapshot(body)
            if not result.coverage.complete:
                report.incomplete_snapshots.append(
                    f"{event_id} snapshot[{snap_index}]: "
                    f"{'; '.join(result.coverage.reasons) or 'incomplete'}")
                continue
            report.snapshots_parsed += 1
            quotes.extend(q for q in result.quotes
                          if q.provider_event_id == event_id)

        contracts: list[ReplayContract] = []
        for contract_index, raw_contract in enumerate(
                raw.get("contracts") or []):
            spot = f"{where}.contracts[{contract_index}]"
            if not isinstance(raw_contract, dict):
                raise BundleError(f"{spot} is {type(raw_contract).__name__}")
            market_ticker = raw_contract.get("market_ticker")
            if not isinstance(market_ticker, str) or not market_ticker:
                raise BundleError(f"{spot}: market_ticker is missing")
            yes_is_home = raw_contract.get("yes_is_home")
            if not isinstance(yes_is_home, bool):
                raise BundleError(
                    f"{spot}: yes_is_home must be a boolean. An unoriented "
                    f"probability is a silently INVERTED signal, not a "
                    f"smaller effect, so it is never defaulted")
            settled = raw_contract.get("settled_yes")
            if settled is not None and settled not in (0, 1):
                raise BundleError(f"{spot}: settled_yes must be 0, 1 or "
                                  f"absent, got {settled!r}")
            candles: list = []
            for body_index, body in enumerate(
                    raw_contract.get("candlesticks") or []):
                report.candle_payloads += 1
                parsed, coverage = parse_candles(body)
                if not coverage.complete:
                    report.incomplete_candles.append(
                        f"{market_ticker} payload[{body_index}]: "
                        f"{'; '.join(coverage.reasons) or 'incomplete'}")
                    continue
                candles.extend(parsed)
            candles.sort(key=lambda c: c.ts)
            report.candles += len(candles)
            report.contracts += 1
            contracts.append(ReplayContract(market_ticker, yes_is_home,
                                            tuple(candles), settled))

        if not contracts:
            raise BundleError(f"{where}: no contracts. A game with no "
                              f"contract is a join failure and has to be "
                              f"reported as one, not dropped")
        quotes.sort(key=lambda q: q.snapshot)
        report.quotes += len(quotes)
        report.games += 1
        report.start_sources[start_source] = (
            report.start_sources.get(start_source, 0) + 1)
        game = ReplayGame(event_id, ticker, start, start_source, as_of,
                          tuple(quotes), tuple(contracts))
        if not game.point_in_time:
            report.point_in_time_unverified.append(event_id)
        if all(c.settled_yes is None for c in contracts):
            report.games_without_settlement.append(event_id)
        games.append(game)
    return games, report


def replay(games: Sequence[ReplayGame], *,
           move_policy: MovePolicy | None = None,
           reaction_policy: ReactionPolicy | None = None,
           eligibility: Eligibility | None = None,
           entry_policy: EntryPolicy | None = None,
           entry_delay: timedelta = timedelta(0),
           declared_role: DataRole = DataRole.UNDECLARED,
           series: str | None = None) -> EpisodeLedger:
    """Detect, measure, screen and account for every game in the bundle.

    ONE DETECTOR ACROSS THE WHOLE RUN, because its state is keyed by stream
    and a per-game detector would re-establish a baseline on every game --
    which is correct here only because streams do not span games. It is kept
    shared anyway so that a future bundle carrying one stream across two
    games cannot silently gain a free trigger.
    """
    move_policy = move_policy or MovePolicy()
    reaction_policy = reaction_policy or ReactionPolicy()
    detector = MoveDetector(move_policy)
    observation_count = 0
    reactions: list = []
    screen_result = ScreenResult()

    for game in games:
        envelopes = [envelope_for_sharp_quote(q) for q in game.quotes]
        observation_count += len(envelopes)
        triggers = []
        for envelope in envelopes:
            trigger = detector.observe(envelope)
            if trigger is not None:
                triggers.append(trigger)
        for trigger in triggers:
            for contract in game.contracts:
                reaction = measure_reaction(
                    trigger, contract.candles,
                    market_ticker=contract.market_ticker,
                    yes_is_home=contract.yes_is_home,
                    policy=reaction_policy)
                reactions.append(reaction)
                screen_result.screened.append(screen_reaction(
                    reaction, trigger, contract.candles,
                    market_ticker=contract.market_ticker,
                    yes_is_home=contract.yes_is_home,
                    start=game.start, settled_yes=contract.settled_yes,
                    entry_delay=entry_delay, eligibility=eligibility,
                    series=series))

    return build_ledger(
        detector_result=detector.result, reactions=reactions,
        screen_result=screen_result, observation_count=observation_count,
        declared_role=declared_role, eligibility=eligibility,
        entry_policy=entry_policy, move_policy=move_policy,
        reaction_policy=reaction_policy, series=series)


def replay_file(path: str | Path, **kwargs
                ) -> tuple[EpisodeLedger, BundleReport]:
    """Load a bundle and replay it. The only entry point a CLI needs."""
    games, report = load_bundle(path)
    return replay(games, **kwargs), report


def render_report(report: BundleReport,
                  games: Sequence[ReplayGame] | None = None) -> str:
    lines = ["REPLAY BUNDLE", ""]
    lines.append(f"    games                       {report.games:>7,}")
    lines.append(f"    contracts                   {report.contracts:>7,}")
    lines.append(f"    odds snapshots parsed       {report.snapshots_parsed:>7,}"
                 f" / {report.snapshot_payloads:,}")
    lines.append(f"    sharp quotes                {report.quotes:>7,}")
    lines.append(f"    candles                     {report.candles:>7,}")
    lines.append("")
    if not report.complete:
        lines.append("    *** PARSE INCOMPLETE. These are LOSSES, not quiet")
        lines.append("    *** zeros: unread payloads read downstream as a")
        lines.append("    *** market with less activity.")
        for reason in (report.incomplete_snapshots
                       + report.incomplete_candles)[:10]:
            lines.append(f"        {reason}")
        lines.append("")
    for source, count in sorted(report.start_sources.items()):
        lines.append(f"    kickoff from {source:<16} {count:>7,}")
    if report.point_in_time_unverified:
        lines.append("")
        lines.append(f"    {len(report.point_in_time_unverified)} game(s) carry "
                     f"a kickoff that was NOT knowable at the")
        lines.append("    decision (external schedule, retrieved after the "
                     "fact, final time")
        lines.append("    only). This run is EXPLORATORY by construction and "
                     "may not be")
        lines.append("    quoted as a point-in-time backtest.")
    if report.games_without_settlement:
        lines.append("")
        lines.append(f"    {len(report.games_without_settlement)} game(s) carry "
                     f"no settlement. Predicted EV is still")
        lines.append("    reportable; every realized figure is WITHHELD for "
                     "those.")
    return "\n".join(lines)
