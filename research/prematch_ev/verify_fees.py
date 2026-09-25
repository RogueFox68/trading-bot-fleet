"""Verify a series' Kalshi fee schedule from Kalshi's own dated record. Free.

    python3 verify_fees.py --series KXNFLGAME \\
        --window 2026-09-24T01:44:22Z 2026-09-25T01:44:22Z

WHY THIS EXISTS
---------------
Every NFL figure in this study is priced at Kalshi's GENERIC taker
coefficient, because no dated schedule for KXNFLGAME is recorded in
`core/fees.py`. That is an assumption, and at one contract it is most of the
cost: the 2026-09-24 session's Green Bay quote cleared the gross edge the
floor needs by 0.0142 per contract and was refused over a fee of 0.02 --
refused at multiplier 1, admitted at 0.5 (`reaction.assessment`'s
sensitivity). KXMLBGAME's multiplier was halved on 2026-08-07, so "the
generic rate" is not a safe default for a sports series. The sessions that
wrote this code cannot reach api.elections.kalshi.com; this command is the
check, for a machine that can.

WHAT IT DOES
------------
Two public GETs per series, no credential:

    /trade-api/v2/series/fee_changes?series_ticker=S&show_historical=true
    /trade-api/v2/series/S

Both raw bodies are saved -- with the request time, status, the response's
Date header and a SHA-256 -- under `study_output/fee_evidence/` (gitignored).
Then it reports every dated change on record, the entry IN FORCE at each end
of the window by the pricing path's own rule (`core.fees.entry_in_force`),
any change inside the window, and the series' current fee fields; and it
PRINTS a `KALSHI_SERIES_SCHEDULES` entry for a person to review.

It never edits `core/fees.py`. A fee that entered the model without anyone
reading its evidence would be exactly the unexamined default this study keeps
refusing, one level up.

WHAT IT CANNOT SETTLE
---------------------
* The ACCOUNT ROUTE -- whether rounding aligns at $0.0001 (direct member) or
  $0.01 (non-direct). It is a property of the account, and no public
  endpoint answers it. Every report keeps it unresolved and prices both.
* COMPLETENESS -- no dated change for a series does not prove its current
  multiplier held on a past date. The verdict says so rather than assume it.
* The SHAPE -- transcribed from documentation, never observed here. An
  unexpected body is refused, with its keys named; the raw body is saved
  either way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from collect_reaction import OUTPUT_DIR, only_declared_endpoints   # noqa: E402
from core.fee_evidence import (                                     # noqa: E402
    entry_snippet, parse_fee_changes, parse_series_fees, verdict,
)
from core.fees import KALSHI_SERIES_SCHEDULES                       # noqa: E402
from data import kalshi_history                                     # noqa: E402
from reaction.capture import CaptureRefused                         # noqa: E402

EVIDENCE_DIR = OUTPUT_DIR / "fee_evidence"
EXIT_OK, EXIT_UNVERIFIED, EXIT_USAGE = 0, 1, 2


def _time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source(url: str) -> str:
    return url.split("://", 1)[-1]


def verify(series: str, window: tuple[datetime, datetime], out_dir: Path,
           now: Callable[[], datetime] = _now) -> tuple[dict, Path]:
    """Read, save, parse, judge. Returns the evidence record and its file."""
    reads = []
    with only_declared_endpoints():
        for url in (kalshi_history.series_fee_changes_url(series),
                    kalshi_history.series_url(series)):
            reads.append(kalshi_history.fetch_evidence(url, now))
    stamp = reads[0]["requested_at"]
    for read in reads:
        body = read["body"] or ""
        read["sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    path = out_dir / f"{series}_{stamp:%Y%m%dT%H%M%SZ}.json"
    evidence = f"evidence {path.name}"

    def body_of(read: dict) -> tuple[Any, str | None]:
        if read["error"] and read["status"] is None:
            return None, f"no answer: {read['error']}"
        if read["status"] != 200:
            return None, f"HTTP {read['status']}"
        try:
            return json.loads(read["body"]), None
        except (TypeError, json.JSONDecodeError) as exc:
            return None, f"body is not JSON: {exc}"

    changes_body, changes_error = body_of(reads[0])
    series_body, series_error = body_of(reads[1])
    entries, problems, key = [], [], None
    if changes_error:
        problems.append(f"fee_changes: {changes_error}")
    else:
        entries, found, key = parse_fee_changes(
            changes_body, series, source=_source(reads[0]["url"]),
            observed_on=stamp.date().isoformat(), observed_by=evidence)
        problems += [f"fee_changes: {p}" for p in found]
    current = None
    if series_error:
        problems.append(f"series: {series_error}")
    else:
        current, found = parse_series_fees(series_body)
        problems += [f"series: {p}" for p in found]
    record = {
        "schema": "kalshi-fee-evidence/1",
        "series": series,
        "window": [window[0].isoformat(), window[1].isoformat()],
        "reads": [{**r, "requested_at": r["requested_at"].isoformat(),
                   "received_at": (r["received_at"].isoformat()
                                   if r["received_at"] else None)}
                  for r in reads],
        "list_key": key,
        "problems": problems,
        "entries": [{"effective_from": e.effective_from.isoformat(),
                     "multiplier": e.multiplier, "fee_type": e.fee_type,
                     "source_id": e.source_id} for e in entries],
        "verdict": verdict(
            entries, current, window,
            record_read=changes_error is None and key is not None),
        "recorded_in_core_fees": series in KALSHI_SERIES_SCHEDULES,
        "snippet": entry_snippet(series, entries, evidence) if entries else None,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return record, path


def render(record: dict, path: Path) -> str:
    v = record["verdict"]
    lines = [f"KALSHI FEE EVIDENCE  {record['series']}",
             f"  window             {record['window'][0]} .. "
             f"{record['window'][1]}"]
    for read in record["reads"]:
        lines.append(f"  read               {read['url']}")
        lines.append(f"                     status {read['status']}, Date "
                     f"{read['date_header']}, sha256 {read['sha256'][:16]}..."
                     + (f", {read['error']}" if read["error"] else ""))
    lines.append(f"  saved              {path}")
    for problem in record["problems"]:
        lines.append(f"  *** {problem}")
    lines.append(f"  dated changes      {v['dated_changes']}")
    for entry in record["entries"]:
        lines.append(f"    {entry['effective_from']}  multiplier "
                     f"{entry['multiplier']:g}  {entry['fee_type']}  "
                     f"(id {entry['source_id']})")
    current = v["current_series_fee"]
    lines.append("  current series fee "
                 + (f"multiplier {current['fee_multiplier']:g} "
                    f"({current.get('fee_type')})" if current else "unread"))
    lines.append("  ESTABLISHED:")
    for item in v["establishes"] or ["nothing about the window"]:
        lines.append(f"    - {item}")
    lines.append("  NOT ESTABLISHED:")
    for item in v["does_not_establish"]:
        lines.append(f"    - {item}")
    if record["snippet"]:
        lines += ["", "  For review -- NOT applied. If the evidence above is "
                  "right, this is the entry for", "  KALSHI_SERIES_SCHEDULES in "
                  "core/fees.py:", "", record["snippet"]]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--series", action="append", required=True,
                        help="a Kalshi series ticker; repeat for several")
    parser.add_argument("--window", nargs=2, required=True,
                        metavar=("START", "END"),
                        help="the UTC span to judge, e.g. a session's start "
                             "and end")
    parser.add_argument("--out-dir", default=str(EVIDENCE_DIR))
    return parser


def main(argv: Sequence[str] | None = None,
         now: Callable[[], datetime] = _now) -> int:
    args = build_parser().parse_args(argv)
    start, end = (_time(v) for v in args.window)
    if start is None or end is None or not start < end:
        print("--window needs two ISO times with a UTC offset, start before "
              "end", file=sys.stderr)
        return EXIT_USAGE
    unverified = False
    for series in args.series:
        try:
            record, path = verify(series, (start, end), Path(args.out_dir),
                                  now=now)
        except CaptureRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(render(record, path))
        print()
        unverified = unverified or bool(record["problems"]) or not (
            record["verdict"]["establishes"])
    return EXIT_UNVERIFIED if unverified else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
