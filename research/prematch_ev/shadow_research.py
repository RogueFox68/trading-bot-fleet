"""Offline read-out of the research channels: drift, and return after a gap.

    python3 shadow_monitor.py --report SESSION.jsonl   # printed after the rest

A RESEARCH SIGNAL IS NOT PERMISSION TO TRADE
--------------------------------------------
Nothing here is an entry, an admission or a recommendation. The adjacent
detector and the frozen screen are reported where they always were, with the
same figures; this section is apart from them, in its own units, under its
own names.

WHAT IT DOES
------------
Replays `reaction.research` over the session's own records -- every answered
poll's quotes, every hole the monitor declared, in the order it wrote them
-- so a session that never ran the channels gets them, and one that did gets
them checked against what it recorded. The channels' private detector is the
adjacent detector by construction, so its triggers are held against the
session's recorded ones: if they differ, the replay did not see what the
monitor saw, and it says so.

For each research EPISODE -- the unit, one per game per debounced move -- on
each joined contract:

* COVERAGE at the signal: joined to an open contract, inside the book
  horizon (the monitor's own rule, `reaction.research.research_coverage`).
* A RESEARCH CAPTURE (`reaction.adjustment.capture`, the reviewed
  machinery): entry at the first usable read at or after the signal was
  READY -- never earlier -- round trips at the declared markouts, both
  fees, depth, and censoring at kickoff and at the session's end.
* THE QUOTE'S PATH after the entry, taken from the capture's own reads:
  descriptive, not executable.
* THE EXCHANGE BEFORE THE SIGNAL: its last read at or before the anchor
  (drift) or the pre-gap sighting (return), beside the entry read. For a
  return the interval includes the gap: no instant or lag inside it is
  claimed.
* A COUNTERFACTUAL SCREEN at the fresh quote: what the frozen screen would
  have said had this been a trigger, through `reaction.assessment.assess`
  unchanged. It is never an admission and never counted as one.

Per channel: episodes, games, the primary contract (the one whose YES the
move favoured) with its MIRROR beside it and never counted again, overlap
with the adjacent detector's triggers and with the other channel, the
signal-size and gap distributions and the refusal reasons that keep a
zero-signal run informative, and the research read load.

WHAT IT MAY NOT DO
------------------
Touch the network (`--report` runs it inside `no_network()`), or change any
figure the adjacent sections report: it reads the session, never the
diagnostics' state.
"""

from __future__ import annotations

import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from data.odds_history import SHARP_BOOK                           # noqa: E402
from reaction.adjustment import (                                  # noqa: E402
    FILL_LIMITS, RESEARCH_DRIFT, RESEARCH_RETURN, CapturePolicy, capture,
    summarize,
)
from reaction.assessment import assess                             # noqa: E402
from reaction.detector import MovePolicy, MoveTrigger              # noqa: E402
from reaction.measure import usable_candle                         # noqa: E402
from reaction.research import (                                    # noqa: E402
    DRIFT, RETURN, DriftPolicy, ResearchChannels, ResearchSignal,
    ReturnPolicy, research_coverage,
)
from reaction.screen import decision_book, execution_candle        # noqa: E402
from shadow_diagnostics import (                                   # noqa: E402
    Replay, move_policy_from_record, parsed_polls, replay, sharp_readings,
    unpolled_intervals,
)
from shadow_monitor import (                                       # noqa: E402
    MAX_UNPOLLED_CADENCES, RESEARCH_PURPOSES, _time, parse_odds_row,
)

SCHEMA = "shadow-research/1"

#: The stops a session makes on an ANSWER, before its quotes reach any
#: detector (`check_answer`, `check_dates`): that answer was never observed.
ANSWER_STOPS = frozenset({"cost_unverifiable", "cost_mismatch", "clock_skew",
                          "quota_floor", "undated_quotes"})

COUNTERFACTUAL = ("COUNTERFACTUAL: the frozen screen's verdict at the fresh "
                  "quote, had this research signal been a trigger. A "
                  "research signal is never screened for entry and never "
                  "entered; this is not an admission")

KINDS = {DRIFT: RESEARCH_DRIFT, RETURN: RESEARCH_RETURN}


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


# --- the channels, as the session declared them ------------------------------

def policies_from_record(start: dict) -> tuple[MovePolicy, DriftPolicy,
                                               ReturnPolicy, str]:
    """The policies the SESSION ran, when it ran the channels; otherwise the
    ones declared now, with the spacing backstop at the session's own hole
    limit -- and the source says which."""
    move = move_policy_from_record(start.get("move_policy"))
    declared = ((start.get("research") or {}).get("policies") or {})
    drift_row, return_row = declared.get(DRIFT), declared.get(RETURN)

    def span(row: dict | None, key: str, fallback: timedelta | None
             ) -> timedelta | None:
        value = (row or {}).get(key, "absent")
        if value == "absent":
            return fallback
        return None if value is None else timedelta(seconds=value)

    limit = start.get("max_poll_spacing_seconds")
    if not isinstance(limit, (int, float)):
        cadence = start.get("cadence_seconds")
        limit = (MAX_UNPOLLED_CADENCES * cadence
                 if isinstance(cadence, (int, float)) else None)
    spacing = None if limit is None else timedelta(seconds=limit)
    d, r = DriftPolicy(), ReturnPolicy()
    drift = DriftPolicy(
        window=span(drift_row, "window_seconds", d.window),
        min_move=(drift_row or {}).get("min_move", d.min_move),
        episode_merge=span(drift_row, "episode_merge_seconds",
                           d.episode_merge),
        max_spacing=span(drift_row, "max_spacing_seconds", spacing),
        label=(drift_row or {}).get("label", d.label))
    ret = ReturnPolicy(
        min_move=(return_row or {}).get("min_move", r.min_move),
        max_gap=span(return_row, "max_gap_seconds", r.max_gap),
        max_return_age=span(return_row, "max_return_age_seconds",
                            r.max_return_age),
        episode_merge=span(return_row, "episode_merge_seconds",
                           r.episode_merge),
        label=(return_row or {}).get("label", r.label))
    source = ("recorded by the session" if declared else
              "not recorded: the session did not run the research channels, "
              "so the policies declared now are applied to its raw polls")
    return move, drift, ret, source


@dataclass
class ResearchReplay:
    channels: ResearchChannels
    #: Each episode-opening signal, with where it arose and the join then.
    openings: list = field(default_factory=list)
    policy_source: str = ""
    ran_live: bool = False
    skipped: list = field(default_factory=list)
    ended: datetime | None = None


def replay_research(rows: Sequence[dict]) -> ResearchReplay:
    """The channels, fed from the file exactly as the monitor fed them."""
    start = next((r for r in rows if r.get("kind") == "session_start"), {})
    end = next((r for r in reversed(rows) if r.get("kind") == "session_end"),
               None)
    move, drift, ret, source = policies_from_record(start)
    channels = ResearchChannels(move, drift=drift, ret=ret)
    out = ResearchReplay(channels=channels, policy_source=source,
                         ran_live="research" in start)
    cadence = start.get("cadence_seconds")
    odds = [(i, r) for i, r in enumerate(rows) if r.get("kind") == "odds"]
    stopped = None
    if end is not None and end.get("reason") in ANSWER_STOPS and odds:
        index, last = odds[-1]
        if (parse_odds_row(last) is not None
                and not last.get("after_authorized_end")):
            stopped = index
    join: dict = {}
    last_seen = None
    for index, row in enumerate(rows):
        kind = row.get("kind")
        for key in ("received_at", "at"):
            moment = _time(row.get(key))
            if moment is not None and (last_seen is None
                                       or moment > last_seen):
                last_seen = moment
        if kind == "join":
            join = dict(row.get("contracts") or {})
        elif (kind == "gap" and row.get("cause") == "not_polled"
              and row.get("recovered") is not False):
            # The tail a session ended inside has no poll after it, and the
            # monitor declared no hole for it: the end censors it instead.
            channels.on_unpolled(_time(row.get("to")))
        elif kind == "outage_start":
            channels.on_outage(_time(row.get("at")))
        elif kind == "odds":
            parsed = parse_odds_row(row)
            if parsed is None:
                channels.on_failed_poll(_time(row.get("received_at")))
                continue
            if row.get("after_authorized_end") or index == stopped:
                out.skipped.append({
                    "index": index,
                    "why": ("answered after the authorized end: recorded, "
                            "never acted on" if index != stopped else
                            f"the session stopped on this answer "
                            f"({end.get('reason')}) before its quotes "
                            f"reached any detector")})
                continue
            received = _time(row.get("received_at"))
            opened = channels.on_answer(
                parsed.quotes, received_at=received,
                sent_at=_time(row.get("sent_at")),
                ready_at=_time(row.get("ready_at")) or received,
                request_url=row.get("url") or "",
                resolution_seconds=cadence)
            for signal in opened:
                out.openings.append({"index": index, "signal": signal,
                                     "join": join})
    out.ended = _time(end.get("at")) if end else last_seen
    channels.finish(out.ended)
    return out


# --- the checks: did the replay see what the monitor saw? --------------------

def _inputs_check(rows: Sequence[dict], channels: ResearchChannels) -> dict:
    """The private detector IS the adjacent detector on the same inputs, so
    its triggers must be the session's recorded ones, in order."""
    recorded = [(r.get("stream_id"), _time(r.get("detected_at")),
                 (r.get("delta") or {}).get("home"))
                for r in rows if r.get("kind") == "trigger"]
    rebuilt = channels.adjacent
    differences = []
    for number in range(max(len(recorded), len(rebuilt))):
        was = recorded[number] if number < len(recorded) else None
        now = rebuilt[number] if number < len(rebuilt) else None
        same = (was is not None and now is not None and was[0] == now[0]
                and was[1] == now[1] and was[2] is not None
                and math.isclose(was[2], now[2], rel_tol=1e-12,
                                 abs_tol=1e-12))
        if not same:
            differences.append({"recorded": None if was is None else {
                                    "stream_id": was[0],
                                    "detected_at": _iso(was[1]),
                                    "delta_home": was[2]},
                                "rebuilt": None if now is None else {
                                    "stream_id": now[0],
                                    "detected_at": _iso(now[1]),
                                    "delta_home": now[2]}})
    return {"adjacent_triggers_recorded": len(recorded),
            "adjacent_triggers_rebuilt": len(rebuilt),
            "agrees": not differences, "differences": differences[:10],
            "note": ("the research channels judge validity with a private copy "
                     "of the adjacent detector fed the same inputs; its "
                     "triggers must be the recorded ones, or the replay did "
                     "not see what the monitor saw")}


def _signal_key(row: dict) -> tuple:
    return (row.get("channel"), row.get("stream_id"),
            _time(row.get("detected_at")), row.get("direction"),
            (row.get("episode") or {}).get("id"))


def _recorded_check(rows: Sequence[dict], openings: Sequence[dict],
                    ran_live: bool) -> dict:
    if not ran_live:
        return {"ran_live": False,
                "note": "the session did not run the research channels: "
                        "nothing recorded to check against"}
    recorded = [_signal_key(r) for r in rows
                if r.get("kind") == "research_signal"]
    rebuilt = [_signal_key(o["signal"].as_dict()) for o in openings]
    missing = [k for k in recorded if k not in rebuilt]
    extra = [k for k in rebuilt if k not in recorded]
    errors = [r for r in rows if r.get("kind") == "research_error"]

    def shown(keys: list) -> list:
        return [{"channel": k[0], "stream_id": k[1],
                 "detected_at": _iso(k[2]), "direction": k[3],
                 "episode": k[4]} for k in keys[:10]]

    return {"ran_live": True, "recorded": len(recorded),
            "rebuilt": len(rebuilt), "agrees": not missing and not extra,
            "recorded_not_rebuilt": shown(missing),
            "rebuilt_not_recorded": shown(extra),
            "stopped_by_error": [e.get("error") for e in errors],
            "note": ("a live session stops its channels on an error and "
                     "records it; the rebuild runs to the end regardless, so "
                     "an error explains signals rebuilt but not recorded")}


# --- one episode on one contract ----------------------------------------------

def _trigger_for_screen(signal: ResearchSignal, at: datetime,
                        devig_method: str) -> MoveTrigger:
    """The research signal in the shape the frozen screen reads -- dated at
    the fresh quote, the earliest a decision could have been priced -- for
    the counterfactual only."""
    current, reference = signal.current, signal.reference
    return MoveTrigger(
        event_id=signal.event_id, stream_id=signal.stream_id, detected_at=at,
        provider_observed_at=current.observed_at,
        book_change_earliest=reference.observed_at,
        book_change_latest=current.observed_at,
        provider_to_available_seconds=(
            None if current.observed_at is None
            else (current.at - current.observed_at).total_seconds()),
        age_at_decision_seconds=current.age_at_decision,
        capture_age_seconds=None,
        fair_before_away=reference.fair_away,
        fair_before_home=reference.fair_home,
        fair_after_away=current.fair_away, fair_after_home=current.fair_home,
        delta_away=signal.delta_away, delta_home=signal.delta_home,
        overround_before=reference.overround,
        overround_after=current.overround,
        devig_disagreement=current.devig_disagreement,
        policy=MovePolicy(devig_method=devig_method, book=SHARP_BOOK,
                          min_move=signal.policy.min_move,
                          label=f"{signal.policy.label}: counterfactual "
                                f"screen only"),
        before_provenance={}, after_provenance={})


def _counterfactual(signal: ResearchSignal, entry_book: Any, ticker: str,
                    yes_is_home: bool, start: datetime | None,
                    replayed: Replay, devig_method: str) -> dict:
    if entry_book is None:
        return {"label": COUNTERFACTUAL, "status": "no_fresh_quote",
                "because": "no usable read at or after the signal within the "
                           "entry window: nothing a decision could be priced "
                           "on"}
    trigger = _trigger_for_screen(signal, entry_book.ts, devig_method)
    decision, record = assess(
        trigger, [entry_book], market_ticker=ticker, yes_is_home=yes_is_home,
        start=start, entry_delay=timedelta(0),
        entry_tolerance=replayed.tolerance, series=replayed.series,
        eligibility=replayed.eligibility, route=replayed.route, tick=None)
    screen = record["screen"]
    return {"label": COUNTERFACTUAL, "status": "screened",
            "would_have_been_admitted": decision.admitted,
            "refusal": screen["refusal"], "rejection": screen["rejection"],
            "best_side": screen["best_side"],
            "best_net_ev": screen["best_net_ev"],
            "best_margin_to_floor": screen["best_margin_to_floor"],
            "priced_at": _iso(entry_book.ts),
            "assessment": record}


def _quote_path(scenario: dict) -> list[dict] | None:
    """The YES midpoint at the entry read and at each markout's exit read,
    from the capture's own reads: what the quote did, not what a round trip
    paid. A markout the capture censored has no point here either."""
    entry = scenario.get("entry")
    if not entry or "markouts" not in scenario:
        return None

    def mid(leg: dict | None) -> float | None:
        if not leg or leg.get("bid") is None or leg.get("ask") is None:
            return None
        return (leg["bid"] + leg["ask"]) / 2.0

    start = mid(entry)
    path = []
    for row in scenario["markouts"]:
        leg = row.get("exit") or (row.get("censored") or {}).get("exit")
        now = mid(leg)
        path.append({"seconds": row["seconds"], "yes_mid": now,
                     "change_from_entry": (None if now is None or start is None
                                           else now - start)})
    return path


def _exchange_before(usable: Sequence, signal: ResearchSignal,
                     entry_book: Any) -> dict:
    """The exchange's last read at or before the reference -- the anchor, or
    the pre-gap sighting -- beside the entry read. For a return the span
    includes the gap, and no instant inside it is claimed."""
    before = decision_book(usable, signal.reference.at)
    out: dict[str, Any] = {
        "reference_at": _iso(signal.reference.at),
        "read_at": None if before is None else _iso(before.ts),
        "yes_mid": None if before is None else before.mid,
        "entry_read_at": None if entry_book is None else _iso(entry_book.ts),
        "entry_yes_mid": None if entry_book is None else entry_book.mid}
    out["change"] = (None if before is None or entry_book is None
                     else entry_book.mid - before.mid)
    out["note"] = ("the exchange's change between its last read before the "
                   + ("gap and its first after the return: the span includes "
                      "the unobserved interval, so no instant or lag inside "
                      "it is claimed" if signal.channel == RETURN else
                      "anchor and the entry read: movement over the window, "
                      "not a reaction timed from any instant"))
    return out


def _contract(signal: ResearchSignal, ticker: str, contract: dict,
              replayed: Replay, policy: CapturePolicy, readings: list,
              unpolled: list, session_end: datetime | None,
              devig_method: str) -> dict:
    yes_is_home = bool(contract.get("yes_is_home"))
    start = _time(contract.get("start"))
    move = signal.delta_for(yes_is_home)
    side = "YES" if move > 0 else "NO"
    coverage = research_coverage(start, signal.detected_at, replayed.horizon)
    item: dict[str, Any] = {
        "ticker": ticker, "yes_is_home": yes_is_home, "start": _iso(start),
        "role": "primary" if move > 0 else "mirror",
        "move_for_yes": move, "favoured_side": side,
        "fair_before_yes": signal.fair_for(yes_is_home, after=False),
        "fair_after_yes": signal.fair_for(yes_is_home),
        "coverage": coverage}
    if coverage != "in_horizon":
        item["not_assessed_because"] = coverage
        return item
    reads = replayed.all_reads.get(ticker, [])
    usable = [r.book for r in reads if r.book is not None
              and usable_candle(r.book)]
    entry_book = execution_candle(usable, signal.detected_at,
                                  policy.entry_within)
    sharp = dict(readings=readings, detected_at=signal.detected_at,
                 yes_is_home=yes_is_home,
                 fair_before_yes=item["fair_before_yes"],
                 fair_after_yes=item["fair_after_yes"],
                 min_move=signal.policy.min_move, session_end=session_end,
                 unpolled=unpolled)
    scenario = capture(
        kind=KINDS[signal.channel], side=side, reads=reads,
        detected_at=signal.detected_at, start=start,
        session_end=session_end, series=replayed.series, policy=policy,
        route=replayed.route, sharp=sharp,
        why=("the side the research signal favoured, entered at the first "
             "usable read at or after the signal was ready: a research "
             "capture, never an entry"))
    item.update(
        capture=scenario, quote_path=_quote_path(scenario),
        exchange_before=_exchange_before(usable, signal, entry_book),
        counterfactual_screen=_counterfactual(signal, entry_book, ticker,
                                              yes_is_home, start, replayed,
                                              devig_method))
    return item


# --- the analysis --------------------------------------------------------------

def _overlaps(episode: Any, others: Sequence[Any], merge: timedelta) -> list:
    lo = episode.first.detected_at - merge
    hi = episode.last_at + merge
    out = []
    for other in others:
        if other.stream_id != episode.stream_id:
            continue
        if other.last_at < lo or other.first.detected_at > hi:
            continue
        out.append({"id": other.id, "direction":
                    "home_up" if other.direction > 0 else "home_down",
                    "same_direction": other.direction == episode.direction})
    return out


def _adjacent_overlaps(episode: Any, triggers: Sequence[tuple],
                       merge: timedelta) -> list:
    lo = episode.first.detected_at - merge
    hi = episode.last_at + merge
    return [{"detected_at": _iso(at), "delta_home": delta,
             "same_direction": (delta > 0) == (episode.direction > 0)}
            for stream, at, delta in triggers
            if stream == episode.stream_id and lo <= at <= hi]


def _read_load(rows: Sequence[dict]) -> dict:
    research = [r for r in rows if r.get("kind") == "book"
                and r.get("purpose") in RESEARCH_PURPOSES]
    by_purpose: dict[str, dict] = {}
    for row in research:
        entry = by_purpose.setdefault(row["purpose"],
                                      {"reads": 0, "failed": 0})
        entry["reads"] += 1
        if row.get("payload") is None or row.get("coverage"):
            entry["failed"] += 1
    adjacent = sum(1 for r in rows if r.get("kind") == "book"
                   and r.get("purpose") not in RESEARCH_PURPOSES)
    end = next((r for r in reversed(rows) if r.get("kind") == "session_end"),
               {})
    return {"research_reads": len(research), "by_purpose": by_purpose,
            "adjacent_reads": adjacent,
            "share_of_all_reads": (len(research) / (len(research) + adjacent)
                                   if research or adjacent else None),
            "recorded_counts": ((end.get("research") or {}).get("counts")),
            "note": ("free public order-book reads; no research request is "
                     "paid, so the credit cap is untouched by them")}


def analyse(rows: Sequence[dict], *, capture_policy: CapturePolicy | None = None,
            replayed: Replay | None = None) -> dict:
    """The research channels in one session, rebuilt and read out. Pure."""
    policy = capture_policy or CapturePolicy()
    replayed = replayed or replay(rows)
    rr = replay_research(rows)
    channels = rr.channels
    session_end = rr.ended
    polls = parsed_polls(replayed.odds)
    unpolled = unpolled_intervals(replayed.gaps)
    readings: dict[str, list] = {}
    by_channel: dict[str, list] = {DRIFT: [], RETURN: []}
    for opening in rr.openings:
        signal: ResearchSignal = opening["signal"]
        key = signal.event_id
        if key not in readings:
            readings[key] = sharp_readings(
                polls, signal.event_id, channels.move_policy.devig_method)
        contracts = sorted((t, c) for t, c in opening["join"].items()
                           if c.get("event") == signal.event_id)
        items = [_contract(signal, ticker, contract, replayed, policy,
                           readings[key], unpolled, session_end,
                           channels.move_policy.devig_method)
                 for ticker, contract in contracts]
        by_channel[signal.channel].append({
            "signal": signal.as_dict(), "contracts": items,
            "joined": bool(items)})
    out: dict[str, Any] = {
        "schema": SCHEMA,
        "label": ("RESEARCH ONLY: a research signal is not permission to "
                  "trade; nothing here is an entry or an admission"),
        "policies": channels.policies(),
        "policy_source": rr.policy_source,
        "ran_live": rr.ran_live,
        "evidence": ("DEVELOPMENT: the session did not run the research "
                     "channels; its signals are recomputed from its raw "
                     "polls under policies declared after its audit was "
                     "read, and its captures use only the reads it made" if
                     not rr.ran_live else
                     "the channels ran live under the recorded policies. "
                     "Validation only if they were frozen before the session "
                     "and it is analysed once at the frozen commit -- "
                     "compare session_start.code with the analysing commit"),
        "inputs_check": _inputs_check(rows, channels),
        "recorded_check": _recorded_check(rows, rr.openings, rr.ran_live),
        "answers_not_observed": rr.skipped,
        "read_load": _read_load(rows),
        "capture_policy": policy.as_dict(),
        "capture_limits": list(FILL_LIMITS),
        "distributions": channels.summary(),
    }
    for channel in (DRIFT, RETURN):
        episodes = channels.episodes[channel]
        other = channels.episodes[RETURN if channel == DRIFT else DRIFT]
        merge = (channels.drift if channel == DRIFT
                 else channels.ret).episode_merge
        listed = []
        for episode, item in zip(episodes, by_channel[channel]):
            listed.append({
                "episode": episode.as_dict(), **item,
                "overlaps_adjacent": _adjacent_overlaps(
                    episode, channels.adjacent, merge),
                "overlaps_other_channel": _overlaps(episode, other, merge)})
        primary = [(e["episode"]["event_id"], c["capture"])
                   for e in listed for c in e["contracts"]
                   if c["role"] == "primary" and "capture" in c]
        mirror = [(e["episode"]["event_id"], c["capture"])
                  for e in listed for c in e["contracts"]
                  if c["role"] == "mirror" and "capture" in c]
        counterfactual: Counter = Counter()
        for e in listed:
            for c in e["contracts"]:
                screened = c.get("counterfactual_screen")
                if c["role"] != "primary" or not screened:
                    continue
                if screened["status"] != "screened":
                    counterfactual[screened["status"]] += 1
                elif screened["would_have_been_admitted"]:
                    counterfactual["would_have_been_admitted"] += 1
                else:
                    counterfactual[screened["rejection"]
                                   or screened["refusal"]
                                   or "not_screened"] += 1
        coverage: Counter = Counter(
            c["coverage"] for e in listed for c in e["contracts"])
        out[channel] = {
            "episodes": len(listed),
            "games": len({e["episode"]["event_id"] for e in listed}),
            "not_joined": sum(1 for e in listed if not e["joined"]),
            "contracts": dict(sorted(coverage.items())),
            "overlapping_adjacent_trigger": sum(
                1 for e in listed if e["overlaps_adjacent"]),
            "overlapping_other_channel": sum(
                1 for e in listed if e["overlaps_other_channel"]),
            "reversals": sum(1 for e in listed if e["episode"]["reverses"]),
            "signal_size_pp": _sizes([abs(e["episode"]["first_delta_home"])
                                      for e in listed]),
            "capture_primary": summarize(primary, policy),
            "capture_mirror": summarize(mirror, policy),
            "counterfactual_screen_primary": dict(sorted(
                counterfactual.items())),
            "items": listed}
    return out


def _sizes(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    return {"n": len(ordered), "min": ordered[0] * 100,
            "median": (ordered[len(ordered) // 2] if len(ordered) % 2 else
                       (ordered[len(ordered) // 2 - 1]
                        + ordered[len(ordered) // 2]) / 2) * 100,
            "max": ordered[-1] * 100}


# --- rendering -------------------------------------------------------------------

def _pp(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}pp"


def _when(value: str | None) -> str:
    moment = _time(value)
    return moment.strftime("%Y-%m-%d %H:%M:%SZ") if moment else "?"


def render_research(figures: dict) -> str:
    from shadow_diagnostics import _render_scenario
    lines = ["", "RESEARCH CHANNELS  (research only: a research signal is not "
             "permission to trade; nothing here is an entry)",
             f"  evidence           {figures['evidence']}",
             f"  policies           {figures['policies'][DRIFT]['label']}, "
             f"{figures['policies'][RETURN]['label']}  "
             f"({figures['policy_source']})"]
    check = figures["inputs_check"]
    lines.append(f"  inputs check       {check['adjacent_triggers_rebuilt']} of "
                 f"{check['adjacent_triggers_recorded']} recorded adjacent "
                 f"trigger(s) rebuilt by the channels' private detector: "
                 + ("agrees" if check["agrees"] else "*** DISAGREES"))
    recorded = figures["recorded_check"]
    if recorded.get("ran_live"):
        lines.append(f"  recorded signals   {recorded['recorded']} recorded, "
                     f"{recorded['rebuilt']} rebuilt: "
                     + ("agree" if recorded["agrees"] else "*** DISAGREE"))
        for error in recorded["stopped_by_error"]:
            lines.append(f"  *** the channels stopped live: {error}")
    for skipped in figures["answers_not_observed"]:
        lines.append(f"  answer not observed: {skipped['why']}")
    load = figures["read_load"]
    lines.append(f"  research reads     {load['research_reads']} (free), "
                 f"beside {load['adjacent_reads']} adjacent reads"
                 + "".join(f"; {p}: {v['reads']} ({v['failed']} failed)"
                           for p, v in sorted(load["by_purpose"].items())))
    dist = figures["distributions"]
    drift, ret = dist[DRIFT], dist[RETURN]
    pd = figures["policies"][DRIFT]
    lines += ["", f"  DRIFT -- the trailing {pd['window_seconds'] / 60:g}-minute "
              f"window, {pd['min_move'] * 100:g}pp, episodes merged within "
              f"{pd['episode_merge_seconds'] / 60:g} min"]
    lines += _render_channel(figures[DRIFT], _render_scenario)
    lines.append(f"    evaluations {drift['evaluations']:,}; with an excursion of "
                 "at least " + ", ".join(
                     f"{k} {v:,}" for k, v in
                     drift["evaluations_with_excursion_at_least"].items()))
    lines.append("    games whose largest excursion reached "
                 + ", ".join(f"{k} {v}" for k, v in
                             drift["games_with_max_excursion_at_least"]
                             .items()))
    for row in drift["largest_excursions"][:3]:
        lines.append(f"      {row['event_id']}: {row['excursion'] * 100:.4f}pp "
                     f"at {_when(row['at'])}")
    lines.append("    refusals: " + ", ".join(
        f"{k} {v:,}" for k, v in drift["counts"].items()))
    if drift["resets_by_cause"]:
        lines.append("    window reset by: " + ", ".join(
            f"{k} {v:,}" for k, v in drift["resets_by_cause"].items()))
    pr = figures["policies"][RETURN]
    lines += ["", f"  RETURN AFTER A GAP -- {pr['min_move'] * 100:g}pp, gap at "
              f"most {pr['max_gap_seconds']:g}s, return at most "
              f"{pr['max_return_age_seconds']:g}s old, never claimed inside "
              f"the gap"]
    lines += _render_channel(figures[RETURN], _render_scenario)
    lines.append(f"    returns {ret['returns']}: " + ", ".join(
        f"{k} {v}" for k, v in ret["verdicts"].items()))
    if ret["gap_causes"]:
        lines.append("    gap causes: " + ", ".join(
            f"{k} {v}" for k, v in ret["gap_causes"].items()))
    for label, key in (("gap length", "gap_seconds"),
                       ("return age when ready", "return_age_seconds")):
        values = ret[key]
        if values:
            lines.append(f"    {label}: n={values['n']}  median "
                         f"{values['median']:.1f}s  min {values['min']:.1f}s  "
                         f"max {values['max']:.1f}s")
    lines.append("    returns with a change of at least " + ", ".join(
        f"{k} {v}" for k, v in ret["returns_with_change_at_least"].items()))
    lines.append("      of those, passing the gap, freshness and newer "
                 "checks: " + ", ".join(
                     f"{k} {v}" for k, v in
                     ret["eligible_returns_with_change_at_least"].items()))
    lines += ["", "  Research captures enter at the first usable read at or "
              "after the signal was ready; the counterfactual screen is the "
              "frozen screen's verdict at that quote had the signal been a "
              "trigger -- never an admission."]
    return "\n".join(lines)


def _render_channel(figures: dict, render_scenario: Any) -> list[str]:
    lines = [f"    episodes {figures['episodes']} on {figures['games']} "
             f"game(s); overlapping an adjacent trigger "
             f"{figures['overlapping_adjacent_trigger']}, the other channel "
             f"{figures['overlapping_other_channel']}; reversals "
             f"{figures['reversals']}; not joined {figures['not_joined']}"]
    if figures["contracts"]:
        lines.append("    contracts: " + ", ".join(
            f"{k} {v}" for k, v in figures["contracts"].items()))
    sizes = figures["signal_size_pp"]
    if sizes:
        lines.append(f"    signal size (pp): min {sizes['min']:.4f}  median "
                     f"{sizes['median']:.4f}  max {sizes['max']:.4f}")
    if figures["counterfactual_screen_primary"]:
        lines.append("    counterfactual screen, primary contracts: " + ", ".join(
            f"{k} {v}" for k, v in
            figures["counterfactual_screen_primary"].items()))
    for item in figures["items"]:
        episode, signal = item["episode"], item["signal"]
        lines.append(f"    [{episode['id']}] {_when(episode['opened_at'])}  "
                     f"{episode['event_id']}  home "
                     f"{_pp(episode['first_delta_home'])} "
                     f"(peak {episode['peak_magnitude'] * 100:.4f}pp, "
                     f"{episode['qualifying_observations']} qualifying)"
                     + (f"  reverses {episode['reverses']}"
                        if episode["reverses"] else ""))
        bracket = signal["change_bracket"]["our_sightings"]
        lines.append(f"      from {_when(bracket['from'])} to "
                     f"{_when(bracket['to'])} ({bracket['seconds']:.0f}s)"
                     + (f"; gap {signal['gap']['first_cause']}, "
                        f"{signal['gap']['seconds']:.1f}s"
                        if "gap" in signal else "")
                     + (f"; largest single step "
                        f"{signal['window']['largest_single_step'] * 100:.4f}pp"
                        if "window" in signal else ""))
        reference = signal["reference"]
        if reference.get("re_served"):
            # The anchor or pre-gap sighting was a copy the provider
            # re-served: its sighting time is not its observation time.
            age = reference.get("age_at_decision_seconds")
            lines.append(f"      reference is a re-served copy: provider stamp "
                         f"{_when(reference['provider_observed_at'])}"
                         + (f", {age:.1f}s old when ready"
                            if age is not None else ""))
        if item["overlaps_adjacent"]:
            lines.append(f"      overlaps {len(item['overlaps_adjacent'])} "
                         f"adjacent trigger(s)")
        for contract in item["contracts"]:
            if "capture" not in contract:
                lines.append(f"      {contract['ticker']} ({contract['role']}):"
                             f" not assessed -- "
                             f"{contract['not_assessed_because']}")
                continue
            screened = contract["counterfactual_screen"]
            verdict = ("no fresh quote" if screened["status"] != "screened"
                       else "would have been admitted"
                       if screened["would_have_been_admitted"] else
                       f"{screened['rejection'] or screened['refusal']}")
            lines.append(f"      {contract['ticker']} ({contract['role']}, "
                         f"{contract['favoured_side']}): counterfactual "
                         f"screen {verdict}"
                         + (f", best net EV {screened['best_net_ev']:+.4f}"
                            if screened.get("best_net_ev") is not None
                            else ""))
            if contract["role"] == "primary":
                lines += render_scenario(contract["capture"])
    return lines
