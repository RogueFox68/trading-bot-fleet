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

from analysis.scoring import Eligibility, Observation, build_report  # noqa: E402
from collect import (                                              # noqa: E402
    Ledger, decision_cutoffs, in_study_window, join_markets,
    market_start_time, observation_at_cutoff, snapshots_per_day_for,
)
from core import fees
from core.matcher import EVENT_BODY_TIMEZONE                                              # noqa: E402
from core.matcher import match_event, unverified_note              # noqa: E402
from data.kalshi_history import (                                  # noqa: E402
    Coverage, enumerate_settled_markets, fetch_candlesticks,
    fetch_historical_cutoff, uses_archive,
)
from data.cache import CreditCapReached, ResponseCache             # noqa: E402
from data.odds_history import (                                    # noqa: E402
    CreditLedger, MAX_QUOTE_AGE_SECONDS, RETRIES, SPORT_KEYS,
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
    p.add_argument("--lead-minutes", type=int, default=60,
                   help="observe both predictors this many minutes before start")
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
    p.add_argument("--out", default="study_output", help="directory for artifacts")
    return p.parse_args(argv)


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


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
    print(f"  fee models       {fees.describe(args.series)}")
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
    starts = [market_start_time(m) for m in markets.values()]
    cutoffs = decision_cutoffs([s for s in starts if s], args.lead_minutes)
    outside_window = sum(1 for c in cutoffs if not (start <= c < end_exclusive))
    if outside_window:
        print(f"          {outside_window:,} cutoff(s) fall outside the game "
              "window and are fetched anyway -- eligible games need them")
    print(f"          {len(cutoffs):,} distinct decision cutoffs from "
          f"{len(markets):,} contracts")

    if not cutoffs and markets:
        coverage.fail(
            f"{len(markets):,} contracts are in the window but no decision "
            f"cutoff falls inside it at a {args.lead_minutes:.0f}-minute lead; "
            "widen --from or reduce --lead-minutes"
        )

    return markets, cutoffs, cutoff, coverage, ledger


def preflight(args) -> int:
    """Enumerate the real work before any paid call. Costs nothing."""
    markets, cutoffs, cutoff, coverage, ledger = survey(args)
    per_call = estimate_credits(1, 1)
    print()
    print("PREFLIGHT (no paid requests made)")
    print(f"  eligible contracts   {len(markets):,}")
    print(f"  distinct cutoffs     {len(cutoffs):,}")
    base = len(cutoffs) * per_call
    print(f"  base credit cost     {base:,} ({per_call} per call)")
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
    print(ledger.render())
    if not coverage.complete:
        print()
        print("  Coverage is incomplete BEFORE any paid call. Fix that first.")
        return 1
    return 0


def collect(args, key: str):
    """Pull both sides, join on identity, and build observations at one cutoff."""
    markets, cutoffs, cutoff, coverage, ledger = survey(args)
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

    # --- one decision timestamp per game -------------------------------------
    # THE STUDY'S OWN DENOMINATOR: one target contract per joined market, each
    # needing a usable quote at its required cutoff. Independently enumerated
    # from the exchange side, never derived from how many observations
    # succeeded -- a denominator defined by its successes always reads 100%.
    observations: list[Observation] = []
    ledger.count("target_contracts", len(joined), unit="contracts")
    for jm in joined:
        decision_at = jm.start - timedelta(minutes=args.lead_minutes)
        candles, cand_cov = fetch_candlesticks(
            jm.market_ticker, args.series,
            decision_at - timedelta(minutes=args.lead_minutes),
            decision_at,
            use_archive=uses_archive(markets[jm.market_ticker], cutoff),
        )
        coverage.merge(cand_cov)
        obs = observation_at_cutoff(
            jm, quotes_by_event.get(jm.provider_event_id, []), candles,
            decision_at, args.sport, ledger, method=args.devig,
            max_quote_age=args.max_quote_age, reject_stage="target_contracts",
        )
        if obs is not None:
            observations.append(obs)

    ledger.count("observations", len(observations), unit="contracts")
    ledger.apply_to(coverage)
    print(f"  built:  {len(observations):,} observations "
          f"({ledger.total_rejected:,} records dropped, see ledger)")
    return observations, coverage, credits, ledger


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
    observations, coverage, credits, ledger = collect(args, key)
    out = Path(args.out)
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
                "lead_minutes": args.lead_minutes,
                "devig": args.devig,
                "max_quote_age_seconds": args.max_quote_age,
                "min_net_ev": args.min_net_ev,
                "max_spread": args.max_spread,
                "max_credits": args.max_credits,
                "cache_dir": args.cache_dir or None,
                "fees": fees.describe(args.series),
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

    report = build_report(
        observations, coverage, fees.describe(args.series),
        eligibility=Eligibility(min_net_ev=args.min_net_ev,
                                max_spread=args.max_spread),
        series=args.series,
        ledger_text=ledger.render(),
    )
    print()
    print(report.render())

    write_coverage()
    (out / "report.txt").write_text(report.render(), encoding="utf-8")
    (out / "observations.json").write_text(
        json.dumps([{
            "game_id": o.game_id, "market_id": o.market_id,
            "decision_at": o.decision_at.isoformat(),
            "sharp_at": o.sharp_at.isoformat() if o.sharp_at else None,
            "exchange_at": o.exchange_at.isoformat() if o.exchange_at else None,
            "minutes_to_start": o.minutes_to_start,
            "yes_participant": o.yes_participant,
            "p_sharp": o.p_sharp, "p_exchange": o.p_exchange,
            "outcome": o.outcome, "bid": o.exchange_bid, "ask": o.exchange_ask,
            "devig_method": o.devig_method,
        } for o in observations], indent=2), encoding="utf-8")
    print(f"\nwrote {out}/report.txt, observations.json and coverage.json ({credits})")
    return 0 if coverage.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
