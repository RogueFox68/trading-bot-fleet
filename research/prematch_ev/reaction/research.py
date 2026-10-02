"""Research-only signal channels: drift across a trailing window, and a price
that changed while it could not be seen.

A RESEARCH SIGNAL IS NOT PERMISSION TO TRADE
--------------------------------------------
The adjacent-jump detector (`reaction.detector`) and the frozen screen are the
only path to a shadow entry, and nothing in this module touches either. These
channels watch the SAME sharp quotes, decide nothing, place nothing, and are
measured only as hypothetical captures (`reaction.adjustment`) under their own
names and in their own counts. They exist because the 2026-10-01 development
session showed two kinds of sharp-book change the adjacent detector cannot
see, by design:

* PIT-CLE drifted 1.1336pp inside an uninterrupted hour in steps of under a
  point each. The adjacent detector moves its baseline on every
  sub-threshold change, so it measures successive jumps, never their sum.
* LAR-PHI's quote went missing for one poll (HTTP 200, the event present,
  `bookmakers: []`) and came back 1.5781pp away. The adjacent detector
  re-anchors across any hole -- correctly: it cannot claim a move across an
  interval it did not see -- so the change never reaches it.

Both are DEVELOPMENT evidence. The rules below were declared after seeing
them; nothing measured on that session can test them, and the thresholds are
not fitted to it (it has one example of each).

ONE IDEA OF A VALID OBSERVATION
-------------------------------
Whether a quote is usable is decided by a private `MoveDetector` running the
session's own `MovePolicy` -- declared book, two-sided, de-viggable, dated by
the provider, clock-coherent, at most 900s old when actionable, in arrival
order, not an older copy -- fed exactly what the monitor's detector is fed:
the same envelopes, the same declared gaps, the same in-play retirement. Its
verdict on each quote classifies it. A second implementation of "valid" would
eventually disagree with the first (rule 19); this one cannot, and because its
triggers are by construction the adjacent detector's, the offline report
checks them against the session's recorded triggers to prove the replay saw
what the monitor saw.

ONE EXCEPTION, AND WHY: A RE-SERVED COPY IS AGED HERE. The detector
recognises unchanged content BEFORE it applies its freshness rule -- by
design: to it a re-served copy means the feed did not stop, so continuity
advances while the baseline stays put -- and therefore never says whether a
repeat is still fresh. A provider re-serving one observation for twenty
minutes would keep a segment alive here, on a price nobody had seen for most
of them (owner's review 5959548662). So a repeat is judged by
`repeat_freshness`: the detector's own bound, on the detector's own clock
(`age_at_decision_seconds`: this copy's provider stamp to its readiness), in
the order the detector applies them to new content. The adjacent detector is
unchanged and still keeps its continuity across such copies.

INTERRUPTIONS: THE ONE THING BOTH CHANNELS MAY NEVER CROSS
-----------------------------------------------------------
A stream is interrupted by:

* a declared gap: a failed poll (and the outage it becomes), a stretch the
  monitor did not poll, or the stream missing from an answered poll -- which
  covers an HTTP 200 whose event carries no usable sharp quote;
* an unusable observation: one-sided, undeviggable, undated, stale at
  decision, or clock-incoherent;
* a RE-SERVED COPY PAST THE AGE BOUND (`re_served:stale_at_decision`): the
  same content again, now more than `max_age_at_decision` old when ready.
  Stricter than the adjacent detector, which never ages a repeat (above).
  The last copy still inside the bound is the last valid sighting; no stale
  copy is one;
* an OLDER COPY re-served (`regressed_content`). Stricter than the adjacent
  detector, which keeps its baseline across one: at that poll the current
  price was not observed, and these channels do not bridge what was not seen;
* more than `max_spacing` between sightings, or more than the detector's own
  `max_gap`: backstops, since the monitor declares every hole it leaves.

After an interruption, only a valid NEW observation restarts anything: a
re-served copy of the old content does not, fresh or stale, exactly as it
cannot restart the adjacent detector's baseline.

CHANNEL 1 -- `drift` (DriftPolicy)
----------------------------------
1. SEGMENT: a stream's valid observations -- new values, and re-served
   repeats while they are still fresh -- since its last interruption. An
   interruption empties it. Each records its own provider stamp, receipt and
   age, so a repeat that anchors a signal says that it is one.
2. ANCHORS: every observation in the segment whose decision-ready time is
   within `window` of the current one's. An anchor older than that has
   expired; none survives an interruption. There is no other age bound: each
   anchor was fresh by the detector's rule when it arrived.
3. SIGNAL: the current observation qualifies UP when the home side's fair
   probability is at least `min_move` above the window's lowest anchor, and
   DOWN when at least `min_move` below its highest -- equivalently, when it
   differs by `min_move` from SOME anchor in the window. Each direction is
   judged on its own. The reported anchor is the most recent observation at
   that extreme: the last time the price stood there, so the change began
   after it.
4. CAUSAL: only observations ready by the current one's decision-ready time
   take part, and the signal is dated at that time -- the executable clock.
5. EPISODES: a qualifying observation joins its stream's episode in the same
   direction if that episode's last qualifying observation was within
   `episode_merge` before it; otherwise it opens one. Only an opening signal
   is assessed and followed. An interruption does not end an episode -- the
   merge window does -- so a hole cannot split one move into two, and a
   window sliding over one move cannot count it again. A qualifying
   observation the other way opens its own episode and names the one it
   reverses.

CHANNEL 2 -- `return` (ReturnPolicy)
------------------------------------
1. GAP: an interruption of a stream that has a valid observation opens one;
   that observation is the PRE-GAP reference, timed by its latest VALID
   sighting (a re-served repeat before the gap is one while it is fresh; a
   stale one never is, so it cannot shorten the gap).
2. INSIDE IT: a re-served copy of the pre-gap content keeps it open, fresh
   or stale: it is not a new observation (after a declared hole it cannot
   restore the adjacent detector's baseline either). So does an unusable or
   older observation, recorded as a refused return with its reason.
3. THE RETURN is the first valid new observation after it -- the one the
   adjacent detector re-anchors on. It is judged once, in this order:
   `gap_too_long` (more than `max_gap` from the pre-gap sighting to the
   return being ready), `return_stale` (more than `max_return_age` old when
   ready), `return_not_newer` (not observed by the provider strictly after
   the pre-gap observation), `below_threshold`; otherwise a signal. The gap
   closes on its return whatever the verdict: nothing later is ever compared
   across it.
4. IN PLAY and THE END: a stream that goes in play with a gap open is
   `censored_in_play`; one still open when the session stops is
   `censored_session_end`.
5. NO INSTANT IS CLAIMED. The change lies somewhere between the provider's
   pre-gap and returning observations, and between our own last sighting and
   the return -- a bracket that INCLUDES the unobserved interval. Nothing
   here names a bookmaker-move instant or an exchange lag inside it; what
   follows the return is measured from the return.
6. EPISODES: as channel 1, per stream and direction.

The policies are recorded on every session that runs them and on every
signal, with their declaration: development-informed, not tuned on outcomes,
and not validated.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .clocks import envelope_for_live_sharp_quote
from .detector import MovePolicy, MoveDetector, Rejection, fair_probabilities

DRIFT = "drift"
RETURN = "return"
CHANNELS = (DRIFT, RETURN)

#: What the private detector's verdict on one quote means here. Every
#: `Rejection` the detector can give a quote is in exactly one of these, and
#: an import-time check below fails the moment a new one is added unplaced.
_NEW = frozenset({"trigger", Rejection.BELOW_THRESHOLD.value,
                  Rejection.VIG_ONLY.value})
_RESTART = frozenset({Rejection.FIRST_OBSERVATION.value,
                      Rejection.BASELINE_INVALIDATED.value,
                      Rejection.GAP.value})
_REPEAT = frozenset({Rejection.UNCHANGED_CONTENT.value})
_BREAK = frozenset({Rejection.MISSING_SIDE.value, Rejection.UNDEVIGGABLE.value,
                    Rejection.UNKNOWN_CONTENT_AGE.value,
                    Rejection.STALE_AT_DECISION.value,
                    Rejection.CLOCK_ORDER_INVALID.value,
                    Rejection.REGRESSED_CONTENT.value})
_IGNORED = frozenset({Rejection.OUT_OF_ORDER.value, Rejection.WRONG_BOOK.value,
                      Rejection.NO_AVAILABILITY.value})
_DECLARED_ONLY = frozenset({Rejection.DECLARED_GAP.value})

_placed = _NEW | _RESTART | _REPEAT | _BREAK | _IGNORED | _DECLARED_ONLY
_unplaced = {r.value for r in Rejection} - _placed
if _unplaced:                                       # pragma: no cover
    raise ImportError(f"detector verdict(s) {sorted(_unplaced)} have no "
                      f"meaning in reaction.research: place them before use")

#: Return verdicts, in the order they are judged.
SIGNAL = "signal"
GAP_TOO_LONG = "gap_too_long"
RETURN_STALE = "return_stale"
RETURN_NOT_NEWER = "return_not_newer"
BELOW_THRESHOLD = "below_threshold"
CENSORED_IN_PLAY = "censored_in_play"
CENSORED_SESSION_END = "censored_session_end"

#: A re-served copy that is no longer a valid sighting interrupts under this
#: prefix and the rejection the detector would give NEW content of that age:
#: `re_served:stale_at_decision`, chiefly.
RE_SERVED = "re_served"

#: The bins every distribution is reported in: the owner's 2026-10-01 drift
#: audit used these, so a run with no signal is still comparable with it.
BINS = (0.001, 0.0025, 0.005, 0.0075, 0.01)

DECLARED = ("2026-10-02, after the 2026-10-01 session's offline audit was read "
            "(PIT-CLE 1.1336pp inside an uninterrupted hour; LAR-PHI 1.5781pp "
            "across one missing quote): development-informed, not tuned on "
            "outcomes, not validated")


def repeat_freshness(policy: MovePolicy, age: float | None) -> str | None:
    """The detector's freshness rule, for a copy the detector does not age.

    `MoveDetector.observe` recognises unchanged content before it reaches its
    age checks, so a re-served copy is never judged stale there. This applies
    those checks -- same `MovePolicy`, same clock (`age` is the copy's own
    `age_at_decision_seconds`), same order -- and returns the rejection the
    detector gives NEW content of that age, or None when it would accept it.
    `test_research_channels` holds the two to the same answer at the bound.
    """
    if age is None:
        return (Rejection.UNKNOWN_CONTENT_AGE.value
                if policy.require_content_age else None)
    if age < 0:
        return Rejection.CLOCK_ORDER_INVALID.value
    if age > policy.max_age_at_decision.total_seconds():
        return Rejection.STALE_AT_DECISION.value
    return None


def _iso(moment: datetime | None) -> str | None:
    return (moment.astimezone(timezone.utc).isoformat()
            if moment is not None else None)


def _seconds(delta: timedelta | None) -> float | None:
    return None if delta is None else delta.total_seconds()


def _positive(name: str, value: Any) -> None:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"{name} must be a positive timedelta, got {value!r}")


def _threshold(value: Any) -> None:
    if not isinstance(value, (int, float)) or not value > 0 or value != value:
        raise ValueError(f"min_move must be positive, got {value!r}")


@dataclass(frozen=True)
class DriftPolicy:
    """Channel 1, declared before any session runs it. See the module text.

    `max_spacing` is a backstop the monitor sets to its own hole limit (three
    cadences): it declares every longer stretch itself, so this can only be
    slightly stricter than the monitor, never looser. None disables it.
    """

    window: timedelta = timedelta(minutes=60)
    min_move: float = 0.01
    episode_merge: timedelta = timedelta(minutes=60)
    max_spacing: timedelta | None = None
    label: str = "research-drift-v1"

    def __post_init__(self) -> None:
        _positive("window", self.window)
        _positive("episode_merge", self.episode_merge)
        if self.max_spacing is not None:
            _positive("max_spacing", self.max_spacing)
        _threshold(self.min_move)

    def as_dict(self) -> dict:
        return {"label": self.label, "channel": DRIFT,
                "window_seconds": self.window.total_seconds(),
                "min_move": self.min_move,
                "episode_merge_seconds": self.episode_merge.total_seconds(),
                "max_spacing_seconds": _seconds(self.max_spacing),
                "anchor": ("every valid observation in the segment within the "
                           "window; the reported one is the most recent at "
                           "the window's extreme"),
                "research_only": True, "tuned_on_outcomes": False,
                "validated": False, "declared": DECLARED}


@dataclass(frozen=True)
class ReturnPolicy:
    """Channel 2, declared before any session runs it. See the module text.

    `max_gap` is half the one measured exchange response bracket (2026-09-24,
    ~610-620s): a gap longer than that may already hold the exchange's whole
    adjustment, so the return would not be news to it. `max_return_age`
    is four 30s cadences, about eight times the reported median age of a
    newly observed quote (14s): a return older than that is the provider
    lagging, not the book coming back.
    """

    min_move: float = 0.01
    max_gap: timedelta = timedelta(seconds=300)
    max_return_age: timedelta = timedelta(seconds=120)
    episode_merge: timedelta = timedelta(minutes=60)
    label: str = "research-return-v1"

    def __post_init__(self) -> None:
        _positive("max_gap", self.max_gap)
        _positive("max_return_age", self.max_return_age)
        _positive("episode_merge", self.episode_merge)
        _threshold(self.min_move)

    def as_dict(self) -> dict:
        return {"label": self.label, "channel": RETURN,
                "min_move": self.min_move,
                "max_gap_seconds": self.max_gap.total_seconds(),
                "max_return_age_seconds": self.max_return_age.total_seconds(),
                "episode_merge_seconds": self.episode_merge.total_seconds(),
                "judged_in_order": [GAP_TOO_LONG, RETURN_STALE,
                                    RETURN_NOT_NEWER, BELOW_THRESHOLD],
                "research_only": True, "tuned_on_outcomes": False,
                "validated": False, "declared": DECLARED}


@dataclass(frozen=True)
class Point:
    """One valid sighting of a stream's price, with that sighting's own
    clocks: a re-served repeat carries ITS receipt and ITS age, beside the
    provider stamp it shares with the first sighting of the value."""

    at: datetime                     # decision-ready: when it could be used
    fair_home: float
    fair_away: float
    observed_at: datetime | None     # the provider's own observation stamp
    received_at: datetime | None     # the whole answer had arrived
    age_at_decision: float | None
    new: bool                        # False: a re-served repeat
    overround: float | None = None
    devig_disagreement: float | None = None

    def as_dict(self) -> dict:
        return {"ready_at": _iso(self.at),
                "fair": {"home": self.fair_home, "away": self.fair_away},
                "provider_observed_at": _iso(self.observed_at),
                "received_at": _iso(self.received_at),
                "age_at_decision_seconds": self.age_at_decision,
                "re_served": not self.new,
                "overround": self.overround,
                "devig_disagreement": self.devig_disagreement}


@dataclass
class Gap:
    """A stream's open interruption, as channel 2 holds it.

    `pre` is the pre-gap value as first seen; `pre_seen` its last VALID
    sighting before the gap (`pre` itself, or a fresh re-served copy of it),
    which is where the gap is measured from."""

    pre: Point
    pre_seen: Point
    opened_at: datetime | None
    causes: Counter = field(default_factory=Counter)
    order: list = field(default_factory=list)
    re_served: int = 0
    refused: list = field(default_factory=list)

    @property
    def pre_last_seen(self) -> datetime:
        return self.pre_seen.at

    def add(self, cause: str) -> None:
        if cause not in self.causes:
            self.order.append(cause)
        self.causes[cause] += 1

    def as_dict(self) -> dict:
        return {"opened_at": _iso(self.opened_at),
                "causes": [{"cause": c, "count": self.causes[c]}
                           for c in self.order],
                "first_cause": self.order[0] if self.order else None,
                "re_served_unchanged": self.re_served,
                "refused_returns": list(self.refused)}


@dataclass(frozen=True)
class ResearchSignal:
    """One qualifying observation on one channel.

    `reference` is the anchor (drift) or the pre-gap observation (return);
    `current` the observation that qualified. `detected_at` is when it was
    READY -- the executable clock -- and is the earliest instant anything may
    be read or decided for it.
    """

    channel: str
    policy: Any
    event_id: str
    stream_id: str
    direction: int                   # +1: the home side's fair probability rose
    reference: Point
    current: Point
    adjacent_trigger_here: bool      # the adjacent detector fired on this quote
    episode_id: str
    opened: bool
    reverses: str | None
    detail: dict

    @property
    def detected_at(self) -> datetime:
        return self.current.at

    @property
    def delta_home(self) -> float:
        return self.current.fair_home - self.reference.fair_home

    @property
    def delta_away(self) -> float:
        return self.current.fair_away - self.reference.fair_away

    @property
    def magnitude(self) -> float:
        return abs(self.delta_home)

    def delta_for(self, yes_is_home: bool) -> float:
        """Oriented to one contract's YES participant."""
        return self.delta_home if yes_is_home else self.delta_away

    def fair_for(self, yes_is_home: bool, *, after: bool = True) -> float:
        point = self.current if after else self.reference
        return point.fair_home if yes_is_home else point.fair_away

    def as_dict(self) -> dict:
        provider = {"earliest": _iso(self.reference.observed_at),
                    "latest": _iso(self.current.observed_at)}
        local = {"from": _iso(self.reference.at), "to": _iso(self.current.at),
                 "seconds": (self.current.at
                             - self.reference.at).total_seconds()}
        return {
            "channel": self.channel, "policy": self.policy.as_dict(),
            "research_only": True,
            "not_an_entry": ("a research signal is not permission to trade: "
                             "it is never screened for entry and never "
                             "entered"),
            "event_id": self.event_id, "stream_id": self.stream_id,
            "detected_at": _iso(self.detected_at),
            "direction": "home_up" if self.direction > 0 else "home_down",
            "reference": self.reference.as_dict(),
            "current": self.current.as_dict(),
            "delta": {"home": self.delta_home, "away": self.delta_away},
            "change_bracket": {
                "provider_observations": provider, "our_sightings": local,
                "note": ("the change lies somewhere in these intervals; no "
                         "instant inside them is claimed"
                         + (", and they include the unobserved gap"
                            if self.channel == RETURN else ""))},
            "adjacent_trigger_on_this_quote": self.adjacent_trigger_here,
            "episode": {"id": self.episode_id, "opened": self.opened,
                        "reverses": self.reverses},
            **self.detail,
        }


@dataclass
class Episode:
    """One debounced research signal: the unit counted per game."""

    id: str
    channel: str
    event_id: str
    stream_id: str
    direction: int
    first: ResearchSignal
    last_at: datetime
    qualifying: int = 1
    peak: float = 0.0
    reverses: str | None = None

    def as_dict(self) -> dict:
        return {"id": self.id, "channel": self.channel,
                "event_id": self.event_id, "stream_id": self.stream_id,
                "direction": "home_up" if self.direction > 0 else "home_down",
                "opened_at": _iso(self.first.detected_at),
                "last_qualifying_at": _iso(self.last_at),
                "qualifying_observations": self.qualifying,
                "first_delta_home": self.first.delta_home,
                "peak_magnitude": self.peak, "reverses": self.reverses,
                "adjacent_trigger_on_opening_quote":
                    self.first.adjacent_trigger_here}


@dataclass
class Stream:
    """Everything both channels hold for one stream."""

    stream_id: str
    event_id: str
    segment: list = field(default_factory=list)       # channel 1's points
    last_valid: Point | None = None       # the latest valid VALUE, first seen
    seen: Point | None = None   # its latest valid sighting: it, or a fresh copy
    last_sighting: datetime | None = None  # latest observation of any kind
    gap: Gap | None = None
    retired: bool = False
    max_excursion: float = 0.0
    max_excursion_at: datetime | None = None

    @property
    def last_seen(self) -> datetime | None:
        return None if self.seen is None else self.seen.at


class ResearchChannels:
    """Both channels over one session's sharp quotes. Pure: no I/O.

    Fed in the monitor's own order: `on_answer` for an answered poll,
    `on_failed_poll`, `on_unpolled` and `on_outage` where the monitor
    declares those, and `finish` when the session stops. The offline report
    makes the same calls from the session file, so a recorded signal and a
    recomputed one come from one implementation.
    """

    def __init__(self, move_policy: MovePolicy, *,
                 drift: DriftPolicy | None = None,
                 ret: ReturnPolicy | None = None) -> None:
        self.move_policy = move_policy
        self.drift = drift or DriftPolicy()
        self.ret = ret or ReturnPolicy()
        self.detector = MoveDetector(move_policy)
        self.streams: dict[str, Stream] = {}
        self.seen: set[str] = set()
        self.started: set[str] = set()
        #: The private detector's own triggers, which are the adjacent
        #: detector's by construction: (stream, detected_at, delta_home).
        self.adjacent: list[tuple[str, datetime, float]] = []
        self.episodes: dict[str, list[Episode]] = {DRIFT: [], RETURN: []}
        self._open: dict[tuple, Episode] = {}
        self.returns: list[dict] = []
        self.counts: dict[str, Counter] = {DRIFT: Counter(), RETURN: Counter(),
                                           "observations": Counter()}
        self.resets: Counter = Counter()
        self.bins: Counter = Counter()
        self.evaluations = 0

    # --- the policies, as a session records them ---------------------------

    def policies(self) -> dict:
        return {DRIFT: self.drift.as_dict(), RETURN: self.ret.as_dict(),
                "observation_rules": {
                    "move_policy": self.move_policy.as_dict(),
                    "interruptions": sorted(_BREAK) + [
                        "declared:absent_from_answer", "declared:failed_poll",
                        "declared:not_polled", "declared:outage",
                        Rejection.GAP.value, "spacing_exceeded"] + [
                        f"{RE_SERVED}:{r.value}" for r in (
                            Rejection.STALE_AT_DECISION,
                            Rejection.UNKNOWN_CONTENT_AGE,
                            Rejection.CLOCK_ORDER_INVALID)],
                    "re_served_copies": (
                        "a sighting while within the move policy's age bound "
                        "when ready, judged on the copy's own provider stamp "
                        "and readiness; past it, an interruption, and the last "
                        "copy within it is the last valid sighting"),
                    "note": ("validity is the adjacent detector's own rule, "
                             "run privately on the same inputs; an older copy "
                             "and a re-served copy past the age bound are "
                             "interruptions here, though not to the adjacent "
                             "detector, which never ages a repeat")}}

    # --- what the monitor declares ----------------------------------------

    def on_answer(self, quotes: Iterable[Any], *, received_at: datetime,
                  sent_at: datetime | None, ready_at: datetime,
                  request_url: str = "",
                  resolution_seconds: float | None = None
                  ) -> list[ResearchSignal]:
        """One answered poll, exactly as `ShadowMonitor.absorb` walks it:
        in play retires a stream, a stream missing from the answer is a
        declared gap. Returns the signals that OPENED an episode."""
        opened: list[ResearchSignal] = []
        present: set[str] = set()
        for quote in quotes:
            envelope = envelope_for_live_sharp_quote(
                quote, received_at=received_at, sent_at=sent_at,
                ready_at=ready_at, request_url=request_url,
                resolution_seconds=resolution_seconds)
            stream = envelope.provenance.market_id
            if quote.commence_time <= received_at:
                self._retire(stream, envelope.provenance.event_id,
                             received_at)
                continue
            present.add(stream)
            opened += self._observe(envelope)
        for stream in sorted(self.seen - present - self.started):
            self._declared(stream, received_at, "absent_from_answer",
                           "absent from the live response")
        self.seen = (self.seen | present) - self.started
        return opened

    def on_failed_poll(self, at: datetime | None) -> None:
        for stream in sorted(self.seen):
            self._declared(stream, at, "failed_poll",
                           "the live poll did not answer")

    def on_unpolled(self, at: datetime) -> None:
        for stream in sorted(self.seen):
            self._declared(stream, at, "not_polled",
                           "the sharp book was not polled")

    def on_outage(self, at: datetime | None) -> None:
        """The failed polls already interrupted every stream; this names
        what they became."""
        for stream in sorted(self.seen):
            state = self.streams.get(stream)
            if state is not None and state.gap is not None:
                state.gap.add("declared:outage")

    def finish(self, at: datetime | None) -> None:
        """The session stopped: a gap still open has no return."""
        for stream in sorted(self.streams):
            state = self.streams[stream]
            if state.gap is not None:
                self._censor(state, CENSORED_SESSION_END, at)

    # --- one quote ---------------------------------------------------------

    def _verdict(self, envelope: Any) -> str:
        rejections = self.detector.result.rejections
        before = len(rejections)
        trigger = self.detector.observe(envelope)
        if trigger is not None:
            verdict = "trigger"
            self.adjacent.append((trigger.stream_id, trigger.detected_at,
                                  trigger.delta_home))
        elif len(rejections) == before + 1:
            verdict = rejections[-1].reason.value
        else:                                       # pragma: no cover
            raise RuntimeError("the detector gave no single verdict on a quote")
        rejections.clear()
        self.detector.result.triggers.clear()
        return verdict

    def _state(self, stream: str, event_id: str) -> Stream:
        state = self.streams.get(stream)
        if state is None:
            state = self.streams[stream] = Stream(stream, event_id)
        return state

    def _observe(self, envelope: Any) -> list[ResearchSignal]:
        stream = envelope.provenance.market_id
        state = self._state(stream, envelope.provenance.event_id)
        verdict = self._verdict(envelope)
        self.counts["observations"][verdict] += 1
        available, _ = envelope.available_at()
        if verdict in _IGNORED or available is None:
            return []
        spacing = self.drift.max_spacing
        if (spacing is not None and state.gap is None
                and state.last_sighting is not None
                and available - state.last_sighting > spacing):
            # A BACKSTOP: the monitor declares every hole it leaves, so this
            # only fires if one went undeclared. Inside an open gap the
            # stream is already interrupted and it would add nothing.
            self._interrupt(state, state.last_sighting + spacing,
                            "spacing_exceeded")
        state.last_sighting = available
        if verdict in _BREAK:
            if state.gap is not None:
                state.gap.refused.append({"ready_at": _iso(available),
                                          "reason": verdict})
            self._interrupt(state, available, verdict)
            return []
        if verdict in _REPEAT:
            return self._repeat(state, envelope, available)
        quote = envelope.payload
        fair = fair_probabilities(getattr(quote, "away_price", None),
                                  getattr(quote, "home_price", None),
                                  self.move_policy.devig_method)
        if fair is None:                            # pragma: no cover
            raise RuntimeError(f"{stream}: the detector accepted a quote that "
                               f"will not de-vig")
        point = Point(at=available, fair_home=fair[1], fair_away=fair[0],
                      observed_at=envelope.provider_observed_at,
                      received_at=envelope.response_received_at,
                      age_at_decision=envelope.age_at_decision_seconds(),
                      new=True, overround=fair[2], devig_disagreement=fair[3])
        if (verdict in (Rejection.BASELINE_INVALIDATED.value,
                        Rejection.GAP.value)
                and state.gap is None and state.last_valid is not None):
            # The detector saw a hole these channels were not told of: its
            # own max_gap, or a mismatch. Either way it is a hole here too.
            self._interrupt(state, state.last_seen, verdict)
        signals: list[ResearchSignal] = []
        adjacent = verdict == "trigger"
        if state.gap is not None:
            signals += self._returned(state, point, adjacent)
        signals += self._drift(state, point, adjacent)
        state.last_valid = state.seen = point
        return [s for s in signals if s.opened]

    def _repeat(self, state: Stream, envelope: Any,
                at: datetime) -> list[ResearchSignal]:
        """The provider re-served content already seen.

        Inside a gap it changes nothing, fresh or stale: it is not a new
        observation, so it can neither close the gap nor restart anything.
        Outside one it is one more sighting of the same price only while it
        is still FRESH by the session's own bound, judged on this copy's own
        provider stamp and readiness (`repeat_freshness`): the private
        detector cannot say, because it recognises a repeat before it ages
        anything. Past the bound the stream is interrupted, the anchors go,
        and the gap opens from the last copy that was still fresh -- a stale
        copy is never a sighting, so it cannot carry continuity or shorten a
        gap."""
        if state.gap is not None:
            state.gap.re_served += 1
            self.counts[RETURN]["re_served_inside_gap"] += 1
            return []
        last = state.last_valid
        if last is None or not state.segment:
            self.counts[DRIFT]["repeat_outside_a_segment"] += 1
            return []
        age = envelope.age_at_decision_seconds()
        problem = repeat_freshness(self.move_policy, age)
        if problem is not None:
            self._interrupt(state, at, f"{RE_SERVED}:{problem}")
            return []
        point = Point(at=at, fair_home=last.fair_home, fair_away=last.fair_away,
                      observed_at=envelope.provider_observed_at,
                      received_at=envelope.response_received_at,
                      age_at_decision=age, new=False,
                      overround=last.overround,
                      devig_disagreement=last.devig_disagreement)
        signals = self._drift(state, point, False)
        state.seen = point
        return [s for s in signals if s.opened]

    def _retire(self, stream: str, event_id: str, at: datetime) -> None:
        """In play: the monitor stops watching the stream, and so does this."""
        self.started.add(stream)
        state = self._state(stream, event_id)
        if state.retired:
            return
        state.retired = True
        state.segment.clear()
        self.counts[DRIFT]["retired_in_play"] += 1
        if state.gap is not None:
            self._censor(state, CENSORED_IN_PLAY, at)

    def _declared(self, stream: str, at: datetime | None, cause: str,
                  reason: str) -> None:
        self.detector.note_gap(stream, at, reason)
        self.detector.result.rejections.clear()
        state = self.streams.get(stream)
        if state is not None:
            self._interrupt(state, at, f"declared:{cause}")

    def _interrupt(self, state: Stream, at: datetime | None,
                   cause: str) -> None:
        """No anchor survives this, and a gap opens if there is anything to
        compare a return with."""
        if state.segment:
            self.resets[cause] += 1
            state.segment.clear()
        if state.gap is not None:
            state.gap.add(cause)
            return
        if state.last_valid is None or state.seen is None:
            self.counts[RETURN]["interrupted_before_any_observation"] += 1
            return
        state.gap = Gap(pre=state.last_valid, pre_seen=state.seen,
                        opened_at=at)
        state.gap.add(cause)
        self.counts[RETURN]["gaps_opened"] += 1

    # --- channel 2 ----------------------------------------------------------

    def _censor(self, state: Stream, verdict: str,
                at: datetime | None) -> None:
        gap, state.gap = state.gap, None
        self.counts[RETURN][verdict] += 1
        self.returns.append({
            "stream_id": state.stream_id, "event_id": state.event_id,
            "verdict": verdict, "pre_gap": gap.pre.as_dict(),
            "pre_gap_last_seen": _iso(gap.pre_last_seen),
            "pre_gap_last_sighting": gap.pre_seen.as_dict(),
            "gap": {**gap.as_dict(), "closed_at": _iso(at),
                    "seconds": (None if at is None else
                                (at - gap.pre_last_seen).total_seconds())},
            "return": None, "delta": None})

    def _returned(self, state: Stream, point: Point,
                  adjacent: bool) -> list[ResearchSignal]:
        gap, state.gap = state.gap, None
        policy = self.ret
        seconds = (point.at - gap.pre_last_seen).total_seconds()
        delta = point.fair_home - gap.pre.fair_home
        failed = []
        if seconds > policy.max_gap.total_seconds():
            failed.append(GAP_TOO_LONG)
        if (point.age_at_decision is None
                or point.age_at_decision > policy.max_return_age.total_seconds()):
            failed.append(RETURN_STALE)
        if (point.observed_at is None or gap.pre.observed_at is None
                or point.observed_at <= gap.pre.observed_at):
            failed.append(RETURN_NOT_NEWER)
        if abs(delta) < policy.min_move:
            failed.append(BELOW_THRESHOLD)
        verdict = failed[0] if failed else SIGNAL
        self.counts[RETURN][verdict] += 1
        detail = {
            "gap": {**gap.as_dict(), "closed_at": _iso(point.at),
                    "seconds": seconds,
                    "pre_gap_last_seen": _iso(gap.pre_last_seen),
                    "pre_gap_value_first_seen": _iso(gap.pre.at),
                    "max_gap_seconds": policy.max_gap.total_seconds()},
            "return_checks": {"failed": failed,
                              "max_return_age_seconds":
                                  policy.max_return_age.total_seconds()},
            "no_instant_claimed": (
                "the book's change and any exchange reaction lie somewhere in "
                "the bracket, which includes the unobserved interval; what "
                "follows is measured from the return")}
        self.returns.append({
            "stream_id": state.stream_id, "event_id": state.event_id,
            "verdict": verdict, "pre_gap": gap.pre.as_dict(),
            "pre_gap_last_seen": _iso(gap.pre_last_seen),
            "pre_gap_last_sighting": gap.pre_seen.as_dict(),
            "gap": detail["gap"], "return": point.as_dict(),
            "delta": {"home": delta,
                      "away": point.fair_away - gap.pre.fair_away},
            "failed": failed, "adjacent_trigger_on_this_quote": adjacent})
        if verdict != SIGNAL:
            return []
        # The reference is the last VALID sighting before the gap, with its
        # own clocks: the first sighting of the value if nothing re-served it,
        # else the last copy that was still fresh.
        reference = gap.pre_seen
        return [self._hit(RETURN, policy, state, reference, point,
                          1 if delta > 0 else -1, adjacent, detail)]

    # --- channel 1 ----------------------------------------------------------

    def _drift(self, state: Stream, point: Point,
               adjacent: bool) -> list[ResearchSignal]:
        policy = self.drift
        horizon = point.at - policy.window
        anchors = [p for p in state.segment if p.at >= horizon]
        state.segment = anchors + [point]
        self.evaluations += 1
        if not anchors:
            self.counts[DRIFT]["no_anchor_in_window"] += 1
            return []
        low = min(p.fair_home for p in anchors)
        high = max(p.fair_home for p in anchors)
        up, down = point.fair_home - low, high - point.fair_home
        excursion = max(up, down)
        for edge in BINS:
            if excursion >= edge:
                self.bins[edge] += 1
        if excursion > state.max_excursion:
            state.max_excursion, state.max_excursion_at = excursion, point.at
        out = []
        for direction, size, extreme in ((1, up, low), (-1, down, high)):
            if size < policy.min_move:
                continue
            index = max(i for i, p in enumerate(anchors)
                        if p.fair_home == extreme)
            path = anchors[index:] + [point]
            steps = [abs(b.fair_home - a.fair_home)
                     for a, b in zip(path, path[1:])]
            detail = {"window": {
                "seconds": policy.window.total_seconds(),
                "anchor_age_seconds": (point.at
                                       - anchors[index].at).total_seconds(),
                "anchors_in_window": len(anchors),
                "segment_since": _iso(anchors[0].at),
                "largest_single_step": max(steps) if steps else 0.0,
                "value_changes": sum(1 for s in steps if s > 0),
                "contains_an_adjacent_size_step":
                    any(s >= self.move_policy.min_move for s in steps)}}
            out.append(self._hit(DRIFT, policy, state, anchors[index], point,
                                 direction, adjacent, detail))
        if not out:
            self.counts[DRIFT]["below_threshold"] += 1
        return out

    # --- episodes -----------------------------------------------------------

    def _hit(self, channel: str, policy: Any, state: Stream, reference: Point,
             point: Point, direction: int, adjacent: bool,
             detail: dict) -> ResearchSignal:
        key = (channel, state.stream_id, direction)
        merge = policy.episode_merge
        episode = self._open.get(key)
        magnitude = abs(point.fair_home - reference.fair_home)
        if episode is not None and point.at - episode.last_at <= merge:
            episode.last_at = point.at
            episode.qualifying += 1
            episode.peak = max(episode.peak, magnitude)
            self.counts[channel]["qualifying_extended"] += 1
            return ResearchSignal(channel, policy, state.event_id,
                                  state.stream_id, direction, reference, point,
                                  adjacent, episode.id, False,
                                  episode.reverses, detail)
        other = self._open.get((channel, state.stream_id, -direction))
        reverses = (other.id if other is not None
                    and point.at - other.last_at <= merge else None)
        number = len(self.episodes[channel]) + 1
        episode_id = f"{channel}-{number}"
        signal = ResearchSignal(channel, policy, state.event_id,
                                state.stream_id, direction, reference, point,
                                adjacent, episode_id, True, reverses, detail)
        episode = Episode(episode_id, channel, state.event_id,
                          state.stream_id, direction, signal, point.at,
                          peak=magnitude, reverses=reverses)
        self._open[key] = episode
        self.episodes[channel].append(episode)
        self.counts[channel]["episodes_opened"] += 1
        return signal

    # --- the read-out -------------------------------------------------------

    def summary(self) -> dict:
        """Counts, distributions and reasons: informative with no signal."""
        maxima = sorted(((s.max_excursion, s.stream_id, s.event_id,
                          s.max_excursion_at)
                         for s in self.streams.values()), reverse=True)
        returns = [r for r in self.returns if r["delta"] is not None]
        sizes = [abs(r["delta"]["home"]) for r in returns]
        gaps = [r["gap"]["seconds"] for r in self.returns
                if r["gap"].get("seconds") is not None]
        ages = [r["return"]["age_at_decision_seconds"] for r in returns
                if r["return"]["age_at_decision_seconds"] is not None]
        causes: Counter = Counter()
        for r in self.returns:
            for c in r["gap"]["causes"]:
                causes[c["cause"]] += 1
        return {
            DRIFT: {
                "policy": self.drift.as_dict(),
                "episodes": len(self.episodes[DRIFT]),
                "counts": dict(sorted(self.counts[DRIFT].items())),
                "resets_by_cause": dict(sorted(self.resets.items())),
                "evaluations": self.evaluations,
                "evaluations_with_excursion_at_least": {
                    f"{edge * 100:g}pp": self.bins.get(edge, 0)
                    for edge in BINS},
                "games_with_max_excursion_at_least": {
                    f"{edge * 100:g}pp": sum(1 for m in maxima
                                             if m[0] >= edge)
                    for edge in BINS},
                "largest_excursions": [
                    {"stream_id": s, "event_id": e, "excursion": m,
                     "at": _iso(at)} for m, s, e, at in maxima[:5]
                    if m > 0],
            },
            RETURN: {
                "policy": self.ret.as_dict(),
                "episodes": len(self.episodes[RETURN]),
                "counts": dict(sorted(self.counts[RETURN].items())),
                "returns": len(self.returns),
                "verdicts": dict(sorted(Counter(
                    r["verdict"] for r in self.returns).items())),
                "gap_causes": dict(sorted(causes.items())),
                "gap_seconds": _distribution(gaps),
                "return_age_seconds": _distribution(ages),
                "returns_with_change_at_least": {
                    f"{edge * 100:g}pp": sum(1 for s in sizes if s >= edge)
                    for edge in BINS},
                "eligible_returns_with_change_at_least": {
                    f"{edge * 100:g}pp": sum(
                        1 for r in returns
                        if abs(r["delta"]["home"]) >= edge
                        and not [f for f in r["failed"]
                                 if f != BELOW_THRESHOLD])
                    for edge in BINS},
            },
            "observations": dict(sorted(self.counts["observations"].items())),
        }


def research_coverage(start: datetime | None, at: datetime,
                      horizon: timedelta) -> str:
    """Whether a contract's book was due to be read for a research signal at
    `at`: the monitor's own horizon rule (`now < start <= now + horizon`).
    The live monitor reads by it and the offline report classifies by it,
    so the two cannot disagree about what was due."""
    if start is None:
        return "no_kickoff_time"
    if start <= at:
        return "at_or_after_start"
    if start > at + horizon:
        return "outside_observation_horizon"
    return "in_horizon"


def _distribution(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    median = (ordered[middle] if len(ordered) % 2 else
              (ordered[middle - 1] + ordered[middle]) / 2)
    return {"n": len(ordered), "median": median, "min": ordered[0],
            "max": ordered[-1]}
