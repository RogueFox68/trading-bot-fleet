"""Event-driven sportsbook-move / Kalshi reaction-lag study.

    python3 run_reaction.py --capability          # the source timing audit (FREE)
    python3 run_reaction.py --capability-verify    # free commands to re-check it
    python3 run_reaction.py --capability --json    # machine-readable, for a manifest
    python3 run_reaction.py --policy --json        # the DECLARED thresholds (FREE)
    python3 run_reaction.py --replay b.json        # OFFLINE replay of a bundle
    python3 run_reaction.py --replay b.json --json # the episode ledger

A SEPARATE ENTRY POINT ON PURPOSE
---------------------------------
The checkpoint study (`run_study.py`) has a published result -- 16 games, 32
contracts, 222/224 observations, zero entries clearing +$0.01 -- and that
result has to stay reproducible. Sharing a CLI with this one would mean every
change here could move it. So this is its own command with its own output
schema, and it imports from the study rather than editing it.

WHAT THIS ANSWERS TODAY
-----------------------
The capability audit, the declared thresholds, and a full OFFLINE REPLAY of a
bundle of raw provider payloads -- detector, reaction measurement, opportunity
screen and episode ledger, end to end. What is NOT here is collection: no
network, no credential, no credits, in any path.

The audit still comes first, because it decides which of the study's
questions these sources can support AT ALL, and several turn out to be
unsupportable at any sample size. `capability.question_verdicts()` is the one
place that grades them and `--capability` prints how many; this docstring
deliberately restates NEITHER a verdict nor a count, because a copy is what
let the line below stay wrong after the code had been corrected:

  answerable          the book's fair probability move, and when the PROVIDER
                      last observed it
  interval-censored   WHEN THE BOOK ITSELF MOVED -- bracketed between
                      consecutive provider observations, never a point
                      estimate -- plus when we could have known, Kalshi's move
                      time, ordering and persistence, each with its bound
  unanswerable        fillable size (no depth), suspension vs absence,
                      provider delivery lag (no receipt time in replay)

`last_update` IS NOT WHEN THE BOOKMAKER CHANGED ITS PRICE. An earlier version
of this docstring said the book's move instant was answerable; it is not, and
every lag in this study is an interval because of it.

DECLARE THE THRESHOLDS BEFORE YOU LOOK
--------------------------------------
`--policy` prints every declared threshold in both policies, with nothing
fitted to any outcome. It costs nothing and touches no network, so it can be
run and its output committed BEFORE any data is collected -- which is the
only thing that makes "declared, not fitted" checkable later rather than
merely asserted. The 16 development games must never be used to tune these.

OFFLINE REPLAY
--------------
`--replay` reads a bundle of RAW provider payloads from disk and runs the
whole chain -- detector, reaction measurement, opportunity screen, episode
ledger -- with no network, no credential and no credits. Producing the bundle
is a collection machine's job; `reaction/capture.py` declares the bounded
read-only interface that job has to satisfy.

EXIT CODES SAY WHAT KIND OF THING WENT WRONG
--------------------------------------------
  0  the replay ran. ZERO ENTRIES IS A RESULT, not a failure -- the
     checkpoint study's published finding is zero entries clearing +$0.01,
     and an exit code that called that broken would burn the signal a real
     defect needs (rule 27).
  1  a defect someone can fix: an unreadable bundle, a mislabelled holdout,
     a ledger whose stage breakdowns do not sum to their totals, or
     INCOMPLETE COVERAGE -- a payload that would not parse, a game with no
     usable sharp quote, a contract with no usable candle. The last three
     are the dangerous ones: they parse cleanly and produce zero moves,
     which is indistinguishable from a quiet market unless the exit code
     says otherwise.
  2  usage.

NOT BUILT, AND NOT FAKED
------------------------
Live capture. No orders, no live capture, no paid requests -- none of those
is authorised and none is implemented. `capture.py` is the interface and its
guards, with no transport that can reach a network.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reaction import capability                                  # noqa: E402
from reaction.detector import MovePolicy                        # noqa: E402
from reaction.episodes import DataRole, HoldoutViolation        # noqa: E402
from reaction.measure import ReactionPolicy                     # noqa: E402
from reaction.replay import (                                   # noqa: E402
    BundleError, render_report, replay_file,
)


#: The options that SELECT an action. The usage line is generated from this
#: tuple rather than typed beside it, because the typed version went stale:
#: it still read "pass --capability or --capability-verify" after --policy
#: and --replay shipped, hiding the two most useful free commands from
#: anyone who ran the tool with no arguments.
MODES = ("--capability", "--capability-verify", "--policy", "--replay")

#: Options that MODIFY a mode rather than select one. Declared so the two
#: sets partition every option the parser accepts -- a new option must be
#: classified as one or the other, and the test fails if it is neither.
MODIFIERS = ("--entry-delay", "--declare-role", "--series", "--json",
             "--help")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--capability", action="store_true",
                        help="print the source timing capability audit. FREE: "
                             "no network, no credential, no credits")
    parser.add_argument("--capability-verify", action="store_true",
                        help="print the exact FREE commands that re-verify the "
                             "transcribed rows, to run where egress works")
    parser.add_argument("--policy", action="store_true",
                        help="print the declared detection and reaction "
                             "thresholds. FREE. Run this BEFORE collecting, "
                             "and keep the output, so 'declared not fitted' "
                             "is checkable rather than asserted")
    parser.add_argument("--replay", metavar="BUNDLE",
                        help="offline replay of a bundle of raw provider "
                             "payloads. FREE: no network, no credential, no "
                             "credits")
    parser.add_argument("--entry-delay", type=float, default=0.0,
                        metavar="SECONDS",
                        help="reaction delay applied to the ENTRY only. The "
                             "trigger and side stay frozen at the decision")
    parser.add_argument("--declare-role", choices=[r.value for r in DataRole],
                        default=DataRole.UNDECLARED.value,
                        help="what this window is. Declaring a run over the "
                             "2026-09-01..16 development window a holdout is "
                             "REFUSED, not warned about")
    parser.add_argument("--series", default=None,
                        help="exchange series, for the DATED fee schedule. "
                             "Omitting it prices at the generic coefficients, "
                             "which is wrong for anything quoted as historical")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output")
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


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

    if args.replay:
        try:
            ledger, report = replay_file(
                args.replay,
                entry_delay=timedelta(seconds=args.entry_delay),
                declared_role=DataRole(args.declare_role),
                series=args.series)
        except BundleError as exc:
            print(f"unusable bundle: {exc}", file=sys.stderr)
            return 1
        except HoldoutViolation as exc:
            print(f"holdout violation: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"bundle": report.as_dict(),
                              "ledger": ledger.as_dict()}, indent=2))
        else:
            print(render_report(report))
            print()
            print(ledger.render())
            print()
            print("  No orders. No live capture. No paid requests.")
        # A ledger whose breakdowns do not sum is a DEFECT: the totals may be
        # right while the reasons are not, which is the failure mode that
        # produced a credible percentage over the wrong denominator twice.
        if not ledger.reconciles:
            print("a stage breakdown does not sum to its total",
                  file=sys.stderr)
            return 1
        # INCOMPLETE COVERAGE is a loss, and a loss is a defect someone can
        # fix. Two different things land here and the message has to say
        # which: a payload the parser could not read, and a payload that
        # parsed perfectly but carried no usable target data. The second is
        # the one that presents as a clean run over a quiet market, so a
        # generic "could not be parsed" would have misdescribed it.
        if not report.complete:
            print("coverage is incomplete -- this run may NOT be read as a "
                  "quiet market:", file=sys.stderr)
            for reason in report.coverage_failures():
                print(f"  {reason}", file=sys.stderr)
            return 1
        return 0

    if args.policy:
        declared = {"move_detection": MovePolicy().as_dict(),
                    "reaction_measurement": ReactionPolicy().as_dict()}
        if args.json:
            print(json.dumps(declared, indent=2))
            return 0
        print("DECLARED THRESHOLDS (nothing here is fitted to an outcome)")
        print()
        for section, values in declared.items():
            print(f"  {section}")
            for key, value in values.items():
                if key == "note":
                    continue
                print(f"    {key:<42} {value}")
            print(f"    -- {values['note']}")
            print()
        print("  Run this before collecting and keep the output. A threshold")
        print("  tuned after seeing outcomes makes every figure downstream a")
        print("  selection artifact, and the 16 development games are")
        print("  DEVELOPMENT data -- they can never serve as a holdout.")
        return 0

    if args.capability:
        if args.json:
            print(json.dumps(capability.as_dict(), indent=2))
            return 0
        print(capability.render())
        print()
        print("  BUILT AND TESTED: the clock contracts, the move detector,")
        print("  the reaction measurement, the executable-opportunity screen,")
        print("  the episode ledger and the offline replay -- run the whole")
        print("  chain with --replay, and --policy prints every threshold.")
        print("  NOT BUILT AND NOT FAKED: collection.")
        print("  No orders, no live capture, no paid requests -- none of "
              "those is")
        print("  authorised and none is implemented. reaction/capture.py is "
              "the")
        print("  bounded read-only interface a collector would have to "
              "satisfy, and")
        print("  it holds no transport that can reach a network.")
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
    print(f"nothing to do: pass one of {', '.join(MODES)}"
          f" (--replay takes a BUNDLE path)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
