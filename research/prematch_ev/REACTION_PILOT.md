# Reaction-lag pilot: proposal

**Status: a proposal. Nothing here is authorised and nothing here has been
run.** No paid collection, no live capture, no orders. The earlier
1,500-credit approval does not carry over, and every credit figure below is a
**price quoted from a transcribed cost model**, not a permission.

Everything described is reachable today from
`python3 run_reaction.py --policy` and `--replay`, over a bundle someone else
produces. This document is about whether producing that bundle is worth
paying for, and what the answer would and would not mean.

---

## 1. The horizon is 72 hours, and that changes the design

The thesis is that a sharp book moves **days before a game** and the exchange
takes a while to follow. So the observation window is **T-72h through
kickoff**, continuously — not a few hours before the whistle. An earlier
version of this proposal recommended three-hour pre-kickoff windows, which
is a different study with a different answer, and substituting it silently
would have measured the wrong thing at a discount.

**The pilot's question is still feasibility, not edge.** Before an EV
estimate is worth buying, one thing upstream has to be true: these two
sources have to be able to *resolve* the lag at all.

A book's own change instant is not in the data. `last_update` is when the
**provider** last observed the market, so a change can only be **bracketed**
between two consecutive provider observations — a bracket as wide as the
cadence we pay for. Kalshi candles are **1-minute**. Two brackets that
overlap do not order, at any sample size.

### The asymmetry that makes 72 hours affordable

The exchange side is free and fine-grained. Only the book side is paid and
coarse. Ordering is determinable whenever the two brackets do not overlap:
with a book bracket `S` and a 60s candle, `BOOK_LED` needs Kalshi's earliest
possible move instant to fall after the book bracket closes.

So a coarse cadence loses **only reactions faster than `S`**. It does not
stop a *long* lag being measured, and it still yields a conservative **lower
bound** on the lag when one is found. Long lags are precisely the regime this
thesis is about, so the 300-second archive floor is not required to test it.

### Why uniform cadence, not fine bursts

The tempting design is a cheap baseline across the horizon plus a few
5-minute windows. It is worse, and it took working the numbers to see why.

Fine bursts only resolve moves that **land inside them**. Moves days out are
sparse and their timing is not predictable, so a 9-hour sample of a 72-hour
horizon establishes nothing at all when no move falls in it — and that is a
coverage fact, not a negative result. A uniform grid at the same price
catches **every** move with a single, stated bracket and has no sampling
holes.

Worked against the same 1,440 credits: uniform 30-minute resolves any lag
over 30 minutes, everywhere. Hourly-plus-bursts resolves over 60 minutes
everywhere and over 5 minutes in one eighth of the horizon. A true lag of
about 45 minutes — plausible, and comfortably tradeable — is resolved by the
first and missed by the second on every move outside a burst. Bursts win only
in the fast-lag regime, which is the regime where the answer is least useful
anyway, because a 3-minute discrepancy is not reachable by a poller whose
delivery delay is itself unmeasured (§5.1).

**Proposed primary output:** the histogram of `lag_earliest_seconds` /
`lag_latest_seconds` from `reaction.measure`, plus the counts of
`ordering = book_led | kalshi_led | indeterminate` and of
`outcome = responded | already_priced | no_response | blind_interval`.

**Proposed decision rule, declared now:** if fewer than **one third** of
measured reactions are `book_led` with a lag interval starting beyond
**1,800s** — option A's own bracket — stop. Do not fund an edge study on a
lag these sources cannot resolve. That threshold is a judgement call and it
is written down *before any data exists*, which is the only thing that makes
it a rule rather than a rationalisation.

**And a null result here is narrow, deliberately.** Failing that rule means
the lag, if there is one, is **shorter than 30 minutes**. It does not mean
there is no edge; it means seeing it costs a finer grid (option C or D) or a
live recorder, and that is a separate decision with a separate price.

---

## 2. Step 0: find out whether the book is even quoted at T-72h

**This is the cheapest thing in the document and it gates everything else.**

Sharp-quote availability 72h out is **unverified for NFL**. MLB was measured
and failed at 48h on sharp coverage. If Pinnacle h2h is not carried at T-72h,
the 72-hour horizon cannot be bought at any price and the window truncates to
wherever coverage actually starts.

Three archive snapshots on a **past** NFL date — at T-72h, T-48h and T-24h —
settle it for **30 credits**. It is a coverage probe, not a measurement, and
its only output is "from when is the sharp book present".

Nothing below should be approved before it answers.

---

## 3. What it costs

The cadence is not a cost knob. It is the measurement: the interval between
paid snapshots **is** the book bracket, and the bracket is what decides which
lags can be resolved.

| option | window | cadence | book bracket | odds requests | **credits** |
|---|---|---|---|---|---|
| **0** | one past NFL date, 3 probe points | — | — | 3 | **30** |
| **A** | 72h → kickoff, one NFL Sunday | 30-min | 1,800s | 144 | **1,440** |
| B | same window | hourly | 3,600s | 72 | **720** |
| C | same window | 15-min | 900s | 288 | **2,880** |
| D | same window | 5-min (archive floor) | 300s | 864 | **8,640** |
| E | two NFL weekends | 30-min | 1,800s | 288 | **2,880** |

Credits are `days × snapshots_per_day × 10`, from
`data.odds_history.estimate_credits` — a **transcribed** figure this session
cannot re-verify. Kalshi candlesticks and the ESPN scoreboard are
unauthenticated and free; a 13-game Sunday is about **27 free requests**
(26 contracts + one schedule), and they are still counted because a bound has
to bound them too.

**Option B is a trap.** It is the cheapest real row and a null result from it
is uninformative: a 3,600s bracket cannot distinguish "the exchange reacts in
four minutes" from "the exchange reacts in fifty", and those two answers point
in opposite directions. Only a *positive* result at hourly would mean
anything, and buying a design whose negative case teaches nothing is buying
half an experiment.

**Recommendation: option A, 1,440 credits**, after option 0 returns. One
NFL weekend, uniform 30-minute cadence across the full 72 hours. It is the
smallest spend whose negative result is still informative.

`reaction/capture.py` renders the plan and derives the enforced budget from
it, so an approved plan cannot be executed with a wider bound than the one
approved:

```python
CapturePlan(purpose="reaction-lag feasibility, NFL, 72h to kickoff",
            sport="americanfootball_nfl", series="KXNFLGAME",
            first_day=..., last_day=...,
            snapshots_per_day=48, contracts_expected=26,
            schedule_requests=1).render()
```

### Cache reuse, baselines and retries

These three decide whether the quoted price is the price paid.

- **One snapshot is the whole sport's slate at one instant.** Per-game
  marginal cost is zero — a 13-game Sunday costs exactly what a one-game
  Thursday costs — so the horizon, not the slate size, sets the bill.
- **A repeated request at the same instant is a duplicate at full price.**
  Every response is written to disk raw, and the run never re-issues an
  instant it already holds. Because the bundle stores raw payloads, every
  subsequent *replay* is free forever: re-analysis costs nothing, only
  re-collection does.
- **The first snapshot of the horizon is a baseline, not a measurement.** A
  stream's first observation can never be a move (`first_observation`), so
  T-72h buys the anchor and the measurement starts at T-71.5h.
- **A dropped request costs the move that spans it.** A gap re-baselines the
  stream by design, so retries are part of the measurement, not merely of the
  budget. Allow **10%** for retries — option A becomes 144 requests plus up
  to 15 retried, and the approved figure must carry them or `CaptureBudget`
  raises mid-horizon and the run ends with a hole in it.

---

## 4. What it would NOT establish

- **Not an edge.** One slate is ~13 games and at most 13 entries under
  one-entry-per-game. Any EV figure from it is an anecdote, and the screen's
  own thresholds were declared against different data.
- **Not a backtest.** NFL kickoffs come from an external schedule retrieved
  after the fact, carrying each game's **final** time. A flexed game's
  schedule cannot say what was believed 72 hours earlier — which is a sharper
  problem at this horizon than near kickoff — so every NFL run carries
  `historical_schedule_as_of=unverified` and `ReplayGame.point_in_time` is
  False. The replay says so on every run.
- **Not a lag finer than the grid.** Nothing here resolves below the cadence
  paid for, and no option resolves below the **300-second** archive floor.
- **Not fillable size.** Candlesticks carry no depth. Every figure is at
  one-contract size and cannot be scaled.
- **Not suspension-aware.** A minute with no candle could be an unmoved quote
  or absent data, and the audit grades that UNANSWERABLE. The measurement
  takes the conservative reading (`BLIND_INTERVAL`) and
  `--capability-verify` carries a free command that would settle it —
  **worth running before any paid collection**, since it costs nothing and
  changes how a whole class of minute is read.
- **Not a holdout result.** See below.

---

## 5. The holdout problem is not solved by this pilot

September 1–16 2026 is **development data**: every threshold in this study
was declared against it, the checkpoint result was measured on it, and the
defects of many review rounds were found in it.
`reaction/episodes.py` raises `HoldoutViolation` on any run over that window
declared a holdout, and `holdout_available` is `False` because none exists.

A pilot over a *later* slate is genuinely out-of-sample for the **thresholds**
— they were fixed before it and `--policy` prints them so that is checkable —
but it is not a holdout in the useful sense, because the pilot's own result
would then inform the design of whatever comes next. Spending it as a holdout
and as a feasibility probe at the same time spends it twice.

**Proposal: run the pilot as an openly exploratory feasibility probe, and
reserve a later, untouched weekend as the holdout** — collected but not looked
at until a policy is frozen. That is a decision to make before collecting,
not after, because a window that has been looked at cannot be un-looked-at.

---

## 6. What would have to be true before a live pilot

Out of scope for this proposal, listed so the gap is not mistaken for a
short step:

1. **A prospective recorder.** Every availability figure in replay assumes
   **zero delivery delay** — a historical snapshot has no receipt time, so
   real delivery lag is unmeasurable from it. The audit grades "what the LIVE
   feed could do" UNANSWERABLE. A live pilot's first job is to *measure* that
   delay, not to assume it. This matters more the shorter the lag turns out
   to be: a 30-minute discrepancy survives a delivery delay that a
   three-minute one does not.
2. **The decision-clock bound, on live records.** The freshness bound moved
   onto `age_at_decision_seconds` precisely because in replay it and the
   capture-age bound are the same number, so no historical fixture
   distinguishes them. That defect only exists on the live path.
3. **Depth, from a source that has it.** Or an explicit decision that
   one-contract size is the whole strategy.
4. **A fee schedule for `KXNFLGAME`.** The dated multiplier is known for
   `KXMLBGAME` (halved on 2026-08-07); NFL's is assumed at the generic 0.07
   and that assumption moves every EV.
5. **The account route.** `$0.0001` direct versus `$0.01` non-direct, where
   at one-contract size the rounding quantum is most of the fee.

---

## 7. Still open, from the owner

Not blocking the work above, and blocking any collection:

- Authorisation for the **30-credit** coverage probe (option 0), and
  separately for **any** measurement collection (option A = 1,440 credits).
- 72h/48h sharp-quote availability for NFL — MLB failed at 48h on sharp
  coverage, and NFL has not been measured. Option 0 exists to answer this.
- The dated `KXNFLGAME` fee schedule.
- The seven-item NCAA gap list.
- Whether to reserve a holdout weekend now, per §5.

---

## 8. What is already built and free

```
python3 run_reaction.py --capability          # the source timing audit
python3 run_reaction.py --capability-verify   # free commands to re-check it
python3 run_reaction.py --policy              # the declared thresholds
python3 run_reaction.py --replay bundle.json  # the whole chain, offline
```

Exit codes: `0` when the replay ran — **zero entries is a result**, since the
checkpoint study's published finding is zero entries clearing +$0.01 —
`1` for a defect someone can fix (unreadable bundle, mislabelled holdout,
non-reconciling ledger, incomplete parse, or a game with no usable target
coverage at all), `2` for usage.
