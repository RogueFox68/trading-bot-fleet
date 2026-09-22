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

## 1. Two independent dimensions, and this pilot only probes one

The objective is to catch a **sharp-book move days before kickoff** and reach
Kalshi before the exchange follows — which it may do **in seconds**. Those
are two separate dimensions:

| dimension | what it is | what sets it |
|---|---|---|
| **pregame horizon** | how far before kickoff the move happens | the observation window: T-72h → kickoff |
| **response lag** | how long Kalshi takes to follow | the market, not us |

A previous revision of this document collapsed them. It argued that "long
lags are precisely the regime this thesis is about" and treated a
three-minute discrepancy as the less interesting case. **That was a different
thesis substituted for the stated one.** A move three days out that Kalshi
follows forty seconds later is exactly the target; why the line moved is
secondary, and the speed of the response is not a preference we get to hold.

What the cadence actually decides is narrower, and it is the honest reason to
care about it:

> **The sampling cadence is simultaneously the measurement's resolution AND
> the simulated poller's own latency.** A run on a 30-minute grid learns of a
> book move up to 30 minutes after it happened, so it can only ever
> demonstrate opportunities that a 30-minute poller could have taken. It
> cannot rule out faster ones. It cannot see them at all.

So option A below is a **constrained long-lived-discrepancy probe**. It is
not the sharp-move capture study and does not stand in for it. Its positive
result would be real and its negative result would be narrow.

### What a 30-minute grid can and cannot answer

**Can:**

- Establish ordering for any exchange response landing strictly after one of
  our samples: `BOOK_LED` needs only that Kalshi's candle bracket opens after
  our snapshot, which is reachable throughout the measurement window.
- Give a **lower** bound on the true lag.
- Show whether a discrepancy that a 30-minute poller could have seen was
  still open when it looked again.

**Cannot:**

- **See a move that happens and reverses inside one interval.** Only the net
  change between consecutive samples is observed, so an out-and-back move is
  invisible and a sequence of moves reads as one. An earlier claim here that
  uniform sampling "catches every move" was simply wrong.
- **Bound the true lag from above.** The book changed somewhere inside the
  preceding interval, so the true lag is somewhere in
  `[measured, measured + cadence]`.
- **Say anything about responses faster than the cadence.** Not "they are
  rare" — unobserved.
- **Support a claim about short lags from a null result.** See §3.

Closing that gap means the archive's own 300-second floor (option D), and
below five minutes it means a live prospective recorder, because the archive
has no finer grid to buy.

---

## 2. The decision rule, declared now — and checked for reachability

**The rule:** of the **book moves** whose brackets actually **order**, at
least **one third (33%)** must be `book_led`, over a floor of at least **20**
ordered book moves.

It asks about **ordering, not duration**. `reaction/episodes.py` evaluates it
(`FeasibilityRule`, `judge_feasibility`), the replay prints the verdict, and
`--policy` prints the rule beside the thresholds it judges — so it can be
committed before any data exists, like every other declared number.

**The unit is the book move, and each exchange response counts once.** A
reaction is one (book move, contract) pair, and every NFL game has two
mirror-image contracts. The first version of this rule counted reactions, so
one move counted twice and the floor of 20 was met by ten. It now groups by
the move: contracts that disagree about one move are `conflicting` and do not
vote, and a Kalshi step already claimed by an earlier move is
`shared_response` rather than a second observation. The games the ordered
moves come from are reported beside the fraction, because moves within one
game are not independent and a share driven by one volatile game should be
visible as one.

**Three outcomes, and two of them are not "no":**

| verdict | meaning |
|---|---|
| `continue` | ordering is resolvable often enough to design a larger study |
| `stop` | these sources rarely establish the book leading |
| `insufficient_observable_events` | fewer than 20 book moves ordered at all |

Censored responses, blind intervals, indeterminate orderings, conflicting
contracts and shared responses are counted **beside** the fraction and never
inside it. One Sunday may well return
`insufficient` — that is a real possible outcome of this spend and it is not
a negative result.

### The rule this replaces was unsatisfiable

The previous revision required a lag interval starting beyond **1,800s**. The
measurement's `max_wait` is 1,800s and a candle bracket opens one 60s period
before its close, so the largest `lag_earliest_seconds` any run can report is
**1,740s**. No measured reaction could ever have satisfied it.

Nothing caught it because the two numbers agreed by eye: `--policy` printed
`max_wait_seconds: 1800` and this document said "beyond 1,800s". The ceiling
was never computed, so nothing could be compared to it.

`ReactionPolicy.max_reportable_lag_seconds` now computes it,
`FeasibilityRule.unreachable_against()` checks any lag threshold against it,
and an impossible rule is **refused** with `rule_unreachable_against_policy`
rather than quietly returning zero. The pilot's own rule carries
`min_lag_seconds = None`, because duration is not what it asks.

**Why `max_wait` should be one cadence interval.** With `max_wait` equal to
the book sampling interval, consecutive moves' response windows tile end to
end, so no Kalshi step can fall in two of them. Set it wider and they
overlap. The code does **not** derive `max_wait` from the cadence — a replay
does not know what cadence the bundle was collected at — so the run must pass
it (`--max-wait 1800` for option A), and the verdict enforces the consequence
instead: a response claimed by two moves counts once, for the earlier, and
the later is reported as `shared_response`.

Overlap would also blur the **lag**, which is why the setting still matters
even though the ordering count is protected: when a second book move follows
inside the window, a Kalshi step after both is book-led either way, but the
lag measured from the first move overstates the lag if the exchange was
answering the second.

---

## 3. What a null result would and would not establish

If option A returns `stop`, that means: **on a 30-minute grid, over this
sample, these sources rarely established the book leading.**

It does **not** establish that the lag is shorter than 30 minutes. The same
output is produced by sparse moves, right-censored responses, blind
candle intervals, or a genuinely smaller share of long-lived discrepancies.
Those are different causes with different implications and the run reports
their counts separately for exactly that reason.

A previous revision said a failure "means the lag is under 30 minutes". It
does not, and that sentence is removed.

---

## 4. Step 0: is the book even quoted at T-72h?

**The cheapest thing here, and it gates everything else.**

Sharp-quote availability 72h out is **unverified for NFL**. MLB was measured
and failed at 48h on sharp coverage. If Pinnacle h2h is not carried at T-72h,
this horizon cannot be bought at any price and the window truncates to
wherever coverage starts.

Four archive snapshots on a **past** NFL date — T-72h, T-48h, T-24h and
kickoff — settle it for **40 credits**. Its only output is "from when is the
sharp book present". Nothing below should be approved before it answers.

It also checks the assumption under §5's measurement count: that the earliest
snapshot carries the *later* clusters' games too.

---

## 5. What it costs, from a timestamp manifest

The cost is **the instants themselves**, enumerated, deduplicated and priced
— not `days × snapshots_per_day`. That arithmetic was wrong twice over here:

- `CapturePlan.days` counts **inclusive calendar dates**. Thursday 13:00 to
  Sunday 13:00 is 72 elapsed hours but **four** dates, so 48/day quoted
  **192** requests for a window holding **145**.
- It cannot express two kickoff clusters sharing most of their windows, which
  is the normal case on an NFL Sunday.

`reaction/capture.build_manifest` enumerates the UTC instants and
`TimestampManifest.budget()` derives the enforced bound from their count,
**including the retry reserve** — which the prose previously promised and
`CapturePlan.budget()` did not enforce.

All rows: one NFL Sunday, three kickoff clusters, **T-72h → kickoff closed at
both ends**, grid-aligned, 10% retry reserve.

| option | cadence | book bracket | instants | + retries | **credits** |
|---|---|---|---|---|---|
| **0** | coverage probe, one window, no reserve | — | 4 | 4 | **40** |
| **A** | 30-min | 1,800s | 161 | 178 | **1,780** |
| B | hourly | 3,600s | 82 | 91 | **910** |
| C | 15-min | 900s | 320 | 352 | **3,520** |
| D | 5-min (archive floor) | 300s | 953 | 1,049 | **10,490** |
| E | two Sundays at A's cadence | 1,800s | 322 | 355 | **3,550** |

**Alignment is most of the bill.** Three clusters at 13:00, 16:25 and 20:20
put their unaligned 30-minute grids minutes apart, so nothing deduplicates:
**435** instants instead of 161, and **4,790** credits instead of 1,780.
Snapping every window to a common 30-minute boundary costs only that each
opens up to one cadence early. The manifest reports both.

**Option B is a trap.** Cheapest real row, and its *negative* case teaches
nothing: a 3,600s bracket cannot distinguish "the exchange follows in four
minutes" from "in fifty", and those point opposite ways.

**Recommendation: option A, 1,780 credits**, after option 0 answers. Read as
a long-lived-discrepancy probe with the limits in §1, not as the capture
study.

### Cache reuse, baselines and retries

- **One snapshot is the whole sport's slate at one instant.** Per-game
  marginal cost is zero; the horizon, not the slate size, sets the bill. It
  is also why only the manifest's **earliest** instant is a pure baseline —
  later clusters' games are already in it.
- **A repeat at the same instant is a duplicate at full price.** Responses
  are written to disk raw and no instant is re-issued. Because the bundle
  stores raw payloads, every subsequent *replay* is free forever.
- **A dropped request costs the move that spans it**, since a gap
  re-baselines the stream. Retries are part of the measurement, not just the
  budget, and the 10% reserve is inside the enforced bound.

---

## 6. What it would NOT establish

- **Not an edge.** One slate is ~13 games and at most 13 entries under
  one-entry-per-game. Any EV figure from it is an anecdote.
- **Not a backtest.** NFL kickoffs come from an external schedule retrieved
  after the fact, carrying each game's **final** time — a sharper problem at
  72h than near kickoff. Every NFL run carries
  `historical_schedule_as_of=unverified` and `point_in_time` is False.
- **Not a lag finer than the grid**, and never finer than the **300-second**
  archive floor.
- **Not fillable size.** Candlesticks carry no depth; every figure is at
  one-contract size.
- **Not suspension-aware.** A minute with no candle could be an unmoved quote
  or absent data, graded UNANSWERABLE. The measurement takes the conservative
  reading (`BLIND_INTERVAL`) and `--capability-verify` carries a free command
  that would settle it — **worth running before any paid collection**.
- **Not a holdout result.** See below.

---

## 7. The holdout problem is not solved by this pilot

September 1–16 2026 is **development data**: every threshold in this study
was declared against it, the checkpoint result was measured on it, and the
defects of many review rounds were found in it.
`reaction/episodes.py` raises `HoldoutViolation` on any run over that window
declared a holdout, and `holdout_available` is `False` because none exists.

A pilot over a *later* slate is out-of-sample for the **thresholds** — they
were fixed before it and `--policy` prints them — but it is not a holdout,
because the pilot's own result would inform whatever comes next. Spending it
as both spends it twice.

**Proposal: run the pilot as an openly exploratory probe, and reserve a
later, untouched weekend as the holdout** — collected but not looked at until
a policy is frozen. Decide that before collecting; a window that has been
looked at cannot be un-looked-at.

---

## 8. What would have to be true before a live pilot

1. **A prospective recorder.** Every availability figure in replay assumes
   **zero delivery delay** — a historical snapshot has no receipt time. The
   audit grades "what the LIVE feed could do" UNANSWERABLE. A live pilot's
   first job is to *measure* that delay. It is also the only route to
   responses faster than the archive's 300-second floor.
2. **The decision-clock bound, on live records.** In replay it and the
   capture-age bound are the same number, so no historical fixture
   distinguishes them. That defect only exists on the live path.
3. **Depth, from a source that has it** — or an explicit decision that
   one-contract size is the whole strategy.
4. **A fee schedule for `KXNFLGAME`.** The dated multiplier is known for
   `KXMLBGAME` (halved 2026-08-07); NFL's is assumed at the generic 0.07.
5. **The account route.** `$0.0001` direct versus `$0.01` non-direct, where
   at one-contract size the rounding quantum is most of the fee.

---

## 9. Still open, from the owner

- Authorisation for the **40-credit** coverage probe (option 0), and
  separately for **any** measurement collection (option A = 1,780 credits).
- 72h/48h sharp-quote availability for NFL. Option 0 exists to answer this.
- The dated `KXNFLGAME` fee schedule.
- The seven-item NCAA gap list.
- Whether to reserve a holdout weekend now, per §7.

---

## 10. What is already built and free

```
python3 run_reaction.py --capability          # the source timing audit
python3 run_reaction.py --capability-verify   # free commands to re-check it
python3 run_reaction.py --policy              # the declared thresholds
python3 run_reaction.py --replay bundle.json  # the whole chain, offline
```

Exit codes: `0` when the replay ran — **zero entries is a result** — `1` for
a defect someone can fix (unreadable bundle, mislabelled holdout,
non-reconciling ledger, incomplete parse, or no usable target coverage),
`2` for usage.
