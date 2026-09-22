"""Event-driven sportsbook-move / Kalshi reaction-lag study.

    python3 run_reaction.py --capability          # the source timing audit (FREE)
    python3 run_reaction.py --capability-verify    # free commands to re-check it
    python3 run_reaction.py --capability --json    # machine-readable, for a manifest

A SEPARATE ENTRY POINT ON PURPOSE
---------------------------------
The checkpoint study (`run_study.py`) has a published result -- 16 games, 32
contracts, 222/224 observations, zero entries clearing +$0.01 -- and that
result has to stay reproducible. Sharing a CLI with this one would mean every
change here could move it. So this is its own command with its own output
schema, and it imports from the study rather than editing it.

WHAT THIS ANSWERS TODAY
-----------------------
Only the capability audit. That is deliberate and it is the honest first
increment: the audit decides which of the study's questions the available
sources can support AT ALL, and two of them turn out to be unsupportable at
any sample size. Building the measurement first and discovering that afterwards
would have produced numbers nobody should read.

  answerable          the book's fair probability move, and when the BOOK moved
  interval-censored   when WE could have known, Kalshi's move time, ordering,
                      persistence -- each with its bound printed
  unanswerable        fillable size (no depth), suspension vs absence,
                      provider delivery lag (no receipt time in replay)

NOT BUILT YET, AND NOT FAKED
----------------------------
Reaction measurement, the executable-opportunity screen, the episode ledger
and the paper replay. The detector and the clock contracts they will sit on
are in `reaction/`, tested, and reachable from here. No orders, no live
capture, no paid requests -- none of those is authorised and none is
implemented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaction import capability                                  # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capability", action="store_true",
                        help="print the source timing capability audit. FREE: "
                             "no network, no credential, no credits")
    parser.add_argument("--capability-verify", action="store_true",
                        help="print the exact FREE commands that re-verify the "
                             "transcribed rows, to run where egress works")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.capability_verify:
        if args.json:
            print(json.dumps(
                {"free_verification_commands":
                 list(capability.free_verification_commands())}, indent=2))
        else:
            print("FREE RE-VERIFICATION OF THE TRANSCRIBED CAPABILITY ROWS")
            print()
            print("  Nothing here costs a credit. Run it somewhere with egress")
            print("  and correct reaction/capability.py if a row disagrees.")
            print()
            for line in capability.free_verification_commands():
                print(f"  {line}")
        return 0

    if args.capability:
        if args.json:
            print(json.dumps(capability.as_dict(), indent=2))
            return 0
        print(capability.render())
        print()
        print("  NEXT INCREMENT (not built, not faked): reaction measurement,")
        print("  executable-opportunity screen, episode ledger, paper replay.")
        print("  No orders. No live capture. No paid requests.")
        # EXIT ZERO even though several questions are unanswerable. Those are
        # PERMANENT properties of these two sources, and the design already
        # accounts for them -- so reporting them through the exit code would
        # put a red line in every run forever and burn the signal a real
        # failure needs (rule 27, the stooq lesson). The audit ran and told
        # the truth; that is success.
        #
        # Non-zero is reserved for a defect someone can fix: the declared
        # cadence disagreeing with measured data, which means the transcribed
        # table is wrong and every lag derived from it is off by the
        # difference. That check needs a run's snapshots, so it fires from the
        # replay rather than from a standalone audit.
        blocked = capability.unanswerable_questions()
        if blocked:
            print()
            print(f"  {len(blocked)} of the study's questions are UNANSWERABLE "
                  "from these")
            print("  sources. That is a limitation to carry into the design, "
                  "not a bug in")
            print("  the analysis and not a failure of this run -- so the exit "
                  "code stays 0.")
        return 0

    print(__doc__)
    print("nothing to do: pass --capability or --capability-verify",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
