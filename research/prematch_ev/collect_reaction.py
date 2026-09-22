"""Backtest collector for the reaction-lag study: provider payloads in, a bundle out.

    python3 collect_reaction.py --day 2026-09-20 --probe              # plan, free
    python3 collect_reaction.py --day 2026-09-20 --probe --spend 40   # coverage probe
    python3 collect_reaction.py --day 2026-09-20 --lead-hours 48      # plan, free
    python3 collect_reaction.py --day 2026-09-20 --lead-hours 48 \\
        --spend 7320 --out bundle.json                                # collect
    python3 run_reaction.py --replay bundle.json --max-wait 1800      # analyse

THE ONLY PART OF THE REACTION STUDY THAT SPENDS CREDITS
-------------------------------------------------------
`reaction/` and `run_reaction.py` are offline. (The checkpoint study's
`run_study.py` is the other paid entry point in this directory, under its own
`--max-credits` cap.) This is the collection machine that
`reaction/capture.py` describes: it resolves one NFL date's slate, prices the
exact UTC instants it would request, and -- only when told to -- buys them from
the odds archive, adds the exchange's free 1-minute candles, and writes a
`reaction_replay_bundle/2` that `run_reaction.py --replay` reads through the
same parsers as everything else.

THE DEFAULT IS A PLAN, NOT A PURCHASE
-------------------------------------
Without `--spend` it makes only FREE requests (the ESPN schedule and Kalshi's
public market listing), prints the manifest and what is already cached, and
exits having spent nothing. `--spend N` is the operator's confirmation that
they accept a price, and it must be at least what this run needs; the cap
actually enforced is what the run needs, never more. Everything a paid request
returns is cached raw, so an interrupted run resumes by paying only for the
instants it has not got -- and the coverage probe's snapshots are reused by the
full run for free, because they fall on the same grid.

WHAT IT CANNOT DO
-----------------
It cannot trade. It holds no exchange credential, `reaction.capture
.assert_read_only` scans every module it can reach before the first paid
request, and every request in the process -- whichever module makes it -- has
to match `ALLOWED_ENDPOINTS` or the run stops. It cannot collect the future:
the archive only holds instants that have happened, so a window reaching past
now is refused before anything is spent. And it does not buy on a partial
picture: a slate whose listing or schedule did not fully answer may be
missing games, so its manifest may be the wrong one, and nothing is bought.

WHAT IT DELIBERATELY DOES NOT DECIDE
------------------------------------
Which contracts belong to which sharp event, and which side a contract pays
on, are decided by `collect.join_markets` and `collect.yes_side` -- the same
code the checkpoint study uses -- not re-derived here. Kickoffs come from the
same `StartResolver`. A collector with its own join would produce bundles the
rest of the study disagreed with (rule 19).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from collect import (                                           # noqa: E402
    START_AGREEMENT, Ledger, StartResolver, join_markets, yes_side,
)
from core.matcher import kalshi_event_ticker, parse_event_body_date  # noqa: E402
from data import espn_schedule, kalshi_history, odds_history      # noqa: E402
from data.cache import (                                           # noqa: E402
    CreditCapReached, ResponseCache, redact, resolve_key,
)
from data.kalshi_history import Coverage, settlement_outcome       # noqa: E402
from data.odds_history import (                                    # noqa: E402
    DEFAULT_BOOKMAKERS, CreditLedger, estimate_credits, parse_snapshot,
)
from reaction.capture import (                                     # noqa: E402
    CaptureWindow, TimestampManifest, assert_read_only, build_manifest,
    require_allowed_endpoint,
)
from reaction.episodes import DEVELOPMENT_WINDOW                   # noqa: E402
from reaction.measure import ReactionPolicy                        # noqa: E402
from reaction.replay import BUNDLE_SCHEMA_POOLED                   # noqa: E402

#: The coverage probe: four instants -- T-72h, T-48h, T-24h and kickoff --
#: against the day's earliest kickoff. It answers one question, "from when is
#: the sharp book quoting these games", which sets the window a full run buys.
PROBE_LEAD = timedelta(hours=72)
PROBE_CADENCE = timedelta(hours=24)

#: Candles are requested in pieces of at most this span. Kalshi documents a
#: candle cap for its BATCH endpoint (10,000 across a batch); this session
#: found none for the single-market endpoint used here, so no chunk size is
#: trusted to be small enough -- see `collect_candles`, which checks.
CANDLE_CHUNK = timedelta(hours=24)

#: The exchange's candle period: candles close on these boundaries.
CANDLE_PERIOD = ReactionPolicy().candle_period

#: Follow-up requests allowed inside one chunk when responses keep stopping
#: short of candles that exist. Past it the span is reported incomplete.
MAX_CONTINUATIONS = 8

#: How far before a game's first odds sample its candles must start: the
#: measurement looks back this far for the exchange's pre-move baseline.
CANDLE_LEAD_IN = ReactionPolicy().lookback + CANDLE_PERIOD

#: Consecutive instants that may fail outright before the run stops buying.
#: One lost instant is a hole the replay can carry; several in a row is an
#: outage or a refused key, and every further attempt reserves credits
#: against the cap for the same answer.
STOP_AFTER_FAILED_INSTANTS = 3

EXIT_OK, EXIT_INCOMPLETE, EXIT_USAGE = 0, 1, 2

#: Where paid responses and bundles land by default: gitignored, and anchored
#: to this file rather than the working directory. A cache relative to the
#: working directory is a different cache from another directory, and a
#: re-run from there would buy again what it had already paid for. It is also
#: where the checkpoint study caches when run from this directory, and the
#: cache key is the request's meaning, so either reuses what the other bought.
OUTPUT_DIR = HERE / "study_output"


class CollectionStopped(RuntimeError):
    """The run stopped buying before its manifest was complete."""


# --- the slate: FREE requests only ------------------------------------------

@dataclass
class Slate:
    """One date's games, as the exchange lists them and the schedule dates them."""

    day: date
    league: str
    series: str
    markets: dict[str, dict] = field(default_factory=dict)
    kickoffs: dict[str, datetime] = field(default_factory=dict)
    resolver: StartResolver | None = None
    coverage: Coverage = field(default_factory=Coverage)
    unresolved: dict[str, int] = field(default_factory=dict)

    def tickers_of(self, event: str) -> list[str]:
        return sorted(t for t, m in self.markets.items()
                      if (kalshi_event_ticker(m) or t) == event)


def resolve_slate(day: date, *, league: str = "NFL",
                  series: str = "KXNFLGAME",
                  schedule_cache: str | None = None,
                  enumerate_markets: Callable = kalshi_history
                  .enumerate_settled_markets,
                  fetch_schedule: Callable = espn_schedule.fetch_schedule
                  ) -> Slate:
    """The day's settled contracts and their kickoffs. Costs no credits.

    A contract belongs to the day its EVENT BODY names (`26SEP13...`), which
    is the local game day -- not the UTC day of kickoff, which for an evening
    game is the next one.
    """
    slate = Slate(day=day, league=league.upper(), series=series)
    every, coverage = enumerate_markets(series)
    slate.coverage.merge(coverage)
    slate.markets = {
        ticker: market for ticker, market in every.items()
        if parse_event_body_date(kalshi_event_ticker(market) or ticker) == day}
    try:
        schedule = fetch_schedule(league, day, day, cache_dir=schedule_cache)
    except Exception as exc:                     # transport-shaped, by contract
        slate.coverage.fail(redact(f"schedule retrieval failed: {exc}. An "
                                   f"unreachable schedule is not an empty "
                                   f"slate"))
        schedule = None
    slate.resolver = StartResolver(league=slate.league, schedule=schedule)
    slate.resolver.prime(slate.markets)
    slate.unresolved = slate.resolver.failure_counts(per="contracts")
    for ticker, market in slate.markets.items():
        kickoff = slate.resolver.start(market)
        if kickoff is not None:
            slate.kickoffs[kalshi_event_ticker(market) or ticker] = kickoff
    return slate


# --- the manifest -----------------------------------------------------------

def windows_for(slate: Slate, *, lead: timedelta, cadence: timedelta,
                probe: bool) -> list[CaptureWindow]:
    """One window per distinct kickoff; the probe uses the earliest only."""
    kickoffs = sorted(set(slate.kickoffs.values()))
    if not kickoffs:
        return []
    if probe:
        return [CaptureWindow(kickoffs[0], PROBE_LEAD, PROBE_CADENCE, "probe")]
    return [CaptureWindow(k, lead, cadence, f"kickoff {k.isoformat()}")
            for k in kickoffs]


def manifest_for(windows: Sequence[CaptureWindow], *, cadence: timedelta,
                 probe: bool, retry_fraction: float) -> TimestampManifest:
    """The probe is four hand-checked instants with no retry reserve; a full
    run is grid-aligned so that kickoff clusters share instants."""
    if probe:
        return build_manifest(windows, retry_fraction=0.0)
    return build_manifest(windows, align_to=cadence,
                          retry_fraction=retry_fraction)


def uncached(manifest: TimestampManifest, cache: ResponseCache,
             sport: str) -> list[datetime]:
    """The instants a run would actually have to BUY.

    Reads through `resolve_key`, the same function the fetch uses, so this
    estimate and the run cannot disagree about what is already on disk.
    """
    if not cache.enabled:
        return list(manifest.timestamps)
    return [at for at in manifest.timestamps
            if not cache.has(resolve_key(cache, sport, at,
                                         DEFAULT_BOOKMAKERS, "h2h"))]


def replay_max_wait_seconds(cadence: timedelta) -> int:
    """The response horizon to replay a bundle collected at `cadence` with.

    The declared 30-minute horizon, or one cadence interval if that is
    longer -- never shorter than the declared horizon. The rule used to be
    "one cadence interval", written when the pilot's grid WAS 30 minutes and
    the two could not differ. At a 5-minute grid they do, and a 300s horizon
    right-censors every exchange response slower than about four minutes:
    precisely the slow followers a 5-minute poller could trade against.

    Shortening the horizon would not buy clean tiling either: the 30-minute
    LOOKBACK overlaps earlier moves' windows at any grid finer than 30
    minutes, whatever the horizon. What guards against one exchange step
    being credited to two moves is the verdict's per-response dedupe, and the
    horizon changes nothing about it.
    """
    declared = ReactionPolicy().max_wait
    return int(max(cadence, declared).total_seconds())


def credits_needed(to_buy: int, retry_fraction: float) -> int:
    """What THIS run may spend: the uncached instants plus their retry
    reserve, priced through the shared cost model."""
    if to_buy <= 0:
        return 0
    return estimate_credits(1, to_buy + math.ceil(to_buy * retry_fraction))


# --- guards -------------------------------------------------------------------

def assert_nothing_here_can_trade() -> None:
    """Scan every module this process can reach for trading capability.

    `assert_read_only` defaults to the reaction package. The collector also
    imports the data layer, the matcher and the checkpoint study, so each of
    those directories is scanned too -- and so is this one.
    """
    for directory in (HERE / "reaction", HERE / "data", HERE / "core",
                      HERE / "analysis", HERE):
        assert_read_only(directory)


@contextmanager
def only_declared_endpoints() -> Iterator[None]:
    """Refuse any request, from any module, outside `ALLOWED_ENDPOINTS`.

    Installed around every network step. The fetchers live in three modules
    and each calls `urllib.request.urlopen` itself, so a convention in one of
    them would not bind the others; this binds the process. A refused request
    raises `CaptureRefused`, which the fetchers do not catch, so it stops the
    run instead of being retried into a quieter failure.
    """
    real = urllib.request.urlopen

    def gated(request: Any, *args: Any, **kwargs: Any) -> Any:
        require_allowed_endpoint(getattr(request, "full_url", request))
        return real(request, *args, **kwargs)

    urllib.request.urlopen = gated
    try:
        yield
    finally:
        urllib.request.urlopen = real


# --- paid collection ------------------------------------------------------------

def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_pool(instants: Sequence[datetime], *, sport: str, api_key: str,
                 ledger: CreditLedger, cache: ResponseCache,
                 failures: list[str],
                 fetch: Callable = odds_history.fetch_snapshot) -> list[Any]:
    """One raw snapshot per instant, in order. Paid, except for cache hits.

    AN INSTANT THAT DID NOT ARRIVE STILL GETS AN ENTRY. Leaving it out would
    make the pool quietly shorter, and a missing snapshot then reads as two
    adjacent ones -- the exact defect the replay's gap handling exists to
    prevent. The placeholder carries the instant and the reason, fails the
    parser, and so reaches the replay as a hole AND a counted loss.

    A RUN OF THEM STOPS THE PURCHASE. `STOP_AFTER_FAILED_INSTANTS` in a row
    raises `CollectionStopped`; `CreditCapReached` is not caught either. Both
    reach `main`, which writes no bundle from a run that did not finish.

    `failures` is appended to AS INSTANTS FAIL, so a caller that sees either
    exception still has the reasons. Without them a refused key surfaced only
    as "budget reached" -- which reads as money spent, not a request refused.
    """
    pool: list[Any] = []
    streak = 0
    for at in instants:
        result = fetch(sport, at, api_key, ledger=ledger, cache=cache)
        if result.raw is not None:
            pool.append(result.raw)
            streak = 0
            continue
        reason = redact("; ".join(result.coverage.reasons) or "no response")
        failures.append(f"{_iso(at)}: {reason}")
        pool.append({"timestamp": _iso(at), "collection_failure": reason})
        streak += 1
        if streak >= STOP_AFTER_FAILED_INSTANTS:
            raise CollectionStopped(
                f"{streak} consecutive instants failed; stopping rather than "
                f"reserving the rest of the budget against the same failure")
    return pool


def collect_candles(ticker: str, *, series: str, market: dict,
                    since: datetime, until: datetime,
                    cutoff: datetime | None,
                    fetch: Callable = kalshi_history.fetch_candlestick_payload
                    ) -> tuple[list[Any], Coverage, int]:
    """Raw 1-minute candles for one contract over [since, until]. Free.

    Returns (payloads, coverage, cut_short): `cut_short` counts responses
    that stopped before candles which the next request then returned.

    CHUNKS NEVER OVERLAP. Boundaries sit at `since + k * CANDLE_CHUNK`, and
    each chunk after the first starts one second past the previous boundary,
    because a candle stored twice is a minute counted twice by everything
    downstream.

    NO RESPONSE IS TRUSTED TO HOLD ITS WHOLE SPAN. An endpoint that caps a
    response returns part of the span with nothing to say so -- the shape of
    the fleet's `limit`-with-`start` bars (fleet rule 12). Here the damage
    would be quiet: every missing minute becomes a hole the measurement
    reports as a blind interval, so a truncated collection reads as an
    exchange nobody could see, not as a truncation. So whenever a response's
    newest candle leaves room for another before its span ends, the rest of
    the span is requested. An empty answer means the market was quiet;
    candles in it mean the previous answer was cut short -- they are kept and
    counted. A span still yielding candles after `MAX_CONTINUATIONS`
    follow-ups is marked incomplete rather than assumed finished, and so is
    one answered with candles outside the span requested -- an endpoint that
    ignores `start_ts` would otherwise make "cut short" look like "quiet".

    A body that does not parse is kept, so the replay's parser reports it
    too, and its parse failure joins this coverage.
    """
    archive = kalshi_history.uses_archive(market, cutoff)
    payloads: list[Any] = []
    coverage = Coverage()
    cut_short = 0
    cursor, boundary = since, since
    while cursor < until:
        boundary = min(boundary + CANDLE_CHUNK, until)
        start = cursor
        for follow_up in range(MAX_CONTINUATIONS + 1):
            body, chunk = fetch(ticker, series, start, boundary,
                                use_archive=archive)
            coverage.merge(chunk)
            if body is None:
                break
            candles, parsed = kalshi_history.parse_candles(body)
            coverage.merge(parsed)
            fresh = [c.ts for c in candles if start <= c.ts <= boundary]
            if len(fresh) < len(candles):
                # "Empty means quiet" only holds for an endpoint that honours
                # the span it was asked for. One that does not could answer
                # every follow-up with candles we already have.
                coverage.fail(
                    f"{ticker}: asked for {_iso(start)}..{_iso(boundary)} and "
                    f"got {len(candles) - len(fresh)} candle(s) outside it; "
                    f"an endpoint not honouring the span cannot show that "
                    f"the span is complete")
            if fresh or not parsed.complete:
                payloads.append(body)
            if not fresh:
                break
            cut_short += follow_up > 0
            if max(fresh) + CANDLE_PERIOD > boundary:
                break
            start = max(fresh) + timedelta(seconds=1)
        else:
            coverage.fail(
                f"{ticker}: {_iso(cursor)}..{_iso(boundary)} was still "
                f"returning candles after {MAX_CONTINUATIONS} follow-up "
                f"requests; the span is not known to be complete")
        cursor = boundary + timedelta(seconds=1)
    return payloads, coverage, cut_short


# --- assembly ---------------------------------------------------------------------

@dataclass
class Assembly:
    games: list[dict] = field(default_factory=list)
    excluded: dict[str, list[str]] = field(default_factory=dict)
    candle_warnings: list[str] = field(default_factory=list)
    #: Candle responses that stopped short and were completed by follow-up
    #: requests. Recovered, so not a warning -- but a count above zero means
    #: `CANDLE_CHUNK` is larger than what the endpoint returns in one piece.
    candles_cut_short: int = 0

    def exclude(self, reason: str, what: str) -> None:
        self.excluded.setdefault(reason, []).append(what)


def assemble(slate: Slate, pool: Sequence[Any],
             windows: Sequence[CaptureWindow], *, cadence: timedelta,
             cutoff: datetime | None,
             candle_fetch: Callable = kalshi_history.fetch_candlestick_payload
             ) -> tuple[Assembly, Ledger]:
    """Join the day's contracts to sharp events and attach their candles.

    The join is `collect.join_markets` and the orientation is
    `collect.yes_side` -- this function only arranges their answers into a
    bundle. Every contract that does not make it in is named with a reason.
    """
    quotes_by_event: dict[str, list] = {}
    for body in pool:
        for quote in parse_snapshot(body).quotes:
            quotes_by_event.setdefault(quote.provider_event_id,
                                       []).append(quote)
    ledger = Ledger()
    joined = join_markets(slate.markets, quotes_by_event, slate.league,
                          ledger, slate.resolver)
    opens = {w.kickoff: w.timestamps(cadence)[0] for w in windows}

    out = Assembly()
    by_event: dict[str, list] = {}
    for contract in joined:
        by_event.setdefault(contract.event_ticker, []).append(contract)
    for event, contracts in sorted(by_event.items()):
        provider_id = contracts[0].provider_event_id
        start = contracts[0].start
        reference = quotes_by_event[provider_id][0]
        observe_from = opens.get(start, start - windows[0].lead)
        rows = []
        for contract in contracts:
            side = yes_side(reference, contract.yes_participant, slate.league)
            if side is None:
                # Never defaulted: an unoriented contract is an INVERTED
                # signal, not a smaller one.
                out.exclude("orientation_unresolved", contract.market_ticker)
                continue
            payloads, coverage, cut_short = collect_candles(
                contract.market_ticker, series=slate.series,
                market=slate.markets[contract.market_ticker],
                since=observe_from - CANDLE_LEAD_IN, until=start,
                cutoff=cutoff, fetch=candle_fetch)
            out.candles_cut_short += cut_short
            if not coverage.complete:
                out.candle_warnings.extend(
                    f"{contract.market_ticker}: {reason}"
                    for reason in coverage.reasons)
            seen, twice = set(), 0
            for body in payloads:
                for candle in kalshi_history.parse_candles(body)[0]:
                    twice += candle.ts in seen
                    seen.add(candle.ts)
            if twice:
                out.candle_warnings.append(
                    f"{contract.market_ticker}: {twice} candle(s) returned in "
                    f"two chunks")
            rows.append({
                "market_ticker": contract.market_ticker,
                "yes_is_home": side == "home",
                "settled_yes": settlement_outcome(
                    slate.markets[contract.market_ticker]),
                "candlesticks": payloads,
            })
        if not rows:
            out.exclude("no_orientable_contract", event)
            continue
        out.games.append({
            "provider_event_id": provider_id,
            "event_ticker": event,
            "start": _iso(start),
            # The SOURCE name ("external_schedule"), which is what a bundle
            # declares and `load_bundle` validates -- not the resolver's
            # `kind` ("external"), which an earlier draft wrote and which
            # every bundle would then have been refused on.
            "start_source": slate.resolver.source if slate.resolver else None,
            "historical_schedule_as_of": "unverified",
            "observe_from": _iso(observe_from),
            "contracts": rows,
        })
    joined_events = set(by_event)
    for event in sorted(set(slate.kickoffs) - joined_events):
        out.exclude("no_sharp_event_joined", event)
    return out, ledger


def write_bundle(path: Path, bundle: dict) -> None:
    """Atomically: a half-written bundle would parse as a shorter slate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(bundle), encoding="utf-8")
    temporary.replace(path)


def probe_findings(pool: Sequence[Any], instants: Sequence[datetime],
                   slate: Slate) -> list[dict]:
    """Per probe instant: how many of the day's kickoffs the sharp book quotes.

    A game counts as quoted when a sharp quote's `commence_time` agrees with
    one of the slate's resolved kickoffs within `START_AGREEMENT` -- the
    tolerance `join_markets` applies to the same two clocks, so the probe and
    the run judge coverage alike. Exact equality would have undercounted
    every game whose two sources differ by a minute.
    """
    kickoffs = set(slate.kickoffs.values())
    rows = []
    for at, body in zip(instants, pool):
        result = parse_snapshot(body)
        quoted = {q.provider_event_id for q in result.quotes
                  if any(abs(q.commence_time - k) <= START_AGREEMENT
                         for k in kickoffs)}
        rows.append({"instant": _iso(at),
                     "hours_before_first_kickoff": round(
                         (min(kickoffs) - at).total_seconds() / 3600, 1)
                     if kickoffs else None,
                     "games_quoted": len(quoted),
                     "games_on_slate": len(slate.kickoffs),
                     "parsed": result.coverage.complete})
    return rows


# --- the command ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """The collector's arguments.

    NO ABBREVIATIONS. argparse accepts any unambiguous prefix by default, so
    `--lead-hour 48` quietly means `--lead-hours 48` -- and whether a prefix
    stays unambiguous depends on flags not yet written. A command that
    spends money is spelled exactly or refused.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--day", required=True,
                        help="the game day (YYYY-MM-DD), as the exchange's "
                             "event tickers name it")
    parser.add_argument("--sport", default="NFL")
    parser.add_argument("--series", default="KXNFLGAME")
    parser.add_argument("--probe", action="store_true",
                        help="the coverage probe: T-72h/48h/24h/0 against the "
                             "earliest kickoff, to learn when the sharp book "
                             "starts quoting")
    parser.add_argument("--lead-hours", type=float, default=72.0)
    parser.add_argument("--cadence-minutes", type=float, default=5.0)
    parser.add_argument("--retry-fraction", type=float, default=0.10)
    parser.add_argument("--spend", type=int, default=None, metavar="CREDITS",
                        help="confirm a price. Without it nothing is bought")
    parser.add_argument("--api-key", default=None,
                        help="Odds API key (or set ODDS_API_KEY)")
    parser.add_argument("--out", default=None,
                        help=f"bundle path (default: {OUTPUT_DIR}/"
                             f"reaction_bundle_<day>.json)")
    parser.add_argument("--cache-dir", default=str(OUTPUT_DIR / "cache"),
                        help="raw paid responses; a re-run reads them free")
    parser.add_argument("--schedule-cache",
                        default=str(OUTPUT_DIR / "schedule_cache"))
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: Sequence[str] | None = None,
         now: datetime | None = None) -> int:
    args = parse_args(argv)
    now = now or datetime.now(timezone.utc)
    try:
        day = date.fromisoformat(args.day)
    except ValueError:
        print(f"--day {args.day!r} is not YYYY-MM-DD", file=sys.stderr)
        return EXIT_USAGE
    if not 0.0 <= args.retry_fraction < 1.0:
        print("--retry-fraction must be in [0, 1)", file=sys.stderr)
        return EXIT_USAGE
    if not (args.lead_hours > 0 and args.cadence_minutes > 0):
        print("--lead-hours and --cadence-minutes must be positive",
              file=sys.stderr)
        return EXIT_USAGE
    lead = timedelta(hours=args.lead_hours)
    cadence = timedelta(minutes=args.cadence_minutes)

    print(f"REACTION COLLECTOR  {args.sport} {args.series}  day {day}")
    if DEVELOPMENT_WINDOW[0] <= day <= DEVELOPMENT_WINDOW[1]:
        print(f"  NOTE: {day} is inside the DEVELOPMENT window "
              f"({DEVELOPMENT_WINDOW[0]}..{DEVELOPMENT_WINDOW[1]}). A replay "
              f"of it is exploratory and can never be declared a holdout.")

    with only_declared_endpoints():
        slate = resolve_slate(day, league=args.sport, series=args.series,
                              schedule_cache=args.schedule_cache)
    print(f"  slate: {len(slate.kickoffs)} game(s) with a kickoff, "
          f"{len(slate.markets)} contract(s)")
    for reason, count in sorted(slate.unresolved.items()):
        print(f"    unresolved: {reason} x{count}")
    if not slate.coverage.complete:
        print(f"  *** slate retrieval incomplete: {slate.coverage}")
    if not slate.kickoffs:
        print("  no game on this day has a resolvable kickoff; nothing to "
              "price. (Contracts are listed once SETTLED, so a day whose "
              "games have not finished has none yet.)", file=sys.stderr)
        return EXIT_INCOMPLETE

    try:
        windows = windows_for(slate, lead=lead, cadence=cadence,
                              probe=args.probe)
        manifest = manifest_for(windows, cadence=cadence, probe=args.probe,
                                retry_fraction=args.retry_fraction)
    except ValueError as exc:
        print(f"  cannot build a manifest: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print()
    print(manifest.render())

    if manifest.timestamps[-1] > now:
        print(f"\n  the window runs to {_iso(manifest.timestamps[-1])}, "
              f"after now ({_iso(now)}): the archive cannot hold snapshots "
              f"that have not happened. Nothing was spent.", file=sys.stderr)
        return EXIT_USAGE

    cache = ResponseCache(Path(args.cache_dir) if args.cache_dir else None)
    to_buy = uncached(manifest, cache, args.sport)
    retry = 0.0 if args.probe else args.retry_fraction
    needed = credits_needed(len(to_buy), retry)
    print(f"\n  already cached              "
          f"{manifest.requests - len(to_buy):>7,}  (free)")
    print(f"  to buy                      {len(to_buy):>7,}")
    print(f"  THIS RUN MAY SPEND          {needed:>7,} credits"
          f"{'  (incl. retry reserve)' if retry else ''}")

    # A FAILED READ IS NOT AN EMPTY ONE. A slate whose listing or schedule
    # did not fully answer may be missing games -- an early kickoff cluster
    # included -- so the manifest above may be the wrong one, and a bundle
    # bought against it reads as a smaller slate. Retrieval is free; the
    # remedy is to run again, not to buy on a partial picture.
    if not slate.coverage.complete:
        print("\n  The slate did not fully answer, so this manifest may be "
              "missing games. Nothing was bought; run again.",
              file=sys.stderr)
        return EXIT_INCOMPLETE
    if args.spend is None:
        print(f"\n  Nothing was spent. To collect, re-run with --spend "
              f"{needed}.")
        return EXIT_OK
    if args.spend < needed:
        print(f"\n  --spend {args.spend} is below the {needed} this run may "
              f"need; refusing rather than stopping halfway.",
              file=sys.stderr)
        return EXIT_USAGE
    api_key = args.api_key or os.environ.get("ODDS_API_KEY", "")
    if needed and not api_key:
        print("  need --api-key or ODDS_API_KEY to buy uncached instants",
              file=sys.stderr)
        return EXIT_USAGE

    assert_nothing_here_can_trade()
    ledger = CreditLedger(cap=needed)
    failures: list[str] = []
    try:
        with only_declared_endpoints():
            pool = collect_pool(
                manifest.timestamps, sport=args.sport, api_key=api_key,
                ledger=ledger, cache=cache, failures=failures)
            cutoff = None
            if not args.probe:
                cutoff, cutoff_coverage = kalshi_history.fetch_historical_cutoff()
                if not cutoff_coverage.complete:
                    failures.extend(cutoff_coverage.reasons)
                assembly, join_ledger = assemble(
                    slate, pool, windows, cadence=cadence, cutoff=cutoff)
    except (CreditCapReached, CollectionStopped) as exc:
        print(f"\n  *** {exc}. No bundle written from an unfinished run; "
              f"everything bought is cached, so a re-run pays only for the "
              f"rest.", file=sys.stderr)
        for failure in failures[-STOP_AFTER_FAILED_INSTANTS:]:
            print(f"  *** {failure}", file=sys.stderr)
        print(f"  {ledger}")
        return EXIT_INCOMPLETE
    print(f"\n  {ledger}")
    print(f"  {cache}")
    for failure in failures:
        print(f"  *** {failure}")

    if args.probe:
        findings = probe_findings(pool, manifest.timestamps, slate)
        print("\n  COVERAGE PROBE (when does the sharp book quote this slate?)")
        for row in findings:
            print(f"    T-{row['hours_before_first_kickoff']:>5}h  "
                  f"{row['games_quoted']:>3} of {row['games_on_slate']} "
                  f"game(s) quoted"
                  f"{'' if row['parsed'] else '  (UNREADABLE)'}")
        if args.out:
            write_bundle(Path(args.out), {"probe": findings,
                                          "manifest": manifest.as_dict()})
        return EXIT_OK if not failures else EXIT_INCOMPLETE

    print(f"\n  assembled {len(assembly.games)} game(s) for the bundle")
    for reason, items in sorted(assembly.excluded.items()):
        print(f"    excluded: {reason} x{len(items)}  e.g. {items[0]}")
    for warning in assembly.candle_warnings[:10]:
        print(f"    candles: {warning}")
    if assembly.candles_cut_short:
        print(f"    candles: {assembly.candles_cut_short} response(s) stopped "
              f"short and were completed by follow-up requests")
    if not assembly.games:
        print("  no game joined; there is nothing to write", file=sys.stderr)
        return EXIT_INCOMPLETE
    bundle = {
        "schema": BUNDLE_SCHEMA_POOLED,
        "odds_snapshots": pool,
        "games": assembly.games,
        "collection": {
            "day": day.isoformat(),
            "sport": args.sport, "series": args.series,
            "collected_at": _iso(now),
            "manifest": manifest.as_dict(),
            "credits_reserved_this_run": ledger.spent_this_run,
            "account_remaining": ledger.remaining,
            "snapshot_failures": failures,
            "excluded": assembly.excluded,
            "candle_warnings": assembly.candle_warnings,
            "candle_responses_cut_short": assembly.candles_cut_short,
            "join_ledger": join_ledger.as_dict(),
            "kickoff_provenance": (slate.resolver.manifest()
                                   if slate.resolver else None),
        },
    }
    out = Path(args.out or OUTPUT_DIR / f"reaction_bundle_{day.isoformat()}.json")
    write_bundle(out, bundle)
    print(f"\n  wrote {out}")
    print(f"  next: python3 run_reaction.py --replay {out} "
          f"--max-wait {replay_max_wait_seconds(cadence)}")
    if failures or assembly.candle_warnings:
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
