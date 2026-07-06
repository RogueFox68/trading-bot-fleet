"""One-off dedupe for trade rows duplicated by the filled_at stamp mismatch.

The submit-time fill write and the accountant's reconciled write both stamp
rows at the broker fill time, but Alpaca's single-order and order-list
endpoints serialize filled_at with sub-µs differences — so an unpredictable
subset of fills landed as TWO points ~1µs apart (one with the strategy
`reason`, one without), inflating SUM(qty) and trade counts.
utils._log_fill_to_influx now floors stamps to the millisecond so the pair
collapses to one point; this tool cleans up the rows written before the fix.

It scans the reconciled bots' measurements for same-symbol point pairs within
a small window carrying identical price+qty where exactly one row has a
`reason`, and deletes the reason-less (reconcile) twin. Dry-run by default.

Usage (inside the trading-fleet container):
    python dedupe_trades.py                 # report only
    python dedupe_trades.py --apply         # delete the duplicate points
    python dedupe_trades.py --days 60 --window-ms 10
"""
import argparse
from collections import defaultdict

import requests

import config
import fleet_registry

QUERY_URL = f"http://{config.INFLUX_HOST}:{config.INFLUX_PORT}/query"


def reconciled_measurements():
    """Measurements that both writers touch, straight from the registry."""
    return sorted(cfg["measurement"] for cfg in fleet_registry.BOTS.values()
                  if cfg["reconciled"])


def fetch_points(measurement, days):
    q = (f'SELECT "price","qty","action","reason","symbol" '
         f'FROM "{measurement}" WHERE time > now() - {days}d')
    r = requests.get(QUERY_URL,
                     params={"db": config.INFLUX_DB_NAME, "q": q, "epoch": "ns"},
                     timeout=30)
    r.raise_for_status()
    series = (r.json().get("results", [{}])[0].get("series") or [])
    points = []
    for s in series:
        cols = s["columns"]
        points.extend(dict(zip(cols, row)) for row in s["values"])
    return points


def find_duplicates(points, window_ms):
    """Pairs within window_ms, same symbol/price/qty, exactly one reason-less."""
    window_ns = window_ms * 1_000_000
    by_symbol = defaultdict(list)
    for p in points:
        if p.get("symbol"):
            by_symbol[p["symbol"]].append(p)
    doomed = []
    for rows in by_symbol.values():
        rows.sort(key=lambda p: p["time"])
        for a, b in zip(rows, rows[1:]):
            if b["time"] - a["time"] > window_ns:
                continue
            if a.get("price") != b.get("price") or a.get("qty") != b.get("qty"):
                continue
            a_reason, b_reason = a.get("reason"), b.get("reason")
            if a_reason and not b_reason:
                doomed.append(b)
            elif b_reason and not a_reason:
                doomed.append(a)
    return doomed


def delete_point(measurement, point):
    # DELETE accepts time + tag predicates only; symbol is the sole tag, so
    # this removes exactly the one duplicate point.
    q = (f'DELETE FROM "{measurement}" '
         f'WHERE "symbol" = \'{point["symbol"]}\' AND time = {point["time"]}')
    r = requests.post(QUERY_URL, params={"db": config.INFLUX_DB_NAME, "q": q},
                      timeout=30)
    r.raise_for_status()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default: dry-run report)")
    ap.add_argument("--days", type=int, default=30, help="lookback window")
    ap.add_argument("--window-ms", type=int, default=10,
                    help="max spacing between twin points")
    ap.add_argument("--measurements", nargs="*", default=None,
                    help="override the registry-derived measurement list")
    args = ap.parse_args()

    total = 0
    for m in args.measurements or reconciled_measurements():
        try:
            points = fetch_points(m, args.days)
        except Exception as e:
            print(f"[{m}] query failed: {e}")
            continue
        doomed = find_duplicates(points, args.window_ms)
        for p in doomed:
            verb = "DELETE" if args.apply else "WOULD DELETE"
            print(f"{verb} {m} {p['symbol']} time={p['time']} "
                  f"price={p.get('price')} qty={p.get('qty')} action={p.get('action')}")
            if args.apply:
                delete_point(m, p)
        total += len(doomed)
        print(f"[{m}] {len(points)} points scanned, {len(doomed)} duplicate(s)")
    print(f"{'Deleted' if args.apply else 'Found'} {total} duplicate point(s) total.")


if __name__ == "__main__":
    main()
