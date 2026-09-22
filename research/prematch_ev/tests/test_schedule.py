"""The NFL schedule adapter: parsing, identity, provenance and failure.

THE FIXTURES ARE TRANSCRIBED, NOT FETCHED
-----------------------------------------
`site.api.espn.com` is refused by the egress proxy from the sessions this was
written in (the gateway answered 403 to CONNECT), so nothing here was read off
the wire in-session. `OBSERVED_*` below are copied VERBATIM from two responses
the repository owner fetched on 2026-09-21 and pasted into review on PR #27.

That distinction is load-bearing in this study. Four defects have now come
from a fixture someone composed rather than copied, and the last one reached a
claim made to the owner: `KXNFLGAME-26SEP211300BUFKC` parsed a kickoff
perfectly because the `1300` in it was mine. So these are labelled as
transcribed everywhere they are relied on, exactly as the Kalshi fee schedules
are, and the live acceptance check belongs to whoever can reach the endpoint.

WHAT THE ACCEPTANCE CASES ARE
-----------------------------
The owner named them, and each has a test here:

  * both real examples resolve exactly, both YES contracts, no end-time
    fallback, correct UTC boundary handling
  * missing / TBD kickoff
  * ambiguous matchup
  * unknown team
  * changed kickoff
  * provider failure
  * cached / offline input
  * empty-universe failure

THE BOUNDARY CASE IS THE POINT
------------------------------
Both observed games kick off after midnight UTC on the day AFTER the day in
their ticker. If the adapter compared UTC days it would mis-date every US
evening game by one, and every lead time with it -- silently, because a
plausible kickoff is indistinguishable from a correct one. So the two
`RequiredIdentityTest` cases are not examples, they are the specification.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.matcher import (                                   # noqa: E402
    START_SOURCE_EXTERNAL, parse_event_body_date, parse_event_body_start,
    start_source, start_source_kind, start_source_provenance,
)
from data import espn_schedule as es                         # noqa: E402


# --- TRANSCRIBED from the owner's 2026-09-21 fetch, PR #27 ------------------
# https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=20260913
OBSERVED_DAL_NYG = json.loads("""
{"id":"401872930","date":"2026-09-14T00:20Z",
 "name":"Dallas Cowboys at New York Giants",
 "competitions":[{"date":"2026-09-14T00:20Z","timeValid":true,
   "competitors":[
     {"homeAway":"home","team":{"id":"19","abbreviation":"NYG",
                                "displayName":"New York Giants"}},
     {"homeAway":"away","team":{"id":"6","abbreviation":"DAL",
                                "displayName":"Dallas Cowboys"}}]}]}
""")
# ?dates=20260914
OBSERVED_DEN_KC = json.loads("""
{"id":"401872931","date":"2026-09-15T00:15Z",
 "name":"Denver Broncos at Kansas City Chiefs",
 "competitions":[{"date":"2026-09-15T00:15Z","timeValid":true,
   "competitors":[
     {"homeAway":"home","team":{"id":"12","abbreviation":"KC",
                                "displayName":"Kansas City Chiefs"}},
     {"homeAway":"away","team":{"id":"7","abbreviation":"DEN",
                                "displayName":"Denver Broncos"}}]}]}
""")

AT = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def board(*events) -> dict:
    return {"events": list(events)}


def parse(payload, bucket="20260913", league="NFL"):
    return es.parse_scoreboard(
        payload, league=league,
        source_url=es.SCOREBOARD_URL.format(bucket=bucket),
        retrieved_at=AT, date_bucket=bucket)


def snapshot(*payload_pairs, league="NFL"):
    return es.snapshot_from_payloads(league, list(payload_pairs), retrieved_at=AT)


def mutate(event: dict, **changes) -> dict:
    """A deep copy of an observed event with named fields replaced."""
    out = json.loads(json.dumps(event))
    for key, value in changes.items():
        if key == "competition":
            out["competitions"][0].update(value)
        elif key == "drop_competition_key":
            out["competitions"][0].pop(value, None)
        else:
            out[key] = value
    return out


class RequiredIdentityTest(unittest.TestCase):
    """The owner's two acceptance examples, resolved exactly.

    KXNFLGAME-26SEP13DALNYG -> ESPN 401872930, kickoff 2026-09-14T00:20:00Z
    KXNFLGAME-26SEP14DENKC  -> ESPN 401872931, kickoff 2026-09-15T00:15:00Z
    """

    def setUp(self):
        self.snap = snapshot(("20260913", board(OBSERVED_DAL_NYG)),
                             ("20260914", board(OBSERVED_DEN_KC)))

    def test_dal_nyg_resolves_to_the_observed_event_and_kickoff(self):
        resolution = self.snap.resolve(
            frozenset({"DAL", "NYG"}),
            parse_event_body_date("KXNFLGAME-26SEP13DALNYG"))
        self.assertTrue(resolution.resolved, resolution.detail)
        self.assertEqual(resolution.event.provider_event_id, "401872930")
        self.assertEqual(resolution.kickoff,
                         datetime(2026, 9, 14, 0, 20, tzinfo=timezone.utc))

    def test_den_kc_resolves_to_the_observed_event_and_kickoff(self):
        resolution = self.snap.resolve(
            frozenset({"DEN", "KC"}),
            parse_event_body_date("KXNFLGAME-26SEP14DENKC"))
        self.assertTrue(resolution.resolved, resolution.detail)
        self.assertEqual(resolution.event.provider_event_id, "401872931")
        self.assertEqual(resolution.kickoff,
                         datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc))

    def test_both_yes_contracts_of_a_game_resolve_to_one_kickoff(self):
        """The matchup comes from the YES suffixes, so orientation is free.

        Kalshi lists one contract per team. Both must date to the same game;
        if they did not, the two sides of one market would be measured at
        different lead times.
        """
        day = parse_event_body_date("KXNFLGAME-26SEP14DENKC")
        for yes_pair in (("DEN", "KC"), ("KC", "DEN")):
            with self.subTest(yes=yes_pair):
                resolution = self.snap.resolve(frozenset(yes_pair), day)
                self.assertEqual(resolution.kickoff,
                                 datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc))

    def test_yes_orientation_is_preserved_by_home_and_away(self):
        """Which side is home survives the parse -- the study needs the side."""
        event = self.snap.events["401872931"]
        self.assertEqual(event.home, "KC")
        self.assertEqual(event.away, "DEN")
        self.assertEqual(event.matchup, frozenset({"DEN", "KC"}))

    def test_ticker_day_is_not_the_utc_kickoff_day(self):
        """The trap, pinned in both directions.

        Both games kick off after midnight UTC on the day after their ticker
        day. Comparing UTC days would shift every US evening game by one.
        """
        for ticker, espn_id, utc_day, ticker_day in (
            ("KXNFLGAME-26SEP13DALNYG", "401872930", 14, date(2026, 9, 13)),
            ("KXNFLGAME-26SEP14DENKC", "401872931", 15, date(2026, 9, 14)),
        ):
            with self.subTest(ticker=ticker):
                event = self.snap.events[espn_id]
                self.assertEqual(event.kickoff.date().day, utc_day)
                self.assertEqual(parse_event_body_date(ticker), ticker_day)
                # the LOCAL day is what the ticker names, and it differs
                self.assertEqual(event.local_day, ticker_day)
                self.assertNotEqual(event.kickoff.date(), ticker_day)

    def test_resolution_offset_is_zero_for_both(self):
        """A non-zero population would mean the timezone assumption is wrong."""
        for matchup, ticker in ((("DAL", "NYG"), "KXNFLGAME-26SEP13DALNYG"),
                                (("DEN", "KC"), "KXNFLGAME-26SEP14DENKC")):
            self.snap.resolve(frozenset(matchup), parse_event_body_date(ticker))
        self.assertEqual(self.snap.offsets_used, {0: 2})

    def test_no_end_of_contract_field_can_supply_a_kickoff(self):
        """close_time / expected_expiration_time / settlement_ts stay closed.

        On the sampled KC contract all three are on the day AFTER kickoff.
        The adapter reads none of them, and the source file must not mention
        them as inputs.
        """
        source = Path(es.__file__).read_text()
        for field in ("close_time", "expected_expiration_time", "settlement_ts"):
            # named in prose (to say they are refused) but never READ
            self.assertNotIn(f'.get("{field}")', source)
            self.assertNotIn(f"['{field}']", source)


class StartSourceRegistrationTest(unittest.TestCase):
    """NFL is schedule-ready through an EXTERNAL source, and labelled as such."""

    def test_nfl_start_source_is_external(self):
        self.assertEqual(start_source("NFL"), START_SOURCE_EXTERNAL)
        self.assertEqual(start_source_kind("NFL"), "external")
        self.assertEqual(start_source_provenance("NFL"), "unverified")

    def test_mlb_keeps_its_own_ticker_source(self):
        """The regression that matters: MLB must not be moved onto a schedule."""
        self.assertEqual(start_source("MLB"), "event_ticker")
        self.assertEqual(start_source_provenance("MLB"),
                         "verified_at_decision_time")
        self.assertEqual(
            parse_event_body_start("KXMLBGAME-26SEP152140MIAAZ").utcoffset(),
            timedelta(hours=-4))

    def test_nfl_ticker_still_yields_no_kickoff_on_its_own(self):
        """The reason the adapter exists, kept pinned.

        If a future change ever made an NFL body appear to carry a time, it
        would be a fabricated one.
        """
        self.assertIsNone(parse_event_body_start("KXNFLGAME-26SEP14DENKC"))
        self.assertIsNone(parse_event_body_start("KXNFLGAME-26SEP13DALNYG"))

    def test_date_only_parser_does_not_swallow_an_mlb_body(self):
        """`26SEP152140MIAAZ` must not read as a date plus a team tail."""
        self.assertEqual(parse_event_body_date("KXMLBGAME-26SEP152140MIAAZ"),
                         date(2026, 9, 15))
        self.assertEqual(parse_event_body_start("KXMLBGAME-26SEP152140MIAAZ").hour,
                         21)

    def test_unparseable_body_yields_no_day(self):
        for ticker in ("KXNFLGAME-NOTADATE", "KXNFLGAME", "", "-"):
            with self.subTest(ticker=ticker):
                self.assertIsNone(parse_event_body_date(ticker))


class SchemaTest(unittest.TestCase):
    """The endpoint is undocumented and can change shape without notice.

    Every structural expectation gets a named failure, because the dangerous
    outcome is not a crash -- it is a plausible kickoff derived from a payload
    that no longer means what it used to.
    """

    def assertRejects(self, payload, reason):
        events, failures = parse(payload)
        self.assertEqual(events, [], f"expected no event, got {events}")
        self.assertEqual([f.reason for f in failures], [reason],
                         f"failures were {[str(f) for f in failures]}")

    def test_missing_kickoff(self):
        self.assertRejects(
            board(mutate(OBSERVED_DEN_KC, date=None)),
            es.FAIL_KICKOFF_MISSING)

    def test_unparseable_kickoff(self):
        self.assertRejects(
            board(mutate(OBSERVED_DEN_KC, date="next Sunday")),
            es.FAIL_KICKOFF_UNPARSEABLE)

    def test_naive_kickoff_is_refused_not_assumed_utc(self):
        """A timestamp with no offset is not an instant.

        Assuming UTC on an 8pm Eastern kickoff lands it on the wrong day,
        which is precisely the error the module exists to prevent.
        """
        naive = mutate(OBSERVED_DEN_KC, date="2026-09-15T00:15:00")
        naive["competitions"][0]["date"] = "2026-09-15T00:15:00"
        self.assertRejects(board(naive), es.FAIL_KICKOFF_NAIVE)

    def test_tbd_kickoff(self):
        self.assertRejects(
            board(mutate(OBSERVED_DEN_KC, competition={"timeValid": False})),
            es.FAIL_TIME_TBD)

    def test_missing_time_valid_is_a_rejection_not_an_assumption(self):
        """Absent `timeValid` must not be read as "valid".

        It is the only field separating a scheduled kickoff from a placeholder,
        so assuming it would let a placeholder into the study looking exactly
        like a real game.
        """
        self.assertRejects(
            board(mutate(OBSERVED_DEN_KC, drop_competition_key="timeValid")),
            es.FAIL_TIME_VALID_MISSING)

    def test_event_and_competition_dates_must_agree(self):
        self.assertRejects(
            board(mutate(OBSERVED_DEN_KC,
                         competition={"date": "2026-09-15T18:00Z"})),
            es.FAIL_DATE_MISMATCH)

    def test_competition_count(self):
        doubled = json.loads(json.dumps(OBSERVED_DEN_KC))
        doubled["competitions"].append(doubled["competitions"][0])
        self.assertRejects(board(doubled), es.FAIL_COMPETITION_COUNT)

    def test_competitor_count(self):
        short = json.loads(json.dumps(OBSERVED_DEN_KC))
        short["competitions"][0]["competitors"].pop()
        self.assertRejects(board(short), es.FAIL_COMPETITOR_COUNT)

    def test_two_home_sides_is_invalid(self):
        both = json.loads(json.dumps(OBSERVED_DEN_KC))
        both["competitions"][0]["competitors"][1]["homeAway"] = "home"
        self.assertRejects(board(both), es.FAIL_HOMEAWAY)

    def test_missing_event_id(self):
        self.assertRejects(board(mutate(OBSERVED_DEN_KC, id="")),
                           es.FAIL_MISSING_ID)

    def test_top_level_shape_change(self):
        for payload in ({"events": "nope"}, {"nogames": []}, [], "text", None):
            with self.subTest(payload=payload):
                events, failures = parse(payload)
                self.assertEqual(events, [])
                self.assertEqual([f.reason for f in failures], [es.FAIL_PAYLOAD])

    def test_unknown_team(self):
        unknown = json.loads(json.dumps(OBSERVED_DEN_KC))
        unknown["competitions"][0]["competitors"][0]["team"] = {
            "id": "99", "abbreviation": "ZZZ", "displayName": "Atlantis Krakens"}
        events, failures = parse(board(unknown))
        self.assertEqual(events, [])
        self.assertEqual([f.reason for f in failures], [es.FAIL_TEAM_UNRESOLVED])
        self.assertIn("ZZZ", failures[0].detail)

    def test_both_sides_same_team(self):
        same = json.loads(json.dumps(OBSERVED_DEN_KC))
        same["competitions"][0]["competitors"][1]["team"] = \
            json.loads(json.dumps(same["competitions"][0]["competitors"][0]["team"]))
        same["competitions"][0]["competitors"][1]["homeAway"] = "away"
        self.assertRejects(board(same), es.FAIL_TEAM_DUPLICATE)

    def test_postponement_is_named_not_inferred(self):
        off = json.loads(json.dumps(OBSERVED_DEN_KC))
        off["competitions"][0]["status"] = {"type": {"name": "STATUS_POSTPONED"}}
        self.assertRejects(board(off), es.FAIL_NOT_PLAYED)

    def test_a_scheduled_status_block_is_accepted(self):
        on = json.loads(json.dumps(OBSERVED_DEN_KC))
        on["competitions"][0]["status"] = {"type": {"name": "STATUS_FINAL",
                                                    "state": "post"}}
        events, failures = parse(board(on))
        self.assertEqual(failures, [])
        self.assertEqual(len(events), 1)

    def test_absent_status_block_yields_no_finding(self):
        """The observed projection has none, so absence must not be evidence."""
        events, failures = parse(board(OBSERVED_DEN_KC))
        self.assertEqual(failures, [])
        self.assertEqual(len(events), 1)

    def test_a_divergent_abbreviation_resolves_by_name_and_says_so(self):
        """ESPN's vocabulary need not be ours; the path taken is recorded."""
        divergent = json.loads(json.dumps(OBSERVED_DEN_KC))
        divergent["competitions"][0]["competitors"][0]["team"]["abbreviation"] = "KAN"
        events, failures = parse(board(divergent))
        self.assertEqual(failures, [])
        self.assertEqual(events[0].home, "KC")
        self.assertTrue(events[0].home_resolved_by.startswith("name_fuzzy:"))
        self.assertTrue(events[0].resolved_by_name)

    def test_one_bad_game_does_not_discard_the_good_ones(self):
        events, failures = parse(
            board(OBSERVED_DAL_NYG,
                  mutate(OBSERVED_DEN_KC, competition={"timeValid": False})))
        self.assertEqual([e.provider_event_id for e in events], ["401872930"])
        self.assertEqual([f.reason for f in failures], [es.FAIL_TIME_TBD])


class ScoresAreNotCarriedTest(unittest.TestCase):
    """The scoreboard serves results. The study must never see one.

    Excluding scores is not a matter of not looking at them downstream: if the
    adapter carried them, a feature or a checkpoint could read one, and the
    study would be conditioning on the outcome it is trying to predict.
    """

    def test_a_payload_with_scores_yields_an_event_with_none(self):
        scored = json.loads(json.dumps(OBSERVED_DEN_KC))
        for competitor in scored["competitions"][0]["competitors"]:
            competitor["score"] = "27"
            competitor["winner"] = True
        events, failures = parse(board(scored))
        self.assertEqual(failures, [])
        event = events[0]
        provenance = event.provenance()
        # Key-wise, not substring-wise: the endpoint is CALLED "scoreboard",
        # so its own url contains the word and a naive substring check passes
        # for the wrong reason. What matters is that no FIELD carries a
        # result and no VALUE is one.
        for key in provenance:
            self.assertNotIn("score", key.lower())
            self.assertNotIn("winner", key.lower())
        self.assertNotIn("27", [str(v) for v in provenance.values()])
        self.assertFalse(
            [f for f in es.ScheduleEvent.__dataclass_fields__
             if "score" in f or "winner" in f])

    def test_the_event_record_has_no_score_shaped_field(self):
        fields = set(es.ScheduleEvent.__dataclass_fields__)
        for banned in ("score", "scores", "winner", "result", "completed"):
            self.assertNotIn(banned, fields)


class SnapshotTest(unittest.TestCase):
    """Deduplication, conflict, ambiguity and the no-match reason."""

    def test_same_game_in_two_buckets_deduplicates(self):
        snap = snapshot(("20260913", board(OBSERVED_DEN_KC)),
                        ("20260914", board(OBSERVED_DEN_KC)))
        self.assertEqual(len(snap.events), 1)
        self.assertEqual(snap.failures, [])

    def test_a_changed_kickoff_between_buckets_is_a_conflict_not_a_pick(self):
        """The provider changing its answer mid-retrieval drops the game.

        Taking either kickoff would be choosing which lead times to be wrong
        about, with nothing in the data to justify the choice.
        """
        moved = mutate(OBSERVED_DEN_KC, date="2026-09-15T17:00Z",
                       competition={"date": "2026-09-15T17:00Z"})
        snap = snapshot(("20260913", board(OBSERVED_DEN_KC)),
                        ("20260914", board(moved)))
        self.assertEqual(snap.events, {})
        self.assertEqual([f.reason for f in snap.failures],
                         [es.FAIL_EVENT_CONFLICT])
        resolution = snap.resolve(frozenset({"DEN", "KC"}), date(2026, 9, 14))
        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.reason, es.FAIL_NO_MATCH)

    def test_ambiguous_matchup_rejects_rather_than_picking(self):
        """Two entries for one matchup near one day cannot be separated."""
        twin = mutate(OBSERVED_DEN_KC, id="401899999",
                      date="2026-09-15T20:00Z",
                      competition={"date": "2026-09-15T20:00Z"})
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC, twin)))
        resolution = snap.resolve(frozenset({"DEN", "KC"}), date(2026, 9, 14))
        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.reason, es.FAIL_AMBIGUOUS)
        self.assertIn("401899999", resolution.detail)
        self.assertIn("401872931", resolution.detail)

    def test_no_match_names_what_was_looked_for(self):
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        resolution = snap.resolve(frozenset({"BUF", "NYJ"}), date(2026, 9, 14))
        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.reason, es.FAIL_NO_MATCH)
        self.assertIn("BUF", resolution.detail)

    def test_a_game_a_week_away_is_not_a_match(self):
        """Identity alone is not enough: the same pair plays again later."""
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        resolution = snap.resolve(frozenset({"DEN", "KC"}), date(2026, 9, 21))
        self.assertFalse(resolution.resolved)
        self.assertEqual(resolution.reason, es.FAIL_NO_MATCH)

    def test_unreadable_ticker_day_is_its_own_reason(self):
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        resolution = snap.resolve(frozenset({"DEN", "KC"}), None)
        self.assertEqual(resolution.reason, es.FAIL_NO_TICKER_DAY)

    def test_manifest_carries_the_full_provenance(self):
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        snap.resolve(frozenset({"DEN", "KC"}), date(2026, 9, 14))
        manifest = snap.manifest()
        self.assertEqual(manifest["historical_schedule_as_of"], "unverified")
        entry = manifest["events"]["401872931"]
        self.assertIn("dates=20260914", entry["source_url"])
        self.assertEqual(entry["provider_event_id"], "401872931")
        self.assertEqual(entry["retrieved_at"], AT.isoformat())
        self.assertEqual(len(entry["payload_sha256"]), 64)
        self.assertEqual(entry["kickoff"], "2026-09-15T00:15:00+00:00")
        self.assertEqual(entry["historical_schedule_as_of"], "unverified")
        self.assertTrue(json.dumps(manifest))          # serialisable

    def test_payload_hash_is_stable_and_key_order_independent(self):
        a = es.payload_hash({"events": [OBSERVED_DEN_KC], "week": 2})
        b = es.payload_hash({"week": 2, "events": [OBSERVED_DEN_KC]})
        self.assertEqual(a, b)
        self.assertNotEqual(a, es.payload_hash({"events": [OBSERVED_DAL_NYG]}))


class BucketTest(unittest.TestCase):
    """Daily buckets with a buffer, not an undocumented range query."""

    def test_buckets_span_the_window_with_a_buffer(self):
        buckets = es.date_buckets(date(2026, 9, 14), date(2026, 9, 15))
        self.assertEqual(buckets, ["20260913", "20260914", "20260915", "20260916"])

    def test_a_reversed_window_is_normalised(self):
        self.assertEqual(es.date_buckets(date(2026, 9, 15), date(2026, 9, 14)),
                         es.date_buckets(date(2026, 9, 14), date(2026, 9, 15)))

    def test_buffer_is_configurable_and_can_be_zero(self):
        self.assertEqual(es.date_buckets(date(2026, 9, 14), date(2026, 9, 14), 0),
                         ["20260914"])


class TransportTest(unittest.TestCase):
    """Injectable transport, cached replay, and a failure that is not empty."""

    def test_fetch_uses_the_injected_transport_and_builds_a_snapshot(self):
        seen: list[str] = []

        def transport(url):
            seen.append(url)
            if "20260913" in url:
                return board(OBSERVED_DAL_NYG)
            if "20260914" in url:
                return board(OBSERVED_DEN_KC)
            return board()

        snap = es.fetch_schedule("NFL", date(2026, 9, 13), date(2026, 9, 14),
                                 transport=transport, retrieved_at=AT)
        self.assertEqual(len(seen), 4)                 # 2 days + 1 buffer each
        self.assertEqual(sorted(snap.events), ["401872930", "401872931"])
        self.assertTrue(all(u.startswith("https://site.api.espn.com") for u in seen))

    def test_provider_failure_is_a_named_loss_not_an_empty_slate(self):
        """Rule 17, one layer out: a failed read is not a zero.

        An unreachable provider and a day with no games both yield no events.
        Only one of them means there were none, and a study that could not
        tell them apart would report "no games" for an outage.
        """
        def transport(url):
            if "20260914" in url:
                raise es.ScheduleFetchError("boom")
            return board(OBSERVED_DAL_NYG)

        snap = es.fetch_schedule("NFL", date(2026, 9, 13), date(2026, 9, 14),
                                 transport=transport, retrieved_at=AT)
        self.assertEqual([f.reason for f in snap.failures], [es.FAIL_FETCH])
        self.assertIn("20260914", snap.failures[0].detail)
        resolution = snap.resolve(frozenset({"DEN", "KC"}), date(2026, 9, 14))
        self.assertFalse(resolution.resolved)

    def test_raw_payloads_are_cached_and_replayed_without_the_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = {"n": 0}

            def transport(url):
                calls["n"] += 1
                return board(OBSERVED_DEN_KC) if "20260914" in url else board()

            first = es.fetch_schedule("NFL", date(2026, 9, 14), date(2026, 9, 14),
                                      transport=transport, retrieved_at=AT,
                                      cache_dir=tmp, buffer_days=0)
            self.assertEqual(calls["n"], 1)
            self.assertEqual(list(first.events), ["401872931"])

            def refuse(url):
                raise AssertionError("cache miss: the network was touched")

            second = es.fetch_schedule("NFL", date(2026, 9, 14), date(2026, 9, 14),
                                       transport=refuse, retrieved_at=AT,
                                       cache_dir=tmp, buffer_days=0)
            self.assertEqual(list(second.events), ["401872931"])
            self.assertEqual(second.events["401872931"].kickoff,
                             first.events["401872931"].kickoff)

    def test_offline_directory_import_matches_a_live_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260914.json").write_text(
                json.dumps(board(OBSERVED_DEN_KC)))
            snap = es.snapshot_from_directory("NFL", tmp, retrieved_at=AT)
            self.assertEqual(list(snap.events), ["401872931"])
            self.assertEqual(snap.events["401872931"].kickoff,
                             datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc))

    def test_a_corrupt_cached_bucket_is_a_miss_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260914.json").write_text("{not json")
            snap = es.fetch_schedule(
                "NFL", date(2026, 9, 14), date(2026, 9, 14),
                transport=lambda url: board(OBSERVED_DEN_KC),
                retrieved_at=AT, cache_dir=tmp, buffer_days=0)
            self.assertEqual(list(snap.events), ["401872931"])
            self.assertIn(es.FAIL_PAYLOAD, snap.failure_counts())

    def test_a_corrupt_offline_file_is_named_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "20260914.json").write_text("{not json")
            snap = es.snapshot_from_directory("NFL", tmp, retrieved_at=AT)
            self.assertEqual(snap.events, {})
            self.assertEqual([f.reason for f in snap.failures], [es.FAIL_PAYLOAD])


class ProvenanceLabellingTest(unittest.TestCase):
    """The limitation has to travel with the data, not sit in a docstring."""

    def test_snapshot_provenance_is_unverified(self):
        self.assertEqual(es.SCHEDULE_PROVENANCE, "unverified")

    def test_every_event_carries_the_label(self):
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        for event in snap.events.values():
            self.assertEqual(event.provenance()["historical_schedule_as_of"],
                             "unverified")

    def test_manifest_states_the_scope_limit_in_words(self):
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        note = snap.manifest()["provenance_note"].lower()
        self.assertIn("cannot certify", note)
        self.assertIn("point-in-time", note)

    def test_free_command_hint_names_no_credential_and_no_paid_flag(self):
        hint = es.free_command_hint(date(2026, 9, 1), date(2026, 9, 15))
        self.assertIn("--preflight", hint)
        self.assertIn("--sport NFL", hint)
        for banned in ("--api-key", "ODDS_API_KEY", "--collect"):
            self.assertNotIn(banned, hint)


# --------------------------------------------------------------------------
# Integration: the REAL survey(), end to end, on the observed games.
#
# The unit tests above drive the adapter. These drive the thing that USES it,
# because both of the P1s in the last round lived in the caller rather than in
# the piece being called -- `survey()` never applied its own loss ledger, and
# the start-resolution gap only showed up when a real run was assembled. A
# test that stops at the adapter would have passed through both.
# --------------------------------------------------------------------------

def nfl_market(event: str, yes: str, kickoff: datetime) -> dict:
    """One NFL contract, shaped like a settled Kalshi market.

    `close_time` and `settlement_ts` are deliberately AFTER the kickoff, on
    the following UTC day, exactly as the sampled KC contract's were. If any
    code path ever reaches for one of them as a start, these fixtures make the
    resulting lead times visibly wrong rather than plausibly wrong.
    """
    end = kickoff + timedelta(hours=3, minutes=15)
    return {
        "ticker": f"{event}-{yes}",
        "event_ticker": event,
        "result": "yes",
        "open_time": "2026-05-15T17:04:00Z",
        "close_time": end.isoformat().replace("+00:00", "Z"),
        "settlement_ts": (end + timedelta(minutes=6)).isoformat().replace("+00:00", "Z"),
    }


OBSERVED_MARKETS = {
    m["ticker"]: m for m in (
        nfl_market("KXNFLGAME-26SEP13DALNYG", "DAL",
                   datetime(2026, 9, 14, 0, 20, tzinfo=timezone.utc)),
        nfl_market("KXNFLGAME-26SEP13DALNYG", "NYG",
                   datetime(2026, 9, 14, 0, 20, tzinfo=timezone.utc)),
        nfl_market("KXNFLGAME-26SEP14DENKC", "DEN",
                   datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc)),
        nfl_market("KXNFLGAME-26SEP14DENKC", "KC",
                   datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc)),
    )
}


class NflSurveyIntegrationTest(unittest.TestCase):
    """The free half of a real NFL run, with the schedule injected."""

    def _survey(self, markets, schedule, start="2026-09-13", end="2026-09-15",
                enumeration=None):
        import run_study
        from data.kalshi_history import Coverage
        args = run_study.parse_args([
            "--sport", "NFL", "--series", "KXNFLGAME",
            "--from", start, "--to", end, "--cache-dir", "",
            "--lead-grid", "72h,48h,24h,12h,6h,3h", "--no-schedule-fetch"])
        with unittest.mock.patch.object(
                run_study, "fetch_historical_cutoff",
                return_value=(datetime(2020, 1, 1, tzinfo=timezone.utc), Coverage())), \
            unittest.mock.patch.object(
                run_study, "enumerate_settled_markets",
                return_value=(markets, enumeration or Coverage())):
            return run_study.survey(args, schedule=schedule)

    def test_a_full_nfl_survey_resolves_both_games_and_derives_cutoffs(self):
        snap = snapshot(("20260913", board(OBSERVED_DAL_NYG)),
                        ("20260914", board(OBSERVED_DEN_KC)))
        result = self._survey(OBSERVED_MARKETS, snap)

        self.assertEqual(len(result.markets), 4, "both contracts of both games")
        self.assertTrue(result.coverage.complete, str(result.coverage))
        self.assertEqual(result.resolver.coverage_counts(), (2, 0))
        # 4 contracts x 7 checkpoints -- the six requested PLUS the 60-minute
        # baseline the grid always retains -- all enumerated before any fetch
        self.assertEqual(len(result.grid), 7)
        self.assertEqual(len(result.matrix.statuses), 28)
        self.assertTrue(result.cutoffs)

    def test_the_cutoffs_count_back_from_the_kickoff_not_the_settlement(self):
        """The whole study is measured from this instant, so pin it exactly.

        A 3-hour checkpoint on DEN/KC is 2026-09-14T21:15Z. Counted back from
        `close_time` instead it would be 2026-09-15T00:30Z -- a plausible
        timestamp, three and a quarter hours wrong, on every single row.
        """
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        result = self._survey(
            {t: m for t, m in OBSERVED_MARKETS.items() if "DENKC" in t}, snap,
            start="2026-09-14", end="2026-09-15")
        kickoff = datetime(2026, 9, 15, 0, 15, tzinfo=timezone.utc)
        for hours in (3, 6, 12, 24, 48, 72):
            self.assertIn(kickoff - timedelta(hours=hours), result.cutoffs)

    def test_an_unresolvable_schedule_fails_coverage_by_name(self):
        """EMPTY UNIVERSE. A zero-cost run over nothing is not a success.

        The schedule is reachable and simply does not carry these games. Every
        contract then fails resolution, the universe is empty, and the run has
        to say so -- the exact shape that previously printed `coverage
        complete / cost 0 / exit 0` beside a ledger reading 100% lost.
        """
        empty = snapshot(("20260913", board()), ("20260914", board()))
        result = self._survey(OBSERVED_MARKETS, empty)

        self.assertEqual(result.markets, {})
        self.assertEqual(result.cutoffs, [])
        self.assertFalse(result.coverage.complete)
        self.assertIn("failure that happens to be cheap", str(result.coverage))
        self.assertIn(es.FAIL_NO_MATCH, result.ledger.rejections)
        self.assertEqual(result.resolver.coverage_counts(), (0, 2))

    def test_no_schedule_at_all_is_a_named_failure_not_an_empty_study(self):
        """Rule 17 at the run level: no snapshot is not "no games"."""
        result = self._survey(OBSERVED_MARKETS, None)
        self.assertEqual(result.markets, {})
        self.assertFalse(result.coverage.complete)
        self.assertIn(es.FAIL_NO_SNAPSHOT, result.ledger.rejections)

    def test_a_partial_schedule_keeps_the_game_it_covers_and_names_the_other(self):
        """Half a schedule is half a study, and the missing half is reported."""
        half = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        result = self._survey(OBSERVED_MARKETS, half)
        self.assertEqual(len(result.markets), 2)
        self.assertTrue(all("DENKC" in t for t in result.markets))
        # ONE unresolved event, but TWO lost contracts -- and the ledger's
        # `contracts` stage counts contracts, so that is what is filed.
        self.assertEqual(result.resolver.coverage_counts(), (1, 1))
        self.assertEqual(result.resolver.failure_counts()[es.FAIL_NO_MATCH], 1)
        self.assertEqual(result.ledger.rejections.get(es.FAIL_NO_MATCH), 2)

    def test_the_preflight_reports_coverage_provenance_and_costs_nothing(self):
        import run_study
        snap = snapshot(("20260913", board(OBSERVED_DAL_NYG)),
                        ("20260914", board(OBSERVED_DEN_KC)))
        with unittest.mock.patch.object(
                run_study, "survey",
                return_value=self._survey(OBSERVED_MARKETS, snap)):
            with unittest.mock.patch("builtins.print") as printed:
                code = run_study.main([
                    "--preflight", "--sport", "NFL", "--series", "KXNFLGAME",
                    "--from", "2026-09-13", "--to", "2026-09-15",
                    "--api-key", "K"])
        text = " ".join(str(c) for c in printed.call_args_list)
        self.assertEqual(code, 0)
        self.assertIn("schedule coverage", text)
        self.assertIn("2/2 events resolved", text)
        self.assertIn("UNVERIFIED", text)
        self.assertIn("EXPLORATORY", text)
        self.assertIn("no paid requests made", text)

    def test_the_resolver_manifest_links_a_contract_to_its_schedule_entry(self):
        """Provenance has to be JOINABLE, not just present.

        Observations carry the Kalshi event ticker; the schedule block is
        keyed by ESPN event id. Without the map between them the two halves
        cannot be reconciled after the fact, which is the point of recording
        either.
        """
        snap = snapshot(("20260914", board(OBSERVED_DEN_KC)))
        result = self._survey(
            {t: m for t, m in OBSERVED_MARKETS.items() if "DENKC" in t}, snap,
            start="2026-09-14", end="2026-09-15")
        manifest = result.resolver.manifest()

        self.assertEqual(manifest["start_source"], "external_schedule")
        self.assertEqual(manifest["historical_schedule_as_of"], "unverified")
        link = manifest["resolutions"]["KXNFLGAME-26SEP14DENKC"]
        self.assertEqual(link["provider_event_id"], "401872931")
        self.assertEqual(link["kickoff"], "2026-09-15T00:15:00+00:00")
        entry = manifest["schedule"]["events"][link["provider_event_id"]]
        self.assertIn("dates=20260914", entry["source_url"])
        self.assertEqual(len(entry["payload_sha256"]), 64)
        self.assertTrue(json.dumps(manifest))

    def test_the_utc_window_boundary_is_reported_not_silent(self):
        """--from/--to are UTC, and a US evening game is on the next UTC day.

        `--to 2026-09-14` drops the Monday night game whose ticker says
        `26SEP14`, because its kickoff is 00:15Z on the 15th. That is the
        EXISTING window semantics and is deliberately left alone -- changing
        it would move the MLB baseline's universe without saying so -- but a
        boundary that quietly thins the final day's slate is exactly the kind
        of loss this study counts rather than discovers later.
        """
        snap = snapshot(("20260913", board(OBSERVED_DAL_NYG)),
                        ("20260914", board(OBSERVED_DEN_KC)))
        result = self._survey(OBSERVED_MARKETS, snap,
                              start="2026-09-13", end="2026-09-14")

        # DAL/NYG (00:20Z on the 14th) is in; DEN/KC (00:15Z on the 15th) is not
        self.assertEqual(len(result.markets), 2)
        self.assertTrue(all("DALNYG" in t for t in result.markets))
        self.assertEqual(
            result.ledger.eligibility_exclusions.get(
                "game_outside_window_utc_boundary"), 2,
            "a contract whose NAMED day is inside the window must be counted")

    def test_widening_the_window_by_one_day_recovers_that_slate(self):
        """The counted loss is actionable: the message names the remedy."""
        snap = snapshot(("20260913", board(OBSERVED_DAL_NYG)),
                        ("20260914", board(OBSERVED_DEN_KC)))
        narrow = self._survey(OBSERVED_MARKETS, snap,
                              start="2026-09-13", end="2026-09-14")
        wide = self._survey(OBSERVED_MARKETS, snap,
                            start="2026-09-13", end="2026-09-15")
        self.assertEqual(len(narrow.markets), 2)
        self.assertEqual(len(wide.markets), 4)
        self.assertNotIn("game_outside_window_utc_boundary",
                         wide.ledger.eligibility_exclusions)

    def test_an_unreachable_exchange_does_not_borrow_another_stage_s_count(self):
        """The empty-universe sentence must count CONTRACTS.

        With the exchange unreachable, zero contracts are ever enumerated and
        none can be rejected -- yet the message fired anyway, quoting
        `total_rejected`, which had summed the failed SCHEDULE buckets. It
        read "no contract survived to be studied: 17 were rejected" on a run
        where no contract had been seen at all: a second, wrong explanation
        printed beside the real one.
        """
        from collect import Ledger
        ledger = Ledger()
        ledger.reject("schedule_fetch_failed", count=17,
                      stage="schedule_entries", diagnostic=True)
        self.assertEqual(ledger.total_rejected, 17)
        self.assertEqual(ledger.rejected_in_stage("contracts"), 0)

        from data.kalshi_history import Coverage
        unreachable = Coverage().fail("/markets page 0: 403 Forbidden")
        result = self._survey({}, None, enumeration=unreachable)
        self.assertEqual(result.markets, {})
        self.assertFalse(result.coverage.complete)
        self.assertIn("403 Forbidden", str(result.coverage))
        self.assertNotIn("no contract survived to be studied",
                         str(result.coverage),
                         "no contract was ever seen, so none was rejected")

    def test_a_real_contract_loss_still_says_so(self):
        """The other half: when contracts ARE lost, the message must fire."""
        empty = snapshot(("20260913", board()), ("20260914", board()))
        result = self._survey(OBSERVED_MARKETS, empty)
        self.assertIn("no contract survived to be studied", str(result.coverage))
        self.assertIn("4 were rejected", str(result.coverage))

    def test_mlb_is_untouched_by_any_of_this(self):
        """The regression that would matter most: MLB must not need a schedule."""
        from collect import StartResolver
        from tests.test_collect import milbal
        resolver = StartResolver(league="MLB")
        self.assertFalse(resolver.needs_schedule)
        resolver.prime({})
        for market in milbal().values():
            self.assertIsNotNone(resolver.start(market))


if __name__ == "__main__":
    unittest.main(verbosity=2)
