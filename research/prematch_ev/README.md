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

**A cache key names one instant.** Its stamp is UTC and it lives in its own
namespace (`v2-…` in the digest and the filename). Two older schemes wrote
unprefixed keys into one shared digest space: first the machine's LOCAL time
with a literal `Z` — which gave the two instants of the autumn fall-back hour
one key — and then UTC. Reading the first as a fallback for the second made
every machine not on UTC serve one instant's snapshot for another: on a UTC-5
clock the local key for 17:00Z *is* the UTC key for 12:00Z, and a request for
17:00Z got the 12:00Z snapshot. Namespacing makes that impossible for new
entries. An unprefixed entry is still read, because every one was paid for,
but only once `answer_problem` shows its own stamps answer the request: the
archive answers with its latest snapshot at or before the instant asked for,
so a body answers when `timestamp <= request < next_timestamp`, or, lacking
`next_timestamp`, when its snapshot is at most ten minutes before the
request — closer than any two instants a legacy key can confuse. Anything
else misses and is bought again. `resolve_key` is the one function both the
fetch and every preflight use, so the plan and the purchase cannot disagree
about what is cached; the collector's suite runs on a UTC-5 clock so that
they are checked where the collision exists.

**If a collector from `ac00e4c`–`e4d8bf5` ran on a machine not on UTC**, its
cache and bundles may hold that substitution. Cache: every unprefixed `*.json`
under the cache directory predates the namespace; the fixed code verifies each
before serving it, so nothing must be deleted — to discard them instead,
delete every file whose name does not start with `v2-`, and the next plan
prices what has to be bought again. Bundles: a bundle now records the instant
behind each snapshot (`odds_snapshot_requested_at`), and the replay refuses
any snapshot that does not answer its instant, as a counted loss. An older
bundle has no such list, so the replay checks its pool's order instead — the
archive's answers never go backwards — and reports a snapshot out of order as
a coverage failure; it cannot see a stranger at either end of the pool, so
re-collect any bundle those revisions wrote on such a machine. Results
replayed from one are suspect until then.

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

## The reaction-lag study (`run_reaction.py`) — separate entry point

A second, **separate** study asking a different question from the checkpoint
one above:

> Detect a meaningful change in a sharp book's de-vigged probability, and test
> whether an executable Kalshi price stays behind long enough to act after
> realistic detection and order delays.

It has its own entry point and output schema **so the checkpoint result stays
reproducible**. Nothing here edits `run_study.py`.

```
python3 run_reaction.py --capability          # the source timing audit
python3 run_reaction.py --capability-verify   # free commands to re-check it
python3 run_reaction.py --policy              # the DECLARED thresholds
python3 run_reaction.py --replay bundle.json  # the whole chain, offline
python3 collect_reaction.py --day 2026-09-20  # price a collection, free
python3 shadow_monitor.py --hours 72          # price a live session, free
```

The four `run_reaction.py` commands are free: no network, no credential, no
credits. The study buys data in two places — `collect_reaction.py` from the
odds archive, `shadow_monitor.py` from the live feed — and both are free
until `--spend` confirms a price. See *The backtest collector* and *The
shadow monitor*.

### `last_update` is the PROVIDER's clock, not the book's

This is the correction that reorganised the whole layer, and the first version
of it asserted the opposite.

`last_update` on an odds-archive row is **the last time the provider's system
saw odds for that market from the bookmaker**. It is *not* when the bookmaker
changed its price — and bookmaker-level `last_update` is deprecated upstream.
The wrong reading came from `SharpQuote`'s own docstring, propagated into the
clock module verbatim, and produced a confident `book_moved_at` point estimate
the source cannot support.

So there are **four clocks**, kept apart:

```
provider_observed_at    the provider last saw odds for this market
provider_snapshot_time  the provider captured/closed this row
local_receipt_time      WE received it  (live capture only; None in replay)
scheduled_start         kickoff
```

and the book's own change instant is reported as a **bracket** between two
consecutive provider observations. There is no point estimate, and
`MoveTrigger` has no `book_moved_at` field — a test asserts its absence.

### The capability audit comes first, and it is a control

`reaction/capability.py` grades each of the study's questions against the
sources actually available, because several turn out to be unanswerable at
**any** sample size.

| question | verdict |
|---|---|
| did the book's fair probability move, and by how much | **answerable** |
| when did the PROVIDER last observe this price | **answerable** |
| when did the BOOK actually change its price | **NO** — bracketed only |
| when could OUR SYSTEM have known | ±300s — the archive is a 5-minute grid |
| when did the Kalshi quote change | ±60s — candles are 1-minute aggregates |
| did the book move *before* Kalshi | ±300s — closer orderings are unidentifiable |
| how long a discrepancy persisted | ±300s, both ends censored |
| was the quote **fillable in size** | **NO** — no depth on either path |
| suspended, or merely absent | **NO** — neither source distinguishes them |
| the provider's delivery lag | **NO** — historical replay has no receipt time |
| what the LIVE feed could do | **NO** — replay assumes zero delivery delay |

**Reaction resolution floor: 300s.** Every lag, ordering and persistence
figure inherits it. A five-minute sample is not second-resolution evidence.

The unanswerable rows are **not pending work**, and the audit exits **0**
anyway. They are permanent properties of these two sources that the design
accounts for, so putting them in the exit code would burn it on a red line
that never clears (rule 27, the stooq lesson).

### The executable clock, and the bound that belongs on it

`reaction/clocks.py` keeps them apart; `reaction/detector.py` decides a move.

```
provider_observed_at    -> when the PROVIDER saw it (we could not act then)
available_at            -> when WE could first have acted
```

A move first appearing in the 11:25 snapshot is detected at **11:25**.
Measuring from `provider_observed_at` is tempting — it is stamped to the
second — and it would credit the strategy with information it did not have.
`assert_no_future_data` **raises**, and it checks the executable clock.

The freshness bound sits on `age_at_decision_seconds`, not on capture age. In
replay the two are the same number, which is why no historical fixture
distinguishes them — but a LIVE record captured at 12:05 and received at 12:25
is 1200s old when actionable, and a capture-age bound calls it fresh at 30s.
That defect only exists on the live path, which is the path a pilot runs on.

An **impossible clock ordering is refused before any bound**: a stamp after
its own capture yields a negative age, and every freshness test ever written
is an upper bound.

### Continuity and comparability are different facts

A stream carries five pieces of state, not one:

```
_baseline         the last quote a move may be measured FROM
_baseline_ok      False once an unusable interval intervened
_last_seen_at     the last VALID observation, changed or not
_last_content     the last record of any kind  (deduplication)
_newest_observed  the latest PROVIDER stamp seen  (regression)
```

Merged, it was wrong in both directions. An unchanged price polled every five
minutes advanced neither, so a feed that never stopped read as `gap_in_input`;
and an unusable observation between two good ones left the baseline standing,
so a move was attributed across an interval the detector had just refused to
read. `note_gap` is the collector's hook for an absence `observe()` cannot
see — a market omitted from a provider response produces no envelope at all.

**An older copy is not news.** A response served from a lagging copy carries
what the provider saw *before* an observation already in hand. It is new
content, so deduplication lets it through, and compared against the baseline
it is a move back; the newer price arriving again a poll later is then a
second move. The provider's own stamp orders them: a record stamped before
the newest one seen is `regressed_content`, which advances continuity and
leaves the baseline with the newest observation. It is judged after the
freshness checks, so a record that is also stale still says `stale_at_decision`.

**Stream identity is provider + book + event + market + orientation.** Keyed
on event and market alone, two bookmakers shared one piece of state and the
second book's price read as the first one moving.

Every non-trigger is named and counted: `first_observation`,
`unchanged_content`, `regressed_content`, `missing_side`, `undeviggable`,
`unknown_content_age`, `gap_in_input`, `declared_gap`, `out_of_order`,
`below_threshold`, `vig_only_change`, `no_availability_time`,
`clock_order_invalid`, `stale_at_decision`, `wrong_book`,
`baseline_invalidated`.

De-vigging happens **within one contemporaneous quote**, through
`core.devig` — one implementation, Shin, named in the policy and recorded on
every trigger. A private multiplicative de-vig here disagreed with the
study's baseline by up to 0.0052 against a 0.01 threshold.

### A lag is an interval; the exchange leading is its own outcome

`reaction/measure.py` answers two questions separately, because collapsing
them is the temptation and "the book led by 90 seconds" is neither:

- **ordering** — from bracket OVERLAP, using the detector's own bracket.
  Two intervals that overlap do not order, at any sample size.
- **tradeable lag** — `lag_earliest_seconds` / `lag_latest_seconds`. There is
  no `lag_seconds`; a point estimate would be quoted and averaged, and half
  its precision would be an artifact of the grid.

`ALREADY_PRICED` is its own outcome, found by a **lookback**. A forward-only
search sees the exchange already adjusted and reports `NO_RESPONSE` — filing
the thesis being falsified under the same name as the thesis holding.

A **missing candle is not a flat price**. Whether a quiet minute gets a candle
is the audit's suspension-versus-absence question one layer down, so the
conservative reading ships (`BLIND_INTERVAL` — we could not see, not nothing
happened) and `--capability-verify` carries a free command that would settle
it.

`discrepancy_survives(delay)` has **three** answers: True, False, and None
when the delay falls inside the lag interval. A boolean there is a coin flip
formatted as a measurement.

### The screen is the checkpoint study's own

`reaction/screen.py` is an **adapter**, not a second screen. This layer's
contribution is *which instants get screened*; `analysis.scoring` still
decides what tradeable means, with the round-2 lesson intact (a midpoint
screen passed bid .40 / ask .60 against a .53 fair — a predicted **-0.09 per
contract**). A test parses this module's source and fails if it declares its
own EV threshold, price band or spread bound.

Settlement is **withheld, not defaulted**: predicted EV never reads the
outcome, so a bundle without settlement still produces a complete run, and the
serialised row is identical for both placeholder values.

### Episode accounting: every count in its own unit

One book move on a two-contract game is **1 trigger, 2 screened rows, 1
entry** — three correct numbers, none interchangeable. Reporting one under
another's name is how a run once printed `50.0% of 4 eligible contracts lost`.
`reaction/episodes.py`'s `StageCount` cannot be built without a `Unit`, and a breakdown that does not
sum to its total is called out loudly.

**September 1–16 2026 is development data.** `HoldoutViolation` raises on a run
over that window declared a holdout, and `holdout_available` is False because
none has been collected.

### Offline replay, and the capture contract

`reaction/replay.py` reads a bundle of **raw provider payloads** and runs them
through the
live collector's own parsers, so a parser fix reaches the replay and
a replay can never disagree with a live run about what a byte sequence means. The
reader refuses rather than degrades — unknown schema, naive timestamp, a
kickoff with no declared source, an unoriented contract — because a silently
skipped game reports thinner coverage, and thinner coverage reads as a market
with less activity.

**A snapshot the provider returned WITHOUT our event is a hole, not a
non-event.** The reader keeps one record per snapshot rather than flattening
every quote into one list, so an interval in which the feed was read and the
target was absent leaves a trace. `replay` declares it to the detector
(`note_gap`), which invalidates that stream's baseline, so the returning
quote re-anchors instead of closing a delta across an interval nobody
observed. Flattening hid exactly that: two quotes either side of a hole sat
inside `max_gap` and produced a trigger, a bracket and a lag measured over an
unread gap. A hole costs the move that spans it and nothing after, and it is
**counted, not graded** — a briefly absent market is what the design exists
to survive (rule 27). An absence with no readable timestamp keeps its place
in the sequence: ordering it to the end would move the hole past the quote it
was meant to stop, which is how the first version of this fix reintroduced
the bug it was fixing.

**Coverage and parse success are different facts, and the second one is not
the one a result depends on.** A bundle whose snapshots are all well-formed
and none of which carries the target parses cleanly and replays to zero
moves, which is byte-for-byte what a quiet slate looks like. So a game with
no usable sharp quote, and a contract with no usable exchange quote, are
named coverage failures with a non-zero exit — while a run with real
observations and no qualifying move still exits 0, because zero entries is a
result.

**A parsed candle is not a usable quote**, and the check asks the
measurement's own predicate rather than counting rows. A payload can carry
well-formed candles — timestamps, volume, trade prices — with no bid or ask
at all, and `usable_candle` rejects every one because there is no mid. 102
candles parsed, zero usable, coverage clean, and every reaction returning
`no_exchange_baseline`, which reads as an exchange that did not move.

**A snapshot after kickoff is in-play, and it is excluded, not observed.**
The parser keeps every event in a response, including games already under
way, and nothing downstream asked whether a quote was pregame — so a
score-driven swing after kickoff was detected as a book move, measured
against the exchange's own in-play repricing, and entered the feasibility
verdict with coverage reported clean. One snapshot carries every game on the
slate, so every snapshot after the early kickoff has those games in play: it
would have been most of the data, not an edge case. The kickoff instant
itself is kept — it is the closing pregame sample, matching the manifest's
window closed at both ends — and exclusions are counted
(`in_play_pairs_excluded`).

**Bundle schema 2 stores each snapshot once.** A per-game copy repeats every
payload for every game on the slate. `reaction_replay_bundle/2` keeps one
root `odds_snapshots` pool, and each game declares `observe_from`, where its
window opened, and reads the pool from there to kickoff. The reader refuses a
schema-2 game carrying its own snapshots, a missing pool, and an
`observe_from` after the kickoff. Schema-1 bundles still load.

`reaction/capture.py` declares the bounded read-only interface a collection
machine must satisfy, and deliberately contains **no HTTP client**: a module
that could fetch would eventually fetch. The budget RAISES at its bound,
`assert_read_only` parses the package for order-placing paths and trading
credentials, and `CapturePlan.budget()` derives the enforced bound from the
approved plan so it cannot be run wider than approved.

### The backtest collector (`collect_reaction.py`)

The archive purchase, and it **plans first**. Without `--spend`
it makes only free requests — the ESPN schedule and Kalshi's public market
listing — prints the manifest of UTC instants it would buy and what is
already cached, and exits having spent nothing. `--spend N` confirms a price:
below what the run needs it is refused before any purchase, and the cap
enforced is the need, never more. A window reaching past now is refused too
— the archive holds no snapshot that has not happened — and so is a slate
whose listing or schedule did not fully answer, because the manifest priced
from it may be missing games.

```
python3 collect_reaction.py --day 2026-09-20 --probe --spend 40   # coverage probe
python3 collect_reaction.py --day 2026-09-20 --lead-hours 72 --spend 10490 --out bundle.json
python3 run_reaction.py --replay bundle.json --max-wait 1800
```

- **Nothing is re-derived.** Contracts join to sharp events through
  `collect.join_markets`, sides come from `collect.yes_side` and kickoffs from
  the same `StartResolver` — the checkpoint study's own code — so a bundle
  cannot disagree with the rest of the study about which contract is which.
- **Every request in the process must be declared.** The fetchers live in
  three modules and each calls `urlopen` itself, so the collector gates
  `urlopen` process-wide against `ALLOWED_ENDPOINTS` rather than trusting a
  convention in each. That list is tested against the URLs the real fetchers
  build, both ways: an entry nothing requests and a request nothing declares
  each fail the suite.
- **A lost instant is a counted hole; a run of them stops the purchase.** An
  instant that fails after its retries keeps a placeholder in the pool, which
  the replay reads as a hole — left out, it would make its neighbours look
  adjacent. Three in a row stop the run: that is an outage or a refused key,
  and the rest of the reserve would buy the same answer. The stop names its
  reasons; a refused key used to surface only as "budget reached", which
  reads as money spent. No bundle is written from a run that stopped.
- **Everything bought is cached**, under `study_output/cache` in the study
  directory whatever the shell's working directory, so a re-run — or the full
  run after the probe — pays only for what it has not got. It is where the
  checkpoint study caches when run from this directory, under the same key,
  so either reuses what the other bought.
- **Candles are not trusted to arrive whole.** Kalshi documents a candle cap
  for its batch endpoint and none this study could find for the single-market
  one, so a response that stops before its span ends is followed by a request
  for the rest, and one answered with candles outside the requested span is
  reported rather than read as quiet. Undetected, a truncation would reach the
  measurement as blind intervals — an exchange nobody could see — rather than
  as a truncation.
- **The key reaches no file and no output, and nothing here can trade.**
  `assert_read_only` scans every module the collector can reach before the
  first paid request. Flags are matched exactly: a command that spends money
  is spelled out or refused, never completed from a prefix.

### The shadow monitor (`shadow_monitor.py`)

The no-order version of the bot the owner described: poll the sharp book,
notice when it moves, look at Kalshi at once, and write down what a bot
would have done — then keep watching Kalshi to learn whether, and how fast,
it followed. It is the prospective recorder every replay figure has been
waiting on, and it measures what no archive can: how old the provider's
observation of a price is when it reaches us, how often the provider
actually re-observes a game, how fast Kalshi follows at the spacing of its
own reads, and what the book held at the moment of a move, with depth.

```
python3 shadow_monitor.py --hours 72                   # the price, free
python3 shadow_monitor.py --hours 72 --spend 4321      # a live, read-only session
python3 shadow_monitor.py --report study_output/shadow/<session>.jsonl
```

- **Each tick, in order:** Kalshi's book for every watched contract (free —
  the *decision* books, what a bot held when a move arrived); one sharp-book
  poll (paid); the study's own detector on every quote, clocked by our
  receipt; for a move on a watched game, that game's books again (the
  *execution* books — how long they took is the entry delay, measured
  rather than assumed); the checkpoint study's own screen, reached through
  `reaction.screen.screen_live`, the same observation path the replay uses;
  then the game is followed every 10s, free, for the declared 30-minute
  response window.
- **Decided on what it knew; charged what it would have paid.** The screen
  judges the decision book, as the replay does. The execution book sets
  `paid`, and the difference is recorded per entry as the **latency cost** —
  the number that says whether a live bot's own delay eats the edge.
- **A failed execution read is not a fill.** Priced at the decision book
  instead, it would record an entry that could not have happened; the
  screen drops the side, as in the replay. One entry per game, as in the
  checkpoint study.
- **What a live feed can fake is refused.** A game missing from a poll is a
  hole, not a move; a game already under way is never a move; the detector
  runs on the clock the decision would have run on.
- **It stops, and says why,** on the credit cap, three failed polls in a
  row, a price per call other than quoted (checked against the provider's
  own `x-requests-last` on the first answer), a clock more than 5s off the
  provider's, a nearly empty account, or a request outside the allow-list.
  Kalshi's book shape is transcribed rather than observed, so one real book
  is read — free — before the first paid poll, and a shape the parser
  refuses stops the session with nothing bought. The provider's observation
  stamp is transcribed too, and the detector refuses a price without one, so
  a first answer in which no pre-match quote carries a stamp stops the
  session after one credit instead of paying for one that could never
  trigger.
- **The report says what it could not measure.** The provider's refresh is
  only measurable by polling faster than it: if no poll ever finds a game
  unchanged, the provider re-observed between every pair of polls, and the
  report calls the figure what it is — a ceiling set by the poll spacing.
  A hole — a failed poll, a game missing from an answer, an undated quote —
  restarts the timing instead of being timed across, and an observation's
  age is taken only when it was first seen new. "0 moves" is printed beside
  the detector's refusals by reason, so a detector that could not see cannot
  pass for a quiet market.
- **Records, not state.** One append-only JSONL file per session: every raw
  response with the clocks around it, every move, decision, gap and stop,
  and each poll's detector refusals. `--report` reads it offline; the key is
  in no record.
- **Not yet:** scoring shadow entries against settlement. A live session
  ends before its games do, so its figures are *predicted*; realised
  results need the settled markets joined back in, which is not built.

### The pilot proposal (`REACTION_PILOT.md`)

`REACTION_PILOT.md` proposes a **feasibility probe, not an edge study**,
over the horizon the thesis is actually about: **T-72h through kickoff**.

**The pregame horizon and the response lag are independent dimensions.** The
objective is a sharp move days out that Kalshi may follow *in seconds*; an
earlier revision of the proposal collapsed the two and argued that long lags
were the thesis, which substituted a slower-response study for the stated
one. What the cadence actually decides is narrower: it is simultaneously the
measurement's resolution **and the simulated poller's own latency**. A
30-minute grid can only demonstrate opportunities a 30-minute poller could
have taken — it cannot rule out faster ones, it cannot see them. A coarse
grid is therefore only a **constrained long-lived-discrepancy probe**, and
the proposal now recommends the archive's own **5-minute floor** over the
full horizon: the owner's call, finer data over more of it, within a
20,000-credit budget. It still says what any grid cannot answer: a move that
reverses inside one interval is invisible, the true lag has no upper bound
tighter than one cadence, and a null result carries no claim about short
lags.

**The replay horizon is not one cadence interval.** An earlier revision tied
`max_wait` to the cadence so that response windows would tile. On a 5-minute
grid that right-censors every exchange response slower than about four
minutes — the followers a 5-minute poller could trade against — and tiling
was never available anyway, because the 30-minute lookback overlaps earlier
moves at any finer grid. The verdict's per-response dedupe is what protects
the count, so the horizon is the declared 30 minutes or one cadence,
whichever is longer, and the collector prints it with the replay command.

**The stop rule is checked for reachability, because the last one was
unsatisfiable.** It required a lag interval starting beyond 1,800s from a
policy whose ceiling is `max_wait − candle_period` = **1,740s**; nothing
could satisfy it and nothing said so, because the ceiling was never
computed. `ReactionPolicy.max_reportable_lag_seconds` computes it now,
`FeasibilityRule.unreachable_against` refuses a rule that exceeds it, and
`judge_feasibility` evaluates the declared rule on every replay — reporting
`continue`, `stop` or `insufficient_observable_events`, with censored, blind
and indeterminate reactions counted beside the fraction and never inside it.
**It counts book moves, not contract rows**: a reaction is one (move,
contract) pair and every NFL game has two mirror-image contracts, so a
first version counted each move twice and met its floor of 20 with ten. A
Kalshi step claimed by two overlapping response windows likewise counts
once, for the earlier move. `--policy` prints the rule with the thresholds
it judges, so it can be committed before collection like every other
declared number.

**Cost comes from a timestamp manifest, not `days × snapshots_per_day`.**
That arithmetic counts inclusive calendar *dates*, so a 72-hour window
opening Thursday and closing Sunday quoted 192 requests for one holding 145,
and it could not express two kickoff clusters sharing most of their windows.
`build_manifest` enumerates the actual UTC instants, deduplicates them,
marks the single true baseline, and derives the enforced bound **including
the retry reserve** the prose used to promise and `budget()` did not carry.
It also exposes the largest lever on a coarse grid: three unaligned NFL
kickoff clusters share nothing on a 30-minute grid, 435 instants against 161
aligned. At the 5-minute floor alignment saves nothing, because NFL kickoffs
are scheduled on five-minute marks.

### Status

Built and tested: the capability audit, the clock/provenance contracts, the
causal move detector, the reaction measurement, the opportunity screen, the
episode ledger, the offline replay, the backtest collector
(`collect_reaction.py`) and the shadow monitor (`shadow_monitor.py`). Every
named test case is present —
book-leads-with-a-surviving-discrepancy, indeterminate ordering, Kalshi-first,
no response, the delay that misses the gap, a quote beyond the allowed wait,
and a move-plus-full-reaction between two coarse checkpoints.

**Not built:** anything that places an order, and scoring shadow entries
against settlement. `capture.py` is the contract both paid commands run
under, with no transport of its own.

No orders: none is authorised and none can be placed — nothing in the study
holds an exchange credential, and the read-only scan fails a run if anything
could. Live capture is read-only. Paid requests exist in exactly two places,
`collect_reaction.py` and `shadow_monitor.py`, each behind an explicit
`--spend`; none is authorised by this repository.

## Relationship to the fleet

None, deliberately. No `fleet_registry` entry, no PM2 app, separate
`requirements.txt`, no imports in either direction. It lives here because the
Beelink is where any eventual service would run and the observability plane is
worth inheriting; it lifts out to its own repo if the thesis survives.

## If it survives

The read-only **forward recorder** of signals, depth and latency now exists
as the shadow monitor, and paper fills still establish no queue priority.
Any execution service after it needs start-time order expiry, position and
exposure reconciliation, and event-level risk accounting built for Kalshi.
