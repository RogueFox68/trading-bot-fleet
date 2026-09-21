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
    EVENT_BODY_TIMEZONE, kalshi_event_ticker, normalise_exchange_code,
    parse_event_body_start, parse_kalshi_game_ticker, resolve_team,
    unknown_exchange_codes,
)
from data.kalshi_history import (
    Coverage, settlement_outcome, settlement_time,
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


def decision_cutoffs(
    commence_times: Iterable[datetime],
    lead_minutes: float,
    resolution: timedelta = SNAPSHOT_RESOLUTION,
) -> list[datetime]:
    """The distinct instants a targeted fetch actually needs.

    Games cluster on common start times, so a slate of fifteen games usually
    needs a handful of fetches rather than fifteen: each cutoff is floored to
    the archive's snapshot grid and then deduplicated. This is what makes
    targeting cheaper than a grid fine enough to satisfy the freshness bound.
    """
    step = resolution.total_seconds()
    seen: set[float] = set()
    for start in commence_times:
        cutoff = start - timedelta(minutes=lead_minutes)
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


def market_start_time(market: dict) -> datetime | None:
    """Scheduled start: a verified payload field if one exists, else the ticker.

    Deliberately does NOT fall back to `open_time`, `close_time` or an
    expiration: those are contract lifecycle facts, not first pitch.
    """
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

    home = resolve_team(quote.home_name, league)
    away = resolve_team(quote.away_name, league)
    # The YES side is an EXCHANGE code; compare it canonically or every
    # Arizona contract silently fails orientation as well as matching.
    target = normalise_exchange_code(yes_participant, league)
    if home.resolved and home.abbreviation == target:
        return fair[0]
    if away.resolved and away.abbreviation == target:
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

        start = market_start_time(markets[tickers[0]])
        if start is None:
            ledger.reject("no_readable_start_time", event_ticker,
                          count=len(tickers), stage="contracts")
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


def observation_at_cutoff(
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
) -> Observation | None:
    """Build one observation from the newest record on EACH side at the cutoff.

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
        ledger.reject("no_sharp_quote_available_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None

    quote = max(usable_quotes, key=lambda q: q.last_update)

    # Age is measured at the DECISION timestamp, not at capture. One limit
    # governs both, rather than `--max-quote-age` applying at snapshot time
    # while a separate constant applied at the decision.
    age_at_decision = (decision_at - quote.last_update).total_seconds()
    if age_at_decision > max_quote_age:
        ledger.reject("sharp_quote_too_old_at_cutoff",
                      f"{joined.market_ticker} {age_at_decision:.0f}s", stage=reject_stage)
        return None

    usable_candles = [c for c in candles if c.mid is not None and c.ts <= decision_at]
    if not usable_candles:
        ledger.reject("no_exchange_quote_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None
    candle = max(usable_candles, key=lambda c: c.ts)
    if decision_at - candle.ts > max_lag:
        ledger.reject("exchange_quote_too_old_at_cutoff", joined.market_ticker, stage=reject_stage)
        return None
    if candle.has_malformed_price:
        ledger.reject("exchange_quote_malformed", joined.market_ticker, stage=reject_stage)
        return None

    p_sharp = orient_probability(quote, joined.yes_participant, league, method)
    if p_sharp is None:
        ledger.reject("yes_side_unresolvable", f"{joined.market_ticker} YES={joined.yes_participant}", stage=reject_stage)
        return None

    lead_minutes = (joined.start - decision_at).total_seconds() / 60.0
    if lead_minutes <= 0:
        ledger.reject("cutoff_at_or_after_start", joined.market_ticker, stage=reject_stage)
        return None

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
        sharp_at=quote.last_update,
        sharp_snapshot_at=quote.snapshot,
        exchange_at=candle.ts,
        devig_method=method,
    )
