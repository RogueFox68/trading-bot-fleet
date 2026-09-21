# Pre-Match +EV — Thesis Test

A read-only study. It holds no trading credentials, imports no exchange SDK,
and cannot place an order.

## What it asks, and what that is worth

> Does the de-vigged sharp line forecast settlement better than the prediction
> market's own price — and would acting on the difference have paid, net of the
> fee actually charged?

Those are **two** questions. An earlier version asked only the first and
treated a win on it as a go signal. Better forecasting is neither sufficient
for positive net return (the spread and fee can eat it) nor necessary for a
useful strategy (a globally weaker forecast can still improve decisions on a
predefined subset). Both are now reported, separately.

### The premise is a hypothesis, not ground truth

"Pinnacle is fair value" is the assumption under test, not a given:

- The odds provider takes Pinnacle prices from the **public website**, which
  may itself be delayed. A disagreement can mean the exchange is wrong, the
  book is stale, the feed is lagging, or the two instruments settle on
  different rules.
- Avoiding in-play trading removes one latency race. Pre-game injury, lineup
  and starting-pitcher news is still a race.
- **Shin is a candidate de-vig model, not a proof** that longshot bias has been
  removed. Run both and compare on held-out data; `--devig` selects.

### Which strategy is being tested

This code tests **one pre-game forecast against settlement**, i.e. buy before
the game and hold. It does **not** measure whether a pre-game repricing can be
caught and monetised before start — that needs entry-to-exit returns, both
transaction costs, depth, and failed-exit behaviour, none of which is here.

## What the review found, and what changed

Every item below was a real defect, each with a reproduction.

| # | Defect | Effect |
|---|---|---|
| 1 | `regions=us` requested while the parser accepted only Pinnacle, an **EU**-listed book | Every call cost 10 credits and returned nothing usable |
| 2 | Kalshi serialises `*_dollars` as a **string**; the parser took only numbers | Every real bid/ask decoded to `None`; candles dropped as "thin coverage" |
| 3 | Ticker regex matched an **invented** shape (`...-BUF-KC`) | Real tickers (`KXMLBGAME-26SEP201920MILBAL-MIL`) all returned `None` |
| 4 | Join compared id **suffixes** and ignored the date | A July 5 market joined to a July 4 game; doubleheaders collapsed |
| 5 | Only the live `/markets` endpoint was enumerated | Markets settled before the archive cutoff silently missing, reported `complete` |
| 6 | The bookmaker's `last_update` was discarded | A stale line in a fresh envelope read as edge |
| 7 | Sharp and exchange records chosen from **different windows** | One predictor could hold 15 minutes more information |
| 8 | Bootstrap resampled **rows**, not games | 200 copies of one row → n=200, **zero-width CI**, "SHARP LINE WINS" |
| 9 | Disagreement "hit rate" counted the favoured side's **base rate** | Calibrated exchange scored 80% "wrong"; correct sharp longshot scored 26.5% |
| 10 | Probability always taken from the **home** team | Away-team contracts would receive an inverted signal |
| 11 | Join/parse losses never reached `coverage` | A run could discard most markets and still certify `complete` |

Fixtures are now copied from observed responses rather than invented, which is
what let defects 2 and 3 pass 77 tests.

## Reading the result

Six sections. **No single number is a go signal.**

1. **Forecast accuracy** — Brier and log loss, with a **cluster** bootstrap CI
   on the paired difference. Clusters are games, not rows: Kalshi lists one
   contract per team and their outcomes are complements.
2. **Net return** on a **predeclared** eligible subset, priced at the
   executable quote (YES at the ask, NO at `1 − bid`) net of the venue fee.
3. **Where they disagree** — a conditional *proper score*, not a hit rate.
4. **Calibration** — by price band.
5. **Over time** — with intervals. A drifting point estimate is not a trend;
   game mix and noise move it too.
6. **Go criteria** — all four must hold.

### Go criteria

```
coverage complete
sample above the game floor
sharp forecasts better          (CI excludes zero)
positive net return             (CI excludes zero, on the declared subset)
```

**"Insufficient evidence" means insufficient evidence** — not that the strategy
is dead. And the 200-game floor is a floor, not a power calculation: the
interval is what says whether the evidence is enough.

### Freeze before you look

Thresholds, lead time, price band, de-vig model and market eligibility go in
`Eligibility` and the CLI flags. **Set them before running a chronological
holdout**, and report sensitivity to latency, spread, fee and de-vig choice.
Tuning them after seeing returns is how a study confirms itself.

## Running it

```bash
cd research/prematch_ev
cp .env.example .env              # ODDS_API_KEY
python3 run_study.py --plan --sport MLB --from 2026-05-13 --to 2026-09-15
python3 run_study.py --probe --sport MLB
python3 data/kalshi_history.py --audit-abbreviations --series KXMLBGAME --league MLB
python3 run_study.py --sport MLB --series KXMLBGAME --from 2026-05-13 --to 2026-09-15
```

Tests: `python3 -m unittest discover -s tests -t .` — 142 tests, no network, no
credentials, and they pass with or without `rapidfuzz`.

Artifacts land in `study_output/`: `report.txt`, `observations.json` (both
source timestamps, actual lead, YES participant, bid/ask), and `coverage.json`
(stage denominators, every rejection reason with examples, and the run config).

### Archive depth

The Odds API publishes **MLB history from June 2020** — earlier than this study
originally claimed. "~May 2026" was wrong and must not be treated as a
provider-wide archive start. What matters is coverage for the **particular
bookmaker and market**, which is narrower than sport coverage and is what
`--probe` checks. Note the probe can mistake an **off-season** empty snapshot
for absent history: probe an in-season date.

## What this does not establish

**Maker fills are not simulable, and "fill everything" is not an upper bound.**
Queue position is unrecoverable from historical data. An earlier version of
this file claimed that assuming every maker fill gives a profit ceiling — it
does not: unfilled hypothetical winners can offset real adverse-selected
losers, so adding imagined fills moves the total in **either** direction. A
valid bound needs an explicit fill-selection argument. Keep maker scenarios
separate from evidence of achievable returns.

**Markouts are not fill-conditioned adverse selection.** Price movement after
every hypothetical quote describes the market, not what you would have been
filled on. Estimating adverse selection requires conditioning on actual or
plausible execution. `observations.json` currently stores one timestamp's
bid/ask — not a forward path — so that analysis is not yet possible here.

**Depth is absent.** Returns are computed against a historical quote with no
size attached: indicative of an opportunity, not demonstrated fills.

**Fixed costs are excluded.** The data subscription and operating cost are not
in the return figure; include them separately before calling anything viable.

## Verify before trusting

- **Fee schedules vary by series.** The generic Kalshi coefficients are a
  default, not a universal rate; resolve the applicable schedule into
  `fees.SERIES_OVERRIDES` and `describe()` will say whether you did. The
  Polymarket **US** entity deliberately raises rather than borrowing the
  international θ. (An earlier comment claimed maker fees "usually round to
  $0.00" — impossible: `ceil` of any positive fee is ≥ 1¢, and the rounding
  makes *small* orders relatively more expensive.)
- **Roster abbreviations are unverified** against live Kalshi tickers. Run the
  audit; a mismatch shows up as a team contributing no data, not as an error.

## Relationship to the fleet

None, deliberately. No `fleet_registry` entry, no PM2 app, separate
`requirements.txt`, no imports in either direction. It lives here because the
Beelink is where any eventual service would run and the observability plane is
worth inheriting; it lifts out to its own repo if the thesis survives.

## If it survives

Only then: a read-only **forward recorder** of signals, depth and latency —
because paper fills establish no queue priority. Any execution service after
that needs start-time order expiry, position and exposure reconciliation, and
event-level risk accounting built for Kalshi.
