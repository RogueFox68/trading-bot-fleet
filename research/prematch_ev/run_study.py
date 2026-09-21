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

from analysis.scoring import Observation, build_report              # noqa: E402
from core import fees                                              # noqa: E402
from core.devig import DevigError, devig_american                   # noqa: E402
from core.matcher import match_event, parse_kalshi_game_ticker, unverified_note  # noqa: E402
from data.kalshi_history import (                                   # noqa: E402
    Coverage, fetch_candlesticks, iter_settled_markets, settlement_outcome,
)
from data.odds_history import (                                     # noqa: E402
    CreditLedger, SPORT_KEYS, estimate_credits, fetch_snapshot, probe_earliest_snapshot,
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


def collect(args, key: str) -> tuple[list[Observation], Coverage, CreditLedger]:
    """Pull both sides, match, and build observations at the target lead time."""
    coverage = Coverage()
    ledger = CreditLedger()
    start, end = _date(args.start), _date(args.end)

    # --- exchange side: settled game markets + their pre-start quote ---------
    settled: dict[str, dict] = {}
    for page, page_cov in iter_settled_markets(args.series):
        coverage.merge(page_cov)
        for market in page:
            ticker = str(market.get("ticker", ""))
            parsed = parse_kalshi_game_ticker(ticker)
            if parsed and settlement_outcome(market) is not None:
                settled[ticker] = market
    print(f"  kalshi: {len(settled):,} settled game markets ({coverage})")

    # --- sharp side: one call per snapshot, keyed by canonical event id ------
    sharp_by_event: dict[str, list] = {}
    cursor, step = start, timedelta(hours=24 / max(1, args.snapshots_per_day))
    while cursor <= end:
        if ledger.exhausted():
            coverage.fail(f"odds quota exhausted at {cursor.date()}; window truncated")
            break
        snap = fetch_snapshot(args.sport, cursor, key, ledger=ledger)
        coverage.merge(snap.coverage)
        for quote in snap.quotes:
            m = match_event(args.sport, quote.away_name, quote.home_name,
                            quote.commence_time)
            if m.matched:
                sharp_by_event.setdefault(m.event_id, []).append(quote)
        cursor += step
    print(f"  odds:   {len(sharp_by_event):,} matched events ({ledger})")

    # --- join at the target lead time ---------------------------------------
    observations: list[Observation] = []
    target = float(args.lead_minutes)
    unmatched = 0
    for ticker, market in settled.items():
        parsed = parse_kalshi_game_ticker(ticker)
        commence = market.get("open_time") or market.get("expected_expiration_time")
        if not parsed or not commence:
            unmatched += 1
            continue
        event_id = None
        for candidate, quotes in sharp_by_event.items():
            if candidate.endswith(f"_{parsed['a']}_{parsed['b']}") or \
               candidate.endswith(f"_{parsed['b']}_{parsed['a']}"):
                event_id = candidate
                break
        if event_id is None:
            unmatched += 1
            continue

        quotes = sharp_by_event[event_id]
        quote = min(quotes, key=lambda q: abs(q.minutes_to_start() - target))
        if abs(quote.minutes_to_start() - target) > 30:
            unmatched += 1
            continue

        try:
            result = devig_american([quote.home_price, quote.away_price])
        except DevigError:
            unmatched += 1
            continue
        p_sharp = (result.shin if args.devig == "shin" else result.multiplicative)[0]

        candles, cand_cov = fetch_candlesticks(
            ticker, args.series,
            quote.commence_time - timedelta(minutes=target + 15),
            quote.commence_time - timedelta(minutes=max(0, target - 15)),
        )
        coverage.merge(cand_cov)
        usable = [c for c in candles if c.mid is not None]
        if not usable:
            unmatched += 1
            continue
        candle = usable[-1]

        observations.append(
            Observation(
                event_id=event_id,
                observed_at=candle.ts,
                minutes_to_start=quote.minutes_to_start(),
                p_sharp=p_sharp,
                p_exchange=candle.mid,
                outcome=settlement_outcome(market),
                exchange_bid=candle.bid_close,
                exchange_ask=candle.ask_close,
                devig_method=args.devig,
            )
        )

    if unmatched:
        print(f"  joined: {len(observations):,} observations "
              f"({unmatched:,} markets dropped, unmatched or unreadable)")
    return observations, coverage, ledger


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
    observations, coverage, ledger = collect(args, key)
    if not observations:
        print("no observations built -- nothing to score. Check --probe first.",
              file=sys.stderr)
        return 1

    report = build_report(observations, coverage, fees.describe())
    print()
    print(report.render())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(report.render(), encoding="utf-8")
    (out / "observations.json").write_text(
        json.dumps([{
            "event_id": o.event_id, "observed_at": o.observed_at.isoformat(),
            "minutes_to_start": o.minutes_to_start, "p_sharp": o.p_sharp,
            "p_exchange": o.p_exchange, "outcome": o.outcome,
            "bid": o.exchange_bid, "ask": o.exchange_ask,
            "devig_method": o.devig_method,
        } for o in observations], indent=2), encoding="utf-8")
    print(f"\nwrote {out}/report.txt and {out}/observations.json ({ledger})")
    return 0 if coverage.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
