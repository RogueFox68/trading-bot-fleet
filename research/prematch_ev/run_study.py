#!/usr/bin/env python3
"""Run the pre-match +EV thesis test end to end.

    python3 run_study.py --plan                       # budget only, no calls
    python3 run_study.py --probe --sport MLB          # archive depth, ~5 credits
    python3 run_study.py --sport MLB --series KXMLBGAME \
        --from 2026-05-13 --to 2026-09-15 --lead-minutes 60

STAGE ORDER IS DELIBERATE. `--probe` runs first and costs almost nothing,
because archive depth decides whether the study is possible at all. Budgeting a
season that is not in the archive is the expensive mistake here.

The study answers ONE question: does the de-vigged sharp line predict
settlement better than the exchange price? It does not simulate fills, and it
cannot -- see README "What this does not answer".
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis.scoring import (                                     # noqa: E402
    DEFAULT_MAX_MINUTES_TO_START, Eligibility, EntryPolicy, Observation,
    build_report,
)
from collect import (                                              # noqa: E402
    BASELINE_LEAD_MINUTES, MAX_SOURCE_LAG, Checkpoint, CheckpointMatrix, Ledger,
    STATUS_NOT_YET_LISTED, STATUS_OBSERVED, STATUS_UNJOINED,
    StartResolver, checkpoint_targets, record_cell_outcome,
    decision_cutoffs, default_lead_grid, grid_reach, in_study_window,
    join_markets, kalshi_event_ticker, listing_status, market_start_time,
    observation_with_status, parse_event_body_date,
    parse_lead_grid, snapshots_per_day_for, support_report,
)
from core import fees
from core.matcher import EVENT_BODY_TIMEZONE                                              # noqa: E402
from core.matcher import match_event, unverified_note              # noqa: E402
from core.matcher import supported_leagues                         # noqa: E402
from data.kalshi_history import (                                  # noqa: E402
    Coverage, enumerate_settled_markets, fetch_candlesticks,
    fetch_historical_cutoff, uses_archive,
)
from data.cache import (                                           # noqa: E402
    CreditCapReached, ResponseCache, key_for,
)
from data.odds_history import (                                    # noqa: E402
    CreditLedger, DEFAULT_BOOKMAKERS, MAX_QUOTE_AGE_SECONDS, RETRIES, SPORT_KEYS,
    estimate_credits, fetch_snapshot, probe_earliest_snapshot,
)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sport", default="MLB", choices=sorted(SPORT_KEYS))
    p.add_argument("--series", default="KXMLBGAME",
                   help="Kalshi series ticker for game markets")
    p.add_argument("--from", dest="start", help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD")
    p.add_argument("--lead-grid", default=None,
                   help="comma-separated lead times, e.g. '72h,48h,24h,12h,6h,3h'. "
                        "The 60-minute baseline is always kept and labelled "
                        "separately. Default: the multi-day exploratory grid.")
    p.add_argument("--entry-delay-minutes", type=float, default=0.0,
                   help="reaction delay: a signal seen at t enters at the first "
                        "quote at/after t+delay. 0 is the INSTANTANEOUS bound, "
                        "not a neutral default.")
    p.add_argument("--max-entries-per-game", type=int, default=1,
                   help="entry policy: repeated checkpoints on one game are "
                        "repeated looks at one outcome, not independent bets")
    p.add_argument("--max-lead-minutes", type=float, default=None,
                   help="eligibility ceiling. Derived from the lead grid when "
                        "unset; a ceiling below the grid is refused, not "
                        "silently applied.")
    p.add_argument("--overwrite", action="store_true",
                   help="allow writing into a non-empty --out directory")
    p.add_argument("--lead-minutes", type=float, default=None,
                   help="SINGLE-checkpoint mode, overriding --lead-grid: "
                        "observe both predictors this many minutes before "
                        "start. Retained for the late pre-game baseline.")
    p.add_argument("--devig", choices=("shin", "multiplicative"), default="shin")
    p.add_argument("--max-quote-age", type=float, default=MAX_QUOTE_AGE_SECONDS,
                   help="reject a sharp quote older than this at the cutoff")
    p.add_argument("--min-net-ev", type=float, default=0.01,
                   help="predeclared minimum PREDICTED net EV per contract, at "
                        "the executable price after fee")
    p.add_argument("--max-spread", type=float, default=0.10,
                   help="widest book the policy will treat as executable")
    p.add_argument("--api-key", help="Odds API key (or set ODDS_API_KEY)")
    p.add_argument("--plan", action="store_true",
                   help="offline ESTIMATE only; use --preflight for the real count")
    p.add_argument("--preflight", action="store_true",
                   help="enumerate the real cutoffs and credit cost. FREE")
    p.add_argument("--max-credits", type=int, default=None,
                   help="hard paid-request budget; collection stops and keeps "
                        "partial diagnostics rather than exceeding it")
    p.add_argument("--schedule-cache", default="study_output/schedule_cache",
                   help="directory for RAW schedule payloads (free endpoint, "
                        "no credential). Re-read on a later run so a study can "
                        "be rebuilt, and debugged, without the network")
    p.add_argument("--schedule-dir", default=None,
                   help="build the schedule ONLY from this directory of "
                        "YYYYMMDD.json payloads; never touches the network. "
                        "For replaying a schedule captured elsewhere")
    p.add_argument("--no-schedule-fetch", action="store_true",
                   help="do not fetch a schedule. A league that needs one then "
                        "resolves no kickoff and fails coverage by name")
    p.add_argument("--cache-dir", default="study_output/cache",
                   help="replay cache for paid responses; the credential never "
                        "enters a key, a path or a log. Empty string disables")
    p.add_argument("--support", action="store_true",
                   help="report what this code supports for --sport/--series "
                        "and exit. Reads the code, makes no request, costs "
                        "nothing.")
    p.add_argument("--probe", action="store_true", help="find archive depth and exit")
    p.add_argument("--fee-route", dest="fee_route",
                   choices=sorted(fees.ROUTE_ALIGNMENT), default=fees.DEFAULT_ROUTE,
                   help="which account's fee rounding drives the HEADLINE "
                        "figures. Both routes are reported either way -- this "
                        "only picks which one the narrative sections use. "
                        "The route is not resolved for this study.")
    p.add_argument("--out", default="study_output", help="directory for artifacts")
    return p.parse_args(argv)


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# How much slack the eligibility ceiling carries above the earliest
# checkpoint. A cutoff floored to the 5-minute snapshot grid can sit slightly
# further out than its nominal lead, and a ceiling exactly at the nominal lead
# would drop those rows for being one minute early.
LEAD_CEILING_SLACK = 60.0

# `observations.json` layout. Bumped when a field CHANGES MEANING, not just
# when one is added: schema 1 called the decision book `bid`/`ask` and
# documented it as the entry book.
OBSERVATION_SCHEMA = 3


class SurveyResult(NamedTuple):
    """What the free half of a run establishes, by NAME.

    A plain tuple grew to eight positional members, and a mis-ordered unpack
    is exactly the kind of defect this study keeps finding in itself: it does
    not raise, it just puts a coverage object where a grid should be. Named
    fields also let a caller take the two it needs without spelling out six
    underscores that silently absorb a new one.
    """

    markets: dict
    cutoffs: list
    cutoff: object
    coverage: object
    ledger: object
    grid: tuple
    matrix: object
    resolver: object


def lead_grid_for(args) -> tuple[Checkpoint, ...]:
    """The checkpoints this run observes, earliest lead first.

    `--lead-minutes` still means ONE checkpoint, because the late pre-game
    baseline has to stay runnable exactly as it was -- its results have been
    inspected and must remain comparable. Everything else gets the grid.
    """
    if args.lead_minutes is not None:
        return (Checkpoint(float(args.lead_minutes),
                           baseline=float(args.lead_minutes) == BASELINE_LEAD_MINUTES),)
    if args.lead_grid:
        return parse_lead_grid(args.lead_grid)
    return default_lead_grid()


def eligibility_for(args, grid) -> Eligibility:
    """The frozen screen, with a ceiling that actually covers the grid.

    Raises when the ceiling would exclude a checkpoint being collected. That
    combination is not a stricter study -- it is a study that pays to fetch
    72-hour observations and then drops them for being 72 hours out, and it
    reads in the diagnostics as absent opportunity rather than as a
    misconfiguration.
    """
    reach = max((c.minutes for c in grid), default=0.0)
    ceiling = (float(args.max_lead_minutes) if args.max_lead_minutes is not None
               else max(DEFAULT_MAX_MINUTES_TO_START, reach + LEAD_CEILING_SLACK))
    if ceiling < reach:
        raise ValueError(
            f"eligibility ceiling {ceiling:g} min is below the earliest "
            f"checkpoint in the grid ({reach:g} min). Every observation at that "
            "checkpoint would be fetched and then discarded as 'too far from "
            "start'. Raise --max-lead-minutes or shorten --lead-grid."
        )
    return Eligibility(min_net_ev=args.min_net_ev, max_spread=args.max_spread,
                       max_minutes_to_start=ceiling)


def entry_policy_for(args) -> EntryPolicy:
    return EntryPolicy(max_entries_per_game=args.max_entries_per_game)


def grid_describe(grid) -> str:
    return ", ".join(c.label for c in grid)


def fee_provenance(args) -> list[str]:
    """The fee stamps for THIS run, resolved at the window it actually covers.

    `describe()` takes a timestamp because the per-series multiplier is dated.
    Passing the window START (not "now") is the point: a run of this study in
    six months must print the rate that was in force in the window it studied,
    not the rate current when it ran.
    """
    at = _date(args.start) if args.start else None
    return fees.describe_lines(args.series, at, args.fee_route)


def fee_schedule_warnings(args) -> list[str]:
    """A window that straddles a fee change is priced at two rates.

    That is correct -- each decision gets the rate in force on its own day --
    but it is invisible in an averaged result, so it is said out loud.
    """
    if not (args.start and args.end):
        return []
    changes = fees.schedule_changes_within(
        args.series, _date(args.start), _date(args.end) + timedelta(days=1))
    return [f"fee schedule CHANGES INSIDE this window: {e.label()} -- "
            "decisions before and after it are priced at different rates"
            for e in changes]


def plan(args) -> int:
    if not (args.start and args.end):
        print("--plan needs --from and --to")
        return 2
    days = (_date(args.end) - _date(args.start)).days + 1
    # The exchange enumeration is FREE and its tickers carry the scheduled
    # start, so the schedule is known before any paid call and the cutoffs come
    # straight from the games being studied. No discovery pass is needed.
    assumed_cutoffs = 6          # games cluster; refined at run time
    targeted = estimate_credits(days, assumed_cutoffs)
    grid_equivalent = estimate_credits(
        days, snapshots_per_day_for(args.max_quote_age))

    print("STUDY PLAN")
    print(f"  sport            {args.sport}  (series {args.series})")
    print(f"  window           {args.start} .. {args.end}  ({days} days)")
    print(f"  lead time        {args.lead_minutes} min before scheduled start")
    print(f"  freshness bound  {args.max_quote_age:.0f}s at the decision timestamp")
    print(f"  de-vig           {args.devig}")
    print()
    print(f"  exchange side    FREE -- start times come from the event tickers")
    print(f"  targeted pass    ~{assumed_cutoffs}/day at decision cutoffs  "
          f"~{targeted:,} credits")
    print(f"  ESTIMATED TOTAL  ~{targeted:,} credits")
    print()
    print(f"  for comparison, a fixed grid fine enough to satisfy the freshness")
    print(f"  bound needs {snapshots_per_day_for(args.max_quote_age)}/day = "
          f"~{grid_equivalent:,} credits, which is why fetches are targeted.")
    print()
    print(f"  fee models       {'; '.join(fee_provenance(args))}")
    print(f"  fee rounding     {fees.route_warning()}")
    for warning in fee_schedule_warnings(args):
        print(f"  !! {warning}")
    print()
    print(f"  NOTE  {unverified_note()}")
    return 0


def probe(args) -> int:
    key = args.api_key
    if not key:
        print("--probe needs --api-key or ODDS_API_KEY")
        return 2
    now = datetime.now(timezone.utc)
    candidates = [now - timedelta(days=d) for d in (1000, 700, 500, 365, 240, 180, 130, 90, 30)]
    found, note = probe_earliest_snapshot(args.sport, key, candidates)
    print(f"archive probe for {args.sport}: "
          f"{'earliest hit ' + found.date().isoformat() if found else 'NO DATA'} -- {note}")
    if found:
        depth = (now - found).days
        print(f"  usable depth ~{depth} days")
        if depth < 120:
            print("  WARNING: under ~120 days is thin for a seasonal sport. Check that")
            print("  the window covers the season you intend to trade before budgeting.")
    return 0 if found else 1


def support(args) -> int:
    """What this code supports for one sport. No network, no cost."""
    report = support_report(args.sport, args.series)
    print(f"SUPPORT: {report.league}  (series {report.series})")
    print(f"  odds-provider key    {report.odds_key or 'NONE'}")
    print(f"  roster teams         {report.roster_teams}")
    print(f"  exchange aliases     {report.alias_count}")
    print(f"  dated fee schedule   {'yes' if report.fee_schedule else 'NO (generic rates)'}")
    print(f"  start source         {report.start_source or 'NONE'}"
          f"{f' ({report.start_source_kind})' if report.start_source_kind else ''}")
    print(f"  schedule provenance  {report.schedule_provenance or 'n/a'}")
    print()
    print(f"  roster-ready         {'yes' if report.roster_ready else 'NO'}"
          "   (team identity resolves)")
    print(f"  schedule-ready       {'yes' if report.schedule_ready else 'NO'}"
          "   (an ADAPTER can derive a kickoff)")
    print(f"  COLLECTABLE          {'yes' if report.ready else 'NO'}"
          "   (needs BOTH)")
    print(f"  point-in-time        {'yes' if report.point_in_time_capable else 'NO'}"
          "   (the kickoff is what was known at the decision)")
    for blocker in report.blockers():
        print(f"  !! BLOCKER  {blocker}")
    for caveat in report.caveats():
        print(f"  ?  CAVEAT   {caveat}")
    print()
    print("  Roster-ready and schedule-ready are DIFFERENT questions, and NFL")
    print("  is the reason they are reported apart: 32 teams and a valid odds")
    print("  key, yet its event body carries a date and no kickoff, so it took")
    print("  an external schedule to make a single observation possible.")
    print()
    print("  COLLECTABLE is adapter support. It is NOT runtime coverage -- a")
    print("  run's own resolved/unresolved counts say whether the schedule")
    print("  actually covered its contracts, and an empty universe still fails.")
    print()
    print("  POINT-IN-TIME is a third question again. An external schedule is")
    print("  retrieved now and cannot say what a kickoff was believed to be")
    print("  before a flex or a reschedule, so runs over it are EXPLORATORY.")
    print()
    print("  And none of the three means data EXISTS at any given lead time.")
    print("  Listing lead times and historical sharp coverage are separate")
    print("  questions again, which only a live audit answers.")
    print()
    print(f"  leagues with a roster: {', '.join(supported_leagues())}")
    return 0 if report.ready else 1


def survey(args, schedule=None):
    """The FREE half: enumerate the exchange, filter, derive the cutoffs.

    Shared by `--preflight` and the real run so the two cannot disagree about
    how many paid requests a window needs. A preflight that estimated its own
    way would eventually differ from the run it is meant to predict.

    `schedule`, when supplied, replaces the retrieval -- the injection point
    for a captured snapshot and for tests. THE SCHEDULE FETCH IS FREE: it is a
    public unauthenticated endpoint and costs no credits, so it happens in the
    free half by right, not by exception.
    """
    coverage = Coverage()
    ledger = Ledger()

    # SUPPORT BEFORE SPEND. A sport with no roster is accepted by argparse,
    # survives preflight, and fails at JOIN time -- after every snapshot has
    # been paid for. That is the `regions=us` defect again: a run that costs
    # credits and returns nothing usable. Refuse it here, where nothing has
    # been spent yet.
    report = support_report(args.sport, args.series)
    for blocker in report.blockers():
        coverage.fail(blocker)
    if not report.ready:
        return SurveyResult({}, [], None, coverage, ledger, lead_grid_for(args),
                            CheckpointMatrix(),
                            StartResolver(league=args.sport.upper()))

    start = _date(args.start)
    end_inclusive = _date(args.end)
    end_exclusive = end_inclusive + timedelta(days=1)

    # --- exchange side, free ------------------------------------------------
    cutoff, cutoff_cov = fetch_historical_cutoff()
    coverage.merge(cutoff_cov)
    all_markets, market_cov = enumerate_settled_markets(args.series)
    coverage.merge(market_cov)
    ledger.count("contracts", len(all_markets), unit="contracts")

    # Retrieval filter: settlement +-1 day, deliberately padded.
    retrieved: dict[str, dict] = {}
    outside = undatable = 0
    for ticker, market in all_markets.items():
        verdict = in_study_window(market, start, end_exclusive)
        if verdict is True:
            retrieved[ticker] = market
        elif verdict is False:
            outside += 1
        else:
            undatable += 1
    if outside:
        ledger.exclude("settled_outside_retrieval_window", count=outside,
                       stage="contracts")
    if undatable:
        ledger.reject("settlement_time_unreadable", f"{undatable} markets",
                      count=undatable, stage="contracts")

    # --- kickoffs, resolved ONCE, BEFORE any eligibility question ---------
    # Game-window eligibility, listing eligibility and every lead-grid cutoff
    # are all measured from the kickoff, so the kickoff has to be settled
    # first and settled once. `resolver` is the single mapping the rest of the
    # run reads -- targeting, collection and the join alike -- because a study
    # whose stages date the same game differently has no lead time at all.
    resolver = StartResolver(
        league=args.sport.upper(),
        schedule=schedule if schedule is not None else _build_schedule(
            args, report, start, end_inclusive, coverage, ledger),
    )
    resolver.prime(retrieved)
    # In CONTRACTS, because that is the stage's unit. See failure_counts().
    for reason, count in resolver.failure_counts(per="contracts").items():
        ledger.reject(reason, f"{count} contracts", count=count,
                      stage="contracts")

    # ELIGIBILITY filter: the declared GAME window, on the resolved start.
    # Padding is right for retrieval and wrong for eligibility -- the padded
    # set previously included Sept 13 and Sept 16 games in a Sept 14-15 run.
    markets: dict[str, dict] = {}
    off_window = no_start = 0
    off_window_by_timezone = 0
    for ticker, market in retrieved.items():
        game_start = market_start_time(market, resolver)
        if game_start is None:
            no_start += 1
            continue
        if start <= game_start < end_exclusive:
            markets[ticker] = market
        else:
            off_window += 1
            # THE CROSS-MIDNIGHT EDGE, counted rather than left to be noticed.
            # --from/--to are UTC bounds, but a US evening kickoff lands on the
            # NEXT UTC day: the `26SEP14` slate starts at 00:15Z on the 15th.
            # So `--to 2026-09-14` silently drops the Monday night game, and
            # `--from` silently admits the previous evening's. The window
            # semantics are deliberately NOT changed here -- they define the
            # existing MLB baseline's universe, and redefining them would move
            # that result without saying so -- but an operator sizing a window
            # should be told, not left to infer it from a thinner slate.
            local_day = parse_event_body_date(kalshi_event_ticker(market) or ticker)
            if local_day and start.date() <= local_day <= end_inclusive.date():
                off_window_by_timezone += 1
    if off_window:
        ledger.exclude("game_outside_declared_window", count=off_window,
                       stage="contracts")
    if off_window_by_timezone:
        ledger.exclude("game_outside_window_utc_boundary",
                       count=off_window_by_timezone, stage="contracts")
        print(f"          {off_window_by_timezone:,} contract(s) name a day "
              "INSIDE the window but kick off outside it in UTC")
        print("          (a US evening game starts on the next UTC day; "
              "--from/--to are UTC bounds)")
        print(f"          extend --to past {end_inclusive.date().isoformat()} "
              "to include that evening's slate")
    if no_start and not resolver.needs_schedule:
        # An externally scheduled league already filed its reasons BY NAME
        # above (no match, ambiguous, TBD, provider failure). Counting them a
        # second time here as a generic `no_readable_start_time` would double
        # the rejection and replace a diagnosis with a symptom.
        ledger.reject("no_readable_start_time", f"{no_start} markets",
                      count=no_start, stage="contracts")

    unroutable = sum(1 for m in markets.values() if uses_archive(m, cutoff) is None)
    if unroutable:
        ledger.reject("candle_partition_unroutable", f"{unroutable} markets",
                      count=unroutable, stage="contracts")
    print(f"  kalshi: {len(all_markets):,} settled -> {len(retrieved):,} retrieved "
          f"-> {len(markets):,} in the declared game window")

    # --- cutoffs derived from THOSE games -----------------------------------
    # Cutoffs are NOT clipped to the study window. They are derived from games
    # already filtered for eligibility, so by construction each one is needed.
    # Clipping them dropped the source snapshot for any game near the lower
    # boundary: a 00:30 UTC game at a 60-minute lead needs the PREVIOUS day's
    # 23:30 snapshot. Widening --from instead would change the study universe,
    # which is a different thing from fetching the inputs that universe needs.
    grid = lead_grid_for(args)
    starts = [market_start_time(m, resolver) for m in markets.values()]
    cutoffs = decision_cutoffs([s for s in starts if s], grid)

    # RETRIEVAL reaches back further than the STUDY WINDOW, deliberately. A
    # 72-hour checkpoint on a Sept 1 game needs an Aug 29 snapshot. Fetching
    # that input is not the same as widening the universe: Aug 29's own games
    # were already excluded above, on their start times, and nothing here adds
    # them back.
    outside_window = sum(1 for c in cutoffs if not (start <= c < end_exclusive))
    reach = grid_reach(grid)
    if outside_window:
        print(f"          {outside_window:,} cutoff(s) precede the game window "
              f"by up to {reach.total_seconds() / 3600:.0f}h and are fetched "
              "anyway -- eligible games need them")
        print(f"          those snapshots are INPUTS ONLY; games starting "
              "before the window stay out of the universe")
    print(f"          {len(cutoffs):,} distinct decision cutoffs from "
          f"{len(markets):,} contracts x {len(grid)} checkpoints "
          f"({grid_describe(grid)})")

    targets = checkpoint_targets(markets, grid, ledger, resolver)
    matrix = CheckpointMatrix()
    matrix.expect(targets)
    ledger.count("checkpoint_cells", len(targets), unit="game-checkpoints")

    if not cutoffs and markets:
        coverage.fail(
            f"{len(markets):,} contracts are in the window but no decision "
            f"cutoff was derived at leads {grid_describe(grid)}; "
            "widen --from or shorten --lead-grid"
        )

    # AN EMPTY UNIVERSE IS NOT A FREE STUDY. The ledger already knew that 32
    # of 32 contracts were lost to `no_readable_start_time` -- it printed
    # "contracts lost 32 (100%)" -- while the preflight beside it reported
    # coverage complete, cost 0, exit 0. The loss was measured and then not
    # consulted (rule 21: a log line is not a control). `collect()` applied
    # this; the FREE path never did, so the cheapest way to run the study was
    # also the only way to have it certify itself.
    ledger.apply_to(coverage)
    # COUNT THE POPULATION THE SENTENCE NAMES. `total_rejected` spans every
    # stage, diagnostic ones included, so with the exchange unreachable and
    # ZERO contracts ever enumerated this read "17 were rejected" -- the
    # number of failed schedule buckets. It also fired where no contract had
    # been rejected at all, adding a second, wrong explanation beside the
    # real one (the enumeration failure, already in coverage).
    lost_contracts = ledger.rejected_in_stage("contracts")
    if not markets and lost_contracts:
        coverage.fail(
            f"no contract survived to be studied: {lost_contracts:,} "
            "were rejected. A zero-cost run over an empty universe is a "
            "failure that happens to be cheap, not a success."
        )

    return SurveyResult(markets, cutoffs, cutoff, coverage, ledger, grid,
                        matrix, resolver)


def _build_schedule(args, report, start, end, coverage, ledger):
    """Retrieve the external schedule a league needs, or say why there is none.

    FREE: a public unauthenticated endpoint, no credential, no credits. It is
    deliberately allowed to run inside the free half for that reason -- and it
    is the only network call in `survey`, so an operator can see exactly what
    a preflight touches.

    A league whose kickoffs come from its own tickers needs nothing here and
    gets None. A league that needs a schedule and cannot have one does NOT get
    a silent empty snapshot: it gets None too, and every one of its contracts
    then fails resolution by name, which fails coverage. An unreachable
    provider and an empty slate must not look alike (the fleet's rule 17).
    """
    from data import espn_schedule

    if not report.schedule_is_external:
        return None
    if args.schedule_dir:
        return espn_schedule.snapshot_from_directory(args.sport, args.schedule_dir)
    if args.no_schedule_fetch:
        # The SCHEDULE stage, not the contracts stage. This is a fact about
        # the run's configuration, and filing it against contracts made an
        # empty run report one rejected contract it had never seen.
        ledger.reject("schedule_fetch_disabled",
                      "--no-schedule-fetch was set and this league needs a "
                      "schedule", count=1, stage="schedule_entries",
                      diagnostic=True)
        ledger.count("schedule_entries", 1, unit="schedule-entries",
                     diagnostic=True)
        return None
    try:
        snapshot = espn_schedule.fetch_schedule(
            args.sport, start.date(), end.date(),
            cache_dir=args.schedule_cache or None)
    except Exception as exc:                      # transport-shaped, by contract
        coverage.fail(f"schedule retrieval failed for {args.sport}: {exc}. "
                      "A league whose kickoffs come from an external schedule "
                      "cannot be studied without one, and an unreachable "
                      "provider is not an empty slate.")
        return None
    print(f"  schedule: {len(snapshot.events):,} games over "
          f"{espn_schedule.describe_buckets(snapshot.buckets)} "
          f"({espn_schedule.SCHEDULE_SOURCE}, free)")
    # COUNT THE DENOMINATOR FIRST. A stage that only ever receives rejections
    # reports 100% loss out of zero considered -- the zero-denominator shape a
    # rename produced in round 6. Schedule entries are a DIAGNOSTIC stage: a
    # provider listing games this study never asked about is not a loss, and
    # only the contracts that fail to resolve are.
    ledger.count("schedule_entries", len(snapshot.events) + len(snapshot.failures),
                 unit="schedule-entries", diagnostic=True)
    for reason, count in snapshot.failure_counts().items():
        ledger.reject(reason, f"{count} schedule entries", count=count,
                      stage="schedule_entries", diagnostic=True)
    return snapshot


def _print_schedule_block(resolver) -> None:
    """Where this run's kickoffs came from, and how far they cover it.

    THREE facts, printed apart because they answer different questions and
    reading one as another is how NFL came to look ready while producing no
    observation:

      adapter        -- can a kickoff be derived at all? (static, from code)
      coverage       -- did one actually resolve, for THIS run's contracts?
      provenance     -- is it what was known at the decision it dates?
    """
    if resolver is None or resolver.source is None:
        return
    resolved, unresolved = resolver.coverage_counts()
    print(f"  start source         {resolver.source} ({resolver.kind})")
    if not resolver.needs_schedule:
        print("  schedule coverage    n/a -- the kickoff is on the contract")
        print("  historical as-of     verified_at_decision_time")
        return
    total = resolved + unresolved
    pct = (100.0 * resolved / total) if total else 0.0
    print(f"  schedule coverage    {resolved:,}/{total:,} events resolved "
          f"({pct:.1f}%)")
    if resolver.schedule is not None:
        snapshot = resolver.schedule
        print(f"  schedule games       {len(snapshot.events):,} over "
              f"{len(snapshot.buckets)} daily bucket(s), free endpoint")
        if snapshot.offsets_used:
            offsets = ", ".join(f"{k:+d}d x{v}" for k, v
                                in sorted(snapshot.offsets_used.items()))
            print(f"  ticker-day offsets   {offsets}")
            if any(k != 0 for k in snapshot.offsets_used):
                print("                       a non-zero population means the "
                      "declared ticker timezone")
                print("                       disagrees with the schedule -- "
                      "check it before budgeting")
        if snapshot.resolved_by_name:
            print(f"  resolved by name     {snapshot.resolved_by_name:,} "
                  "(provider code is not canonical; enumerate into "
                  "ESPN_CODE_ALIASES)")
    for reason, count in resolver.failure_counts().items():
        print(f"  !! unresolved        {reason}: {count:,}")
    print("  historical as-of     UNVERIFIED -- the schedule was retrieved now,")
    print("                       not as it stood before each decision. This run")
    print("                       is EXPLORATORY: it can size availability and")
    print("                       cost; it cannot certify a point-in-time")
    print("                       backtest or a live strategy.")


def preflight(args) -> int:
    """Enumerate the real work before any paid call. Costs nothing."""
    result = survey(args)
    markets, cutoffs, coverage, ledger, grid, matrix, resolver = (
        result.markets, result.cutoffs, result.coverage, result.ledger,
        result.grid, result.matrix, result.resolver)
    per_call = estimate_credits(1, 1)

    # WHAT IS ACTUALLY MISSING, not what a run would request. The cache already
    # holds the snapshots an earlier run paid for, and a cost that ignored them
    # would quote a price nobody is going to be charged.
    cache = ResponseCache(Path(args.cache_dir) if args.cache_dir else None)
    missing = [at for at in cutoffs
               if not cache.has(key_for(args.sport, at, DEFAULT_BOOKMAKERS, "h2h"))]
    cached = len(cutoffs) - len(missing)

    print()
    print("PREFLIGHT (no paid requests made)")
    _print_schedule_block(resolver)
    print(f"  eligible contracts   {len(markets):,}")
    print(f"  lead grid            {grid_describe(grid)}  ({len(grid)} checkpoints)")
    print(f"  checkpoint cells     {len(matrix.statuses):,}  "
          "(contracts x checkpoints -- the coverage denominator)")
    print(f"  distinct cutoffs     {len(cutoffs):,}")
    print(f"  already cached       {cached:,}  (free)")
    print(f"  unique missing       {len(missing):,}  <-- what this run would buy")
    base = len(missing) * per_call
    gross = len(cutoffs) * per_call
    print(f"  base credit cost     {base:,} ({per_call} per call; "
          f"{gross:,} without the cache)")
    print(f"  worst case w/ retries {base * RETRIES:,} ({RETRIES} attempts each)")
    print("                        a provider can charge a request whose response")
    print("                        is lost, so retries are budgeted, not free")
    if args.max_credits is not None:
        over = base > args.max_credits
        print(f"  configured cap       {args.max_credits:,}"
              f"{'  <-- WOULD BE EXCEEDED' if over else ''}")
    else:
        print("  configured cap       none  <-- set --max-credits before a paid run")
    print(f"  coverage             {coverage}")
    print()
    try:
        eligibility_for(args, grid)
    except ValueError as exc:
        print(f"  !! CONFIGURATION: {exc}")
        print()
        return 2
    print(matrix.render())
    print("    (every cell reads 'unreported' before a run -- this is the")
    print("     expected universe, established without fetching anything)")
    print()
    print(ledger.render())
    if not coverage.complete:
        print()
        print("  Coverage is incomplete BEFORE any paid call. Fix that first.")
        return 1
    return 0


def collect(args, key: str):
    """Pull both sides, join on identity, and build observations at one cutoff."""
    result = survey(args)
    markets, cutoffs, cutoff, coverage, ledger, grid, matrix, resolver = result
    credits = CreditLedger(cap=args.max_credits)
    cache = ResponseCache(Path(args.cache_dir) if args.cache_dir else None)

    # --- sharp side: one fetch per cutoff -----------------------------------
    quotes_by_event: dict[str, list] = {}
    snapshots = 0
    for at in cutoffs:
        if credits.exhausted():
            coverage.fail(f"odds quota exhausted at cutoff {at.isoformat()}; "
                          "window truncated")
            break
        try:
            snap = fetch_snapshot(args.sport, at, key, ledger=credits, cache=cache)
        except CreditCapReached as exc:
            # Stop and keep what we have: the point of a cap is that partial
            # diagnostics survive rather than the overspend being discovered
            # afterwards.
            coverage.fail(str(exc))
            break
        coverage.merge(snap.coverage)
        snapshots += 1
        # PROVIDER diagnostics, not study coverage. A snapshot legitimately
        # carries events the study never asked about: other days, other games,
        # times nowhere near a decision cutoff. Counting those against coverage
        # let 88 irrelevant event-quotes -- 78 of them for a day outside the
        # declared window -- fail a run whose 40 target contracts all resolved.
        ledger.count("provider_events", snap.events_seen, unit="event-quotes",
                     diagnostic=True)
        for reason, n in (("event_without_sharp_book", snap.events_without_sharp_book),
                          ("odds_event_without_id", snap.events_without_id),
                          ("sharp_quote_without_update_time",
                           snap.quotes_without_update_time)):
            if n:
                ledger.reject(reason, f"at {at.isoformat()}", count=n,
                              stage="provider_events", diagnostic=True)
        for quote in snap.quotes:
            quotes_by_event.setdefault(quote.provider_event_id, []).append(quote)
    ledger.count("odds_snapshots", snapshots, unit="snapshots")
    print(f"  odds:   {snapshots:,} snapshots -> {len(quotes_by_event):,} "
          f"sharp events ({credits}; {cache})")

    # --- join ----------------------------------------------------------------
    joined = join_markets(markets, quotes_by_event, args.sport, ledger, resolver)
    print(f"  joined: {len(joined):,} contracts matched to a sharp event")

    # AN UNJOINED CONTRACT IS NOT AN UNREPORTED CELL. The loop below only
    # visits joined markets, so every cell of a contract that failed to match
    # a sharp event stayed blank -- and "unreported" means "the collector
    # skipped this", i.e. a bug. These are an EXPLAINED loss: the contract was
    # enumerated, the join could not match it, and nobody is coming back for
    # it. The denominator is untouched either way; only the label changes,
    # from a bug to a reason.
    matched = {jm.market_ticker for jm in joined}
    unjoined = [t for t in markets if t not in matched]
    for ticker in unjoined:
        matrix.record_all(ticker, STATUS_UNJOINED)
        record_cell_outcome(ledger, STATUS_UNJOINED, ticker,
                            count=len(grid))
    if unjoined:
        print(f"          {len(unjoined):,} enumerated contract(s) never matched "
              f"a sharp event ({len(unjoined) * len(grid):,} cells)")

    # --- every (contract, checkpoint) cell -----------------------------------
    # THE STUDY'S OWN DENOMINATOR is the enumerated cell count, not the rows
    # that succeeded: a denominator defined by its successes always reads 100%.
    # Each cell resolves to exactly one status, and the three benign ones are
    # kept apart from the one that threatens the sample.
    observations: list[Observation] = []
    entry_delay = timedelta(minutes=args.entry_delay_minutes)
    ledger.count("target_contracts", len(joined), unit="contracts")
    for jm in joined:
        market = markets[jm.market_ticker]
        # ONE candle fetch per contract, spanning the WHOLE grid. Fetching per
        # checkpoint would re-request the same series seven times; the archive
        # is free but not instant, and the window is contiguous anyway.
        earliest = min(c.decision_at(jm.start) for c in grid)
        candles, cand_cov = fetch_candlesticks(
            jm.market_ticker, args.series,
            earliest - MAX_SOURCE_LAG, jm.start,
            use_archive=uses_archive(market, cutoff),
        )
        coverage.merge(cand_cov)

        for checkpoint in grid:
            decision_at = checkpoint.decision_at(jm.start)

            # DID THE CONTRACT EXIST? A 72-hour checkpoint often predates the
            # listing. That is a feasibility RESULT -- there was no
            # opportunity to miss -- and it is counted as an eligibility
            # exclusion, never as a coverage failure.
            listing = listing_status(market, decision_at)
            if listing is not None:
                matrix.record(jm.market_ticker, checkpoint, listing)
                record_cell_outcome(ledger, listing, jm.market_ticker)
                continue

            if not candles:
                matrix.record(jm.market_ticker, checkpoint, "candles_unavailable")
                record_cell_outcome(ledger, "candles_unavailable",
                                    jm.market_ticker)
                continue

            obs, status = observation_with_status(
                jm, quotes_by_event.get(jm.provider_event_id, []), candles,
                decision_at, args.sport, ledger, method=args.devig,
                max_quote_age=args.max_quote_age,
                reject_stage="checkpoint_cells",
                checkpoint=checkpoint, entry_delay=entry_delay,
            )
            matrix.record(jm.market_ticker, checkpoint, status)
            if obs is not None:
                observations.append(obs)

    ledger.count("observations", len(observations), unit="contracts")
    ledger.apply_to(coverage)
    print(f"  built:  {len(observations):,} observations across "
          f"{len(matrix.statuses):,} cells "
          f"({matrix.source_failures():,} source failures, "
          f"{matrix.unreported():,} unreported)")
    return observations, coverage, credits, ledger, grid, matrix, resolver


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.support:
        return support(args)
    if args.plan:
        return plan(args)
    if args.preflight:
        if not (args.start and args.end):
            print("--preflight needs --from and --to", file=sys.stderr)
            return 2
        return preflight(args)

    import os
    key = args.api_key or os.environ.get("ODDS_API_KEY", "")
    if args.probe:
        args.api_key = key
        return probe(args)
    if not key:
        print("need --api-key or ODDS_API_KEY", file=sys.stderr)
        return 2
    if not (args.start and args.end):
        print("need --from and --to", file=sys.stderr)
        return 2

    print(f"collecting {args.sport} {args.start}..{args.end}")
    out = Path(args.out)
    # A NEW DESIGN MUST NOT OVERWRITE THE OLD RESULTS. The baseline run's
    # artifacts are the only record of what the late pre-game window looked
    # like, and a multi-day run writing over them would destroy the comparison
    # it exists to make.
    if out.exists() and any(out.iterdir()) and not args.overwrite:
        print(f"{out} already holds artifacts. Use a separate --out directory "
              "for this run, or pass --overwrite to replace them.", file=sys.stderr)
        return 2

    try:
        eligibility = eligibility_for(args, lead_grid_for(args))
    except ValueError as exc:
        print(f"CONFIGURATION: {exc}", file=sys.stderr)
        return 2

    observations, coverage, credits, ledger, grid, matrix, resolver = collect(args, key)
    out.mkdir(parents=True, exist_ok=True)

    def write_coverage():
        """Always written, ESPECIALLY on zero observations.

        A live run that built nothing returned before creating the output
        directory, so the structured diagnostics were lost exactly where they
        were most needed. The API key never enters this file.
        """
        (out / "coverage.json").write_text(json.dumps({
            "complete": coverage.complete,
            "reasons": coverage.reasons,
            "collection": ledger.as_dict(),
            "credits": str(credits),
            "observations_built": len(observations),
            # WHERE EVERY KICKOFF CAME FROM. Source url, provider event id,
            # retrieval time, payload hash and the resolved kickoff, per game,
            # plus the kalshi-event -> provider-event mapping that links them
            # to the observations. A lead-time study whose start times cannot
            # be re-derived later is not auditable, and the `historical
            # schedule as of` label rides here so no reader of this file can
            # mistake a retrospective schedule for a point-in-time one.
            "schedule": resolver.manifest() if resolver is not None else None,
            "run": {
                "sport": args.sport, "series": args.series,
                "from": args.start, "to": args.end,
                "lead_grid": [c.label for c in grid],
                "lead_grid_minutes": [c.minutes for c in grid],
                "baseline_lead_minutes": BASELINE_LEAD_MINUTES,
                "entry_delay_minutes": args.entry_delay_minutes,
                "max_entries_per_game": args.max_entries_per_game,
                "max_minutes_to_start": eligibility.max_minutes_to_start,
                "design_status": "EXPLORATORY -- multi-day grid chosen after "
                                 "inspecting the late pre-game baseline; not a "
                                 "preregistered test",
                "checkpoint_coverage": matrix.by_checkpoint(),
                "checkpoint_detail": matrix.detail_by_checkpoint(),
                "devig": args.devig,
                "max_quote_age_seconds": args.max_quote_age,
                "min_net_ev": args.min_net_ev,
                "max_spread": args.max_spread,
                "max_credits": args.max_credits,
                "cache_dir": args.cache_dir or None,
                "fees": "; ".join(fee_provenance(args)),
                "fee_route_headline": args.fee_route,
                "fee_route_resolved": fees.ROUTE_RESOLVED,
                "fee_schedule_warnings": fee_schedule_warnings(args),
                "event_body_timezone": EVENT_BODY_TIMEZONE,
            },
        }, indent=2), encoding="utf-8")

    if not observations:
        write_coverage()
        worst = sorted(ledger.rejections.items(), key=lambda kv: -kv[1])[:5]
        print("\nNO OBSERVATIONS BUILT. Diagnose by the failing STAGE, not by "
              "assuming absent data:", file=sys.stderr)
        for reason, n in worst:
            print(f"    {n:>7,}  {reason} "
                  f"[{ledger.rejection_stage.get(reason, '?')}]", file=sys.stderr)
        for name, s in ledger.unaccounted_stages():
            print(f"    !! stage {name!r} rejected {s.rejected:,} against a zero "
                  "denominator -- its loss is unmeasured", file=sys.stderr)
        print(f"\n  wrote {out}/coverage.json with the full ledger",
              file=sys.stderr)
        print(ledger.render(), file=sys.stderr)
        return 1

    for warning in fee_schedule_warnings(args):
        print(f"!! {warning}")
    report = build_report(
        observations, coverage, fee_provenance(args),
        eligibility=eligibility,
        series=args.series,
        ledger_text=ledger.render(),
        route=args.fee_route,
        policy=entry_policy_for(args),
        baseline_minutes=BASELINE_LEAD_MINUTES,
        checkpoint_coverage=matrix.render(),
    )
    print()
    print(report.render())

    write_coverage()
    (out / "report.txt").write_text(report.render(), encoding="utf-8")
    # SCHEMA 2 renamed bid/ask and added the execution book. Version it
    # rather than reusing the old names: schema 1's `bid`/`ask` were described
    # as the entry book, and silently changing what a field MEANS is worse
    # than changing what it is called -- an old reader would keep working and
    # keep being wrong. A delayed run that saved only one book could not be
    # reproduced from its own artifacts at all.
    (out / "observations.json").write_text(
        json.dumps({
            "schema": OBSERVATION_SCHEMA,
            "semantics": (
                "decision_* is the book SEEN AT decision_at and is what the "
                "screen and the side selection used; entry_* is the book AT "
                "entry_at and is what execution paid. They are the same candle "
                "when entry_delay_minutes is 0."
            ),
            "observations": [{
                "game_id": o.game_id, "market_id": o.market_id,
                "decision_at": o.decision_at.isoformat(),
                "sharp_at": o.sharp_at.isoformat() if o.sharp_at else None,
                "sharp_snapshot_at": (o.sharp_snapshot_at.isoformat()
                                      if o.sharp_snapshot_at else None),
                "exchange_at": o.exchange_at.isoformat() if o.exchange_at else None,
                "minutes_to_start": o.minutes_to_start,
                "yes_participant": o.yes_participant,
                "p_sharp": o.p_sharp, "p_exchange": o.p_exchange,
                "outcome": o.outcome,
                "decision_bid": o.exchange_bid, "decision_ask": o.exchange_ask,
                "entry_bid": o.entry_bid, "entry_ask": o.entry_ask,
                "entry_at": o.entry_at.isoformat() if o.entry_at else None,
                "entry_delay_minutes": o.entry_delay_minutes,
                "checkpoint_minutes": o.checkpoint_minutes,
                "checkpoint": o.checkpoint_label,
                "devig_method": o.devig_method,
            } for o in observations],
        }, indent=2), encoding="utf-8")
    print(f"\nwrote {out}/report.txt, observations.json and coverage.json ({credits})")
    return 0 if coverage.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
