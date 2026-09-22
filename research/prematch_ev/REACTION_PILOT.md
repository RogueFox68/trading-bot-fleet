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

## 1. The pilot's question is feasibility, not edge

The obvious pilot — collect a slate, run the screen, look at the EV — is the
wrong first spend, because there is a cheaper question upstream of it whose
answer decides whether the expensive one can be answered at all.

**The odds archive is on a 300-second grid.** A book's own change instant is
not in the data (`last_update` is when the *provider* last observed the
market, not when the bookmaker moved), so the change is only ever *bracketed*
between two consecutive provider observations — a bracket about 300s wide.
Kalshi candles are 1-minute.

Two brackets that overlap do not order, at any sample size. So:

| if the exchange typically reacts in… | then |
|---|---|
| **well under 5 minutes** | the brackets overlap, ordering is `INDETERMINATE`, and **these two sources cannot see this effect at all** |
| **well over 5 minutes** | ordering is determinable, the lag interval is informative, and a larger study is worth designing |
| **around 5 minutes** | a mixed sample; the resolvable fraction is the number that matters |

That is a **distributional** question about reaction times, and it needs far
less data than an edge estimate. It is also **falsifiable cheaply**, and a
negative answer is a real finding of the same kind as the existing
72h/48h coverage result: *not "there is no edge", but "these feeds cannot
observe the thing the thesis is about."*

**Proposed primary output:** the histogram of `lag_earliest_seconds` /
`lag_latest_seconds` from `reaction.measure`, plus the counts of
`ordering = book_led | kalshi_led | indeterminate` and of
`outcome = responded | already_priced | no_response | blind_interval`.

**Proposed decision rule, declared now:** if fewer than **one third** of
measured reactions are `book_led` with a lag interval starting beyond 300s,
stop. Do not fund an edge study on a lag the sources cannot resolve. That
threshold is a judgement call and it is written down *before* any data
exists, which is the only thing that makes it a rule rather than a
rationalisation.

---

## 2. What it costs, and the tension that sets the price

The archive grid cuts both ways. 300s is the **finest cadence that returns a
distinct snapshot** — and requesting more often buys duplicates at full
price — but it is also the **widest bracket the study can ever achieve**.
Coarsening the request cadence to save money directly widens the bracket and
pushes the ordering answer toward `INDETERMINATE`, which is the one thing the
pilot exists to measure.

So the cadence is not a cost knob. It is the measurement.

| option | window | cadence | odds requests | **credits** | free requests |
|---|---|---|---|---|---|
| **A** | 1 Sunday, 3h before each of 3 kickoff clusters | 5-min | 108 | **1,080** | 27 |
| B | same windows | 15-min | 36 | **360** | 27 |
| C | 1 Sunday, 6h windows | 5-min | 216 | **2,160** | 27 |
| D | Thu + Sun + Mon | 5-min | 324 | **3,240** | 35 |
| E | 2 NFL weeks | 5-min | 1,080 | **10,800** | 74 |

Credits are `days × snapshots_per_day × 10`, from
`data.odds_history.estimate_credits` — a **transcribed** figure this session
cannot re-verify. Kalshi candlesticks and the ESPN scoreboard are
unauthenticated and free; they are counted as requests because a bound has to
bound them too.

**Option B is a trap.** It is the cheapest line in the table and it cannot
answer the question: a 15-minute grid makes the bracket ~900s wide, so almost
every reaction lands inside it and the run reports `INDETERMINATE` for
structural reasons rather than empirical ones. Spending 360 credits to
discover that would be spending 360 credits to re-derive arithmetic already
in `reaction/capability.py`.

**Recommendation: option A, 1,080 credits.** One Sunday at the archive's own
cadence. It is the smallest spend that can return a real answer, and if the
answer is "indeterminate", that is the *finding* — not a reason to buy more.

`reaction/capture.py` renders the plan and derives the enforced budget from
it, so an approved plan cannot be executed with a wider bound than the one
approved:

```python
CapturePlan(purpose="reaction-lag feasibility, one NFL Sunday",
            sport="americanfootball_nfl", series="KXNFLGAME",
            first_day=..., last_day=...,
            snapshots_per_day=108, contracts_expected=26,
            schedule_requests=1).render()
```

---

## 3. What it would NOT establish

- **Not an edge.** One slate is ~13 games and at most 13 entries under
  one-entry-per-game. Any EV figure from it is an anecdote, and the screen's
  own thresholds were declared against different data.
- **Not a backtest.** NFL kickoffs come from an external schedule retrieved
  after the fact, carrying each game's **final** time. A flexed game's
  schedule cannot say what was believed three hours earlier, so every NFL run
  carries `historical_schedule_as_of=unverified` and
  `ReplayGame.point_in_time` is False. The replay says so on every run.
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

## 4. The holdout problem is not solved by this pilot

September 1–16 2026 is **development data**: every threshold in this study
was declared against it, the checkpoint result was measured on it, and the
defects of thirteen review rounds were found in it.
`reaction/episodes.py` raises `HoldoutViolation` on any run over that window
declared a holdout, and `holdout_available` is `False` because none exists.

A pilot over a *later* slate is genuinely out-of-sample for the **thresholds**
— they were fixed before it and `--policy` prints them so that is checkable —
but it is not a holdout in the useful sense, because the pilot's own result
would then inform the design of whatever comes next. Spending it as a holdout
and as a feasibility probe at the same time spends it twice.

**Proposal: run the pilot as an openly exploratory feasibility probe, and
reserve a later, untouched window as the holdout** — collected but not looked
at until a policy is frozen. That is a decision to make before collecting,
not after, because a window that has been looked at cannot be un-looked-at.

---

## 5. What would have to be true before a live pilot

Out of scope for this proposal, listed so the gap is not mistaken for a
short step:

1. **A prospective recorder.** Every availability figure in replay assumes
   **zero delivery delay** — a historical snapshot has no receipt time, so
   real delivery lag is unmeasurable from it. The audit grades "what the LIVE
   feed could do" UNANSWERABLE. A live pilot's first job is to *measure* that
   delay, not to assume it.
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

## 6. Still open, from the owner

Not blocking the work above, and blocking any collection:

- Authorisation for **any** paid collection (option A = 1,080 credits).
- 72h/48h sharp-quote availability for NFL — MLB failed at 48h on sharp
  coverage, and NFL has not been measured.
- The dated `KXNFLGAME` fee schedule.
- The seven-item NCAA gap list.
- Whether to reserve a holdout window now, per §4.

---

## 7. What is already built and free

```
python3 run_reaction.py --capability          # the source timing audit
python3 run_reaction.py --capability-verify   # free commands to re-check it
python3 run_reaction.py --policy              # the declared thresholds
python3 run_reaction.py --replay bundle.json  # the whole chain, offline
```

Exit codes: `0` when the replay ran — **zero entries is a result**, since the
checkpoint study's published finding is zero entries clearing +$0.01 — `1`
for a defect someone can fix (unreadable bundle, mislabelled holdout,
non-reconciling ledger, incomplete parse), `2` for usage.
