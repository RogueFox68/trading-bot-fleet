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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analysis.scoring import (                                     # noqa: E402
    DEFAULT_MAX_MINUTES_TO_START, Eligibility, EntryPolicy, Observation,
    build_report,
)
from collect import (                                              # noqa: E402
    BASELINE_LEAD_MINUTES, MAX_SOURCE_LAG, Checkpoint, CheckpointMatrix, Ledger,
    STATUS_NOT_YET_LISTED, STATUS_OBSERVED, checkpoint_targets,
    decision_cutoffs, default_lead_grid, grid_reach, in_study_window,
    join_markets, listing_status, market_start_time, observation_with_status,
    parse_lead_grid, snapshots_per_day_for,
)
from core import fees
from core.matcher import EVENT_BODY_TIMEZONE                                              # noqa: E402
from core.matcher import match_event, unverified_note              # noqa: E402
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
    p.add_argument("--cache-dir", default="study_output/cache",
                   help="replay cache for paid responses; the credential never "
                        "enters a key, a path or a log. Empty string disables")
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
OBSERVATION_SCHEMA = 2


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


def survey(args):
    """The FREE half: enumerate the exchange, filter, derive the cutoffs.

    Shared by `--preflight` and the real run so the two cannot disagree about
    how many paid requests a window needs. A preflight that estimated its own
    way would eventually differ from the run it is meant to predict.
    """
    coverage = Coverage()
    ledger = Ledger()
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

    # ELIGIBILITY filter: the declared GAME window, on the verified start.
    # Padding is right for retrieval and wrong for eligibility -- the padded
    # set previously included Sept 13 and Sept 16 games in a Sept 14-15 run.
    markets: dict[str, dict] = {}
    off_window = no_start = 0
    for ticker, market in retrieved.items():
        game_start = market_start_time(market)
        if game_start is None:
            no_start += 1
            continue
        if start <= game_start < end_exclusive:
            markets[ticker] = market
        else:
            off_window += 1
    if off_window:
        ledger.exclude("game_outside_declared_window", count=off_window,
                       stage="contracts")
    if no_start:
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
    starts = [market_start_time(m) for m in markets.values()]
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

    targets = checkpoint_targets(markets, grid, ledger)
    matrix = CheckpointMatrix()
    matrix.expect(targets)
    ledger.count("checkpoint_cells", len(targets), unit="game-checkpoints")

    if not cutoffs and markets:
        coverage.fail(
            f"{len(markets):,} contracts are in the window but no decision "
            f"cutoff was derived at leads {grid_describe(grid)}; "
            "widen --from or shorten --lead-grid"
        )

    return markets, cutoffs, cutoff, coverage, ledger, grid, matrix


def preflight(args) -> int:
    """Enumerate the real work before any paid call. Costs nothing."""
    markets, cutoffs, cutoff, coverage, ledger, grid, matrix = survey(args)
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
    markets, cutoffs, cutoff, coverage, ledger, grid, matrix = survey(args)
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
    joined = join_markets(markets, quotes_by_event, args.sport, ledger)
    print(f"  joined: {len(joined):,} contracts matched to a sharp event")

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
            if listing == STATUS_NOT_YET_LISTED:
                matrix.record(jm.market_ticker, checkpoint, listing)
                ledger.exclude("contract_not_yet_listed", stage="checkpoint_cells")
                continue
            if listing is not None:
                matrix.record(jm.market_ticker, checkpoint, listing)
                ledger.reject("listing_time_unreadable", jm.market_ticker,
                              stage="checkpoint_cells")
                continue

            if not candles:
                matrix.record(jm.market_ticker, checkpoint, "candles_unavailable")
                ledger.reject("candles_unavailable", jm.market_ticker,
                              stage="checkpoint_cells")
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
    return observations, coverage, credits, ledger, grid, matrix


def main(argv=None) -> int:
    args = parse_args(argv)
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

    observations, coverage, credits, ledger, grid, matrix = collect(args, key)
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
