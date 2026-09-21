# Pre-Match +EV — Thesis Test

A read-only study answering one question before any bot gets built:

> **Does the de-vigged sharp line predict settlement better than the prediction
> market's own price?**

That is the entire strategy thesis. If the answer is no, the strategy is dead
and no amount of connector quality, Kelly tuning or fee modelling rescues it.
The question needs **no fill simulation, no order book model and no execution
logic** — three columns per settled game answer it.

Nothing here can place an order. It holds no trading credentials and imports no
exchange SDK.

---

## Why this exists before the bot

The originating spec jumped from the maths straight
to an execution engine. Three problems in it would have lost money, and one of
them inverts the strategy's own selection. All three are corrected here, and
each correction is pinned by a test.

### 1. The fee model was structurally wrong

The spec modelled fees as a flat fraction of the $1.00 payout
(`W_net = 1 - F_rate`). Neither venue works that way — both charge a
**parabolic fee at execution**:

| Venue | Taker | Maker |
|---|---|---|
| Kalshi | `ceil(0.07 × C × P × (1-P))`, rounded up per order | ~¼ the coefficient; often $0 after rounding |
| Polymarket (intl.) | `C × θ × P × (1-P)`, θ = 0.05 for sports | zero, plus rebates |

As a fraction of **stake** — the unit a `EV / price` screen is denominated in —
the Kalshi fee is `0.07 × (1 - price)`: **monotonically decreasing in price.**
A 5¢ contract costs 6.65% of stake to trade; a 95¢ contract costs 0.35%.

No flat rate approximates a parabola. Modelled at a flat 2%, every contract
clearing a 3% screen has a true edge between **−1.55% and +4.75%**:

```
 price  P_fair@3%  spec says   fee/stake   TRUE edge
  0.05     0.0526       3.0%       6.65%      -1.55%
  0.10     0.1051       3.0%       6.30%      -1.20%
  0.35     0.3679       3.0%       4.55%      +0.55%
  0.50     0.5255       3.0%       3.50%      +1.60%
  0.90     0.9459       3.0%       0.70%      +4.40%
```

Pinned by `tests/test_fees.py::FeeCurveTest`.

### 2. The threshold was non-uniform, and three errors compounded

`EV / price ≥ 3%` demands **18× less probability edge** on a 5¢ contract
(0.26pp) than on a 90¢ one (4.59pp). Meanwhile multiplicative de-vigging
**overstates longshot probability**, and fees are highest there. Threshold
loosest, model most biased upward, fees highest — all in the same corner of the
price curve. A screen that systematically selects cheap contracts.

Corrected three ways: an absolute probability floor rides alongside the
percentage, Shin's method is available and is the default, and a stated price
band (0.15–0.85) marks where the model is trustworthy.

### 3. Fuzzy title matching is a silent wrong-answer generator

`rapidfuzz.extractOne` at 85 over natural-language market titles will
confidently return the wrong game for a divisional rematch or any shared-city
pair. A mismatched event means trading the wrong game holding a confident fair
probability — not an error, a wrong-side-of-the-market position.

Here, fuzzy matching is **scoped and bounded**: it maps one team name to an
abbreviation within one league's closed roster, never compares two titles. The
match key is hard — `(league, date, away, home)` plus start-time agreement.
Ambiguous names reject. There is no default answer, the same rule as the
fleet's ownership resolver.

---

## Running it

Staged deliberately — archive depth decides whether the study is possible at
all, and that costs almost nothing to check.

```bash
cd research/prematch_ev
cp .env.example .env          # fill in ODDS_API_KEY
pip install -r requirements.txt   # optional; stdlib-only otherwise

# 0. What would this cost? No network calls.
python3 run_study.py --plan --sport MLB --from 2026-05-13 --to 2026-09-15

# 1. How deep is the odds archive? ~5 credits. RUN THIS FIRST.
python3 run_study.py --probe --sport MLB

# 2. Do our abbreviations match Kalshi's tickers? Free.
python3 data/kalshi_history.py --audit-abbreviations --series KXMLBGAME --league MLB

# 3. The study.
python3 run_study.py --sport MLB --series KXMLBGAME \
    --from 2026-05-13 --to 2026-09-15 --lead-minutes 60
```

Tests: `python3 -m unittest discover -s tests -t .` (77 tests, no network, no
credentials, runs with or without rapidfuzz).

### Start with MLB

If the odds archive is as shallow as it appears (~May 2026), MLB is the only
sport with a usable sample right now: a full May–September stretch, roughly
2,000 games, and two-way moneylines that fit the de-vig maths exactly. NFL has
about three weeks of the 2026 season; NBA and NHL have essentially nothing in
that window.

---

## Reading the result

Four sections, in the order they matter.

**[1] Accuracy** — Brier and log loss with a bootstrap CI on the *paired*
difference. `SHARP LINE WINS` requires the interval to exclude zero.

**[2] Calibration** — a predictor can win on Brier while being biased in the
exact price region you intend to trade.

**[3] Disagreement** — when the two differ materially, who is right? This is
the money table. An edge only exists where they disagree, so pooled accuracy
over games they agree on dilutes the signal being tested.

**[4] Decay** — everything above, by month. **Read the trend, not the pooled
mean.** Kalshi's sports markets are young and their volume has ramped hard; an
edge that existed in early 2025 and has since been arbitraged away produces an
encouraging pooled number and no tradeable present.

### Calibrating your expectations

On synthetic data where the sharp line genuinely *is* sharper (1% noise vs 6%),
the disagreement hit rate runs about **51–54%**, not 60%. A real edge here
looks like many small correct nudges, not dramatic per-game calls. If the
disagreement table shows 65% you have a bug, not a goldmine.

### What counts as a go

- Accuracy CI strictly below zero, **and**
- disagreement hit rate above 50% and rising with the threshold, **and**
- the decay series **not** trending to zero in recent months, **and**
- coverage `complete`.

Anything less is a no-go or a re-run, not a judgement call.

---

## What this does not answer

**Maker fills cannot be simulated honestly.** Queue position is unrecoverable
from historical data: a bid resting at 52¢ with trades printing tells you
nothing about whether *you* would have been filled, since you would have been
behind everyone already there. Every naive maker backtest overestimates fills.

Treat any maker result as an **upper bound**. That is still a valid
one-directional test — if it is not profitable under optimistic fill
assumptions, it certainly is not in reality — but it can only kill the
strategy, never confirm it.

**Adverse selection is measurable even though fills are not.** For every
hypothetical fill, look at where the price went over the next 5/15/60 minutes.
If they are systematically followed by adverse movement, you have measured the
effect directly — better than live paper trading would, because you have the
complete forward path on every observation, not just the ones that filled.
That analysis is not built yet; `observations.json` carries the bid/ask needed
for it.

**Taker fills are clean**, bounded by depth. Candlesticks carry no size at the
quote, so full-size fills on thin markets are assumed, not proven.

---

## Relationship to the fleet

**None, deliberately.** Different exchange, different broker, different
settlement model, different risk profile.

- Not registered in `fleet_registry.BOTS`. That registry derives ownership
  tags, accountant queries, `reconcile_fills`, Alpaca-denominated budgets and
  the config audit — a non-Alpaca entry breaks every one of those consumers.
- No entry in `deploy/ecosystem.config.js`. Nothing here runs under PM2.
- Separate `requirements.txt`. The root manifest is pinned and drives the
  container image; this directory must never touch it.
- Imports nothing from the fleet, and the fleet imports nothing from here.

It lives in this repo as a **quarantined study** because the Beelink is where
the eventual service would run, and because the observability plane
(`logger.py`, `error_watchdog.py` → InfluxDB → Grafana) is worth inheriting.
When the thesis validates, this directory lifts out into its own repo and
becomes a sibling container — it does not join the fleet.

---

## Verify before trusting

Two things in here are conventional spellings rather than verified facts, and
both fail *silently* as thin coverage rather than loudly as errors:

1. **Fee schedules change.** Both are stamped with the date verified
   (2026-09-21) and the source. `core.fees.describe()` prints the stamps and
   every report carries them. The Polymarket **US** entity publishes a
   different schedule and is deliberately `NotImplementedError` rather than a
   plausible guess.
2. **Roster abbreviations have not been checked against live Kalshi tickers.**
   Run the audit in step 2 above first. An abbreviation the exchange spells
   differently never matches, and shows up as a team contributing no data.
