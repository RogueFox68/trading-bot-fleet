# Reaction-lag pilot: proposal

**Status: a proposal. Nothing here is authorised and nothing here has been
run.** No paid collection, no live capture, no orders. The earlier
1,500-credit approval does not carry over, and every credit figure below is a
**price quoted from a transcribed cost model**, not a permission.

The owner has set the direction (2026-09-22): **finer data over more of it**,
within a **20,000-credit** account budget. That decides which option §5
recommends; it does not spend anything. The collector that would spend it,
`collect_reaction.py`, is built and **plans by default**: it buys only when
`--spend` confirms a price at least as large as the one it has just printed,
on the owner's machine.

Everything downstream is reachable today from `python3 run_reaction.py
--policy` and `--replay`. This document is about whether producing the bundle
is worth paying for, and what the answer would and would not mean.

---

## 1. Two independent dimensions, and the grid decides what is visible

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

So a coarse grid is a **constrained long-lived-discrepancy probe**. It is
not the sharp-move capture study and does not stand in for it. Its positive
result would be real and its negative result would be narrow. That is why the
recommendation is now the archive's own **300-second** floor (option D, §5):
the finest grid the archive sells is the closest a backtest can come to the
poller the strategy would actually run. Below five minutes only a live
recorder sees anything (§8).

### What a sampled grid can and cannot answer

**Can:**

- Establish ordering for any exchange response landing strictly after one of
  our samples: `BOOK_LED` needs only that Kalshi's candle bracket opens after
  our snapshot, which is reachable throughout the measurement window.
- Give a **lower** bound on the true lag.
- Show whether a discrepancy that a poller at that cadence could have seen
  was still open when it looked again.

**Cannot:**

- **See a move that happens and reverses inside one interval.** Only the net
  change between consecutive samples is observed, so an out-and-back move is
  invisible and a sequence of moves reads as one. An earlier claim here that
  uniform sampling "catches every move" was simply wrong.
- **Bound the true lag from above.** The book changed somewhere inside the
  preceding interval, so the true lag is somewhere in
  `[measured, measured + cadence]`.
- **See an opportunity shorter than the cadence.** An exchange that follows
  faster than the grid has already moved by the time the poller looks; the
  replay files that as `exchange_moved_before_trigger`, an opportunity this
  poller could not have taken — not evidence that none existed. Not "they are
  rare" — unobserved. At 30 minutes that is most of the range that matters;
  at 5 minutes it is the sub-five-minute followers.
- **Support a claim about short lags from a null result.** See §3.

Closing that gap as far as history allows means the archive's floor, which
is what §5 recommends; below five minutes it means a live prospective
recorder, because the archive has no finer grid to buy.

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

### Why the horizon is not one cadence interval

An earlier revision said `max_wait` should be one cadence interval, so that
consecutive moves' response windows would tile end to end. On the 30-minute
grid it priced, that was the declared horizon anyway and nothing turned on
it. On a 5-minute grid it would **right-censor every exchange response slower
than about four minutes** — the slow followers a 5-minute poller could trade
against, which is the thesis — and a censored response leaves the fraction
entirely.

Tiling was not available at a finer grid in any case: the 30-minute
**lookback** overlaps earlier moves' windows at any grid finer than 30
minutes, whatever the horizon. What protects the count is the verdict: a
response claimed by two moves counts once, for the earlier, and the later is
reported as `shared_response`.

So the horizon is the declared 30 minutes or one cadence interval, whichever
is longer. A replay does not derive it — it does not know the grid a bundle
was collected at — so the collector prints it with the replay command:
`--max-wait 1800` at 5 minutes, and at 30.

Two limits remain, and neither is the horizon's to fix. The dedupe matches
**identical** exchange brackets, so an exchange drifting in sub-threshold
steps can still register at different candles for two moves. And overlap
blurs the **lag**: when a second book move follows inside the window, a
Kalshi step after both is book-led either way, but the lag measured from the
first move overstates the lag if the exchange was answering the second.

---

## 3. What a null result would and would not establish

If the recommended run returns `stop`, that means: **on a 5-minute grid,
over this sample, these sources rarely established the book leading.**

It does **not** establish that the lag is shorter than 5 minutes. A null
result — `stop` or `insufficient_observable_events` — is produced by sparse
moves, right-censored responses (a follower slower than the horizon leaves
the fraction, which can push it toward `stop`), blind candle intervals, or a
genuinely smaller share of book-led moves. Those are different causes with
different implications and the run reports their counts separately for
exactly that reason.

A previous revision said a failure of its 30-minute option "means the lag is
under 30 minutes". It does not, and that sentence is removed; the same
reasoning holds at 5 minutes.

---

## 4. Step 0: is the book even quoted at T-72h?

**The cheapest thing here, and it gates everything else.**

Sharp-quote availability 72h out is **unverified for NFL**. MLB was measured
and failed at 48h on sharp coverage. If Pinnacle h2h is not carried at T-72h,
this horizon cannot be bought at any price and the window truncates to
wherever coverage starts.

Four archive snapshots on a **past** NFL date — T-72h, T-48h, T-24h and
kickoff, against that date's earliest kickoff — settle it for **40
credits**:

```
python3 collect_reaction.py --day 2026-09-20 --probe             # the price, free
python3 collect_reaction.py --day 2026-09-20 --probe --spend 40  # the probe
```

It prints, per instant, how many of the slate's games the sharp book quotes.
Its only output is "from when is the sharp book present", and that answer
picks the row in §5: the full 72h (option D) if the slate is quoted at T-72h,
48h (D48) if it first appears at T-48h. Nothing below should be approved
before it answers. Its snapshots sit on the 5-minute grid, so the full run
reuses them for free.

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

Every row: three kickoff clusters, closed at both ends, grid-aligned, 10%
retry reserve; one NFL Sunday, **T-72h → kickoff**, except D48 (T-48h) and E
(two Sundays).

| option | cadence | book bracket | instants | + retries | **credits** |
|---|---|---|---|---|---|
| **0** | coverage probe, one window, no reserve | — | 4 | 4 | **40** |
| A | 30-min | 1,800s | 161 | 178 | **1,780** |
| B | hourly | 3,600s | 82 | 91 | **910** |
| C | 15-min | 900s | 320 | 352 | **3,520** |
| **D** | 5-min (archive floor) | 300s | 953 | 1,049 | **10,490** |
| D48 | 5-min (archive floor), T-48h | 300s | 665 | 732 | **7,320** |
| E | two Sundays at A's cadence | 1,800s | 322 | 355 | **3,550** |

**Recommendation: option D, 10,490 credits**, after option 0 answers — or
D48 at 7,320 if the sharp book first appears at T-48h. This is the owner's
decision made concrete: **finer data over more of it**. One Sunday at the
archive's floor asks a question a coarse grid cannot ask at all (§1), while
more Sundays of a coarse grid (option E) only enlarge the sample of the one
narrow question it can. The price is for this table's three clusters; a slate
with an early international kickoff opens its window earlier (a 13:30Z London
game adds 42 instants and 460 credits), and the collector's plan mode prices
the actual day before anything is bought.

**What the rest of the budget is for is a separate decision.** After the
probe and option D, 9,470 credits remain of the 20,000. They could buy a
second Sunday at the same floor as the §7 holdout (D48, 7,320), or run the
shadow monitor (§8) — 72 hours at one poll a minute is 4,321 credits — which
answers what no archive can: responses faster than five minutes, and the
delivery delay every replay assumes is zero. Neither is proposed here.

**On a coarse grid, alignment is most of the bill.** Three clusters at 13:00,
16:25 and 20:20 put their unaligned 30-minute grids minutes apart, so nothing
deduplicates: **435** instants instead of 161, and **4,790** credits instead
of 1,780. Snapping every window to a common 30-minute boundary costs only that
each opens up to one cadence early. The manifest reports both. **At the
5-minute floor alignment saves nothing:** NFL kickoffs are scheduled on
five-minute marks, so the clusters already share instants.

**Option B is a trap.** Cheapest real row, and its *negative* case teaches
nothing: a 3,600s bracket cannot distinguish "the exchange follows in four
minutes" from "in fifty", and those point opposite ways.

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
- **The collector buys exactly this manifest.** Plan mode prints it and its
  price; `--spend` must cover the price and the cap enforced is the price,
  never more. An instant that still fails after its retries is kept as a
  counted hole; three in a row stop the purchase, because that is an outage
  or a refused key rather than a gap, and no bundle is written from a run
  that stopped — a re-run pays only for what is not cached.
- **Candles are free and are not trusted to arrive whole.** Kalshi documents
  a candle cap for its batch endpoint and none this study could find for the
  single-market one, so every candle response that stops before its span
  ends is followed by a request for the rest. A truncated series would
  otherwise read as blind intervals rather than as a truncation.

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

The first three are now the **shadow monitor's** job (`shadow_monitor.py`):
the owner's forward plan with the order left out — poll the sharp book,
notice a move, look at Kalshi at once, and record what a bot would have done.

1. **A prospective recorder — built.** Every availability figure in replay
   assumes **zero delivery delay**, because a historical snapshot has no
   receipt time. The monitor measures it (how old each new provider
   observation was when it reached us), measures the provider's actual
   refresh, and times Kalshi's follow at the spacing of its own reads — 10
   seconds after a move, finer than the archive's 300-second floor.
2. **The decision-clock bound, on live records — exercised.** The detector
   runs on the receipt clock, so a record captured at 12:05 and received at
   12:25 is judged 20 minutes old when it could be acted on.
3. **Depth — recorded, not decided.** The monitor reads Kalshi's public
   order book, so every shadow decision carries the size resting at the
   price it would have paid. Whether that is enough is a strategy question.
4. **A fee schedule for `KXNFLGAME`.** The dated multiplier is known for
   `KXMLBGAME` (halved 2026-08-07); NFL's is assumed at the generic 0.07.
5. **The account route.** `$0.0001` direct versus `$0.01` non-direct, where
   at one-contract size the rounding quantum is most of the fee.

**Polling, not listening.** The odds provider publishes no push feed, so the
monitor polls, at 1 credit a poll — transcribed, and checked against the
provider's own `x-requests-last` on the first answer. Polling faster than
the provider refreshes buys the same answer twice, which is why the report
prints the refresh it measured — and a first session is how the cadence gets
set from evidence, **provided it polls faster than the provider refreshes**.
If no poll finds a game unchanged, the report says the figure is only a
ceiling set by the poll spacing; an hour at 15 seconds (241 credits) measures
any refresh slower than that.

| session | every 60s | every 120s | every 300s |
|---|---|---|---|
| 24h | 1,441 | 721 | 289 |
| 72h | 4,321 | 2,161 | 865 |

Its entries are **predicted**, not realised: a live session ends before its
games do, and scoring shadow entries against settlement is not built yet.

---

## 9. Still open, from the owner

- Authorisation for the **40-credit** coverage probe (option 0), and
  separately for the measurement collection (option D = 10,490 credits, or
  D48 = 7,320 if the probe says T-48h).
- 72h/48h sharp-quote availability for NFL. Option 0 exists to answer this.
- The dated `KXNFLGAME` fee schedule.
- The seven-item NCAA gap list.
- Whether to reserve a holdout weekend now, per §7.

---

## 10. What is already built

```
python3 run_reaction.py --capability          # the source timing audit
python3 run_reaction.py --capability-verify   # free commands to re-check it
python3 run_reaction.py --policy              # the declared thresholds
python3 collect_reaction.py --day 2026-09-20 --lead-hours 72   # the price, free
python3 collect_reaction.py --day 2026-09-20 --lead-hours 72 --spend 10490 --out bundle.json
python3 run_reaction.py --replay bundle.json --max-wait 1800   # the whole chain, offline
python3 shadow_monitor.py --hours 72                           # a live session's price, free
python3 shadow_monitor.py --hours 72 --spend 4321              # live and read-only
python3 shadow_monitor.py --report study_output/shadow/SESSION.jsonl
```

Everything but a `--spend` run is free. The key comes from `--api-key` or
`ODDS_API_KEY` and reaches no cache key, path, bundle or output.

Replay exit codes: `0` when the replay ran — **zero entries is a result** —
`1` for a defect someone can fix (unreadable bundle, mislabelled holdout,
non-reconciling ledger, incomplete parse, or no usable target coverage),
`2` for usage. Collector exit codes: `0` for a complete bundle or probe, `1`
for an incomplete one (holes, candle warnings, nothing joinable) or a stopped
purchase — including a slate that did not fully answer, which buys nothing —
and `2` for a refusal made before anything was bought (a `--spend` below the
price, a window reaching past now, no key).
