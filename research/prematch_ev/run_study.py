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
    Ledger, in_study_window, join_markets, observation_at_cutoff,
)
from core import fees                                              # noqa: E402
from core.matcher import match_event, unverified_note              # noqa: E402
from data.kalshi_history import (                                  # noqa: E402
    Coverage, enumerate_settled_markets, fetch_candlesticks,
    fetch_historical_cutoff, uses_archive,
)
from data.odds_history import (                                    # noqa: E402
    CreditLedger, MAX_QUOTE_AGE_SECONDS, SPORT_KEYS, estimate_credits,
    fetch_snapshot, probe_earliest_snapshot,
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
    p.add_argument("--snapshots-per-day", type=int, default=8)
    p.add_argument("--devig", choices=("shin", "multiplicative"), default="shin")
    p.add_argument("--max-quote-age", type=float, default=MAX_QUOTE_AGE_SECONDS,
                   help="reject a sharp quote older than this at the cutoff")
    p.add_argument("--min-net-ev", type=float, default=0.01,
                   help="predeclared minimum PREDICTED net EV per contract, at "
                        "the executable price after fee")
    p.add_argument("--max-spread", type=float, default=0.10,
                   help="widest book the policy will treat as executable")
    p.add_argument("--api-key", help="Odds API key (or set ODDS_API_KEY)")
    p.add_argument("--plan", action="store_true", help="print the budget and exit")
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
    credits = estimate_credits(days, args.snapshots_per_day)
    print("STUDY PLAN")
    print(f"  sport            {args.sport}  (series {args.series})")
    print(f"  window           {args.start} .. {args.end}  ({days} days)")
    print(f"  snapshots/day    {args.snapshots_per_day}")
    print(f"  odds credits     ~{credits:,}  (10 per region per market per call)")
    print(f"  kalshi calls     free, unauthenticated")
    print(f"  lead time        {args.lead_minutes} min before scheduled start")
    print(f"  de-vig           {args.devig}")
    print()
    print(f"  fee models       {fees.describe()}")
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


def collect(args, key: str):
    """Pull both sides, join on identity, and build observations at one cutoff.

    Returns (observations, coverage, credit ledger, collection ledger). Every
    stage carries its own denominator in its own units and every drop carries a
    reason and a count -- see collect.Ledger.
    """
    coverage = Coverage()
    credits = CreditLedger()
    ledger = Ledger()
    start = _date(args.start)
    # `--to` is INCLUSIVE, so the exclusive bound is the following midnight.
    # With `cursor <= midnight(end)` the final day got only its midnight
    # snapshot, silently truncating the last day of every window.
    end_inclusive = _date(args.end)
    end_exclusive = end_inclusive + timedelta(days=1)

    # --- exchange side: BOTH partitions, then restricted to the window ------
    cutoff, cutoff_cov = fetch_historical_cutoff()
    coverage.merge(cutoff_cov)
    all_markets, market_cov = enumerate_settled_markets(args.series)
    coverage.merge(market_cov)
    ledger.count("contracts", len(all_markets), unit="contracts")

    markets: dict[str, dict] = {}
    outside = undatable = 0
    for ticker, market in all_markets.items():
        verdict = in_study_window(market, start, end_exclusive)
        if verdict is True:
            markets[ticker] = market
        elif verdict is False:
            outside += 1
        else:
            undatable += 1
    if outside:
        # A market the study deliberately skipped is not a gap in what it could
        # see. Counting these as failures made a fully collected one-day run
        # report massive unexplained loss.
        ledger.exclude("settled_outside_study_window", count=outside,
                       stage="contracts")
    if undatable:
        ledger.reject("settlement_time_unreadable", f"{undatable} markets",
                      count=undatable, stage="contracts")

    unroutable = sum(1 for m in markets.values() if uses_archive(m, cutoff) is None)
    if unroutable:
        ledger.reject("candle_partition_unroutable", f"{unroutable} markets",
                      count=unroutable, stage="contracts")
    print(f"  kalshi: {len(all_markets):,} settled markets -> {len(markets):,} in window "
          f"({outside:,} outside, cutoff {cutoff.date() if cutoff else 'UNKNOWN'})")

    # --- sharp side: one call per snapshot ----------------------------------
    quotes_by_event: dict[str, list] = {}
    cursor, step = start, timedelta(hours=24 / max(1, args.snapshots_per_day))
    snapshots = 0
    while cursor < end_exclusive:
        if credits.exhausted():
            coverage.fail(f"odds quota exhausted at {cursor.date()}; window truncated")
            break
        snap = fetch_snapshot(args.sport, cursor, key, ledger=credits)
        coverage.merge(snap.coverage)
        snapshots += 1
        ledger.count("odds_events", snap.events_seen, unit="event-quotes")
        if snap.events_without_sharp_book:
            ledger.reject("event_without_sharp_book", f"at {cursor.isoformat()}",
                          count=snap.events_without_sharp_book, stage="odds_events")
        if snap.events_without_id:
            ledger.reject("odds_event_without_id", f"at {cursor.isoformat()}",
                          count=snap.events_without_id, stage="odds_events")
        if snap.quotes_without_update_time:
            ledger.reject("sharp_quote_without_update_time", f"at {cursor.isoformat()}",
                          count=snap.quotes_without_update_time, stage="odds_events")
        for quote in snap.quotes:
            quotes_by_event.setdefault(quote.provider_event_id, []).append(quote)
        cursor += step
    ledger.count("odds_snapshots", snapshots, unit="snapshots")
    print(f"  odds:   {len(quotes_by_event):,} sharp events over {snapshots:,} snapshots "
          f"({credits})")

    # --- join on participants, then time ------------------------------------
    joined = join_markets(markets, quotes_by_event, args.sport, ledger)
    ledger.count("joined_contracts", len(joined), unit="contracts")
    unverified_starts = sum(1 for j in joined if not j.start_verified)
    if unverified_starts:
        print(f"  NOTE: {unverified_starts:,} joins used the SHARP event's start time; "
              "no verified exchange start key is configured")
    print(f"  joined: {len(joined):,} contracts matched to a sharp event")

    # --- one decision timestamp per game -------------------------------------
    observations: list[Observation] = []
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
            max_quote_age=args.max_quote_age,
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
    if not observations:
        print("no observations built -- nothing to score. Check --probe first.",
              file=sys.stderr)
        print(ledger.render(), file=sys.stderr)
        return 1

    report = build_report(
        observations, coverage, fees.describe(),
        eligibility=Eligibility(min_net_ev=args.min_net_ev,
                                max_spread=args.max_spread),
        ledger_text=ledger.render(),
    )
    print()
    print(report.render())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(report.render(), encoding="utf-8")
    # The ledger is persisted, not just printed: a reader of the artifacts has
    # to be able to see what was lost without going back to a terminal buffer.
    (out / "coverage.json").write_text(json.dumps({
        "complete": coverage.complete,
        "reasons": coverage.reasons,
        "collection": ledger.as_dict(),
        "credits": str(credits),
        "run": {
            "sport": args.sport, "series": args.series,
            "from": args.start, "to": args.end,
            "lead_minutes": args.lead_minutes,
            "snapshots_per_day": args.snapshots_per_day,
            "devig": args.devig,
            "max_quote_age_seconds": args.max_quote_age,
            "min_net_ev": args.min_net_ev,
            "max_spread": args.max_spread,
            "fees": fees.describe(),
        },
    }, indent=2), encoding="utf-8")
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
