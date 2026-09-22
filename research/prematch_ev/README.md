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

**A retry is budgeted, not free.** The reservation happens immediately before
*each* network attempt, not once before the retry loop — a provider can process
and charge a request whose response never reaches us. Reserving once let three
attempts run against a single debit, so a 10-credit cap permitted three
chargeable requests. `--preflight` therefore reports a **base** cost and a
worst case at `RETRIES` attempts; the base figure is not a guaranteed bill.

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

## Study coverage vs provider diagnostics

These are different questions and were being conflated. A snapshot
legitimately carries events the study never asked about — other days, other
games, times nowhere near a decision cutoff. A live run returned 344
event-quotes across snapshots; 88 had empty bookmaker arrays, **78 of them for
a day outside the declared window**, and none was a required observation. All
40 target contracts resolved, yet coverage failed.

Coverage is now measured against the **independently enumerated target
universe** — the eligible contracts and the decision cutoffs they require,
counted from the free exchange side. Provider-side counts are marked
`diagnostic`: reported in full, never gating. The denominator is deliberately
**not** derived from how many observations succeeded, because a denominator
defined by its successes always reads 100%.

## Telling absent edge from a broken collection

Both print "no eligible trades". `SCREEN DIAGNOSTICS` separates them: counts by
filter (price band, lead time, missing quotes, spread, below the EV floor) plus
the distribution of best predicted net EV and best gross edge per contract.

A real run produced predicted net EV between **−$0.0247 and −$0.0145** with a
maximum *pre-fee* edge of **$0.0055** — the fee alone exceeds the best gross
edge found. That is a legitimate no-trade result, and it is visible as one.
**It is not a reason to loosen the frozen threshold.**

Those figures were priced at the generic 0.07 coefficient, before the dated
schedule landed. The best quote on that sample survives every combination of
the corrections below and stays negative:

| taker coeff | account route | fee on the best quote | best net EV |
|---|---|---|---|
| 0.07 (generic) | non-direct, `$0.01` | `$0.0200` | **−$0.014525** |
| 0.035 (dated, Sept 2026) | non-direct, `$0.01` | `$0.0100` | **−$0.004525** |
| 0.035 (dated, Sept 2026) | direct, `$0.0001` | `$0.0088` | **−$0.003325** |

The first two reproduce the review's own offline sensitivity check exactly; the
third is the most favourable combination available and is still below zero.
`tests/test_fees.py::ReviewSensitivityTest` pins all three. This is a 20-game
sample and settles nothing — it says the correction did not turn a no into a
yes, which is the only thing a sensitivity check can say.

## Three different credit numbers

`x-requests-used` is the **account's cumulative** usage across every run.
Presenting it as "credits used" made a 140-credit run report 340. The ledger
now names all three: this run's spend (what `--max-credits` acts on), the
account cumulative, and the remaining balance.

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

## The lead-time grid: days, not one late snapshot

The study began with ONE checkpoint **60 minutes** before start. That measures
the late pre-game market and nothing else — and the thesis it exists to test,
that injuries, scratches, suspensions and other pre-game news reprice a game
over **days**, lives almost entirely outside that window. A single late
snapshot cannot reject that thesis, because it was never looking where the
effect would be. The prior late-window result is a **baseline**, not a verdict.

The default grid is **72h, 48h, 24h, 12h, 6h, 3h**, with the 60-minute point
retained as a separately labelled baseline. `--lead-grid` changes it.

This is **exploratory design, recorded as such** — chosen after inspecting the
baseline window, so it is not a preregistered test. Sept 1–15 is development
data that has been looked at; a later, untouched window must be reserved as a
chronological holdout before anything is evaluated on it.

**Inputs reach back before the study window; the universe does not.** A 72-hour
checkpoint on a Sept 1 game needs an Aug 29 snapshot. Fetching that snapshot is
not the same as studying Aug 29's games — those are still excluded on their own
start times. The two filters are separate and stay separate.

**The eligibility ceiling follows the grid.** It was `24 * 60` inline, and a
literal is exactly what makes a widened grid a silent filter: every 72h and 48h
row would be fetched, then dropped for being "too far from start", and the
screen diagnostics would report that as *absent opportunity*. `run_study`
derives the ceiling from the grid in use and **refuses** a ceiling that does not
reach the earliest checkpoint rather than applying it quietly.

### Coverage is game × checkpoint, and three outcomes are kept apart

The denominator is every (contract, checkpoint) cell the **enumeration** says
should exist, established before any quote is fetched. A denominator defined by
its own successes always reads 100%.

| group | meaning | counts as |
|---|---|---|
| `not_listed` | the contract did not exist yet (`decision_at < open_time`) | **a result** — there was no opportunity to miss |
| `no_quote` | listed, but nobody quoted it or the quote was unusable | **a result** — listed but untradeable |
| `source_failure` | the archive, parser or provider failed | **the only group that threatens the sample** |
| `listing_unknown` | `open_time` unreadable — not evidence either way (rule 17) | its own bucket |
| `unreported` | no run resolved this cell | expected before a run, a bug after one |

An unrecognised status counts as a **source failure**, so a new failure mode
cannot land in a benign bucket by default.

### A reaction delay, because instant is not a neutral default

`--entry-delay-minutes` makes a signal seen at *t* enter at the first quote
at/after *t + delay*, within a declared tolerance and strictly before start.
Zero delay keeps the **instantaneous bound** bit-for-bit, so the baseline stays
comparable — but it is labelled as a bound, not as neutral.

A **missing** delayed quote is not a fill at the price you saw. Substituting the
observed price is precisely the error this parameter exists to measure, and it
would do it at the exact moment the market had moved away from you. The cell
records `no_entry_quote_after_delay` instead.

**The strategy is a committed signal, and the two books are separate.** The
trigger and the side are frozen at *t*, on the book visible then
(`exchange_bid`/`exchange_ask`); execution is then priced at *t + delay*
(`entry_bid`/`entry_ask`). Nothing downstream of the decision reads the
execution book except the realised cost.

The first version wrote the execution quote straight into the decision book,
which is what the screen and the side selection read. That let a delayed run
**qualify a trade the signal had not triggered**, or **flip YES to NO**, using a
price that did not exist at *t* — lookahead in the costume of a cost model. The
cost of being late must show up as a **worse return**, never as a trade that
quietly leaves the sample or reverses direction. At zero delay the two books are
the same candle, so the bound is unchanged.

### Repeated checkpoints are not independent bets

Seven checkpoints on one game are seven looks at **one outcome**. Summing every
qualifying row books the same settlement repeatedly and hands the bootstrap
seven times the evidence it has; reading off whichever checkpoint did best is
retrospective selection. Neither is a policy anyone could have followed.

Selection applies the **full** `Eligibility` rule — price band, lead bounds and
spread cap, not just the net-EV floor. Gating on `as_trade` alone (which knows
only about side prices and the threshold) let selection buy books the screen
rejects, and since the policy path *is* the headline return, the screened figure
was looser than the unscreened one. A rejected look leaves the game **open**, so
one unexecutable early quote cannot silently cancel every later chance to trade
it.

The predeclared baseline: process checkpoints **chronologically** and take the
**first** that qualifies, then stop looking at that game. Mutually exclusive
contracts deduplicate through the same rule — both team contracts share a
`game_id`, so taking one closes the game to the other. Per-checkpoint figures
are shown **separately and never summed**, under a header that says so.

### What a sparse grid cannot do

It can test whether the two prices **diverge over days**. It cannot establish
minute-scale reaction lag, and it does not claim to have caught every
news-driven move — only what happened to fall between two checkpoints. The
report says this where the price paths are printed. A denser history around
candidate moves is a **separately costed** design, and selection for it must use
only information available at the time.

**Price movement is not news attribution.** The multi-day price study runs
without news labels, and it must not be described as measuring injuries,
scratches or suspensions. Those claims need timestamped, historically available
news records, and no retrospective attribution.

### What the grid costs

Cutoffs deduplicate across games *and* checkpoints, so N checkpoints cost far
less than N times one. Modelled on a ~10-games/day slate over 15 days, using the
study's own `decision_cutoffs`:

```
72h    +150 new cutoffs   (running  150 = 1,500 credits)
48h    + 10               (running  160 = 1,600)
24h    + 10               (running  170 = 1,700)
12h    +150               (running  320 = 3,200)
6h     +150               (running  470 = 4,700)
3h     +150               (running  620 = 6,200)
1h     +136               (running  756 = 7,560)
```

**The three 24h-multiple checkpoints are nearly free together.** Teams play the
same clock time on consecutive days, so 48h and 24h land almost entirely on
cutoffs the 72h pass already bought. The cost is in the *intra-day* points —
12h, 6h, 3h, 1h — each of which is a full fresh pass. If the budget is tight,
dropping 12h and 6h saves ~3,000 credits while 48h and 24h cost almost nothing
on top of 72h. Whole-grid cost is about **5×** a single checkpoint, not 7×; the
hard upper bound with no collisions at all would be 7×.

`--preflight` reports the real figure for a real window, including how many of
those snapshots are **already cached** and therefore free.

## Sport support, and what "supported" does not mean

`python3 run_study.py --support --sport NFL --series KXNFLGAME` reads this out
of the code. No network, no key, no cost.

| league | odds key | roster | start source | roster-ready | schedule-ready | collectable | point-in-time |
|---|---|---|---|---|---|---|---|
| MLB | `baseball_mlb` | 30 | `event_ticker` | yes | yes | **yes** | **yes** |
| NFL | `americanfootball_nfl` | 32 | `external_schedule` | yes | yes | **yes** | **NO** |
| NBA | `basketball_nba` | **0** | none | no | no | **no** | no |
| NHL | `icehockey_nhl` | **0** | none | no | no | **no** | no |
| NCAAF | **none** | **0** | none | no | no | **no** | no |

**Roster-ready and schedule-ready are different questions, and NFL is why they
are reported apart.** Structural ticker parsing *is* league-agnostic — series,
event and YES participant come out of any of these. **Deriving a start is not:**

```
MLB  26SEP152140MIAAZ   date + HHMM + teams   -> the start is in the ticker
NFL  26SEP14DENKC       date + teams, NO TIME -> the ticker cannot say when
```

NFL has 32 teams and a valid odds key, and before the external schedule landed
every one of its contracts failed on `no_readable_start_time`. A single "ready"
flag said yes.

`START_SOURCES` therefore declares per league where a start comes from. It must
never be inferred from `close_time`, `expected_expiration_time` or
`settlement_ts` — on the sampled KC contract those are 03:15:19Z, 03:15:00Z and
03:21:19Z on the day *after* the game. A study whose lead times count back from
the final whistle is measuring the wrong thing precisely.

### NFL: the kickoff comes from outside, and that changes what a run may claim

NFL's missing kickoff is supplied by `data/espn_schedule.py`, an isolated
adapter over the **free, public** ESPN NFL scoreboard. It is injectable
(transport, cached raw JSON, or an offline directory of payloads), costs no
credits, and holds no credential.

**The ticker day is not the UTC kickoff day**, and the adapter is built around
that:

```
KXNFLGAME-26SEP13DALNYG  ticker day 2026-09-13  ESPN 401872930  kickoff 2026-09-14T00:20Z
KXNFLGAME-26SEP14DENKC   ticker day 2026-09-14  ESPN 401872931  kickoff 2026-09-15T00:15Z
```

Both are US evening games whose UTC instant lands after midnight. Candidates
are compared on the **local (US Eastern) day**, identity comes from the YES
suffixes — never from splitting `DALNYG`, which has several readings — and the
match must be **unique**. The day offset actually used is recorded on every
resolution, so a wrong timezone assumption shows up as a population of non-zero
offsets rather than as quietly shifted timestamps.

Everything else is a **named failure, never an inferred kickoff**: missing or
TBD time (a missing `timeValid` is a rejection, not an assumed-valid kickoff),
naive timestamp, event/competition date disagreement, wrong competitor count,
unknown team, ambiguous matchup, cancellation, a kickoff that changes between
buckets, and a provider failure — which is a *loss*, not an empty slate. Scores
are never carried: the endpoint serves results and the study must not see one.

**Three questions are now reported apart, because collapsing any two of them is
how NFL read "ready" while producing no observation:**

| question | what it means | where it is answered |
|---|---|---|
| **schedule-ready** | an adapter *can* derive a kickoff | `--support`, static |
| **runtime coverage** | one *was* derived, for this run's contracts | preflight, per run |
| **point-in-time** | it is what was known *at the decision* | `--support`, and **NO** for NFL |

**A schedule fetched now does not prove what was known 72h before kickoff.** A
flexed or rescheduled game carries its *final* time here. Every snapshot,
manifest and observation therefore carries
`historical_schedule_as_of = "unverified"`. That is a scope limit, not a
footnote: an NFL run can size availability and cost, and it **cannot certify a
point-in-time backtest or a live strategy**. A prospective or holdout run needs
schedule snapshots recorded *before* the decisions they date.

Provenance is persisted and **joinable**: `coverage.json` carries the source
URL, ESPN event id, retrieval timestamp, raw-payload SHA-256 and resolved
kickoff per game, plus the Kalshi-event → provider-event map that links them to
the observations.

The free command, end to end:

```bash
python3 run_study.py --support  --sport NFL --series KXNFLGAME
python3 run_study.py --preflight --sport NFL --series KXNFLGAME \
  --from 2026-09-01 --to 2026-09-15 --lead-grid 72h,48h,24h,12h,6h,3h
```

**`--from`/`--to` are UTC bounds**, so a US evening game on the last day of the
window starts on the *next* UTC day and falls outside it. Those semantics are
deliberately unchanged — they define the existing MLB baseline's universe — but
the run now counts and names that exclusion
(`game_outside_window_utc_boundary`) and says which way to widen `--to`, rather
than quietly returning a thinner final slate.

**An empty universe is not a free study.** The live NFL preflight went
826 settled → 32 retrieved → **0 eligible**, all 32 rejected as
`no_readable_start_time` — and reported *coverage complete, cost 0, exit 0*.
The ledger had measured the loss correctly and printed `contracts lost 32
(100%)` right beside it; nothing consulted it. `collect()` applied the loss
ledger to coverage, the **free** path never did — so the cheapest way to run
the study was also the only way to have it certify itself. `survey()` now
applies it before returning.

**`--sport NBA` used to be accepted, survive preflight, and fail at JOIN time**
— after every snapshot had been paid for — because the join maps bookmaker team
*names* onto exchange team *codes* and neither NBA nor NHL has a roster. That is
the `regions=us` defect from round 1 in different clothes: a run that costs
credits and returns nothing, whose only symptom is an empty result that reads
like an absent edge. `survey()` now refuses such a sport before anything is
spent, and `--support` exits non-zero.

**`ready` means the JOIN can work. It does not mean data exists at any lead
time.** Those are separate questions, and conflating them is exactly how "we
could not see it" becomes "there was nothing there".

Two caveats the report raises for NFL specifically:

- **No exchange-code aliases are recorded.** That is an unverified assumption,
  not evidence there are none — MLB needed `AZ→ARI`, and a missing alias shows
  up as a team contributing no data, never as an error.
- **No dated fee schedule for `KXNFLGAME`.** The generic coefficients would be
  assumed, and every return figure would inherit that. The MLB schedule *halved*
  the taker coefficient, so this is not a rounding concern.

The ticker parser and the identity path are league-agnostic — identity comes
from the YES suffixes rather than splitting the concatenated team tail — so an
NFL or NCAA-shaped ticker parses **structurally** today. Deriving a start is a
separate question with a separate answer per league, and running the two
together is how an invented fixture (`KXNFLGAME-26SEP211300BUFKC`, whose `1300`
was composed rather than observed) came to support a claim that NFL was ready.
The fixtures are now copied from real contracts.

### What the first multi-day run found

Sept 1–15, full grid, zero delay, one entry per game, thresholds unchanged.
402 contracts enumerated, 400 joined, **1,595 observations over 200 games**.
Coverage INCOMPLETE. Observed cells per contract, by checkpoint:

| lead | observed | not listed | no sharp quote |
|---|---|---|---|
| 72h | **0** | **400** | — |
| 48h | **0** | — | **400** |
| 24h | 95 | — | 304 |
| 12h | 322 | — | 76 |
| 6h | 378 | — | 22 |
| 3h | **400** | — | — |
| 1h | **400** | — | — |

**The days-ahead window is not observable from these two sources.** Kalshi
contracts do not exist at 72h, and the sharp feed does not carry the game at
48h. Coverage only becomes usable inside ~12 hours of first pitch — which is
roughly the window the study already had.

This is a **data-availability** finding, not a verdict on the thesis. It says
these sources cannot see the period the thesis is about; it does not say
nothing happens there. The headline policy selected 1 trade, which is not
feasibility evidence in either direction. Aggregate checkpoint loss was
805/2,414 eligible cells (33.3%).

The next question is therefore a *sourcing* question — when do Pinnacle prices
and Kalshi listings actually become available, and is there a feed that covers
the earlier window — not a bigger run against the same two sources.

**The measured figure for Sept 1–15 on the full grid** (run free against the
real cache): 402 contracts, **2,814 cells**, **732 unique snapshots**, 145
cached, **587 missing → 5,870 new credits**, worst case 17,610 with retries.
That validates the structural claim — 732 ÷ 145 = **5.05×** a single checkpoint,
against 5.04× modelled — while the model's headline understated the existing
cache by an order of magnitude (14 assumed, 145 real).

## Fees must carry the series *and the date*

`fee_for()` takes a `series` **and an `at`**, and threads both to the venue
model. A resolved schedule the *pricing* path cannot see is worse than no
feature: `describe()` once reported an override as resolved while every fee was
still computed at the generic rate.

The date is not a refinement. Kalshi publishes per-series fee changes with a
`scheduled_ts`, and KXMLBGAME's multiplier went from **1 to 0.5 on
2026-08-07T04:59:45.131Z** (id `38032af2-e3fa-4659-9280-da64300b544c`, read from
the public `fee_changes` endpoint on 2026-09-21). So the taker coefficient for a
September 2026 decision is **0.035, not 0.07** — a factor of two on every fee in
the study. Pricing by series alone gives one rate for all of history; pricing at
"now" silently reprices the study's own past every time it is re-run.
`resolve_schedule()` is strictly `effective_from <= at`, and refuses (rather
than extrapolating) both a scheduled series with no timestamp and a date earlier
than the first recorded entry.

**The conflict is carried, not resolved.** Kalshi's fee-schedule PDF dated
2026-07-07 still lists multiplier 1 for this series. The dated API says 0.5. The
API is dated and the PDF is not, which is why the API is used — but
`SCHEDULE_CONFLICTS` puts the disagreement into `describe()` so no report can
print "verified" over an open question.

**Maker quotes on a scheduled series raise.** `quadratic_with_maker_fees`
establishes that makers are charged; no source consulted says what, or whether
the multiplier scales maker fees. The study prices takers only, so refusing
costs nothing and removes an invented number.

## The account route is unresolved, so both are reported

Kalshi's rounding rules ceil the model fee to `$0.000001`, then align the charge
to the account's balance precision: **`$0.0001` for a direct member, `$0.01` for
a non-direct member**. Which applies is a fact about the *account*, not the
market, and this study does not know it.

At the one-contract size the study prices at, that quantum is not a rounding
digit — it is most of the fee. A 50c September taker contract owes `$0.00875`
raw, which a non-direct account pays as `$0.01` (+14%) and a direct account as
`$0.0088` (+0.6%). So the report renders **both routes, labelled**, flags which
one the narrative sections used (`--fee-route`), and `readiness()` carries a row
saying whether the conclusion survives the difference. Neither is adopted.

One ambiguity in the source turns out not to matter, and that is worth knowing:
its prose says centicents while its own example table shows cents. That sits at
the *model* step — and since `$0.0001` and `$0.01` are both whole multiples of
`$0.000001`, ceiling to the model precision and then to the alignment gives
exactly the alignment ceiling alone. The prose/table conflict is confined to the
account-route question, which is the one reported both ways.

**Raw fees are computed in `Decimal`.** In float, `0.035 * 100 * 0.50 * 0.50` is
`0.8750000000000001`, and a ceiling turns that last bit into a whole extra
quantum — billing an exact `$0.8750` as `$0.8751`. The noise is invisible until
something rounds up, which is the very next step.

## Running it



```bash
cd research/prematch_ev
cp .env.example .env              # ODDS_API_KEY
python3 run_study.py --plan --sport MLB --from 2026-05-13 --to 2026-09-15
python3 run_study.py --probe --sport MLB
python3 data/kalshi_history.py --audit-abbreviations --series KXMLBGAME --league MLB
python3 run_study.py --sport MLB --series KXMLBGAME --from 2026-05-13 --to 2026-09-15
python3 run_study.py ... --fee-route direct   # headline on the other account route
python3 run_study.py ... --lead-grid 72h,48h,24h,12h,6h,3h --entry-delay-minutes 10
```

Tests: `python3 -m unittest discover -s tests -t .` — 369 tests, no network, no
credentials, and they pass with or without `rapidfuzz`.

Artifacts land in `study_output/`: `report.txt`, `observations.json` (schema 2 —
both source timestamps, actual lead, YES participant, and **both books**:
`decision_*` as seen at the cutoff, `entry_*` as paid at execution), and
`coverage.json`
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

- **Fee schedules vary by series *and by date*.** The generic Kalshi
  coefficients are a default, not a universal rate; resolve the applicable
  schedule into `fees.KALSHI_SERIES_SCHEDULES` with its `effective_from` and
  its source, and `describe(series, at)` will say which entry it used and what
  it could not check. The two KXMLBGAME entries here were **transcribed** from
  a 2026-09-21 reading of Kalshi's public endpoint and have **not** been
  re-verified since — re-read them before promoting any result built on them.
  The Polymarket **US** entity deliberately raises rather than borrowing the
  international θ. (An earlier comment claimed maker fees "usually round to
  $0.00" — impossible: a ceiling of any positive fee is ≥ one quantum, and the
  rounding makes *small* orders relatively more expensive.)
- **The account route is not resolved.** Every fee figure here exists in two
  versions and the report shows both. Before any number is quoted outside this
  study, establish which balance precision the account actually settles at.
- **Roster abbreviations are unverified** against live Kalshi tickers. Run the
  audit; a mismatch shows up as a team contributing no data, not as an error.
- **The ESPN payload shapes were transcribed, not fetched in-session.**
  `site.api.espn.com` is refused by the egress proxy from the sessions this was
  written in (the gateway answers 403 to `CONNECT`), exactly as
  `api.elections.kalshi.com` is. The fixtures in `tests/test_schedule.py` are
  copied verbatim from two responses the repository owner fetched on
  2026-09-21; the acceptance run against the live endpoint belongs to whoever
  can reach it. Treat the endpoint as a **version-sensitive dependency**: it is
  undocumented and public, and the schema tests are what turn a shape change
  into a named failure instead of a plausible wrong kickoff.
- **An NFL run is exploratory by construction.** Its kickoffs were retrieved
  after the fact (`historical_schedule_as_of = unverified`), so no NFL result
  may be quoted as a point-in-time backtest, however green its coverage is.

## The reaction-lag study (`run_reaction.py`) — separate, in progress

A second, **separate** study asking a different question from the checkpoint
one above:

> Detect a meaningful change in a sharp book's de-vigged probability, and test
> whether an executable Kalshi price stays behind long enough to act after
> realistic detection and order delays.

It has its own entry point and output schema **so the checkpoint result stays
reproducible**. Nothing here edits `run_study.py`.

### The capability audit comes first, and it is a control

`python3 run_reaction.py --capability` — free, no network, no credential. It
grades each of the study's questions against the sources actually available,
because two of them turn out to be unanswerable at **any** sample size, and
building the measurement before discovering that would have produced numbers
nobody should read.

| question | verdict |
|---|---|
| did the book's fair probability move, and by how much | **answerable** |
| when did the BOOK move | **answerable** (`last_update`, seconds) |
| when could OUR SYSTEM have known | ±300s — the archive is a 5-minute grid |
| when did the Kalshi quote change | ±60s — candles are 1-minute aggregates |
| did the book move *before* Kalshi | ±300s — closer orderings are unidentifiable |
| how long a discrepancy persisted | ±60s, both ends censored |
| was the quote **fillable in size** | **NO** — no depth on either path |
| suspended, or merely absent | **NO** — neither source distinguishes them |
| the provider's delivery lag | **NO** — historical replay has no receipt time |

**Reaction resolution floor: 300s.** Every lag, ordering and persistence figure
inherits it. A five-minute sample is not second-resolution evidence.

The unanswerable rows are **not pending work** — and the audit exits **0**
anyway. They are permanent properties of these two sources that the design
accounts for, so putting them in the exit code would burn it on a red line that
never clears (rule 27, the stooq lesson). Non-zero is reserved for the
transcribed cadence disagreeing with measured data, which is a real defect
someone can fix.

### The executable clock, which is the whole correctness story

```
last_update  -> when the BOOK moved            (we could not have known it then)
snapshot     -> when WE could first have known (the clock a decision runs on)
```

A book move stamped 11:20 that first appears in the 11:25 snapshot is detected
at **11:25**. Measuring from `last_update` is enormously tempting — it is
stamped to the second and it *is* when the book moved — and it would credit the
strategy with information it did not have. That is lookahead wearing a
timestamp, and this study has already shipped lookahead twice (a snapshot
captured after the decision; an execution quote written into the decision
book), both caught in review rather than by the code. So it is an **exception**
now: `assert_no_future_data` raises, and it checks the executable clock, not the
source's own stamp.

`local_receipt_time` is `None` for every historical record and may not be
invented. Only a prospective recorder can supply one.

### The detector triggers on content, never on arrival

The archive re-serves an unchanged price every five minutes and a live feed
re-sends on reconnect; both arrive looking fresh. Detection keys on the
**content** (market + source update stamp + payload hash), so *a provider
reconnect cannot masquerade as a move* — and a fresh envelope carrying a
three-hour-old book price is `stale_content`, not a signal.

Every non-trigger is named and counted, so a run reports why it saw fewer moves
than updates instead of a bare total: `first_observation` (a baseline has
nothing to have moved from), `unchanged_content`, `missing_side`,
`undeviggable`, `stale_content`, `unknown_content_age`, `gap_in_input`
(a hole is not continuity — the baseline resets rather than calling the
difference across it one move), `out_of_order`, `below_threshold`,
`vig_only_change` (the margin moved while the fair view held — a real
observation, not noise), `no_availability_time`.

De-vigging happens **within one contemporaneous quote**. Mixing an away price
from one update with a home price from another manufactures a move out of two
honest quotes.

`MovePolicy` is declared, recorded on every trigger, and marked
`tuned_on_outcomes: false`. **The 16 NFL games are development data**; fitting
the threshold on them would make every downstream figure a selection artifact.

### Status

Built and tested: the capability audit, the clock/provenance contracts, and the
causal move detector. **Not built and not faked:** reaction measurement, the
executable-opportunity screen, the episode ledger and the paper replay. The
owner's named test cases that need those layers (book-leads/Kalshi-follows with
a surviving gap, indeterminate ordering, Kalshi-first, no response, a gap the
delay misses) are deliberately absent rather than stubbed — a test that
pretended to cover them would be worse than their absence.

No orders. No live capture. No paid requests. None is authorised and none is
implemented.

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
