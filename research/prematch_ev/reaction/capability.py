"""What each source can and cannot time, and which questions that permits.

THE POINT OF THIS MODULE
------------------------
The reaction-lag hypothesis is a question about ORDER and DURATION: did the
sportsbook move first, how long did an executable Kalshi price stay behind.
Both are measured in time, and neither source here delivers time at the
resolution the question is phrased in. So the resolution floor is computed
first, declared, and then used to GATE what the analysis is allowed to claim
-- rather than discovered afterwards in a caveat nobody reads (rule 21: a log
line is not a control).

The specific trap this exists to prevent: The Odds API archive is sampled on a
**five-minute grid**, and a market carries its own `last_update`. It is very
tempting to measure lag from `last_update`, because it is stamped finely and
looks like the moment the book moved. It is -- and our system could not have
known it then. A strategy polling that archive learns about the move at the
next SNAPSHOT. Measuring from `last_update` would credit the strategy with
information it did not have, which is lookahead wearing a timestamp.

  `last_update`  -> when the BOOK moved            (unobservable to us, then)
  `snapshot`     -> when WE could first have known (the executable clock)

`earliest_observable_at` is the single function that makes that conversion, so
no site can disagree about which clock a decision runs on (rule 19).

WHAT IS TRANSCRIBED RATHER THAN RE-READ
---------------------------------------
`api.the-odds-api.com`, `the-odds-api.com` and `api.elections.kalshi.com` are
ALL unreachable from the sessions this was written in (connection refused
through the egress proxy). Nothing below was re-verified against live provider
documentation here. Every entry therefore carries an `Evidence` label saying
where it came from, and the audit prints those labels rather than presenting
the table as measured fact. `free_verification_commands()` gives the exact
free checks to run somewhere with egress.

Provider facts marked PROVIDER_DOC come from the docstrings of this repo's own
adapters (`data/odds_history.py`, `data/kalshi_history.py`), which were written
against real payloads in earlier rounds of this study. That makes them better
than a guess and worse than a reading. They are labelled accordingly, and the
one that matters most -- the five-minute grid -- is ALSO derived
independently at runtime from the snapshot timestamps a replay actually
contains (`measure_snapshot_grid`), so a wrong constant shows up as a
disagreement rather than as a confidently wrong lag.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable, Sequence


class Evidence(str, Enum):
    """Where a capability claim comes from. Never decoration -- the audit
    refuses to let an UNKNOWN or a PROVIDER_DOC claim be read as MEASURED."""

    MEASURED = "measured"                    # a real payload in hand proves it
    PROVIDER_DOC = "provider_doc"            # documented; transcribed, not re-read here
    INFERRED_FROM_PAYLOAD = "inferred"       # derived from the shape our parser handles
    UNKNOWN = "unknown"                      # not established, and not guessed


class Resolution(str, Enum):
    """How finely a source locates an event in time."""

    INSTANT = "instant"        # a timestamp for the event itself
    INTERVAL = "interval"      # an aggregate over a period; the event is somewhere inside
    GRID = "grid"              # sampled on a fixed cadence; between samples is invisible
    UNKNOWN = "unknown"


# --- the clocks a study has to keep apart ----------------------------------

class Clock(str, Enum):
    PROVIDER_OBSERVED = "provider_observed_at"        # when the SOURCE says the value changed
    PROVIDER_SNAPSHOT = "provider_snapshot_time"  # when the provider captured/closed it
    LOCAL_RECEIPT = "local_receipt_time"        # when WE actually received it (live only)
    SCHEDULED_START = "scheduled_start"         # kickoff


@dataclass(frozen=True)
class ClockCapability:
    clock: Clock
    evidence: Evidence
    precision: str                # human-readable, e.g. "seconds", "1 minute", "unknown"
    note: str = ""

    @property
    def known(self) -> bool:
        return self.evidence is not Evidence.UNKNOWN


@dataclass(frozen=True)
class SourceCapability:
    """One data source's timing warranty."""

    source: str
    role: str
    resolution: Resolution
    resolution_seconds: float | None      # None when unknown
    resolution_evidence: Evidence
    clocks: tuple[ClockCapability, ...]
    depth_available: bool
    depth_evidence: Evidence
    suspension_observable: bool
    suspension_evidence: Evidence
    notes: tuple[str, ...] = ()

    def clock(self, which: Clock) -> ClockCapability | None:
        for capability in self.clocks:
            if capability.clock is which:
                return capability
        return None

    def has_clock(self, which: Clock) -> bool:
        capability = self.clock(which)
        return bool(capability and capability.known)

    @property
    def is_interval_censored(self) -> bool:
        return self.resolution in (Resolution.INTERVAL, Resolution.GRID)


# --- the declared table ----------------------------------------------------
#
# ODDS ARCHIVE. From data/odds_history.py, written against real payloads:
#   * "Snapshots are 5-minutely; the endpoint returns the closest snapshot at
#     or BEFORE `date`." -> a GRID, not a stream.
#   * `SharpQuote` already carries three times on purpose, and its own
#     docstring says a comparison "is only honest against" `last_update`.
#     That is right about which clock describes the BOOK and wrong about which
#     clock describes US -- see the module docstring.
#   * `last_update` is read from the MARKET when present, else the BOOKMAKER.
#     Its absence is already treated as unknown-not-fresh (`age_seconds`
#     returns None, `is_fresh` returns False), which is the behaviour this
#     study needs and gets to inherit.
#
# KALSHI CANDLESTICKS. From data/kalshi_history.py:
#   * `period_interval` is 1 (minute) | 60 (hour) | 1440 (day). One minute is
#     the FLOOR; there is no tick history on this path.
#   * a candle's `ts` is `end_period_ts` -- the END of its period. The quote it
#     reports (`bid_close`/`ask_close`) is the book at that close, so the
#     instant is known but everything BETWEEN closes is not.
#   * volume and open interest are counts. There is no book DEPTH, so a
#     quantity that could have been filled at the quoted price is not
#     available on this path at all.

ODDS_SNAPSHOT_GRID_SECONDS = 300.0        # 5 minutes, PROVIDER_DOC
KALSHI_CANDLE_FLOOR_SECONDS = 60.0        # 1 minute, PROVIDER_DOC

SHARP_BOOK_CAPABILITY = SourceCapability(
    source="the_odds_api_historical",
    role="sharp sportsbook (de-vigged fair probability)",
    resolution=Resolution.GRID,
    resolution_seconds=ODDS_SNAPSHOT_GRID_SECONDS,
    resolution_evidence=Evidence.PROVIDER_DOC,
    clocks=(
        ClockCapability(
            Clock.PROVIDER_OBSERVED, Evidence.PROVIDER_DOC, "seconds",
            "market `last_update`: the last time the PROVIDER'S SYSTEM saw "
            "odds for this market from the bookmaker. It is NOT when the "
            "bookmaker changed the price, and it is NOT when a poller could "
            "have known. (Bookmaker-level `last_update` is deprecated.) An "
            "earlier version of this table claimed it was the book's own "
            "change time; the change instant is BRACKETED, never a point."),
        ClockCapability(
            Clock.PROVIDER_SNAPSHOT, Evidence.MEASURED, "5 minutes",
            "payload `timestamp`. The archive returns the closest snapshot at "
            "or BEFORE the requested instant, so this is a grid point."),
        ClockCapability(
            Clock.LOCAL_RECEIPT, Evidence.UNKNOWN, "unknown",
            "HISTORICAL REPLAY: we were not listening at the time, so there is "
            "no receipt time and none may be invented. A prospective recorder "
            "is the only way to obtain one."),
        ClockCapability(
            Clock.SCHEDULED_START, Evidence.MEASURED, "seconds",
            "`commence_time`."),
    ),
    depth_available=False,
    depth_evidence=Evidence.INFERRED_FROM_PAYLOAD,
    suspension_observable=False,
    suspension_evidence=Evidence.UNKNOWN,
    notes=(
        "A market absent from a snapshot may be suspended, unlisted, or simply "
        "not carried. The archive does not distinguish those, so a gap is not "
        "evidence of suspension.",
        "THE BOOK'S OWN CHANGE INSTANT IS NOT IN THIS DATA. `last_update` is "
        "the provider's observation, so a price change can only be BRACKETED "
        "between the previous observation of the old value and the one "
        "carrying the new value. Nothing here yields a point.",
        "Delivery lag from the bookmaker to the provider is NOT established, "
        "and neither is provider-to-us lag. A historical snapshot time is a "
        "ZERO-DELIVERY-DELAY REPLAY ASSUMPTION, not a measurement.",
        "This 5-minute grid is the ARCHIVE's cadence. The live feed's update "
        "intervals are a separate, better capability and are NOT described by "
        "this row: "
        "https://the-odds-api.com/sports-odds-data/update-intervals.html",
    ),
)

KALSHI_CANDLE_CAPABILITY = SourceCapability(
    source="kalshi_candlesticks_historical",
    role="exchange executable quote (bid/ask)",
    resolution=Resolution.INTERVAL,
    resolution_seconds=KALSHI_CANDLE_FLOOR_SECONDS,
    resolution_evidence=Evidence.PROVIDER_DOC,
    clocks=(
        ClockCapability(
            Clock.PROVIDER_OBSERVED, Evidence.UNKNOWN, "unknown",
            "A candle does not say when inside its period the quote changed."),
        ClockCapability(
            Clock.PROVIDER_SNAPSHOT, Evidence.MEASURED, "1 minute",
            "`end_period_ts` -- the END of the candle's period. bid_close and "
            "ask_close are the book at that close."),
        ClockCapability(
            Clock.LOCAL_RECEIPT, Evidence.UNKNOWN, "unknown",
            "HISTORICAL REPLAY: no receipt time exists."),
        ClockCapability(
            Clock.SCHEDULED_START, Evidence.MEASURED, "seconds",
            "from the schedule resolver, not from the candle."),
    ),
    depth_available=False,
    depth_evidence=Evidence.PROVIDER_DOC,
    suspension_observable=False,
    suspension_evidence=Evidence.UNKNOWN,
    notes=(
        "NO DEPTH. A quoted bid/ask says a price existed, never that any "
        "quantity was available at it. Missing depth must not be read as "
        "infinite liquidity.",
        "A period with no candle is not evidence the previous quote remained "
        "available. It is an absence of data, which is its own state.",
    ),
)

SOURCES: tuple[SourceCapability, ...] = (
    SHARP_BOOK_CAPABILITY, KALSHI_CANDLE_CAPABILITY,
)


def source(name: str) -> SourceCapability | None:
    for candidate in SOURCES:
        if candidate.source == name:
            return candidate
    return None


# --- the executable clock --------------------------------------------------

def earliest_observable_at(
    provider_observed: datetime | None,
    snapshot_times: Sequence[datetime],
) -> tuple[datetime | None, str]:
    """When a strategy polling this archive could FIRST have known a move.

    THE conversion this module exists for. Returns `(instant, reason)`.

    A book move stamped `provider_observed` is invisible to a poller until a
    snapshot carrying it is taken, so the answer is the earliest snapshot at or
    after `provider_observed` -- chosen from the snapshots the replay ACTUALLY has,
    not from an assumed grid, because a gap in what was fetched is a real gap
    in what a poller would have seen.

    Returns (None, reason) rather than guessing when:
      * there is no `provider_observed` at all -- unknown is not "now";
      * every snapshot predates it, so the move is right-censored: it happened
        after our last look and we cannot say when it became visible.
    """
    if provider_observed is None:
        return None, "no provider_observed_at: the move instant is unknown"
    later = sorted(t for t in snapshot_times if t >= provider_observed)
    if not later:
        return None, (
            "right-censored: no snapshot at or after the move, so the instant "
            "it became observable is outside the sample")
    return later[0], "first snapshot at or after the source update"


def detection_lag_seconds(
    provider_observed: datetime | None,
    snapshot_times: Sequence[datetime],
) -> float | None:
    """How long our system was blind to a move. None when not computable."""
    observable, _ = earliest_observable_at(provider_observed, snapshot_times)
    if observable is None or provider_observed is None:
        return None
    return (observable - provider_observed).total_seconds()


def measure_snapshot_grid(snapshot_times: Iterable[datetime]) -> dict:
    """Derive the archive's real cadence from the snapshots a run holds.

    The 5-minute figure is PROVIDER_DOC and transcribed. This measures it from
    data instead, so a wrong constant surfaces as a disagreement rather than as
    a lag figure that is confidently off by the difference (rule 26: take the
    verdict the data gives, do not only assert one).
    """
    times = sorted(set(snapshot_times))
    if len(times) < 2:
        return {"samples": len(times), "median_gap_seconds": None,
                "min_gap_seconds": None, "max_gap_seconds": None,
                "agrees_with_declared": None,
                "declared_seconds": ODDS_SNAPSHOT_GRID_SECONDS}
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    gaps.sort()
    mid = len(gaps) // 2
    median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
    smallest = gaps[0]
    # A run deliberately fetches sparse checkpoints, so the MEDIAN gap reflects
    # the study's sampling plan, not the provider's cadence. The SMALLEST gap is
    # the informative one: the archive cannot serve two distinct snapshots
    # closer together than its own grid.
    agrees = None
    if smallest > 0:
        agrees = smallest >= ODDS_SNAPSHOT_GRID_SECONDS - 1.0
    return {
        "samples": len(times),
        "median_gap_seconds": median,
        "min_gap_seconds": smallest,
        "max_gap_seconds": gaps[-1],
        "declared_seconds": ODDS_SNAPSHOT_GRID_SECONDS,
        "agrees_with_declared": agrees,
        "note": ("the SMALLEST observed gap bounds the provider grid; the "
                 "median reflects this run's sampling plan, not the archive"),
    }


# --- what the table permits to be claimed ----------------------------------

class Answerability(str, Enum):
    ANSWERABLE = "answerable"
    INTERVAL_CENSORED = "interval_censored"    # answerable only to a coarse bound
    UNANSWERABLE = "unanswerable"


@dataclass(frozen=True)
class QuestionVerdict:
    question: str
    answerability: Answerability
    bound_seconds: float | None
    because: str

    @property
    def may_be_claimed(self) -> bool:
        return self.answerability is not Answerability.UNANSWERABLE


def question_verdicts(
    book: SourceCapability = SHARP_BOOK_CAPABILITY,
    exchange: SourceCapability = KALSHI_CANDLE_CAPABILITY,
) -> tuple[QuestionVerdict, ...]:
    """The study's questions, graded against the sources it actually has.

    This is the control, not the commentary. `replay` reads these verdicts and
    refuses to emit a figure whose question is UNANSWERABLE, and labels every
    INTERVAL_CENSORED figure with its bound.
    """
    grid = book.resolution_seconds or float("inf")
    candle = exchange.resolution_seconds or float("inf")
    coarsest = max(grid, candle)

    out = [
        QuestionVerdict(
            "did the book's fair probability change, and by how much",
            Answerability.ANSWERABLE, None,
            "two-sided moneyline prices are present in each snapshot and "
            "de-vig within that one contemporaneous quote"),
        QuestionVerdict(
            "when did the PROVIDER last observe this price",
            Answerability.ANSWERABLE, None,
            "market last_update, stamped to the second -- the provider's "
            "observation, NOT the bookmaker's change"),
        QuestionVerdict(
            "when did the BOOK actually change its price",
            Answerability.UNANSWERABLE, None,
            "last_update is when the PROVIDER saw the odds. The change can "
            "only be BRACKETED between consecutive provider observations; no "
            "point estimate exists in this data, and an earlier version of "
            "this table wrongly claimed one did"),
        QuestionVerdict(
            "when could OUR SYSTEM first have known",
            Answerability.INTERVAL_CENSORED, grid,
            f"pinned to an archive snapshot on a {grid:.0f}s grid, and only "
            "under an explicit ZERO-DELIVERY-DELAY assumption -- real "
            "provider-to-us lag is unmeasurable in replay"),
        QuestionVerdict(
            "when did the Kalshi quote change",
            Answerability.INTERVAL_CENSORED, candle,
            f"candles are {candle:.0f}s aggregates stamped at period close; "
            "the change is somewhere inside the period"),
        QuestionVerdict(
            "did the book move BEFORE Kalshi (ordering)",
            Answerability.INTERVAL_CENSORED, coarsest,
            f"a gap wider than {coarsest:.0f}s is NECESSARY but NOT "
            "SUFFICIENT to rank an ordering: unknown provider lag, missing "
            "samples and intervening moves all survive it. Intervals must be "
            "derived from actual consecutive valid observations, never from "
            "this constant, and anything unresolved is indeterminate"),
        QuestionVerdict(
            "how long an executable discrepancy persisted",
            Answerability.INTERVAL_CENSORED, grid,
            f"the COARSER input governs: candles are {candle:.0f}s but the "
            f"sharp side is sampled every {grid:.0f}s, so a duration computed "
            "between candle closes holds the sharp value constant across its "
            "own sampling interval. That is a HELD-SHARP-VALUE calculation "
            "and must be labelled one, not a fine-grained measurement"),
        QuestionVerdict(
            "was the quoted price fillable in the size we wanted",
            Answerability.UNANSWERABLE, None,
            "no depth on either path: a quote proves a price existed, never a "
            "quantity. This cannot be answered from historical candles at all"),
        QuestionVerdict(
            "was the market suspended rather than merely absent",
            Answerability.UNANSWERABLE, None,
            "neither source distinguishes suspension from absence, so a gap "
            "has no attributable cause"),
        QuestionVerdict(
            "what the provider's delivery lag was",
            Answerability.UNANSWERABLE, None,
            "no local receipt time exists in historical replay; only a "
            "prospective recorder can measure it, so every availability "
            "answer rides on a zero-delay ASSUMPTION"),
        QuestionVerdict(
            "what the LIVE feed could do",
            Answerability.UNANSWERABLE, None,
            "this table describes the ARCHIVE. Live update intervals are a "
            "separate and better capability, and presenting the archive's "
            "grid as a universal limit understates the live feed"),
    ]
    return tuple(out)


def verdict_for(question_fragment: str) -> QuestionVerdict | None:
    needle = question_fragment.lower()
    for verdict in question_verdicts():
        if needle in verdict.question.lower():
            return verdict
    return None


def unanswerable_questions() -> tuple[str, ...]:
    return tuple(v.question for v in question_verdicts()
                 if v.answerability is Answerability.UNANSWERABLE)


def reaction_resolution_floor_seconds() -> float:
    """The coarsest resolution any reaction figure inherits.

    One number, derived from the table, used by the replay to label every lag
    it reports. A study that quotes a lag finer than this is quoting noise.
    """
    return max(
        SHARP_BOOK_CAPABILITY.resolution_seconds or 0.0,
        KALSHI_CANDLE_CAPABILITY.resolution_seconds or 0.0,
    )


def free_verification_commands() -> tuple[str, ...]:
    """Exact FREE checks to re-verify the transcribed rows, off this session.

    None of these costs a credit: the docs pages are public, and an odds
    request with no key is rejected before it bills. Run them where egress
    works and correct the table if a row disagrees.
    """
    return (
        "# 1. Odds archive cadence + last_update semantics (public docs, free)",
        "curl -sS https://the-odds-api.com/liveapi/guides/v4/#historical-odds",
        "",
        "# 2. Odds historical shape WITHOUT a key -- 401 before any billing.",
        "#    Confirms the parameter surface only; it returns no odds.",
        "curl -sS -o /dev/null -w '%{http_code}\\n' \\",
        "  'https://api.the-odds-api.com/v4/historical/sports/americanfootball_nfl/odds'",
        "",
        "# 2b. The CORRECTED last_update semantics + the archive cadence, and",
        "#     the LIVE update intervals, which are a different capability.",
        "curl -sS https://the-odds-api.com/historical-odds-data/",
        "curl -sS https://the-odds-api.com/sports-odds-data/update-intervals.html",
        "",
        "# 3. Kalshi candlestick period_interval + end_period_ts (public docs, free)",
        "curl -sS https://docs.kalshi.com/api-reference/endpoint/get-market-candlesticks",
        "",
        "# 4. Kalshi candles for one settled market, 1-minute period (free, no auth).",
        "#    Confirms the interval floor and that no depth field is present.",
        "curl -sS 'https://api.elections.kalshi.com/trade-api/v2/series/KXNFLGAME"
        "/markets/KXNFLGAME-26SEP14DENKC-KC/candlesticks"
        "?start_ts=1789000000&end_ts=1789086400&period_interval=1' | head -c 2000",
        "",
        "# 4b. DOES A QUIET MINUTE GET A CANDLE? This decides a default, and",
        "#     the two readings give opposite answers: if every minute in a",
        "#     market's lifetime has a candle, a missing one is MISSING DATA;",
        "#     if candles are only emitted on activity, a missing one is an",
        "#     UNCHANGED QUOTE. `reaction.measure` currently assumes the",
        "#     first (BLIND_INTERVAL, the conservative reading) because this",
        "#     session cannot check. Count the candles against the wall-clock",
        "#     minutes the window spans -- equal means every minute is",
        "#     emitted, fewer means activity-gated:",
        "curl -sS 'https://api.elections.kalshi.com/trade-api/v2/series/KXNFLGAME"
        "/markets/KXNFLGAME-26SEP14DENKC-KC/candlesticks"
        "?start_ts=1789000000&end_ts=1789003600&period_interval=1' \\",
        "  | python3 -c 'import json,sys; c=json.load(sys.stdin)"
        "[\"candlesticks\"]; print(len(c), \"candles for 60 minutes\")'",
        "#     If they are activity-gated, set",
        "#     ReactionPolicy(treat_missing_candles_as_unchanged=True) and say",
        "#     so in the run manifest. Do NOT flip it on an assumption.",
        "",
        "# 5. Then re-run the audit and confirm it reports no disagreement:",
        "python3 run_reaction.py --capability",
    )


def render(grid_measurement: dict | None = None) -> str:
    """The audit, as text. Free, no network, no credential."""
    lines: list[str] = []
    lines.append("SOURCE TIMING CAPABILITY AUDIT")
    lines.append("")
    lines.append("  NOTHING BELOW WAS RE-VERIFIED IN THIS SESSION. api.the-odds-api.com,")
    lines.append("  the-odds-api.com and api.elections.kalshi.com are all unreachable")
    lines.append("  through this environment's egress proxy. Every row carries the")
    lines.append("  evidence class it actually has -- see --capability-verify for the")
    lines.append("  exact free commands to re-check them somewhere with network.")
    lines.append("")
    for capability in SOURCES:
        lines.append(f"  {capability.source}")
        lines.append(f"    role               {capability.role}")
        floor = (f"{capability.resolution_seconds:.0f}s"
                 if capability.resolution_seconds else "unknown")
        lines.append(f"    resolution         {capability.resolution.value} "
                     f"({floor})  [{capability.resolution_evidence.value}]")
        for clock in capability.clocks:
            mark = "  " if clock.known else "!!"
            lines.append(f"    {mark} {clock.clock.value:24s} "
                         f"{clock.precision:10s} [{clock.evidence.value}]")
            if clock.note:
                for chunk in _wrap(clock.note, 60):
                    lines.append(f"         {chunk}")
        lines.append(f"    depth available    "
                     f"{'yes' if capability.depth_available else 'NO'}  "
                     f"[{capability.depth_evidence.value}]")
        lines.append(f"    suspension visible "
                     f"{'yes' if capability.suspension_observable else 'NO'}  "
                     f"[{capability.suspension_evidence.value}]")
        for note in capability.notes:
            for chunk in _wrap(note, 66):
                lines.append(f"      - {chunk}" if chunk is note[:len(chunk)]
                             else f"        {chunk}")
        lines.append("")

    lines.append(f"  REACTION RESOLUTION FLOOR: "
                 f"{reaction_resolution_floor_seconds():.0f}s")
    lines.append("  Every lag, ordering and persistence figure inherits this bound.")
    lines.append("  A five-minute sample is not second-resolution evidence.")
    lines.append("")

    if grid_measurement is not None:
        lines.append("  MEASURED SNAPSHOT CADENCE (from this run's own snapshots)")
        lines.append(f"    samples            {grid_measurement['samples']}")
        for key in ("min_gap_seconds", "median_gap_seconds", "max_gap_seconds"):
            value = grid_measurement.get(key)
            shown = f"{value:.0f}s" if isinstance(value, (int, float)) else "n/a"
            lines.append(f"    {key:18s} {shown}")
        agrees = grid_measurement.get("agrees_with_declared")
        if agrees is None:
            lines.append("    agrees w/ declared  not determinable from this sample")
        elif agrees:
            lines.append("    agrees w/ declared  yes")
        else:
            lines.append("    agrees w/ declared  *** NO -- the declared "
                         "5-minute grid is wrong ***")
            lines.append("      Two snapshots arrived closer together than the")
            lines.append("      documented cadence, so the transcribed constant is")
            lines.append("      not the provider's real resolution. Fix the table")
            lines.append("      before quoting any lag derived from it.")
        lines.append("")

    lines.append("  WHAT THESE SOURCES PERMIT")
    for verdict in question_verdicts():
        if verdict.answerability is Answerability.ANSWERABLE:
            tag = "[ yes    ]"
        elif verdict.answerability is Answerability.INTERVAL_CENSORED:
            tag = (f"[ ±{verdict.bound_seconds:.0f}s".ljust(9) + "]"
                   if verdict.bound_seconds else "[ coarse ]")
        else:
            tag = "[ NO     ]"
        lines.append(f"    {tag} {verdict.question}")
        for chunk in _wrap(verdict.because, 62):
            lines.append(f"               {chunk}")
    lines.append("")
    lines.append("  The UNANSWERABLE rows are not pending work. They are questions")
    lines.append("  these two historical sources cannot answer at any sample size,")
    lines.append("  and the replay refuses to emit a figure for them rather than")
    lines.append("  publishing a proxy (rule 24).")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list[str]:
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


def as_dict(grid_measurement: dict | None = None) -> dict:
    """Machine-readable audit, for the run manifest."""
    return {
        "verified_in_session": False,
        "unreachable_hosts": ["api.the-odds-api.com", "the-odds-api.com",
                              "api.elections.kalshi.com"],
        "reaction_resolution_floor_seconds": reaction_resolution_floor_seconds(),
        "sources": [
            {
                "source": c.source,
                "role": c.role,
                "resolution": c.resolution.value,
                "resolution_seconds": c.resolution_seconds,
                "resolution_evidence": c.resolution_evidence.value,
                "clocks": [
                    {"clock": k.clock.value, "evidence": k.evidence.value,
                     "precision": k.precision, "known": k.known, "note": k.note}
                    for k in c.clocks
                ],
                "depth_available": c.depth_available,
                "depth_evidence": c.depth_evidence.value,
                "suspension_observable": c.suspension_observable,
                "notes": list(c.notes),
            }
            for c in SOURCES
        ],
        "questions": [
            {"question": v.question, "answerability": v.answerability.value,
             "bound_seconds": v.bound_seconds, "because": v.because}
            for v in question_verdicts()
        ],
        "measured_snapshot_cadence": grid_measurement,
    }
