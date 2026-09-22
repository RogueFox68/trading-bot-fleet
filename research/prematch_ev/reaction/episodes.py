"""The episode ledger: every detected move accounted for, in its own unit.

WHY THIS IS MOSTLY COUNTING
---------------------------
`analysis.scoring.select_entries` already turns repeated looks at one game
into at most one bet, chronologically, under the full eligibility rule. It
already clusters on `game_id`, which is also what makes the two contracts of
one game mutually exclusive. None of that is rewritten here; this module
reports its diagnostics rather than recomputing them (rule 26).

What is new is the accounting, and the accounting is where this study has
twice been wrong in the same way.

EVERY COUNT CARRIES ITS UNIT, BECAUSE THE UNITS GENUINELY DIFFER
----------------------------------------------------------------
One book move on one stream, on a game with two contracts, is:

    1 trigger          (a stream moved once)
    2 screened rows    (each contract priced against it)
    1 selected entry   (one bet per game, at most)

Three different numbers, all correct, none interchangeable. Reporting any of
them under another's name is the defect that made an earlier run print
`50.0% of 4 eligible contracts lost` when resolution failures were counted per
EVENT against a stage denominated in CONTRACTS -- a specific, credible
percentage measuring the wrong population. So `Unit` is not decoration:
`StageCount` cannot be built without one, and the renderer prints it.

A STAGE'S BREAKDOWN MUST SUM TO ITS TOTAL
-----------------------------------------
`StageCount.reconciles` checks it, and the ledger refuses to render a stage
that does not. A breakdown that silently omits a category is how a loss
becomes invisible: the total stays right, the reasons stop adding up, and
nothing says so.

THE HOLDOUT IS NOT A LABEL YOU CHOOSE
-------------------------------------
September 1-16 2026 is DEVELOPMENT data. Every threshold in this study was
declared against it, the checkpoint result was measured on it, and six
rounds of defects were found using it. A run over that window is exploratory
however it is labelled, and labelling it a holdout would not make it one --
it would just make the next reader believe a number that has been fitted.

So `HoldoutViolation` RAISES. Not a warning field, not a note: the same
treatment as `FutureDataError`, for the same reason. A caller that declares
`DataRole.HOLDOUT` over a window overlapping development gets an exception,
and `holdout_available` is False until a window outside that range is
actually collected -- which, as of this commit, none is.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analysis.scoring import (                                   # noqa: E402
    Eligibility, EntryPolicy, Observation, SelectedEntry,
    SelectionDiagnostics, select_entries,
)
from .detector import DetectorResult, MovePolicy, MoveTrigger     # noqa: E402
from .measure import (                                           # noqa: E402
    Ordering, Reaction, ReactionOutcome, ReactionPolicy,
)
from .screen import ScreenedReaction, ScreenResult               # noqa: E402

# DEVELOPMENT DATA. Declared here, as a constant, because it is a fact about
# this study's history rather than a parameter: every threshold was set
# against these dates and six rounds of defects were found in them. Widening
# it is safe; narrowing it is how a fitted window gets called a holdout.
DEVELOPMENT_WINDOW = (date(2026, 9, 1), date(2026, 9, 16))

DEVELOPMENT_NOTE = (
    "2026-09-01..2026-09-16 is DEVELOPMENT data: every declared threshold was "
    "set against it and the checkpoint result was measured on it. A run "
    "overlapping it is EXPLORATORY whatever it is labelled.")


class Unit(str, Enum):
    """What a count counts. Required on every stage, never inferred."""

    GAMES = "games"
    STREAMS = "streams"                      # provider+book+event+market+orientation
    OBSERVATIONS = "provider_observations"   # envelopes fed to the detector
    OUTCOMES = "detector_outcomes"           # one per observation + one per declared gap
    TRIGGERS = "triggers"                    # detected book moves
    REACTIONS = "reactions"                  # one per trigger
    SCREENED = "screened_contract_rows"      # one per (trigger, contract)
    ENTRIES = "selected_entries"             # at most one per game


class DataRole(str, Enum):
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"
    UNDECLARED = "undeclared"


class HoldoutViolation(RuntimeError):
    """A run over development data was declared a holdout.

    Raised, never warned. A mislabelled holdout does not corrupt one figure;
    it corrupts how every figure in the run is read, and it does so silently
    and permanently once the number is quoted.
    """


class Feasibility(str, Enum):
    """The pilot's verdict. Four values, and three of them are not "no"."""

    CONTINUE = "continue"
    STOP = "stop"
    INSUFFICIENT = "insufficient_observable_events"
    UNREACHABLE = "rule_unreachable_against_policy"


@dataclass(frozen=True)
class FeasibilityRule:
    """The pilot stop rule, DECLARED BEFORE ANY DATA AND CHECKED.

    WHAT IT ASKS, AND WHAT IT DELIBERATELY DOES NOT
    ------------------------------------------------
    It asks whether these two sources can ESTABLISH AN ORDERING often enough
    to be worth funding: of the reactions whose brackets actually order, how
    many put the book first.

    It does NOT ask whether lags are long. An earlier version did, and that
    was a different thesis substituted for the one being tested: the study is
    about catching a sharp move days before kickoff, and the exchange may
    follow in seconds. Horizon and response speed are independent, and a rule
    that rewards slow exchanges answers neither.

    WHY A REACHABILITY CHECK EXISTS
    -------------------------------
    The rule it replaces required a lag interval starting beyond 1,800s from
    a policy whose ceiling is 1,740s. Nothing could satisfy it, and nothing
    said so -- `--policy` printed `max_wait_seconds: 1800` and the proposal
    quoted 1,800s, and the two numbers read as agreement. So any lag
    threshold is now checked against `max_reportable_lag_seconds` and an
    impossible rule is a REFUSAL, not a quiet zero.

    WHY AN INSUFFICIENCY FLOOR EXISTS
    ---------------------------------
    A fraction over three reactions is not evidence. Sparse events, censored
    responses and blind intervals all shrink the denominator, and a rule with
    no floor turns any of them into a confident "stop".
    """

    min_determinate: int = 20
    min_book_led_fraction: float = 1.0 / 3.0
    #: Optional. None is the declared pilot value: ordering is the question,
    #: not duration. Present so that a lag threshold, if one is ever wanted,
    #: cannot be declared beyond what the policy can report.
    min_lag_seconds: float | None = None
    label: str = "reaction-feasibility-v1"

    def unreachable_against(self, policy: ReactionPolicy) -> str | None:
        """Why no run under `policy` could satisfy this rule, or None."""
        if self.min_lag_seconds is None:
            return None
        ceiling = policy.max_reportable_lag_seconds
        if self.min_lag_seconds >= ceiling:
            return (f"the rule needs a lag interval starting beyond "
                    f"{self.min_lag_seconds:,.0f}s, but this policy can "
                    f"never report more than {ceiling:,.0f}s "
                    f"(max_wait {policy.max_wait.total_seconds():,.0f}s "
                    f"minus one {policy.candle_period.total_seconds():,.0f}s "
                    f"candle period). No measured reaction could satisfy it")
        return None

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "min_determinate_reactions": self.min_determinate,
            "min_book_led_fraction": self.min_book_led_fraction,
            "min_lag_seconds": self.min_lag_seconds,
            "tuned_on_outcomes": False,
            "note": ("ordering, not duration: the exchange may follow in "
                     "seconds and that is still a book-led reaction"),
        }


@dataclass(frozen=True)
class FeasibilityVerdict:
    """The rule's answer, with every count that did NOT enter it."""

    verdict: Feasibility
    rule: FeasibilityRule
    determinate: int = 0
    book_led: int = 0
    kalshi_led: int = 0
    indeterminate: int = 0
    unknown_ordering: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    detail: str = ""

    @property
    def book_led_fraction(self) -> float | None:
        """None, not zero, when nothing ordered. An empty denominator has no
        fraction, and reporting 0.0 would read as a measured absence."""
        if not self.determinate:
            return None
        return self.book_led / self.determinate

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "rule": self.rule.as_dict(),
            "determinate_reactions": self.determinate,
            "book_led": self.book_led,
            "kalshi_led": self.kalshi_led,
            "book_led_fraction": self.book_led_fraction,
            "excluded_from_the_fraction": {
                "indeterminate_ordering": self.indeterminate,
                "unknown_ordering": self.unknown_ordering,
                "by_outcome": dict(sorted(self.outcomes.items())),
            },
            "detail": self.detail,
            "note": ("censored, blind and indeterminate reactions are "
                     "reported beside the fraction and never inside it"),
        }

    def render(self) -> str:
        lines = ["FEASIBILITY RULE (declared before any data)", ""]
        lines.append(f"    verdict                     {self.verdict.value}")
        fraction = self.book_led_fraction
        shown = "n/a" if fraction is None else f"{fraction:.0%}"
        lines.append(f"    book-led share              {shown}"
                     f"   ({self.book_led:,} of {self.determinate:,} that "
                     f"ordered)")
        lines.append(f"    needs                       "
                     f"{self.rule.min_book_led_fraction:.0%} of at least "
                     f"{self.rule.min_determinate:,}")
        lines.append("")
        lines.append("    NOT in that denominator (reported, never folded in):")
        lines.append(f"        indeterminate ordering  "
                     f"{self.indeterminate:>7,}")
        lines.append(f"        unknown ordering        "
                     f"{self.unknown_ordering:>7,}")
        for name, count in sorted(self.outcomes.items()):
            lines.append(f"        {name:<23} {count:>7,}")
        if self.detail:
            lines.append("")
            for chunk in _wrap_detail(self.detail):
                lines.append(f"    {chunk}")
        return "\n".join(lines)


def _wrap_detail(text: str, width: int = 66) -> list[str]:
    words, out, line = text.split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def judge_feasibility(reactions: Sequence[Reaction],
                      rule: FeasibilityRule | None = None,
                      policy: ReactionPolicy | None = None
                      ) -> FeasibilityVerdict:
    """Evaluate the declared stop rule. Refuses an unreachable one."""
    rule = rule or FeasibilityRule()
    policy = policy or ReactionPolicy()
    outcomes: dict[str, int] = {}
    for reaction in reactions:
        name = reaction.outcome.value
        outcomes[name] = outcomes.get(name, 0) + 1

    why = rule.unreachable_against(policy)
    if why is not None:
        return FeasibilityVerdict(Feasibility.UNREACHABLE, rule,
                                  outcomes=outcomes, detail=why)

    book_led = sum(1 for r in reactions if r.ordering is Ordering.BOOK_LED)
    kalshi_led = sum(1 for r in reactions if r.ordering is Ordering.KALSHI_LED)
    indeterminate = sum(1 for r in reactions
                        if r.ordering is Ordering.INDETERMINATE)
    unknown = sum(1 for r in reactions if r.ordering is Ordering.UNKNOWN)
    determinate = book_led + kalshi_led
    common = dict(determinate=determinate, book_led=book_led,
                  kalshi_led=kalshi_led, indeterminate=indeterminate,
                  unknown_ordering=unknown, outcomes=outcomes)

    if determinate < rule.min_determinate:
        return FeasibilityVerdict(
            Feasibility.INSUFFICIENT, rule, **common,
            detail=(f"only {determinate:,} reaction(s) ordered, against a "
                    f"floor of {rule.min_determinate:,}. This is NOT a "
                    f"negative result: sparse moves, right-censored "
                    f"responses and blind intervals all shrink this "
                    f"denominator, and none of them is evidence about the "
                    f"exchange"))

    fraction = book_led / determinate
    if fraction < rule.min_book_led_fraction:
        return FeasibilityVerdict(
            Feasibility.STOP, rule, **common,
            detail=(f"{fraction:.0%} of ordered reactions are book-led, "
                    f"under the declared {rule.min_book_led_fraction:.0%}. "
                    f"That says these sources rarely establish the book "
                    f"leading -- NOT that the lag is short, and NOT that "
                    f"there is no edge"))
    return FeasibilityVerdict(
        Feasibility.CONTINUE, rule, **common,
        detail=(f"{fraction:.0%} of ordered reactions are book-led, over the "
                f"declared {rule.min_book_led_fraction:.0%}. Ordering is "
                f"resolvable from these sources often enough to design a "
                f"larger study"))


@dataclass(frozen=True)
class StageCount:
    """One stage's total, with its unit and a breakdown that must sum to it."""

    stage: str
    unit: Unit
    total: int
    breakdown: dict[str, int] = field(default_factory=dict)
    note: str = ""

    @property
    def reconciles(self) -> bool:
        """Does the breakdown account for the whole total?

        An empty breakdown is not a claim, so it reconciles trivially. A
        partial one is a claim, and a wrong one.
        """
        return not self.breakdown or sum(self.breakdown.values()) == self.total

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "unit": self.unit.value,
            "total": self.total,
            "breakdown": dict(sorted(self.breakdown.items())),
            "breakdown_reconciles": self.reconciles,
            "note": self.note,
        }

    def render(self) -> str:
        head = f"    {self.stage:<34} {self.total:>7,}  {self.unit.value}"
        if not self.reconciles:
            head += (f"   *** BREAKDOWN SUMS TO "
                     f"{sum(self.breakdown.values()):,}, NOT {self.total:,}")
        lines = [head]
        for key, value in sorted(self.breakdown.items()):
            lines.append(f"        {key:<38} {value:>7,}")
        if self.note:
            lines.append(f"        -- {self.note}")
        return "\n".join(lines)


def _span(instants: Iterable[datetime]) -> tuple[datetime | None, datetime | None]:
    ordered = sorted(i for i in instants if i is not None)
    return (ordered[0], ordered[-1]) if ordered else (None, None)


@dataclass(frozen=True)
class WindowVerdict:
    """What window this run covered, and what it is therefore allowed to be."""

    first: datetime | None
    last: datetime | None
    declared_role: DataRole
    overlaps_development: bool

    @property
    def effective_role(self) -> DataRole:
        """DEVELOPMENT wins over any declaration that overlaps it."""
        if self.overlaps_development:
            return DataRole.DEVELOPMENT
        return self.declared_role

    @property
    def exploratory(self) -> bool:
        return self.effective_role is not DataRole.HOLDOUT

    def as_dict(self) -> dict:
        return {
            "first_decision_at": self.first.isoformat() if self.first else None,
            "last_decision_at": self.last.isoformat() if self.last else None,
            "declared_role": self.declared_role.value,
            "effective_role": self.effective_role.value,
            "overlaps_development_window": self.overlaps_development,
            "development_window": [d.isoformat() for d in DEVELOPMENT_WINDOW],
            "exploratory": self.exploratory,
            "holdout_available": False,
            "note": DEVELOPMENT_NOTE,
            "holdout_note": (
                "no chronological holdout has been collected for this study, "
                "so `holdout_available` is False and every run to date is "
                "exploratory by construction"),
        }


def classify_window(instants: Iterable[datetime],
                    declared_role: DataRole = DataRole.UNDECLARED
                    ) -> WindowVerdict:
    """Decide what a run's window permits. Raises on a mislabelled holdout."""
    first, last = _span(instants)
    overlaps = False
    if first is not None and last is not None:
        start, end = DEVELOPMENT_WINDOW
        overlaps = (first.astimezone(timezone.utc).date() <= end
                    and last.astimezone(timezone.utc).date() >= start)
    if declared_role is DataRole.HOLDOUT and overlaps:
        raise HoldoutViolation(
            f"this run spans {first.date()}..{last.date()}, which overlaps the "
            f"development window {DEVELOPMENT_WINDOW[0]}..{DEVELOPMENT_WINDOW[1]}"
            f", and was declared a HOLDOUT. {DEVELOPMENT_NOTE}")
    return WindowVerdict(first, last, declared_role, overlaps)


@dataclass(frozen=True)
class Episode:
    """One GAME's worth of activity: its moves, their fate, and its one bet.

    The episode is the game, not the trigger. A book that walks a line three
    times is three triggers and one episode, because it is one outcome -- and
    the two contracts of that game are two priced rows on the same episode,
    which is what makes them mutually exclusive rather than a hedge.
    """

    game_id: str
    triggers: tuple[MoveTrigger, ...]
    reactions: tuple[Reaction, ...]
    screened: tuple[ScreenedReaction, ...]
    entry: SelectedEntry | None

    @property
    def stream_ids(self) -> tuple[str, ...]:
        return tuple(sorted({t.stream_id for t in self.triggers}))

    @property
    def contract_ids(self) -> tuple[str, ...]:
        return tuple(sorted({s.market_ticker for s in self.screened}))

    def as_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "streams": list(self.stream_ids),
            "contracts": list(self.contract_ids),
            "counts": {
                Unit.TRIGGERS.value: len(self.triggers),
                Unit.REACTIONS.value: len(self.reactions),
                Unit.SCREENED.value: len(self.screened),
                Unit.ENTRIES.value: 1 if self.entry else 0,
            },
            "triggers": [t.as_dict() for t in self.triggers],
            "reactions": [r.as_dict() for r in self.reactions],
            "screened": [s.as_dict() for s in self.screened],
            "entry": None if self.entry is None else {
                "market_id": self.entry.observation.market_id,
                "decision_at": self.entry.observation.decision_at.isoformat(),
                "minutes_to_start": self.entry.observation.minutes_to_start,
                "side": self.entry.trade.side,
                "entry_price": self.entry.trade.entry_price,
                "fee": self.entry.trade.fee,
                "predicted_ev": self.entry.trade.predicted_ev,
                "note": ("the FIRST qualifying move on this game, "
                         "chronologically; the game is closed to further "
                         "entries after it"),
            },
        }


@dataclass
class EpisodeLedger:
    """The run, end to end, with every stage's denominator preserved."""

    window: WindowVerdict
    episodes: list[Episode] = field(default_factory=list)
    stages: list[StageCount] = field(default_factory=list)
    selection: SelectionDiagnostics | None = None
    move_policy: MovePolicy | None = None
    reaction_policy: ReactionPolicy | None = None
    entry_policy: EntryPolicy | None = None
    eligibility: Eligibility | None = None
    feasibility: FeasibilityVerdict | None = None

    @property
    def entries(self) -> list[SelectedEntry]:
        return [e.entry for e in self.episodes if e.entry is not None]

    @property
    def reconciles(self) -> bool:
        return all(stage.reconciles for stage in self.stages)

    def stage(self, name: str) -> StageCount | None:
        for item in self.stages:
            if item.stage == name:
                return item
        return None

    def as_dict(self) -> dict:
        return {
            "schema": "reaction_episode_ledger/1",
            "window": self.window.as_dict(),
            "feasibility": (self.feasibility.as_dict()
                            if self.feasibility else None),
            "declared_policies": {
                "move_detection": (self.move_policy.as_dict()
                                   if self.move_policy else None),
                "reaction_measurement": (self.reaction_policy.as_dict()
                                         if self.reaction_policy else None),
                "entry": (self.entry_policy.describe()
                          if self.entry_policy else None),
                "eligibility": None if self.eligibility is None else {
                    "min_net_ev": self.eligibility.min_net_ev,
                    "price_band": list(self.eligibility.price_band),
                    "max_spread": self.eligibility.max_spread,
                    "min_minutes_to_start":
                        self.eligibility.min_minutes_to_start,
                    "max_minutes_to_start":
                        self.eligibility.max_minutes_to_start,
                },
                "note": ("declared before inspecting outcomes; "
                         "`run_reaction.py --policy` prints these without "
                         "running anything"),
            },
            "stages": [s.as_dict() for s in self.stages],
            "all_stages_reconcile": self.reconciles,
            "selection": None if self.selection is None else {
                "looks_considered": self.selection.considered,
                "entries_taken": self.selection.taken,
                "skipped_game_already_entered":
                    self.selection.skipped_game_already_entered,
                "skipped_not_qualifying": self.selection.skipped_not_qualifying,
                "games_seen": self.selection.games_seen,
                "games_entered": self.selection.games_entered,
                "unit_note": ("`looks_considered` counts SCREENED CONTRACT "
                              "ROWS, not games and not triggers"),
            },
            "episodes": [e.as_dict() for e in self.episodes],
        }

    def render(self) -> str:
        lines = ["REACTION EPISODE LEDGER", ""]
        role = self.window.effective_role.value.upper()
        lines.append(f"  WINDOW: {self.window.as_dict()['first_decision_at']}"
                     f" .. {self.window.as_dict()['last_decision_at']}")
        lines.append(f"  ROLE:   {role}"
                     + ("  (EXPLORATORY)" if self.window.exploratory else ""))
        if self.window.overlaps_development:
            lines.append(f"  {DEVELOPMENT_NOTE}")
        lines.append("")
        lines.append("  STAGES -- each count in its OWN unit; they are not")
        lines.append("  interchangeable and none is a percentage of another.")
        lines.append("")
        for item in self.stages:
            lines.append(item.render())
        lines.append("")
        if self.selection is not None:
            lines.append(self.selection.render())
            lines.append("    (looks = screened contract rows, not games)")
        if self.feasibility is not None:
            lines.append("")
            lines.append(self.feasibility.render())
        if not self.reconciles:
            lines.append("")
            lines.append("  *** A STAGE'S BREAKDOWN DOES NOT SUM TO ITS TOTAL.")
            lines.append("  *** Some outcome is unaccounted for; the totals")
            lines.append("  *** above may be right and the reasons are not.")
        return "\n".join(lines)


def build_ledger(*, detector_result: DetectorResult,
                 reactions: Sequence[Reaction],
                 screen_result: ScreenResult,
                 observation_count: int,
                 declared_role: DataRole = DataRole.UNDECLARED,
                 eligibility: Eligibility | None = None,
                 entry_policy: EntryPolicy | None = None,
                 move_policy: MovePolicy | None = None,
                 reaction_policy: ReactionPolicy | None = None,
                 feasibility_rule: FeasibilityRule | None = None,
                 venue: str = "kalshi", role: str = "taker",
                 series: str | None = None) -> EpisodeLedger:
    """Assemble the ledger, and run the entry policy over the screened rows.

    THE GAME IS THE PROVIDER'S EVENT ID, everywhere: on the trigger, on the
    `Observation` the screen builds, and on the cluster `select_entries` keys.
    There is deliberately no ticker-to-game override -- one applied to the
    screened rows but not to the triggers would split a single game into two
    episodes, and both contracts of a game sharing one id is exactly what
    closes the game to the second once the first is taken. That is what "do
    not buy both sides" means mechanically.
    """
    eligibility = eligibility or Eligibility()
    entry_policy = entry_policy or EntryPolicy()
    triggers = list(detector_result.triggers)
    window = classify_window([t.detected_at for t in triggers], declared_role)

    observations = screen_result.observations
    entries, selection = select_entries(
        observations, eligibility=eligibility, venue=venue, role=role,
        series=series, policy=entry_policy)
    entry_by_game = {e.observation.game_id: e for e in entries}

    def bucket(key: str) -> dict:
        return grouped.setdefault(key, {"triggers": [], "reactions": [],
                                        "screened": []})

    grouped: dict[str, dict] = {}
    for trigger in triggers:
        bucket(trigger.event_id)["triggers"].append(trigger)
    for reaction in reactions:
        bucket(reaction.event_id)["reactions"].append(reaction)
    for item in screen_result.screened:
        bucket(item.event_id)["screened"].append(item)

    episodes = [
        Episode(game_id=game_id,
                triggers=tuple(parts["triggers"]),
                reactions=tuple(parts["reactions"]),
                screened=tuple(parts["screened"]),
                entry=entry_by_game.get(game_id))
        for game_id, parts in sorted(grouped.items())
    ]

    admitted = len(screen_result.admitted)
    refused = sum(screen_result.refusal_counts().values())
    # The detector records exactly one outcome per observation -- a trigger or
    # a named rejection -- plus one per `note_gap`, which has no observation
    # behind it. So the outcome total is derived from the outcomes themselves
    # rather than from the envelope count, and it RECONCILES. An earlier
    # version hung this breakdown off the trigger stage and excused the
    # mismatch in a note, which would have printed the ledger's
    # does-not-reconcile warning on every healthy run (rule 10).
    outcome_breakdown = {"triggered": len(triggers),
                         **detector_result.counts()}
    stages = [
        StageCount("provider observations fed", Unit.OBSERVATIONS,
                   observation_count,
                   note="envelopes the detector saw, valid and invalid alike"),
        StageCount("detector outcomes", Unit.OUTCOMES,
                   sum(outcome_breakdown.values()),
                   breakdown=outcome_breakdown,
                   note=("one per observation, plus one per declared gap -- "
                         "so this can exceed the envelope count above, and "
                         "the difference IS the declared absences")),
        StageCount("streams that produced a move", Unit.STREAMS,
                   len({t.stream_id for t in triggers}),
                   note="provider + book + event + market + orientation"),
        StageCount("moves detected", Unit.TRIGGERS, len(triggers),
                   note=("a subset of the outcomes above; one book move on a "
                         "two-contract game is ONE trigger and TWO screened "
                         "rows")),
        StageCount("reactions measured", Unit.REACTIONS, len(reactions),
                   breakdown=_reaction_counts(reactions),
                   note="one per trigger; censored outcomes included"),
        StageCount("contract rows screened", Unit.SCREENED,
                   len(screen_result.screened),
                   breakdown={"admitted": admitted,
                              "refused_before_screening": refused,
                              "screened_but_not_admitted":
                                  len(screen_result.screened) - admitted
                                  - refused},
                   note=("one row per (move, contract): a game with two "
                         "contracts screens twice for one move")),
        StageCount("entries selected", Unit.ENTRIES, len(entries),
                   note=("at most one per game, at the EARLIEST qualifying "
                         "move; taking one closes the game to the other "
                         "contract")),
        StageCount("games with any detected move", Unit.GAMES, len(episodes),
                   breakdown={"entered": len(entry_by_game),
                              "no entry": len(episodes) - len(entry_by_game)}),
    ]
    return EpisodeLedger(window=window, episodes=episodes, stages=stages,
                         selection=selection, move_policy=move_policy,
                         reaction_policy=reaction_policy,
                         entry_policy=entry_policy, eligibility=eligibility,
                         feasibility=judge_feasibility(
                             reactions, feasibility_rule, reaction_policy))


def _reaction_counts(reactions: Sequence[Reaction]) -> dict[str, int]:
    out: dict[str, int] = {}
    for reaction in reactions:
        out[reaction.outcome.value] = out.get(reaction.outcome.value, 0) + 1
    return out
