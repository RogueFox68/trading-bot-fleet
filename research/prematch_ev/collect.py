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

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from analysis.scoring import Observation
from core.devig import DevigError, devig_american
from core.matcher import (
    kalshi_event_ticker, parse_kalshi_game_ticker, resolve_team,
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


@dataclass
class Stage:
    """One pipeline stage, in its OWN units."""

    unit: str                 # "snapshots" | "event-quotes" | "contracts" | "games"
    considered: int = 0
    excluded: int = 0         # eligibility: the study's own choice
    rejected: int = 0         # failures: a gap in what it could see

    @property
    def eligible(self) -> int:
        return max(0, self.considered - self.excluded)

    @property
    def loss_rate(self) -> float:
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

    def stage(self, name: str, unit: str = "records") -> Stage:
        return self.stages.setdefault(name, Stage(unit=unit))

    def count(self, stage: str, n: int = 1, unit: str = "records") -> None:
        self.stage(stage, unit).considered += n

    def reject(self, reason: str, example: str = "", count: int = 1,
               stage: str = "join") -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + count
        self.rejection_stage[reason] = stage
        self.stage(stage).rejected += count
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
                if s.eligible and s.loss_rate > limit]

    def apply_to(self, coverage: Coverage) -> Coverage:
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
                           "loss_rate": round(s.loss_rate, 6)}
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
            lines.append(
                f"    {name:26} {s.considered:>8,} {s.unit:<13} "
                f"excluded {s.excluded:>7,}  lost {s.rejected:>7,}  "
                f"({s.loss_rate:.1%})")
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
        lossy = self.lossy_stages()
        lines.append(f"  lossy stages: {[n for n, _ in lossy] or 'none'}")
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


# NO VERIFIED KEY CARRIES SCHEDULED START. A previous version listed seven
# candidate field names; a replay of two real market payloads showed that none
# of them exists, so `market_start_time` returned None for every real market
# and the collector rejected all of them -- while the tests passed, because the
# fixtures supplied `game_start_ts`, a name that was invented and never
# observed. That is the same defect as the invented ticker shape, made twice.
#
# So this mapping is EMPTY, deliberately. Populate it from a recorded response
# once a field's semantics are confirmed -- and confirm them: `open_time` is
# the contract listing time, and the sample's occurrence time lands around game
# END, so neither is a substitute for first pitch.
#
# Until then the join degrades explicitly rather than guessing: see
# `join_markets`, which matches on PARTICIPANTS and only needs a start time to
# separate a doubleheader.
VERIFIED_START_KEYS: tuple[str, ...] = ()
VERIFIED_START_KEYS_ISO: tuple[str, ...] = ()


def market_start_time(market: dict) -> datetime | None:
    """Scheduled start of the underlying game, or None if no verified key.

    Deliberately does NOT fall back to `open_time`, `close_time` or an
    expiration: those are contract lifecycle facts, not first pitch, and using
    one would make every start-agreement check pass while meaning nothing.
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
    return None


def start_metadata_available() -> bool:
    """Whether any verified scheduled-start key is configured at all."""
    return bool(VERIFIED_START_KEYS or VERIFIED_START_KEYS_ISO)


def exchange_matchup(tickers: list[str]) -> frozenset[str]:
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
            out.add(parsed["yes_participant"])
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
    target = yes_participant.strip().upper()
    if home.resolved and home.abbreviation == target:
        return fair[0]
    if away.resolved and away.abbreviation == target:
        return fair[1]
    return None


def join_markets(
    markets: dict[str, dict],
    quotes_by_event: dict[str, list[SharpQuote]],
    league: str,
    ledger: Ledger,
) -> list[JoinedMarket]:
    """Match settled contracts to sharp events on PARTICIPANTS, then time.

    The previous version filtered candidates on start time alone, so on any
    normal slate every simultaneous game was a candidate for every market and
    the uniqueness check rejected them all as ambiguous: a MIL-BAL market was
    discarded because an unrelated BOS-NYY game started at the same minute.
    The safety property was right and the candidate set was wrong, which
    discarded valid data wholesale rather than mismatching it.

    Identity now comes from the matchup. Start time is used only where the
    matchup alone cannot separate two games -- a doubleheader -- and when no
    verified start key is configured, those are rejected explicitly instead of
    being resolved by guess.
    """
    # Group markets into events so an event's participant set can be derived.
    events: dict[str, list[str]] = {}
    for ticker, market in markets.items():
        event = kalshi_event_ticker(market)
        if event is None:
            ledger.reject("ticker_unparseable", ticker)
            continue
        events.setdefault(event, []).append(ticker)

    # Index sharp events by matchup.
    by_matchup: dict[frozenset[str], list[tuple[str, SharpQuote]]] = {}
    unresolved = 0
    for event_id, quotes in quotes_by_event.items():
        if not quotes:
            continue
        matchup = sharp_matchup(quotes[0], league)
        if matchup is None:
            unresolved += 1
            continue
        by_matchup.setdefault(matchup, []).append((event_id, quotes[0]))
    if unresolved:
        ledger.reject("sharp_teams_unresolvable", f"{unresolved} events", count=unresolved)

    joined: list[JoinedMarket] = []
    for event_ticker, tickers in events.items():
        matchup = exchange_matchup(tickers)
        if not matchup:
            ledger.reject("event_participants_unresolvable", event_ticker,
                          count=len(tickers))
            continue

        # Exact matchup when both contracts survived; containment when only one
        # did, which is weaker but still far stronger than time alone.
        if len(matchup) == 2:
            candidates = by_matchup.get(matchup, [])
        else:
            candidates = [c for m, cs in by_matchup.items() if matchup <= m for c in cs]

        if not candidates:
            ledger.reject("no_sharp_event_for_matchup",
                          f"{event_ticker} {sorted(matchup)}", count=len(tickers))
            continue

        if len(candidates) > 1:
            # Same teams more than once in the window: a doubleheader or a
            # reschedule. Only a start time separates them.
            start = market_start_time(markets[tickers[0]])
            if start is None:
                reason = ("doubleheader_needs_start_time"
                          if start_metadata_available()
                          else "doubleheader_unresolvable_no_verified_start_key")
                ledger.reject(reason, f"{event_ticker} matched {len(candidates)}",
                              count=len(tickers))
                continue
            timed = [c for c in candidates
                     if abs(c[1].commence_time - start) <= START_AGREEMENT]
            if len(timed) != 1:
                ledger.reject("ambiguous_start_match",
                              f"{event_ticker} matched {len(timed)} on time",
                              count=len(tickers))
                continue
            candidates = timed

        event_id, quote = candidates[0]
        start = market_start_time(markets[tickers[0]])
        if start is not None and abs(quote.commence_time - start) > START_AGREEMENT:
            ledger.reject("start_times_disagree", event_ticker, count=len(tickers))
            continue
        # With no verified exchange start, the sharp event's scheduled start is
        # the only one available. It is recorded as unverified so the report can
        # say so rather than implying the two sources agreed.
        effective_start = start or quote.commence_time

        for ticker in tickers:
            parsed = parse_kalshi_game_ticker(ticker)
            outcome = settlement_outcome(markets[ticker])
            if outcome is None:
                ledger.reject("no_readable_settlement", ticker)
                continue
            joined.append(JoinedMarket(
                market_ticker=ticker,
                event_ticker=event_ticker,
                yes_participant=parsed["yes_participant"],
                start=effective_start,
                outcome=outcome,
                provider_event_id=event_id,
                settled_at=settlement_time(markets[ticker]),
                start_verified=start is not None,
            ))

    ledger.count("markets_joined", len(joined))
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
        ledger.reject("no_sharp_quote_available_at_cutoff", joined.market_ticker)
        return None

    quote = max(usable_quotes, key=lambda q: q.last_update)

    # Age is measured at the DECISION timestamp, not at capture. One limit
    # governs both, rather than `--max-quote-age` applying at snapshot time
    # while a separate constant applied at the decision.
    age_at_decision = (decision_at - quote.last_update).total_seconds()
    if age_at_decision > max_quote_age:
        ledger.reject("sharp_quote_too_old_at_cutoff",
                      f"{joined.market_ticker} {age_at_decision:.0f}s")
        return None

    usable_candles = [c for c in candles if c.mid is not None and c.ts <= decision_at]
    if not usable_candles:
        ledger.reject("no_exchange_quote_at_cutoff", joined.market_ticker)
        return None
    candle = max(usable_candles, key=lambda c: c.ts)
    if decision_at - candle.ts > max_lag:
        ledger.reject("exchange_quote_too_old_at_cutoff", joined.market_ticker)
        return None
    if candle.has_malformed_price:
        ledger.reject("exchange_quote_malformed", joined.market_ticker)
        return None

    p_sharp = orient_probability(quote, joined.yes_participant, league, method)
    if p_sharp is None:
        ledger.reject("yes_side_unresolvable", f"{joined.market_ticker} YES={joined.yes_participant}")
        return None

    lead_minutes = (joined.start - decision_at).total_seconds() / 60.0
    if lead_minutes <= 0:
        ledger.reject("cutoff_at_or_after_start", joined.market_ticker)
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
