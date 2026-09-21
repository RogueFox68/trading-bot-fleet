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
from core.matcher import kalshi_event_ticker, parse_kalshi_game_ticker, resolve_team
from data.kalshi_history import Coverage, settlement_outcome, settlement_time
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
class Ledger:
    """Stage-by-stage denominators and rejection reasons.

    Eligibility exclusions (a market outside the study window, a deliberately
    skipped sport) are counted SEPARATELY from failures (an unparseable ticker,
    a join that could not be resolved). The first is the study's own choice;
    the second is a gap in what it could see, and only the second threatens
    whether the surviving sample is representative.
    """

    stage_totals: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    eligibility_exclusions: dict[str, int] = field(default_factory=dict)
    examples: dict[str, list[str]] = field(default_factory=dict)

    def count(self, stage: str, n: int = 1) -> None:
        self.stage_totals[stage] = self.stage_totals.get(stage, 0) + n

    def reject(self, reason: str, example: str = "") -> None:
        self.rejections[reason] = self.rejections.get(reason, 0) + 1
        if example:
            shown = self.examples.setdefault(reason, [])
            if len(shown) < 5:
                shown.append(example)

    def exclude(self, reason: str) -> None:
        self.eligibility_exclusions[reason] = (
            self.eligibility_exclusions.get(reason, 0) + 1
        )

    @property
    def total_rejected(self) -> int:
        return sum(self.rejections.values())

    @property
    def total_excluded(self) -> int:
        return sum(self.eligibility_exclusions.values())

    def unexplained_loss_rate(self) -> float:
        considered = self.stage_totals.get("markets_enumerated", 0) - self.total_excluded
        if considered <= 0:
            return 0.0
        return self.total_rejected / considered

    def apply_to(self, coverage: Coverage) -> Coverage:
        rate = self.unexplained_loss_rate()
        if rate > MAX_UNEXPLAINED_LOSS:
            coverage.fail(
                f"{rate:.1%} of eligible markets were lost to parse/join failures "
                f"({self.total_rejected} of "
                f"{self.stage_totals.get('markets_enumerated', 0) - self.total_excluded}); "
                "the surviving sample may be selected rather than representative"
            )
        return coverage

    def as_dict(self) -> dict:
        return {
            "stage_totals": dict(sorted(self.stage_totals.items())),
            "rejections": dict(sorted(self.rejections.items())),
            "eligibility_exclusions": dict(sorted(self.eligibility_exclusions.items())),
            "rejection_examples": {k: v for k, v in sorted(self.examples.items())},
            "unexplained_loss_rate": round(self.unexplained_loss_rate(), 6),
        }

    def render(self) -> str:
        lines = ["COLLECTION LEDGER"]
        for stage, n in sorted(self.stage_totals.items()):
            lines.append(f"    {stage:38} {n:>8,}")
        if self.eligibility_exclusions:
            lines.append("  excluded by design (not a gap):")
            for reason, n in sorted(self.eligibility_exclusions.items()):
                lines.append(f"    {reason:38} {n:>8,}")
        if self.rejections:
            lines.append("  LOST to parse/join failures:")
            for reason, n in sorted(self.rejections.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {reason:38} {n:>8,}")
                for ex in self.examples.get(reason, [])[:2]:
                    lines.append(f"        e.g. {ex}")
        lines.append(f"  unexplained loss rate: {self.unexplained_loss_rate():.1%}")
        return "\n".join(lines)


def market_start_time(market: dict) -> datetime | None:
    """Scheduled start of the underlying game.

    Deliberately does NOT fall back to `open_time`: that is when the contract
    was listed, which can be days before first pitch, and using it would make
    every start-time agreement check meaningless while looking like it worked.
    An unreadable start is a rejected market, not an assumed one.
    """
    for key in ("game_start_ts", "event_start_ts", "scheduled_start_ts", "expected_start_ts"):
        value = market.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, tz=timezone.utc)
    for key in ("game_start_time", "event_start_time", "scheduled_start_time"):
        value = market.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


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
    """Match settled contracts to sharp events on identity + start agreement."""
    starts: list[tuple[datetime, str, SharpQuote]] = [
        (quotes[0].commence_time, event_id, quotes[0])
        for event_id, quotes in quotes_by_event.items() if quotes
    ]

    joined: list[JoinedMarket] = []
    for ticker, market in markets.items():
        parsed = parse_kalshi_game_ticker(ticker)
        if not parsed:
            ledger.reject("ticker_unparseable", ticker)
            continue
        event_ticker = kalshi_event_ticker(market) or parsed["event_ticker"]

        outcome = settlement_outcome(market)
        if outcome is None:
            ledger.reject("no_readable_settlement", ticker)
            continue

        start = market_start_time(market)
        if start is None:
            ledger.reject("no_readable_start_time", ticker)
            continue

        candidates = [
            (event_id, quote) for when, event_id, quote in starts
            if abs(when - start) <= START_AGREEMENT
        ]
        if not candidates:
            ledger.reject("no_sharp_event_at_that_start", f"{ticker} @ {start.isoformat()}")
            continue
        if len(candidates) > 1:
            # Two sharp events within the agreement window: a doubleheader whose
            # games are close together, or a reschedule. Resolving it by picking
            # one is how the first version attached the wrong game.
            ledger.reject("ambiguous_start_match", f"{ticker} matched {len(candidates)}")
            continue

        event_id, _ = candidates[0]
        joined.append(JoinedMarket(
            market_ticker=ticker,
            event_ticker=event_ticker,
            yes_participant=parsed["yes_participant"],
            start=start,
            outcome=outcome,
            provider_event_id=event_id,
            settled_at=settlement_time(market),
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
    usable_quotes = [
        q for q in quotes
        if q.last_update is not None
        and q.last_update <= decision_at
        and q.is_fresh(max_quote_age)
    ]
    if not usable_quotes:
        ledger.reject("no_fresh_sharp_quote_at_cutoff", joined.market_ticker)
        return None
    quote = max(usable_quotes, key=lambda q: q.last_update)
    if decision_at - quote.last_update > max_lag:
        ledger.reject("sharp_quote_too_old_at_cutoff", joined.market_ticker)
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
        exchange_at=candle.ts,
        devig_method=method,
    )
