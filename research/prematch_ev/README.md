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

## Live smoke test: 0 observations, and what it found

A two-day run with working odds access built **nothing**:

```
kalshi: 9,128 settled markets -> 100 in window (9,028 outside)
discovery: 20 scheduled starts over 4 snapshots
targeted: 16 snapshots -> 37 sharp events
joined: 2 contracts
built: 0 observations          200 credits spent
```

Six defects, all reproduced. The most damaging was the one that hid the rest:

**The failing stage reported 0% loss.** The ledger printed
`join: 0 considered, 100 lost (0.0%)` and omitted the stage from
`lossy_stages` — `reject()` defaulted to a stage nothing ever counted, and a
zero denominator returned a reassuring zero. A run that dropped *every*
contract certified coverage complete. A stage with rejections and no
denominator is now `UNACCOUNTED`, `loss_rate` is `None` rather than `0.0`, and
coverage fails on it.

**The scheduled start was in the ticker all along.** `26SEP152140MIAAZ` is a
fixed-width date and time followed by the teams. The 11-character prefix is
unambiguous; only the *team* tail is not (`MIAAZ` = `MIA|AZ` or `MI|AAZ`) — and
that tail isn't needed, because the YES suffixes already give the
participants. Earlier rounds hunted for a start *field*, invented seven key
names, and rejected every real market while the fixtures agreed with the
invention.

The **timezone is a declared assumption**, not a verified fact. It is
cross-checked per game against the sharp feed's own `commence_time`; agreement
and disagreement are counted in a `start_time_crosscheck` stage, and a game
whose sources disagree is rejected rather than silently shifted.

**Ordinary rematches were called doubleheaders.** 54 contracts dropped:
candidates were indexed by team pair alone, so a series on successive dates
produced three candidates and the uniqueness check rejected all of them.
Identity is now `(matchup, local date)`, with time reserved for a genuine
same-day doubleheader. The date is taken in the *schedule's* timezone — a
21:40 ET game is the next day in UTC, which would file the two sources of one
game under different days.

**Exchange codes are not canonical abbreviations.** 44 contracts dropped
because the exchange says `AZ` and "Arizona Diamondbacks" resolves to `ARI`.
Only observed codes are aliased; an unknown code is rejected **by name**, so
one run enumerates the whole gap instead of a guess hiding it.

**Retrieval padding was being used as eligibility.** Sept 13 and Sept 16
tickers were selected for a Sept 14–15 run. Settlement ±1 day remains the
*retrieval* filter; the declared **game** window is now a separate eligibility
filter on the verified start.

**Artifacts are written on zero observations.** The run returned before
creating the output directory, losing the diagnostics exactly where they were
most needed. `coverage.json` is now always written, and the failure message
names the top failing stages instead of suggesting `--probe`.

### A consequence worth noting

The exchange enumeration is free and now yields the schedule, so the discovery
pass is gone entirely and the targeted cutoffs are derived from the games
actually being studied. The previous run derived cutoffs from a separate
discovery grid that never covered the two contracts it managed to join — which
is why both then failed `no_sharp_quote_available_at_cutoff`.

## Before any paid run

Two crashes shipped with 171 tests passing, because nothing drove `main()` or
the audit command. A component suite cannot catch a missing import in an entry
point — only calling the entry point can. `tests/test_cli.py` now does.

```bash
# 1. FREE. Enumerates every exchange code the roster does not know,
#    so the alias gap closes in one pass, not one code per paid run.
python3 data/kalshi_history.py --audit-abbreviations --series KXMLBGAME --league MLB

# 2. FREE. The REAL cutoff count and credit cost, from the actual schedule.
#    `--plan` is an offline estimate and cannot validate this.
python3 run_study.py --preflight --sport MLB --series KXMLBGAME \
    --from 2026-09-14 --to 2026-09-15

# 3. Paid, capped, cached.
python3 run_study.py --sport MLB --series KXMLBGAME \
    --from 2026-09-14 --to 2026-09-15 --max-credits 300
```

**`--max-credits` is a hard stop, not a warning.** Collection halts and keeps
its partial diagnostics rather than the overspend being discovered afterwards.

**Responses are cached** under `--cache-dir`, so debugging a local join or
report never costs credits twice. The credential never enters a cache key, a
path or a log: keys are built from the request's *meaning* (sport, instant,
book, market), and `redact()` scrubs anything destined for a message. Only
successful, parseable responses are stored — caching a failure would make a
transient outage permanent on replay.

**Cutoffs are not clipped to the study window.** A 00:30 UTC game at a
60-minute lead needs the previous day's 23:30 snapshot. Widening `--from`
would change the study universe, which is a different thing from fetching the
inputs that universe needs.

## Reading the result## Reading the result

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

### Readiness, not GO

```
coverage complete
traded sample above floor        (games the POLICY would have traded)
positive net return              (CI excludes zero, on the frozen policy)
—
forecast accuracy                DIAGNOSTIC ONLY, gates nothing
```

Global Brier superiority is **not** a gate. It is neither sufficient for net
return nor necessary for a useful conditional policy, and making it mandatory
contradicted that. The sample gate counts games the policy would have *traded*,
so a large unrelated universe cannot satisfy it for a tiny trading subset.

**Every run of this code is EXPLORATORY.** No chronological holdout and no
delay/cost robustness check exist here, so nothing it prints can be a GO.
Passing every line means the policy is worth testing out of sample.

**"Insufficient evidence" means insufficient evidence** — not that the strategy
is dead. The 200-game floor is a floor, not a power calculation.

### The selection rule is predicted net EV

Eligibility screens on **predicted EV at the executable price** — YES at the
ask, NO at `1 − bid`, each with its own fee — not on disagreement with the
midpoint, which is not a price anyone trades at. Both sides are priced and the
better predicted EV wins; the direction is not taken from the sign of a
midpoint gap. Midpoint disagreement survives only as a diagnostic.

### Freeze before you look

Thresholds, lead time, price band, de-vig model and market eligibility go in
`Eligibility` and the CLI flags. **Set them before running a chronological
holdout**, and report sensitivity to latency, spread, fee and de-vig choice.
Tuning them after seeing returns is how a study confirms itself.

## Snapshots are targeted, not gridded

Two settings that were individually reasonable and jointly fatal: a fixed grid
of **8 snapshots/day** steps every 180 minutes against a **15-minute** freshness
bound, so essentially no decision cutoff had a fresh quote. A fully working
collector returning an empty answer — the worst failure shape, because it looks
like a result.

A grid fine enough to satisfy the bound needs 96/day: **~121,000 credits** for a
126-day season, far past any sane tier. So fetches are **targeted at the
decision cutoffs the games actually imply**:

1. **Discovery pass** — a coarse grid (2/day) purely to learn when the games are.
2. **Targeted pass** — one fetch per distinct cutoff, floored to the archive's
   5-minute snapshot grid and deduplicated. Games cluster on common start
   times, so a 15-game slate needs about three fetches, not fifteen.

**~10,000 credits** for the same season. `cadence_is_viable()` refuses a
grid-only configuration that cannot satisfy the bound, rather than running it
and returning nothing.

## Fees must carry the series

`fee_for()` takes a `series` and threads it to the venue model. A resolved
`SERIES_OVERRIDES` entry that the *pricing* path cannot see is worse than no
feature: `describe()` reported an override as resolved while every fee was
still computed at the generic rate. `describe(series)` now names whether **this
run's** series has a resolved schedule, and says plainly when it does not.

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
