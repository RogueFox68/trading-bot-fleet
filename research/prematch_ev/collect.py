"""Build observations by joining the sharp feed to exchange contracts.

Split out of run_study.py because every defect the review found lived here
rather than in the components, and a module nothing imports directly is a
module nothing tests directly.

FOUR RULES, each replacing a specific defect:

1. ONE DECISION TIMESTAMP. Both forecasts must be the best information
   available at or before the same instant. The first version chose a sharp
   quote within +-30 minutes of a target lead and, separately, the last
   exchange candle in a +-15 minute window -- so a 19:00 sharp price could be
   compared against a 19:15 exchange price and labelled "60 minutes to start".
   Giving one predictor a quarter hour more information can manufacture or
   destroy the entire effect being measured.

2. ORIENT TO THE CONTRACT'S YES PARTICIPANT. `candle.mid` and the settlement
   outcome describe the team that contract pays out on. The first version
   always took the HOME probability, so every away-team contract would have
   received an inverted signal.

3. JOIN ON IDENTITY, NOT ON TEAM NAMES. The first version compared canonical
   id suffixes and ignored the date entirely, attaching the first same-team
   event anywhere in the window -- a July 5 market joined to a July 4 game.
   The join is now on provider event id plus a start-time agreement, and an
   ambiguous join is rejected rather than resolved arbitrarily.

4. EVERY LOSS IS COUNTED AND CARRIED. Drops were printed as one aggregate and
   never reached `coverage`, so a run that discarded most of its markets still
   reported `complete` -- and the README made completeness a go criterion. The
   ledger below records a denominator and a reason for every stage, rides along
   in the saved artifacts, and fails coverage when unexplained losses are large
   enough that the surviving sample may be selected rather than representative.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from analysis.scoring import Observation
from core.devig import DevigError, devig_american
from zoneinfo import ZoneInfo

from core.matcher import (
    EVENT_BODY_TIMEZONE, EXCHANGE_CODE_ALIASES, ROSTERS,
    START_SOURCE_EXTERNAL, START_SOURCE_TICKER,
    kalshi_event_ticker, normalise_exchange_code, start_source,
    start_source_kind, start_source_provenance,
    parse_event_body_date, parse_event_body_start, parse_kalshi_game_ticker,
    resolve_team, unknown_exchange_codes,
)
from data.espn_schedule import (
    FAIL_NO_SNAPSHOT, SCHEDULE_SOURCE, ScheduleSnapshot, StartResolution,
)
from data.kalshi_history import (
    Coverage, market_open_time, settlement_outcome, settlement_time,
)
from data.odds_history import MAX_QUOTE_AGE_SECONDS, SharpQuote

# How closely the two sources' scheduled start times must agree to be the same
# game. Providers round and occasionally disagree by a few minutes; they do not
# disagree by hours, and a doubleheader's two games are ~3h apart.
START_AGREEMENT = timedelta(minutes=20)

# How far before the decision timestamp a source record may have been published
# and still count as "available at" it.
MAX_SOURCE_LAG = timedelta(minutes=20)

# Above this share of unexplained loss the surviving sample cannot be assumed
# representative, and coverage fails rather than certifying it.
MAX_UNEXPLAINED_LOSS = 0.20

# How far past the intended entry instant a candle may sit and still count as
# that entry. Beyond this the quote is a DIFFERENT moment, not a late version
# of the same one, and the run records a missing entry rather than reaching for
# whatever came next.
ENTRY_TOLERANCE = timedelta(minutes=15)


# The archive's snapshot resolution. Requesting a time between snapshots
# returns the closest one at or before it, so cutoffs are aligned to this grid
# before deduplication -- two games five minutes apart share one fetch.
SNAPSHOT_RESOLUTION = timedelta(minutes=5)


def cadence_is_viable(snapshots_per_day: int, max_quote_age: float) -> bool:
    """Whether a FIXED GRID of snapshots can ever satisfy the freshness limit.

    These two settings are individually reasonable and jointly fatal. At the
    old default of 8 snapshots a day the grid steps every 180 minutes while the
    freshness bound is 15, so a decision cutoff almost never has a quote within
    the limit and the study yields close to nothing -- a fully working
    collector producing an empty result, which is the worst kind of failure
    because it looks like an answer.

    A grid is viable only when its step is no wider than the freshness bound.
    Otherwise snapshots must be TARGETED at the decision cutoffs instead.
    """
    if snapshots_per_day <= 0:
        return False
    return (24 * 3600 / snapshots_per_day) <= max_quote_age


def snapshots_per_day_for(max_quote_age: float) -> int:
    """The coarsest grid that could satisfy a freshness bound."""
    return max(1, math.ceil(24 * 3600 / max_quote_age))


# --- the lead-time grid -----------------------------------------------------
#
# The study began with ONE checkpoint 60 minutes before start. That measures
# the late pre-game market and nothing else, and the thesis it was built to
# test -- that injuries, scratches and suspensions reprice a game over DAYS --
# lives almost entirely outside it. A single late snapshot cannot reject that
# thesis; it was never looking at the window where it would show up.
#
# These checkpoints are EXPLORATORY DESIGN, chosen now and recorded as such.
# They are not a preregistered test, and the 60-minute point is kept as a
# separately labelled baseline rather than quietly folded in with the rest --
# it is the only one with data already inspected, so pooling it would mix a
# fresh grid with a window that has been looked at.
DEFAULT_LEAD_GRID_MINUTES: tuple[float, ...] = (4320, 2880, 1440, 720, 360, 180)
BASELINE_LEAD_MINUTES = 60.0


@dataclass(frozen=True)
class Checkpoint:
    """One lead time, carrying its own label and whether it is the baseline."""

    minutes: float
    baseline: bool = False

    @property
    def label(self) -> str:
        if self.minutes >= 60 and self.minutes % 60 == 0:
            base = f"{int(self.minutes // 60)}h"
        else:
            base = f"{self.minutes:g}m"
        return f"{base} (baseline)" if self.baseline else base

    @property
    def key(self) -> str:
        return f"{self.minutes:g}"

    def decision_at(self, start: datetime) -> datetime:
        return start - timedelta(minutes=self.minutes)


def default_lead_grid(include_baseline: bool = True) -> tuple[Checkpoint, ...]:
    grid = [Checkpoint(m) for m in DEFAULT_LEAD_GRID_MINUTES]
    if include_baseline:
        grid.append(Checkpoint(BASELINE_LEAD_MINUTES, baseline=True))
    return tuple(sorted(grid, key=lambda c: -c.minutes))


def parse_lead_grid(spec: str, baseline_minutes: float | None = BASELINE_LEAD_MINUTES
                    ) -> tuple[Checkpoint, ...]:
    """Parse `72h,48h,180` into checkpoints, earliest lead first.

    Accepts an `h` or `m` suffix, or bare minutes. Sorted descending by lead so
    that "earliest qualifying checkpoint" is simply the first match -- the
    selection rule depends on that order, so it is established once here rather
    than re-sorted by each caller.
    """
    minutes: list[float] = []
    for raw in spec.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        try:
            if token.endswith("h"):
                value = float(token[:-1]) * 60.0
            elif token.endswith("m"):
                value = float(token[:-1])
            else:
                value = float(token)
        except ValueError:
            raise ValueError(f"unparseable lead time {raw!r} in {spec!r}") from None
        if value <= 0:
            raise ValueError(f"lead time must be positive, got {raw!r}")
        minutes.append(value)
    if not minutes:
        raise ValueError(f"no lead times in {spec!r}")

    out = {m: Checkpoint(m) for m in minutes}
    if baseline_minutes is not None:
        # The baseline rides along even when not named, and keeps its label
        # even when it IS named -- a run that silently dropped it would have
        # no comparison against the window already studied.
        out[baseline_minutes] = Checkpoint(baseline_minutes, baseline=True)
    return tuple(sorted(out.values(), key=lambda c: -c.minutes))


def grid_reach(grid: Iterable[Checkpoint]) -> timedelta:
    """How far before the earliest game an input fetch must reach."""
    return timedelta(minutes=max((c.minutes for c in grid), default=0.0))


def decision_cutoffs(
    commence_times: Iterable[datetime],
    lead_grid: "float | Iterable[Checkpoint]",
    resolution: timedelta = SNAPSHOT_RESOLUTION,
) -> list[datetime]:
    """The distinct instants a targeted fetch actually needs.

    Games cluster on common start times, so a slate of fifteen games usually
    needs a handful of fetches rather than fifteen: each cutoff is floored to
    the archive's snapshot grid and then deduplicated. This is what makes
    targeting cheaper than a grid fine enough to satisfy the freshness bound.

    With a GRID, the same deduplication does more work, not less: every game
    in a slate shares its 72h/48h/24h cutoffs with the others just as it shared
    the 60-minute one, so N checkpoints cost far less than N times one.

    A bare number is still accepted so a single-checkpoint run reads the same
    as it always did.
    """
    grid = ([Checkpoint(float(lead_grid))]
            if isinstance(lead_grid, (int, float))
            else list(lead_grid))
    step = resolution.total_seconds()
    seen: set[float] = set()
    for start in commence_times:
        for checkpoint in grid:
            cutoff = checkpoint.decision_at(start)
            seen.add(math.floor(cutoff.timestamp() / step) * step)
    return [datetime.fromtimestamp(ts, tz=timezone.utc) for ts in sorted(seen)]


@dataclass
class Stage:
    """One pipeline stage, in its OWN units.

    `diagnostic` marks a stage that describes what a PROVIDER returned rather
    than whether the STUDY got what it needed. A snapshot legitimately carries
    events the study never asked about -- other days, other games, times far
    from any decision cutoff -- and counting those against coverage let 88
    irrelevant event-quotes (78 of them for a day outside the declared window)
    fail a run whose 40 target contracts all resolved. Diagnostic stages are
    reported in full and never gate coverage.
    """

    unit: str                 # "snapshots" | "event-quotes" | "contracts" | "games"
    considered: int = 0
    excluded: int = 0         # eligibility: the study's own choice
    rejected: int = 0         # failures: a gap in what it could see
    diagnostic: bool = False

    @property
    def eligible(self) -> int:
        return max(0, self.considered - self.excluded)

    @property
    def accounting_is_valid(self) -> bool:
        """False when records were rejected against no denominator.

        A stage that lost 100 records while `considered` is 0 has not measured
        a 0% loss -- it has failed to account. Reporting that as 0.0% and
        omitting it from `lossy_stages` is how a run that dropped EVERY
        contract printed `join: 0 considered, 100 lost (0.0%)` and still
        certified coverage complete.
        """
        return not (self.rejected and self.eligible <= 0)

    @property
    def loss_rate(self) -> float | None:
        """None when the accounting is invalid -- never a reassuring zero."""
        if not self.accounting_is_valid:
            return None
        return self.rejected / self.eligible if self.eligible else 0.0


@dataclass
class Ledger:
    """Per-stage denominators and rejection reasons.

    TWO earlier defects, both of which let a badly-degraded run certify itself:

    1. A stage that dropped ten thousand records called `reject()` ONCE, with
       the real figure only inside an example string. Counts are now explicit.
    2. One global loss rate divided odds-event and snapshot failures by
       EXCHANGE-MARKET counts -- different units, so the ratio meant nothing.
       Each stage now carries its own unit and denominator, and coverage fails
       if ANY stage is too lossy rather than if a blended average is.

    Eligibility exclusions (a market outside the study window) are counted
    apart from failures (an unparseable ticker). The first is the study's own
    choice; only the second threatens whether the sample is representative.
    """

    stages: dict[str, Stage] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    rejection_stage: dict[str, str] = field(default_factory=dict)
    eligibility_exclusions: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def stage(self, name: str, unit: str = "records",
              diagnostic: bool = False) -> Stage:
        found = self.stages.get(name)
        if found is None:
            found = self.stages[name] = Stage(unit=unit, diagnostic=diagnostic)
        elif diagnostic:
            found.diagnostic = True
        return found

    def count(self, stage: str, n: int = 1, unit: str = "records",
              diagnostic: bool = False) -> None:
        self.stage(stage, unit, diagnostic).considered += n

    def reject(self, reason: str, example: str = "", count: int = 1,
               stage: str = "contracts", diagnostic: bool = False) -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + count
        self.rejection_stage[reason] = stage
        self.stage(stage, diagnostic=diagnostic).rejected += count
        if example:
            shown = self.examples.setdefault(reason, [])
            if len(shown) < 5:
                shown.append(example)

    def exclude(self, reason: str, count: int = 1, stage: str = "join") -> None:
        self.eligibility_exclusions[reason] = (
            self.eligibility_exclusions.get(reason, 0) + count)
        self.stage(stage).excluded += count

    @property
    def total_rejected(self) -> int:
        return sum(self.rejections.values())

    def rejected_in_stage(self, stage: str) -> int:
        """Rejections filed against ONE stage.

        `total_rejected` sums every stage, including the diagnostic ones, so
        a sentence about contracts that quotes it reports a number from a
        different population. With Kalshi unreachable and zero contracts ever
        enumerated, the empty-universe message read "17 were rejected" -- the
        count of failed SCHEDULE buckets. A specific, credible number produced
        by code that never looked at the thing it names is the same defect as
        `fleet_doctor`'s "2 bars" on a healthy feed.
        """
        return sum(count for reason, count in self.rejections.items()
                   if self.rejection_stage.get(reason) == stage)

    @property
    def total_excluded(self) -> int:
        return sum(self.eligibility_exclusions.values())

    def lossy_stages(self, threshold: float = None) -> list[tuple[str, Stage]]:
        limit = MAX_UNEXPLAINED_LOSS if threshold is None else threshold
        return [(name, s) for name, s in sorted(self.stages.items())
                if not s.diagnostic and s.loss_rate is not None
                and s.eligible and s.loss_rate > limit]

    def unaccounted_stages(self) -> list[tuple[str, Stage]]:
        """Stages that rejected records against no denominator."""
        return [(name, s) for name, s in sorted(self.stages.items())
                if not s.diagnostic and not s.accounting_is_valid]

    def apply_to(self, coverage: Coverage) -> Coverage:
        for name, s in self.unaccounted_stages():
            coverage.fail(
                f"stage {name!r}: {s.rejected:,} {s.unit} rejected against a "
                "denominator of zero -- the loss cannot be measured, which is "
                "not the same as no loss"
            )
        for name, s in self.lossy_stages():
            coverage.fail(
                f"stage {name!r}: {s.loss_rate:.1%} of {s.eligible:,} eligible "
                f"{s.unit} lost to parse/join failures ({s.rejected:,}); the "
                "surviving sample may be selected rather than representative"
            )
        return coverage

    def as_dict(self) -> dict:
        return {
            "stages": {n: {"unit": s.unit, "considered": s.considered,
                           "excluded": s.excluded, "rejected": s.rejected,
                           "loss_rate": (round(s.loss_rate, 6)
                                         if s.loss_rate is not None else None),
                           "accounting_valid": s.accounting_is_valid,
                           "diagnostic": s.diagnostic}
                       for n, s in sorted(self.stages.items())},
            "rejections": {r: {"count": c, "stage": self.rejection_stage.get(r, "?")}
                           for r, c in sorted(self.rejections.items())},
            "eligibility_exclusions": dict(sorted(self.eligibility_exclusions.items())),
            "rejection_examples": {k: v for k, v in sorted(self.examples.items())},
            "lossy_stages": [n for n, _ in self.lossy_stages()],
        }

    def render(self) -> str:
        lines = ["COLLECTION LEDGER"]
        for name, s in sorted(self.stages.items()):
            rate = ("diagnostic" if s.diagnostic
                    else "UNACCOUNTED" if s.loss_rate is None
                    else f"{s.loss_rate:.1%}")
            lines.append(
                f"    {name:26} {s.considered:>8,} {s.unit:<13} "
                f"excluded {s.excluded:>7,}  lost {s.rejected:>7,}  ({rate})")
        if self.eligibility_exclusions:
            lines.append("  excluded by design (not a gap):")
            for reason, n in sorted(self.eligibility_exclusions.items()):
                lines.append(f"    {reason:38} {n:>8,}")
        if self.rejections:
            lines.append("  LOST to parse/join failures:")
            for reason, n in sorted(self.rejections.items(), key=lambda kv: -kv[1]):
                lines.append(
                    f"    {reason:38} {n:>8,}  [{self.rejection_stage.get(reason,'?')}]")
                for ex in self.examples.get(reason, [])[:2]:
                    lines.append(f"        e.g. {ex}")
        lines.append(f"  lossy stages:       {[n for n, _ in self.lossy_stages()] or 'none'}")
        lines.append(f"  UNACCOUNTED stages: "
                     f"{[n for n, _ in self.unaccounted_stages()] or 'none'}")
        return "\n".join(lines)


# --- game x checkpoint coverage ---------------------------------------------
#
# The expected universe is every (contract, checkpoint) pair the enumeration
# says should exist -- established BEFORE any quote is fetched, so it cannot be
# defined by its own successes. A denominator derived from what succeeded
# always reads 100%.
#
# Three outcomes are deliberately kept apart, because they mean opposite things
# for feasibility:
#
#   NOT LISTED     the contract did not exist yet. There was no opportunity to
#                  miss. This is a RESULT -- "you cannot trade this game three
#                  days out" is exactly what a multi-day study is asking -- and
#                  it is an eligibility exclusion, never a coverage failure.
#   NO QUOTE       the contract existed and nobody had quoted it, or the quote
#                  was unusable. Also a result: listed but untradeable.
#   SOURCE FAILURE the archive, the parser or the provider let us down. This is
#                  the only group that threatens whether the sample is
#                  representative.
#
# An UNREADABLE listing time is none of the three and gets its own bucket. It
# is not evidence the market was listed (rule 17).
STATUS_OBSERVED = "observed"
STATUS_NOT_YET_LISTED = "not_yet_listed"
STATUS_LISTING_UNKNOWN = "listing_time_unknown"
# A contract the enumeration found but the join could not match to a sharp
# event. Its cells are NOT "unreported" -- nobody is coming back for them, and
# leaving them blank made a real, explained loss look like a collector that
# skipped rows. The denominator keeps them either way.
STATUS_UNJOINED = "contract_unjoined"

STATUS_GROUPS: dict[str, str] = {
    STATUS_OBSERVED: "observed",
    STATUS_NOT_YET_LISTED: "not_listed",
    STATUS_LISTING_UNKNOWN: "listing_unknown",
    "no_exchange_quote_at_cutoff": "no_quote",
    "exchange_quote_too_old_at_cutoff": "no_quote",
    "no_entry_quote_after_delay": "no_quote",
    "no_sharp_quote_available_at_cutoff": "no_sharp",
    "sharp_quote_too_old_at_cutoff": "no_sharp",
    "exchange_quote_malformed": "source_failure",
    "candles_unavailable": "source_failure",
    "yes_side_unresolvable": "source_failure",
    "cutoff_at_or_after_start": "not_listed",
    STATUS_UNJOINED: "unjoined",
}
# Groups that do NOT indicate a gap in what the study could see.
#
# "unreported" is deliberately NOT a source failure. A cell nobody has resolved
# yet is not a cell the source failed on -- before a run EVERY cell is
# unreported, and folding the two together would make the preflight's failure
# count equal to the whole universe. It is counted separately, and after a run
# a non-zero count means the collector skipped cells, which is its own bug.
BENIGN_GROUPS = frozenset({"observed", "not_listed", "no_quote", "unreported"})


def cell_is_benign(status: str) -> bool:
    """Is this cell outcome a RESULT rather than a gap in what we could see?

    THE ONE CLASSIFIER. The matrix and the collection ledger used to answer
    this separately, and disagreed: the matrix called
    `exchange_quote_too_old_at_cutoff` a result (`no_quote` -- the market was
    listed and nobody had a fresh price on it) while the ledger counted it as a
    parse/join FAILURE against coverage. Two verdicts on one fact, in the two
    places a reader would check (rule 19). Both now read this.
    """
    return STATUS_GROUPS.get(status, "source_failure") in BENIGN_GROUPS


def record_cell_outcome(ledger: "Ledger", status: str, example: str = "",
                        stage: str = "checkpoint_cells", count: int = 1) -> None:
    """File a cell outcome under the bucket its own classification implies.

    A benign outcome is an ELIGIBILITY exclusion -- the contract could not have
    been traded at that moment, which is a fact about the market. Anything else
    is a REJECTION, a gap in what the study could see. `no_sharp` is
    deliberately on the rejection side: the provider not covering a game 48
    hours out is a coverage limit of the feed, not a property of the market,
    and it has to stay loud.
    """
    if cell_is_benign(status):
        ledger.exclude(status, count=count, stage=stage)
    else:
        ledger.reject(status, example, count=count, stage=stage)


@dataclass(frozen=True)
class CheckpointTarget:
    """One cell of the expected universe: this contract, at this lead time."""

    market_ticker: str
    event_ticker: str
    checkpoint: Checkpoint
    start: datetime
    decision_at: datetime


def checkpoint_targets(
    markets: dict[str, dict],
    lead_grid: Iterable[Checkpoint],
    ledger: "Ledger | None" = None,
    resolver: "StartResolver | None" = None,
) -> list[CheckpointTarget]:
    """Every (contract, checkpoint) the enumeration says should exist.

    Derived from exchange enumeration ALONE -- no quote, no join, no fetch.
    That is what makes it a denominator rather than a tally.

    A contract with no readable start cannot have checkpoints at all. The
    caller is expected to have filtered those already, so reaching one here
    means that filter changed -- which is exactly the kind of silent shrinkage
    of a DENOMINATOR that makes coverage measure itself. It is counted, not
    skipped quietly.
    """
    grid = list(lead_grid)
    out: list[CheckpointTarget] = []
    undatable = 0
    for ticker, market in markets.items():
        start = market_start_time(market, resolver)
        if start is None:
            undatable += 1
            continue
        event = kalshi_event_ticker(market) or ticker
        for checkpoint in grid:
            out.append(CheckpointTarget(ticker, event, checkpoint, start,
                                        checkpoint.decision_at(start)))
    if undatable and ledger is not None:
        ledger.reject("no_readable_start_time_at_targeting",
                      f"{undatable} contracts", count=undatable,
                      stage="contracts")
    return out


def listing_status(market: dict, decision_at: datetime) -> str | None:
    """`not_yet_listed`, `listing_time_unknown`, or None when it was listed."""
    opened = market_open_time(market)
    if opened is None:
        return STATUS_LISTING_UNKNOWN
    return STATUS_NOT_YET_LISTED if decision_at < opened else None


@dataclass
class CheckpointMatrix:
    """What happened at each (contract, checkpoint) cell.

    Every target is recorded, including the ones that never became an
    observation -- a matrix that only holds successes cannot answer "how early
    could this have been traded?", which is the whole question.
    """

    statuses: dict[tuple[str, str], str] = field(default_factory=dict)
    checkpoints: list[Checkpoint] = field(default_factory=list)

    def expect(self, targets: Iterable[CheckpointTarget]) -> None:
        seen: dict[str, Checkpoint] = {}
        for t in targets:
            self.statuses.setdefault((t.market_ticker, t.checkpoint.key), "")
            seen.setdefault(t.checkpoint.key, t.checkpoint)
        self.checkpoints = sorted(seen.values(), key=lambda c: -c.minutes)

    def record(self, market_ticker: str, checkpoint: Checkpoint, status: str) -> None:
        self.statuses[(market_ticker, checkpoint.key)] = status

    def record_all(self, market_ticker: str, status: str) -> None:
        """Set one status across every checkpoint of a contract."""
        for checkpoint in self.checkpoints:
            self.record(market_ticker, checkpoint, status)

    def group_of(self, status: str) -> str:
        if not status:
            return "unreported"
        return STATUS_GROUPS.get(status, "source_failure")

    def by_checkpoint(self) -> dict[str, dict[str, int]]:
        """Per checkpoint, a count of each outcome GROUP."""
        out: dict[str, dict[str, int]] = {}
        for (_, key), status in self.statuses.items():
            out.setdefault(key, {})
            group = self.group_of(status)
            out[key][group] = out[key].get(group, 0) + 1
        return out

    def detail_by_checkpoint(self) -> dict[str, dict[str, int]]:
        """Per checkpoint, a count of each raw status -- what to debug from."""
        out: dict[str, dict[str, int]] = {}
        for (_, key), status in self.statuses.items():
            out.setdefault(key, {})
            name = status or "unreported"
            out[key][name] = out[key].get(name, 0) + 1
        return out

    def source_failures(self) -> int:
        """Cells where the archive, parser or provider let us down.

        The only group that threatens whether the sample is representative.
        An unrecognised status counts here rather than being ignored: a new
        failure mode must not land in a benign bucket by default.
        """
        return sum(1 for s in self.statuses.values()
                   if self.group_of(s) not in BENIGN_GROUPS)

    def unreported(self) -> int:
        """Cells no run has resolved. Expected before a run, a bug after one."""
        return sum(1 for s in self.statuses.values() if not s)

    def observed(self) -> int:
        return sum(1 for s in self.statuses.values() if s == STATUS_OBSERVED)

    def render(self) -> str:
        cols = ["observed", "not_listed", "no_quote", "no_sharp",
                "listing_unknown", "unjoined", "source_failure", "unreported"]
        grouped = self.by_checkpoint()
        if not self.checkpoints:
            # A section that prints its header and its explanation over an
            # EMPTY table reads as "nothing went wrong here". It has to say it
            # has nothing to show, or it is confident formatting over an
            # unasked question -- the same shape as reporting "2 bars" for a
            # tuple's arity.
            return ("GAME x CHECKPOINT COVERAGE\n"
                    "    NO CELLS. The expected universe was never built, so "
                    "this section\n"
                    "    is blind rather than clean -- it is not reporting that "
                    "coverage is fine.")
        lines = ["GAME x CHECKPOINT COVERAGE",
                 "    the denominator is the ENUMERATED universe, not the rows that",
                 "    succeeded. 'not_listed' is a RESULT (no contract existed that",
                 "    early), not a gap. 'unjoined' is an explained loss: the",
                 "    contract never matched a sharp event, so its cells were never",
                 "    reachable. 'unreported' after a run means the collector skipped",
                 "    cells, which is a bug.",
                 "    " + f"{'lead':<14}" + "".join(f"{c:>16}" for c in cols)]
        for checkpoint in self.checkpoints:
            counts = grouped.get(checkpoint.key, {})
            lines.append("    " + f"{checkpoint.label:<14}"
                         + "".join(f"{counts.get(c, 0):>16,}" for c in cols))
        return "\n".join(lines)


@dataclass(frozen=True)
class SupportReport:
    """What this code can and cannot do for one sport, before any paid call.

    Every field is read from the code, not assumed. `ready` is deliberately
    narrow: it means the JOIN can work, not that the study will find data --
    listing lead times and historical sharp coverage are separate questions
    that only a live audit answers.
    """

    league: str
    odds_key: str | None
    roster_teams: int
    alias_count: int
    series: str | None
    fee_schedule: bool
    start_source: str | None = None
    start_source_kind: str | None = None
    schedule_provenance: str | None = None

    @property
    def roster_ready(self) -> bool:
        """Team identity resolves: the JOIN can match names to codes."""
        return bool(self.odds_key) and self.roster_teams > 0

    @property
    def schedule_ready(self) -> bool:
        """An ADAPTER exists that can derive a scheduled start.

        Three different questions live around this one flag, and collapsing
        them is how NFL came to read "ready" while producing no observation:

          1. ADAPTER SUPPORT -- this property. Static, read from the code.
          2. RUNTIME SCHEDULE COVERAGE -- how many of a particular run's
             contracts actually resolved a kickoff. Per-run, reported by the
             preflight from `StartResolver.coverage_counts`, and not knowable
             from here.
          3. HISTORICAL PROVENANCE -- whether that kickoff is what was known
             at the decision it dates. `schedule_provenance`, below.

        An adapter that exists and resolves nothing still fails the run, and
        an adapter whose answers are retrospective still cannot certify a
        backtest. Neither is visible in this flag, so neither is implied by it.
        """
        return self.start_source is not None

    @property
    def schedule_is_external(self) -> bool:
        return self.start_source_kind == "external"

    @property
    def point_in_time_capable(self) -> bool:
        """Can a run over this league date its own decisions honestly?

        False for an externally scheduled league: the kickoff was retrieved
        after the fact. Such a run is exploratory by construction, which is a
        limit on what its OUTPUT may claim, not on whether it may run.
        """
        return self.schedule_provenance == "verified_at_decision_time"

    @property
    def ready(self) -> bool:
        """Collectable: identity AND a schedule. Both, or neither is enough.

        These were one flag, and NFL passed it -- 32 teams and a valid odds
        key -- while every one of its contracts failed on `no_readable_start_time`,
        because an NFL event body carries no kickoff. A report that says
        "ready" over a sport that cannot produce a single observation is worse
        than no report.
        """
        return self.roster_ready and self.schedule_ready

    def blockers(self) -> list[str]:
        out: list[str] = []
        if not self.odds_key:
            out.append(f"no odds-provider sport key for {self.league!r} "
                       "(data.odds_history.SPORT_KEYS)")
        if not self.roster_teams:
            out.append(f"no team roster for {self.league!r} "
                       "(core.matcher.ROSTERS) -- every game would fail the "
                       "join as sharp_teams_unresolvable, AFTER paying for "
                       "its snapshots")
        if not self.schedule_ready:
            out.append(f"no scheduled-start source for {self.league!r} "
                       "(core.matcher.START_SOURCES). Real event bodies for "
                       "this league carry a DATE and no kickoff time, so every "
                       "contract fails on no_readable_start_time and the "
                       "checkpoint grid -- which is measured in hours -- has "
                       "nothing to count back from. close_time, "
                       "expected_expiration_time and settlement_ts describe "
                       "the END of the contract and must not be substituted.")
        return out

    def caveats(self) -> list[str]:
        out: list[str] = []
        if self.roster_teams and not self.alias_count:
            out.append(f"{self.league} has NO exchange-code aliases recorded. "
                       "That is an unverified assumption, not evidence there "
                       "are none: MLB needed AZ->ARI, and a missing alias "
                       "shows up as a team contributing no data, not as an "
                       "error. Run the abbreviation audit before budgeting.")
        if self.series and not self.fee_schedule:
            out.append(f"no dated fee schedule for series {self.series!r}; "
                       "the generic Kalshi coefficients would be assumed and "
                       "every return figure would inherit that. The MLB "
                       "schedule halved the taker coefficient.")
        if self.schedule_is_external:
            out.append(
                f"{self.league} kickoffs come from an EXTERNAL schedule "
                f"({SCHEDULE_SOURCE}), retrieved at run time, so "
                "historical_schedule_as_of=unverified. A schedule fetched now "
                "does not establish what a kickoff was believed to be 72h "
                "earlier: a flexed or rescheduled game carries its FINAL time. "
                "Runs over this league are EXPLORATORY: good for an "
                "availability and cost assessment, and they cannot certify a "
                "point-in-time backtest or a live strategy. A prospective or "
                "holdout run needs schedule snapshots recorded BEFORE the "
                "decisions they date.")
            out.append(
                f"{self.league} adapter support is not runtime coverage. This "
                "says a kickoff CAN be derived, not that one was: a run's own "
                "resolved/unresolved counts are what say whether the schedule "
                "actually covered its contracts, and an empty universe still "
                "fails coverage.")
        return out


def support_report(league: str, series: str | None = None) -> SupportReport:
    """Read this sport's support out of the code. No network, no assumptions."""
    from core import fees
    from data.odds_history import SPORT_KEYS
    key = (league or "").upper()
    return SupportReport(
        league=key,
        odds_key=SPORT_KEYS.get(key),
        roster_teams=len(ROSTERS.get(key, {})),
        alias_count=len(EXCHANGE_CODE_ALIASES.get(key, {})),
        series=series,
        fee_schedule=bool(series and fees.series_schedule(series)),
        start_source=start_source(key),
        start_source_kind=start_source_kind(key),
        schedule_provenance=start_source_provenance(key),
    )


def in_study_window(market: dict, start: datetime, end: datetime,
                    buffer: timedelta = timedelta(days=1)) -> bool | None:
    """Whether a settled market belongs to the declared window.

    Uses SETTLEMENT time as a proxy for game date, because no verified
    scheduled-start key exists yet (see VERIFIED_START_KEYS). The buffer covers
    a late game settling after midnight UTC. Returns None when settlement is
    unreadable, so the caller counts it rather than assuming either way.

    Without this filter every settled market in a series' whole history was
    joined against odds fetched for the requested dates only, so out-of-window
    markets became join FAILURES and swamped the denominator: a fully collected
    one-day study could report massive unexplained loss.
    """
    settled = settlement_time(market)
    if settled is None:
        return None
    return start - buffer <= settled <= end + buffer


# THE SCHEDULED START IS IN THE EVENT TICKER. A real event body is a
# fixed-width date and time followed by the teams (26SEP152140MIAAZ), and the
# 11-character prefix is unambiguous even though the team tail is not. Earlier
# versions searched for a start FIELD, invented seven key names that no real
# payload carries, and rejected every real market while the fixtures agreed
# with the invention.
#
# The timezone is a DECLARED ASSUMPTION (core.matcher.EVENT_BODY_TIMEZONE) and
# is cross-checked per game against the sharp feed's own `commence_time`. The
# ledger records agreement and disagreement counts, so a wrong assumption
# surfaces as systematic disagreement rather than as silently shifted times.
#
# A payload FIELD remains preferred if one is ever verified; these stay empty
# until a real response confirms one.
VERIFIED_START_KEYS: tuple[str, ...] = ()
VERIFIED_START_KEYS_ISO: tuple[str, ...] = ()


def market_start_time(market: dict,
                      resolver: "StartResolver | None" = None) -> datetime | None:
    """Scheduled start: a verified payload field, the ticker, or the resolver.

    Deliberately does NOT fall back to `open_time`, `close_time` or an
    expiration: those are contract lifecycle facts, not first pitch.

    `resolver`, when supplied, is AUTHORITATIVE -- it is the one mapping the
    whole run shares (see `StartResolver`). When it is absent this falls back
    to the ticker, which is right for MLB and yields None for a league whose
    body carries no time. That default is safe in the direction that matters:
    forgetting to thread the resolver costs a loud `no_readable_start_time`
    rejection, never a plausible wrong kickoff.
    """
    if resolver is not None:
        return resolver.start(market)
    for key in VERIFIED_START_KEYS:
        value = market.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
    for key in VERIFIED_START_KEYS_ISO:
        value = market.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    event = kalshi_event_ticker(market)
    return parse_event_body_start(event) if event else None


@dataclass
class StartResolver:
    """THE mapping from contract to kickoff for one run. Built once, reused.

    Targeting, eligibility, collection and the join must all agree about when
    a game started, or the study has no single answer to the question its
    whole lead-time grid is measured against. So the resolution happens once,
    up front, and every later consumer reads the SAME memo -- rather than each
    site re-deriving it and drifting (the fleet's rule 26, pointed at a study).

    Two sources, kept apart on purpose:

      * `event_ticker` (MLB): the exchange published the time on the contract.
        Available at decision time, so it dates a decision honestly.
      * `external_schedule` (NFL): a schedule retrieved at run time. It
        supplies the kickoff the ticker lacks, and it CANNOT say what that
        kickoff was believed to be earlier. Everything derived from it is
        exploratory; `provenance` carries that label into the manifest.
    """

    league: str
    schedule: ScheduleSnapshot | None = None
    _by_event: dict[str, StartResolution] = field(default_factory=dict)
    _contracts_per_event: dict[str, int] = field(default_factory=dict)
    _primed: bool = False

    @property
    def source(self) -> str | None:
        return start_source(self.league)

    @property
    def kind(self) -> str | None:
        return start_source_kind(self.league)

    @property
    def provenance(self) -> str | None:
        return start_source_provenance(self.league)

    @property
    def needs_schedule(self) -> bool:
        return self.source == START_SOURCE_EXTERNAL

    def prime(self, markets: dict[str, dict]) -> None:
        """Resolve every event's kickoff ONCE, before eligibility is applied.

        An external schedule is matched on the event's MATCHUP, which is the
        union of its contracts' YES suffixes -- so it needs every contract of
        an event at once, not one at a time. That is why this is a priming
        step rather than a lazy per-market lookup, and why the result is a
        memo the rest of the run reads instead of recomputing.
        """
        self._primed = True
        if not self.needs_schedule:
            return
        events: dict[str, list[str]] = {}
        for ticker, market in markets.items():
            event = kalshi_event_ticker(market) or ticker
            events.setdefault(event, []).append(ticker)
        for event, tickers in events.items():
            self._by_event[event] = self._resolve_event(event, tickers)
            self._contracts_per_event[event] = len(tickers)

    def _resolve_event(self, event: str, tickers: list[str]) -> StartResolution:
        if self.schedule is None:
            return StartResolution(
                None, None, FAIL_NO_SNAPSHOT,
                f"{self.league} kickoffs come from {SCHEDULE_SOURCE} and no "
                "schedule snapshot was supplied to this run")
        matchup = exchange_matchup(tickers, self.league)
        if len(matchup) < 2:
            return StartResolution(
                None, None, "event_participants_unresolvable",
                f"{event} yielded {sorted(matchup)} from its YES suffixes")

        # A CODE WE DO NOT KNOW IS OUR FAULT, NOT THE SCHEDULE PROVIDER'S.
        # Kalshi spells Jacksonville JAC; the roster and ESPN both say JAX. With
        # no alias the matchup reads {CLE, JAC}, nothing in the schedule matches
        # it, and the run reported `schedule_no_event_for_matchup` --
        # "CLE/JAC ... is not in the retrieved schedule" -- on a day when the
        # schedule carried that exact game as CLE/JAX. A diagnosis pointing at
        # the wrong component sends the next person to audit ESPN coverage for
        # a one-line alias gap.
        #
        # `unknown_exchange_codes` already answers this, and `join_markets`
        # already calls it -- but the JOIN never sees this contract, because
        # resolution dropped it first. The check has to run where the record
        # dies, not downstream of it. Naming the codes means ONE run enumerates
        # the whole alias gap rather than one code per run (the same discipline
        # the join's own message uses).
        unknown = unknown_exchange_codes(sorted(matchup), self.league)
        if unknown:
            return StartResolution(
                None, None, f"unknown_exchange_code:{','.join(unknown)}",
                f"{event}: {'/'.join(unknown)} is not in the {self.league} "
                "roster and has no alias in EXCHANGE_CODE_ALIASES, so no "
                "schedule entry can match it. This is a gap in our mapping, "
                "not a missing game")

        return self.schedule.resolve(matchup, parse_event_body_date(event))

    def resolution(self, market: dict) -> StartResolution:
        """The full outcome -- kickoff AND reason -- for one contract."""
        if not self.needs_schedule:
            start = market_start_time(market)
            if start is None:
                return StartResolution(None, None, "no_readable_start_time",
                                       "the event body carries no kickoff")
            return StartResolution(start, None, None)
        if not self._primed:
            return StartResolution(
                None, None, "schedule_resolver_not_primed",
                "prime(markets) must run before any kickoff is read")
        event = kalshi_event_ticker(market) or str(market.get("ticker", ""))
        return self._by_event.get(event, StartResolution(
            None, None, "schedule_event_not_primed", event))

    def start(self, market: dict) -> datetime | None:
        return self.resolution(market).kickoff

    def failure_counts(self, per: str = "events") -> dict[str, int]:
        """Unresolved events by reason, counted in EVENTS or in CONTRACTS.

        The unit matters because the caller files these against the
        `contracts` stage, whose denominator is contracts. Counting events
        there under-reports by the number of contracts per event -- a
        two-way market is two contracts -- and a run where nothing survived
        reported "50.0% of 4 eligible contracts lost". A coverage figure
        that flatters itself is the failure this ledger exists to prevent.
        """
        out: dict[str, int] = {}
        for event, resolution in self._by_event.items():
            if resolution.resolved or not resolution.reason:
                continue
            weight = (self._contracts_per_event.get(event, 1)
                      if per == "contracts" else 1)
            out[resolution.reason] = out.get(resolution.reason, 0) + weight
        return dict(sorted(out.items()))

    def coverage_counts(self) -> tuple[int, int]:
        """(events with a kickoff, events without). RUNTIME coverage.

        A different fact from "this league has an adapter", which is static.
        An adapter that exists and resolves nothing is exactly the shape that
        reported NFL ready while producing no observation at all.
        """
        resolved = sum(1 for r in self._by_event.values() if r.resolved)
        return resolved, len(self._by_event) - resolved

    def manifest(self) -> dict:
        resolved, unresolved = self.coverage_counts()
        out = {
            "league": self.league,
            "start_source": self.source,
            "start_source_kind": self.kind,
            "historical_schedule_as_of": self.provenance,
            "events_resolved": resolved,
            "events_unresolved": unresolved,
            "resolution_failures": self.failure_counts(),
            # The link from an observation back to the schedule entry that
            # dated it. Observations carry the Kalshi event ticker; the
            # schedule block is keyed by provider event id. Without this map
            # the two halves of the provenance cannot be joined after the
            # fact, which is the whole point of recording them.
            "resolutions": {
                event: {
                    "provider_event_id": (resolution.event.provider_event_id
                                          if resolution.event else None),
                    "kickoff": (resolution.kickoff.isoformat()
                                if resolution.kickoff else None),
                    "ticker_day_offset": resolution.day_offset,
                    "failure": resolution.reason,
                }
                for event, resolution in sorted(self._by_event.items())
            },
        }
        if self.schedule is not None:
            out["schedule"] = self.schedule.manifest()
        return out


def start_metadata_available() -> bool:
    return True      # via the event body; see market_start_time


def exchange_matchup(tickers: list[str], league: str) -> frozenset[str]:
    """The participants of one exchange event, from its markets' YES suffixes.

    Kalshi lists one contract per team, so the union of an event's YES
    participants IS its matchup -- derived from data that parses reliably,
    without splitting the concatenated `MILBAL` in the event body, which has no
    unambiguous reading.
    """
    out: set[str] = set()
    for ticker in tickers:
        parsed = parse_kalshi_game_ticker(ticker)
        if parsed:
            out.add(normalise_exchange_code(parsed["yes_participant"], league))
    return frozenset(out)


def sharp_matchup(quote: SharpQuote, league: str) -> frozenset[str] | None:
    """The participants of one sharp event, or None if either side won't resolve."""
    home = resolve_team(quote.home_name, league)
    away = resolve_team(quote.away_name, league)
    if not home.resolved or not away.resolved:
        return None
    return frozenset({home.abbreviation, away.abbreviation})


@dataclass(frozen=True)
class JoinedMarket:
    """One exchange contract matched to one sharp event, identity preserved."""

    market_ticker: str
    event_ticker: str
    yes_participant: str
    start: datetime
    outcome: int
    provider_event_id: str
    settled_at: datetime | None
    start_verified: bool = False


def yes_side(quote: SharpQuote, yes_participant: str,
             league: str) -> str | None:
    """"home" or "away": which of the sharp event's teams this contract pays on.

    THE orientation decision, in one place. `orient_probability` reads it to
    pick a de-vigged probability and the reaction collector reads it to
    write `yes_is_home` into a replay bundle; two derivations of the same
    fact would eventually disagree, and a contract oriented the wrong way is
    a silently INVERTED signal (rule 19).

    None when the YES code matches neither team, never a guess.
    """
    home = resolve_team(quote.home_name, league)
    away = resolve_team(quote.away_name, league)
    # The YES side is an EXCHANGE code; compare it canonically or every
    # Arizona contract silently fails orientation as well as matching.
    target = normalise_exchange_code(yes_participant, league)
    if home.resolved and home.abbreviation == target:
        return "home"
    if away.resolved and away.abbreviation == target:
        return "away"
    return None


def orient_probability(
    quote: SharpQuote, yes_participant: str, league: str, method: str = "shin"
) -> float | None:
    """The de-vigged probability for the participant this contract pays on.

    Returns None when the YES side cannot be matched to either team, because a
    probability attached to the wrong side is an inverted signal, which is
    worse than a missing one.
    """
    try:
        result = devig_american([quote.home_price, quote.away_price])
    except DevigError:
        return None
    fair = result.shin if method == "shin" else result.multiplicative
    side = yes_side(quote, yes_participant, league)
    if side == "home":
        return fair[0]
    if side == "away":
        return fair[1]
    return None


def _local_date(when: datetime, tz_name: str = EVENT_BODY_TIMEZONE):
    """Calendar date in the schedule's own timezone.

    A UTC date splits a night game from its own start: 2026-09-15 21:40 ET is
    2026-09-16 01:40 UTC, so a UTC-keyed index would file the two sources of
    one game under different days.
    """
    try:
        return when.astimezone(ZoneInfo(tz_name)).date()
    except Exception:
        return when.date()


def join_markets(
    markets: dict[str, dict],
    quotes_by_event: dict[str, list[SharpQuote]],
    league: str,
    ledger: Ledger,
    resolver: "StartResolver | None" = None,
) -> list[JoinedMarket]:
    """Match settled contracts to sharp events on (matchup, DATE), then time.

    Three defects this replaces, each seen in a live run:

    1. Candidates were indexed by matchup ALONE, so a team pair playing a
       normal series on consecutive days produced three candidates and the
       uniqueness check called every one an unresolvable doubleheader. 54
       contracts were dropped that way (DET-TOR, NYY-MIN). MLB teams playing
       each other on successive dates is ordinary, not ambiguous.
    2. Exchange ticker suffixes were compared raw against canonical
       abbreviations, so every Arizona game missed (`AZ` vs `ARI`).
    3. Start time was looked for in a payload field that does not exist.

    The date separates rematches; the time separates a genuine same-day
    doubleheader. A start derived from the ticker is cross-checked against the
    sharp feed's own `commence_time` and the agreement counted, so the
    timezone assumption is validated by data rather than asserted.
    """
    events: dict[str, list[str]] = {}
    for ticker, market in markets.items():
        event = kalshi_event_ticker(market)
        if event is None:
            ledger.reject("ticker_unparseable", ticker, stage="contracts")
            continue
        events.setdefault(event, []).append(ticker)

    # Index sharp events by (matchup, local date).
    by_key: dict[tuple, list[tuple[str, SharpQuote]]] = {}
    unresolved = 0
    for event_id, quotes in quotes_by_event.items():
        if not quotes:
            continue
        matchup = sharp_matchup(quotes[0], league)
        if matchup is None:
            unresolved += 1
            continue
        key = (matchup, _local_date(quotes[0].commence_time))
        by_key.setdefault(key, []).append((event_id, quotes[0]))
    if unresolved:
        # A PROVIDER event whose team names this roster does not know. The feed
        # returns events the study never asked about -- other leagues, other
        # days -- so this is a diagnostic, not a gap in the study's own
        # universe. It previously rejected into a stage name nothing counted
        # any more, producing a zero denominator that failed coverage on a run
        # whose every target contract resolved.
        ledger.reject("sharp_teams_unresolvable", f"{unresolved} events",
                      count=unresolved, stage="provider_events",
                      diagnostic=True)

    ledger.stage("provider_events", unit="event-quotes", diagnostic=True)

    joined: list[JoinedMarket] = []
    agreed = disagreed = 0
    for event_ticker, tickers in events.items():
        matchup = exchange_matchup(tickers, league)
        if not matchup:
            ledger.reject("event_participants_unresolvable", event_ticker,
                          count=len(tickers), stage="contracts")
            continue

        unknown = unknown_exchange_codes(
            [parse_kalshi_game_ticker(x)["yes_participant"] for x in tickers
             if parse_kalshi_game_ticker(x)], league)
        if unknown:
            # Name the code so ONE run enumerates the whole alias gap.
            ledger.reject(f"unknown_exchange_code:{','.join(unknown)}",
                          event_ticker, count=len(tickers), stage="contracts")
            continue

        # THE SAME resolved mapping the eligibility filter used. Re-deriving
        # it here would let the join date a game differently from the run that
        # selected it, which is the one disagreement a lead-time study cannot
        # survive.
        resolution = (resolver.resolution(markets[tickers[0]])
                      if resolver is not None else None)
        start = (resolution.kickoff if resolution is not None
                 else market_start_time(markets[tickers[0]]))
        if start is None:
            reason = (resolution.reason if resolution is not None
                      and resolution.reason else "no_readable_start_time")
            detail = (f"{event_ticker} {resolution.detail}"
                      if resolution is not None and resolution.detail
                      else event_ticker)
            ledger.reject(reason, detail, count=len(tickers), stage="contracts")
            continue

        key = (matchup, _local_date(start))
        candidates = by_key.get(key, [])
        if not candidates and len(matchup) < 2:
            candidates = [c for (m, d), cs in by_key.items()
                          if matchup <= m and d == _local_date(start) for c in cs]

        if not candidates:
            ledger.reject("no_sharp_event_for_matchup_and_date",
                          f"{event_ticker} {sorted(matchup)} {_local_date(start)}",
                          count=len(tickers), stage="contracts")
            continue

        if len(candidates) > 1:
            # A genuine same-day doubleheader: the time separates them.
            timed = [c for c in candidates
                     if abs(c[1].commence_time - start) <= START_AGREEMENT]
            if len(timed) != 1:
                ledger.reject("same_day_doubleheader_unresolved",
                              f"{event_ticker} matched {len(timed)} on time",
                              count=len(tickers), stage="contracts")
                continue
            candidates = timed

        event_id, quote = candidates[0]
        drift = abs(quote.commence_time - start)
        if drift <= START_AGREEMENT:
            agreed += 1
        else:
            # The ticker's timezone assumption and the feed disagree for this
            # game. Reject it rather than silently preferring one.
            disagreed += 1
            ledger.reject("start_times_disagree",
                          f"{event_ticker} drift={drift}",
                          count=len(tickers), stage="contracts")
            continue

        for ticker in tickers:
            parsed = parse_kalshi_game_ticker(ticker)
            outcome = settlement_outcome(markets[ticker])
            if outcome is None:
                ledger.reject("no_readable_settlement", ticker, stage="contracts")
                continue
            joined.append(JoinedMarket(
                market_ticker=ticker,
                event_ticker=event_ticker,
                yes_participant=normalise_exchange_code(
                    parsed["yes_participant"], league),
                start=start,
                outcome=outcome,
                provider_event_id=event_id,
                settled_at=settlement_time(markets[ticker]),
                start_verified=True,
            ))

    # The timezone assumption, validated by data.
    ledger.count("start_time_crosscheck", agreed + disagreed, unit="games")
    if disagreed:
        ledger.stage("start_time_crosscheck").rejected += disagreed
    ledger.count("joined_contracts", len(joined), unit="contracts")
    return joined


def observation_with_status(
    joined: JoinedMarket,
    quotes: list[SharpQuote],
    candles: list,
    decision_at: datetime,
    league: str,
    ledger: Ledger,
    method: str = "shin",
    max_lag: timedelta = MAX_SOURCE_LAG,
    max_quote_age: float = MAX_QUOTE_AGE_SECONDS,
    reject_stage: str = "observation_build",
    checkpoint: "Checkpoint | None" = None,
    entry_delay: timedelta = timedelta(0),
    entry_tolerance: timedelta = ENTRY_TOLERANCE,
) -> tuple[Observation | None, str]:
    """Build one observation, and say what happened either way.

    Returns `(observation, status)`. The status is `STATUS_OBSERVED` or the
    exact reason this cell failed, which is what the game x checkpoint matrix
    records -- a matrix that only knows "no row here" cannot tell a contract
    nobody quoted from one the archive lost.

    `observation_at_cutoff` is the same function viewed as just the
    observation, so the two can never disagree about why a cell is empty.

    Both sides take the latest record published at or before `decision_at`, and
    both must be within `max_lag` of it. That is what makes the comparison a
    like-for-like forecast rather than a race between two clocks.
    """
    # AVAILABILITY, not just publication. An earlier version required only
    # `last_update <= decision_at`, which accepted a quote from a snapshot
    # CAPTURED AFTER the decision: a 16:07 snapshot carrying a 16:04 update
    # stamp was used for a 16:05 decision. That price was not demonstrated
    # available through this feed at 16:05, and on a delayed retail feed the
    # difference is exactly the effect being measured.
    #
    # Both orderings must hold: last_update <= snapshot <= decision_at.
    usable_quotes = [
        q for q in quotes
        if q.last_update is not None
        and q.last_update <= q.snapshot <= decision_at
    ]
    if not usable_quotes:
        record_cell_outcome(ledger, "no_sharp_quote_available_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None, "no_sharp_quote_available_at_cutoff"

    quote = max(usable_quotes, key=lambda q: q.last_update)

    # Age is measured at the DECISION timestamp, not at capture. One limit
    # governs both, rather than `--max-quote-age` applying at snapshot time
    # while a separate constant applied at the decision.
    age_at_decision = (decision_at - quote.last_update).total_seconds()
    if age_at_decision > max_quote_age:
        record_cell_outcome(ledger, "sharp_quote_too_old_at_cutoff",
                      f"{joined.market_ticker} {age_at_decision:.0f}s", stage=reject_stage)
        return None, "sharp_quote_too_old_at_cutoff"

    usable_candles = [c for c in candles if c.mid is not None and c.ts <= decision_at]
    if not usable_candles:
        record_cell_outcome(ledger, "no_exchange_quote_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None, "no_exchange_quote_at_cutoff"
    candle = max(usable_candles, key=lambda c: c.ts)
    if decision_at - candle.ts > max_lag:
        record_cell_outcome(ledger, "exchange_quote_too_old_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None, "exchange_quote_too_old_at_cutoff"
    if candle.has_malformed_price:
        record_cell_outcome(ledger, "exchange_quote_malformed", joined.market_ticker, stage=reject_stage)
        return None, "exchange_quote_malformed"

    p_sharp = orient_probability(quote, joined.yes_participant, league, method)
    if p_sharp is None:
        record_cell_outcome(ledger, "yes_side_unresolvable", f"{joined.market_ticker} YES={joined.yes_participant}", stage=reject_stage)
        return None, "yes_side_unresolvable"

    lead_minutes = (joined.start - decision_at).total_seconds() / 60.0
    if lead_minutes <= 0:
        record_cell_outcome(ledger, "cutoff_at_or_after_start", joined.market_ticker, stage=reject_stage)
        return None, "cutoff_at_or_after_start"

    # THE ENTRY IS NOT THE OBSERVATION, and it does not get to change the
    # DECISION. Acting on a signal takes time, and the price you get is the one
    # available once you have acted -- but the trigger and the side were fixed
    # at the decision, on the book visible then. The execution book is recorded
    # separately (`entry_bid`/`entry_ask`) and never reaches the screen.
    #
    # At ZERO delay the entry IS the observation candle, preserving the
    # existing baseline bit for bit. That is the instantaneous bound and it is
    # labelled as one, not as a neutral default: it assumes you trade at the
    # last price you saw, the moment you saw it.
    entry_candle = candle
    entry_at = candle.ts
    if entry_delay > timedelta(0):
        target = decision_at + entry_delay
        available = [c for c in candles
                     if c.mid is not None and not c.has_malformed_price
                     and target <= c.ts < joined.start
                     and c.ts - target <= entry_tolerance]
        if not available:
            # A missing delayed quote is NOT a fill at the observation price.
            # Substituting the price you saw for the price you could have got
            # is the whole error this parameter exists to measure.
            record_cell_outcome(ledger, "no_entry_quote_after_delay",
                          f"{joined.market_ticker} +{entry_delay.total_seconds() / 60:.0f}m",
                          stage=reject_stage)
            return None, "no_entry_quote_after_delay"
        entry_candle = min(available, key=lambda c: c.ts)
        entry_at = entry_candle.ts

    return Observation(
        game_id=joined.event_ticker,
        market_id=joined.market_ticker,
        decision_at=decision_at,
        minutes_to_start=lead_minutes,
        p_sharp=p_sharp,
        p_exchange=candle.mid,
        outcome=joined.outcome,
        yes_participant=joined.yes_participant,
        exchange_bid=candle.bid_close,
        exchange_ask=candle.ask_close,
        entry_bid=entry_candle.bid_close,
        entry_ask=entry_candle.ask_close,
        sharp_at=quote.last_update,
        sharp_snapshot_at=quote.snapshot,
        exchange_at=candle.ts,
        devig_method=method,
        checkpoint_minutes=checkpoint.minutes if checkpoint else None,
        entry_at=entry_at,
        entry_delay_minutes=entry_delay.total_seconds() / 60.0,
    ), STATUS_OBSERVED


def observation_at_cutoff(*args, **kwargs) -> Observation | None:
    """`observation_with_status`, keeping only the observation.

    One implementation, two views. A second copy of this logic that returned
    the status its own way would eventually disagree with the one that built
    the row, in exactly the run where the matrix was the thing you were
    reading (rule 26).
    """
    return observation_with_status(*args, **kwargs)[0]
