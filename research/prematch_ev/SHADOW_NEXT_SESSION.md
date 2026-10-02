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

## The research channels: development now, validation later

The 2026-10-01 session showed two shapes the adjacent detector cannot see:
PIT-CLE's 1.1336pp drift inside an hour in sub-point steps, and LAR-PHI's
1.5781pp change across one quote missing under HTTP 200. Two research-only
channels now watch for them (`reaction/research.py`, README "Research
channels"). They are kept apart from the frozen screen's validation in
every respect:

- **Development data for them:** the 2026-10-01 session -- both examples
  were read before the rules were written, and each threshold rests on one
  example -- and the synthetic development fixture built from its figures.
  Nothing measured on either is evidence for the channels.
- **The freeze** fixes, beside everything listed above: `research-drift-v1`
  (60-minute trailing window, 1pp, episodes merged within 60 minutes, the
  interruption rules, the spacing backstop at three cadences) and
  `research-return-v1` (1pp, gap at most 300s, return at most 120s old,
  the judging order, the same episode rule), and the research read bounds
  (a fresh execution read per newly opened episode, follows every 10s for
  30 minutes, at most 4 games read per answer and 4 followed at once, no
  read within 10s of the next paid request). A validation session records
  them (`session_start.research`).
- **The read-out is declared now,** in the same terms as the adjacent one
  and reported beside it, never pooled with it:
  - the unit is one **game-level episode** per channel; its contract is the
    one whose YES the move favoured, its mirror reported beside it and
    never counted again; an episode overlapping an adjacent trigger or the
    other channel is counted, and named, as overlapping;
  - for each declared markout: priced and censored counts (by reason), the
    median, minimum and maximum net after both fees on the non-direct
    route, the number positive, and the sharp state; the counterfactual
    screen's verdicts at the fresh quote, as counts, labelled as never an
    admission;
  - every run reports its distributions whatever it finds: excursions and
    return changes in the 0.1/0.25/0.5/0.75/1pp bins, gap causes and
    lengths, refusals by reason, and the research read load;
  - **no claim that a channel repeats on fewer than 20 game-level
    episodes** of that channel, pooled across validation sessions only.
- **The extra free load** is bounded and disclosed: per newly opened
  in-horizon episode, two execution reads and at most two reads per 10s for
  30 minutes (fewer when slots fall inside the 10s guard, about a third at
  a 30s cadence, or the adjacent path is already following the game), at
  most 4 games at once -- no more than 0.8 reads a second in the worst
  case, none of them paid. The synthetic development session made 480
  research reads beside 1,676 adjacent ones over 2.5 hours on 3 games.

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

1. **Re-analyse the two development sessions offline, on the new
   commit.** 2026-10-01 first: it shows both research channels on the
   session that motivated them, as development evidence (its research
   captures enter at its decision reads, since it predates research reads):

   ```
   cd research/prematch_ev
   python3 shadow_monitor.py \
       --report study_output/shadow/shadow_20261001T044202Z.jsonl \
       --json study_output/shadow/shadow-20261001-diagnostics.json \
       > study_output/shadow/shadow-20261001-diagnostics.txt
   ```

   The research section's `inputs check` should read "agrees": the
   channels' private detector rebuilds the session's recorded triggers (0),
   which shows the replay saw what the monitor saw. Then 2026-09-24:

   ```
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
JSONL file under `study_output/shadow/` (gitignored), and beside it
`<session>.status.json`, so there is nothing to commit. Read the status file
first: `complete`, `recovered_with_gaps`, `ended_in_outage` or
`stopped_early`, and why.

## What to expect operationally

- **Reads per tick.** Each game in the horizon costs 2 free book reads at
  the start of every tick, before the paid poll. A full Sunday slate of
  about 14 games is about 28 reads. At 0.2-0.4 seconds each, the read phase
  runs 5-10 seconds, and the first books read are that much older when a
  move arrives. Each assessment now records its decision book's age, so
  this is measured rather than hidden. One failed read abandons the rest of
  that tick's reads, and the next tick recovers.
- **An outage no longer ends the session.** Three transient failures in a
  row pause paid polling; probes follow at 60s, 120s, 240s, 480s, 600s and
  600s, and the session stops only if none answers within those six or 45
  minutes -- judged when a probe actually leaves, so a laptop that slept
  through a probe's due time stops rather than probing late. Each probe is
  one reserved credit, inside the same cap; the session's end does not
  move, and nothing is sent at or after it (the status file's `deadline`
  block counts such requests: zero). A refused key stops on its first
  poll. A laptop that sleeps is a declared gap, not a long interval: the
  first poll on waking re-anchors rather than moves, and sleeping through
  the end leaves a gap too. Keeping the machine awake still matters -- a
  gap is data the session does not have.
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
