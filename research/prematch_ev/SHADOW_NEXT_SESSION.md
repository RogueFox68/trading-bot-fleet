# The next shadow session: proposed, not started

**Status: a proposal.** Nothing here is started or authorised. A session
costs up to 2,881 credits and runs only with the owner's `--spend`, on the
owner's machine.

## The question it serves

The 2026-09-24 session screened 8 contract assessments and refused all 8 at
the frozen net-EV floor. None failed on spread, price band or lead time. It
detected 5 moves in total, too few to say anything. The question now is
narrower than "is there +EV": **do Kalshi's delayed adjustments to sharp
moves repeatedly leave executable price improvement after costs?** That is
the adjustment-capture measure (`reaction.adjustment`). It needs no
settlement, so a live session can answer it, which the settlement-value
screen cannot do before the games are played.

One more session will not settle it either. At 2026-09-24's rate of 5 moves
a day, the 20 game-moves the read-out below requires take about four
sessions. This proposal is for the first validation session, not the last.

## Development and validation, kept apart

- **Development data:** the September 1-16 replay data, the 2026-09-24 shadow
  session, and the synthetic fixture built from its reported figures. Every
  diagnostic, the capture rules and the markout set were designed with
  these in view, so none of them can test anything.
- **Validation data:** a session run after a **freeze**. The freeze is a
  commit made before the session starts. The session records it
  (`session_start.code`), and the analysis records its own
  (`session.analysed_by`), so the claim is checkable. The freeze fixes:
  - `MovePolicy` and `ReactionPolicy`, as recorded on every session;
  - `Eligibility`, the frozen screen, unchanged since the checkpoint study;
  - `CapturePolicy`: markouts at 30s, 1m, 2m, 5m, 10m, 15m and 30m from the
    entry, entry within 30s of the move, exit tolerance 30s, one contract;
  - the book horizon: 73h, the screen's own lead-time ceiling;
  - the fee model, including KXNFLGAME's dated schedule: multiplier 1
    from 2026-01-01T08:00Z (change `babedc22-e303-4aaf-8e0b-5016f1239786`),
    read by `verify_fees.py` on the owner's machine on 2026-09-25 and
    recorded in `core/fees.py`. A change found later is recorded before a
    freeze, never after looking at the session.
- A validation session is analysed **once, at the frozen commit**. If
  anything is changed after that read, the session becomes development data
  and the next untouched session is the validation set.
- **The read-out is declared now, and nothing is a go/no-go:**
  - the unit is one **game-move**. Its contract is the one whose YES the move
    favoured. The other contract is its mirror through a separate book: it
    is reported beside it and never counted as a second observation;
  - for each declared markout: priced and censored counts (by reason);
    the median, minimum and maximum net after both fees on the dearer
    (non-direct) route; the number with a positive net; the same figures on
    the direct route as a sensitivity; and the sharp state at that markout;
  - **no claim that improvement repeats on fewer than 20 game-moves**, pooled
    across validation sessions only. The floor is the reaction pilot's, for
    the same reason: below it, one lucky afternoon is the result.

## The window, and why this one

**24 hours at a 30-second cadence: 2,881 polls, 2,881 credits** at one credit
per call (`floor(24 x 3600 / 30) + 1`).

**Proposed: Friday 2026-10-02 14:00 UTC to Saturday 2026-10-03 14:00 UTC.**

- **Friday injury reports.** Friday's report is the last practice report
  for Sunday games and the one that carries game-status designations. It is
  due by 4 p.m. ET (20:00 UTC). Starting at 14:00 UTC covers the morning
  news cycle and the afternoon reports, with six hours of lead.
- **Every Sunday game is inside the 73-hour horizon for the whole
  session.**
  - 1 p.m. ET kickoffs (17:00 UTC) entered it on Thursday at 16:00 UTC.
  - 4:05 and 4:25 p.m. ET kickoffs entered it on Thursday at 19:05 and
    19:25 UTC.
  - Sunday night (00:20 UTC Monday) entered it on Thursday at 23:20 UTC.
- **Monday night (00:15 UTC Tuesday) enters at 23:15 UTC Friday** and is
  watched for the last 14.75 hours of the session. Any move on it before
  then is classified `outside_observation_horizon`, not as a collection
  failure.
- **Thursday night's game** finishes before the session starts.

**These are the NFL's usual kickoff slots, not a confirmed schedule.** The
environment this proposal was written in cannot reach ESPN or Kalshi. The
free plan (step 3 below) is what confirms the games. If the slate differs
(an international game at 13:30 UTC, a flexed kickoff, byes), the window
moves with it. The same offsets work on any Friday.

## Before it runs (all free)

1. **Re-analyse 2026-09-24 offline, on the new commit:**

   ```
   cd research/prematch_ev
   python3 shadow_monitor.py \
       --report study_output/shadow_24h/shadow_20260924T014422Z.jsonl \
       --json study_output/shadow_24h/shadow-24h-diagnostics.json \
       > study_output/shadow_24h/shadow-24h-diagnostics.txt
   ```

   It makes no network request and cannot: it runs inside `no_network()`,
   and running it with the network off proves as much. The coverage block
   should reproduce the owner's reported figures: 5 moves, 4 games, 10
   assessments, 8 `below_ev_floor`, Houston
   `outside_observation_horizon`. Any disagreement between a recorded
   decision and its recomputation is printed rather than resolved.

2. **Re-check the NFL fee for the new window.** The old session's window
   was verified on 2026-09-25: one dated change, multiplier 1 from
   2026-01-01T08:00Z, the current series fields in agreement. That entry
   is recorded. Re-read it for the new window, and the old one beside it:

   ```
   python3 verify_fees.py --series KXNFLGAME \
       --window 2026-10-02T14:00:00Z 2026-10-03T14:00:00Z
   python3 verify_fees.py --series KXNFLGAME \
       --window 2026-09-24T01:44:22Z 2026-09-25T01:44:22Z
   ```

   Both should report the same entry in force at each end and no change
   inside. If a new change appears, a person records it in `core/fees.py`
   from the printed snippet before the freeze. The account route stays
   unresolved either way: no public endpoint answers it.

3. **Plan the window on Thursday 2026-10-01 or later.** The slate covers
   today-1 to today+7, and the week's markets must already be listed:

   ```
   python3 shadow_monitor.py --hours 24 --cadence-seconds 30 \
       --starts-at 2026-10-02T14:00:00Z
   ```

   For each game it prints when the game enters the horizon and how many
   hours of the session watch it. It also reads one real order book for
   free and prices the session.

4. **Freeze.** Commit, and record the SHA in this file.

## Running it (only once authorised)

The session starts when the command is run, so run it at 14:00 UTC Friday:

```
python3 shadow_monitor.py --hours 24 --cadence-seconds 30 --spend 2881
```

The machine must stay awake, on the network, and NTP-synchronised: a clock
more than 5 seconds off the provider's stops the session. It writes one
JSONL file under `study_output/shadow/` (gitignored), so there is nothing to
commit.

## What to expect operationally

- **Reads per tick.** Each game in the horizon costs 2 free book reads at
  the start of every tick, before the paid poll. A full Sunday slate of
  about 14 games is about 28 reads. At 0.2-0.4 seconds each, the read phase
  runs 5-10 seconds, and the first books read are that much older when a
  move arrives. Each assessment now records its decision book's age, so
  this is measured rather than hidden. One failed read abandons the rest of
  that tick's reads, and the next tick recovers.
- **What changed since the first session:** decisions record their tick
  and assessment; `session_start` records the fee route, entry tolerance,
  book memory and commit; and the hourly rejoin no longer sits between a
  move and its execution read. In the analysis, an admitted capture enters
  at the admitted trade's own execution quote, price and fee, and every
  read a round trip uses must arrive before kickoff and by the session's
  end. The screen, the detector, the thresholds and the cadence are
  unchanged.

## What it will not answer

- **Fills.** No order is placed. A fill is assumed at the top of the book
  as it was read.
- **Settlement value.** The games settle after the session ends. Realised
  figures need `--report --settlements`, and there are none until the
  screen admits something.
- **The account route.** The KXNFLGAME multiplier is dated; the account
  route and the rounding source are not. Every net figure is priced on the
  dearer non-direct route, the direct route is priced beside it, and each
  assessment's sensitivity table shows when the route decides a verdict.
