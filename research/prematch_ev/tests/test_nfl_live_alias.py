"""Regression for the real JAC/JAX mismatch observed 2026-09-21.
ESPN scoreboard dates=20260913; Kalshi KXNFLGAME-26SEP13CLEJAC-JAC.
Fixture is a field projection of the locally fetched public ESPN response.
"""
import json
import unittest
from datetime import datetime, timezone
from collect import StartResolver
from data.espn_schedule import ScheduleSnapshot, parse_scoreboard

PAYLOAD = json.loads('{"events": [{"id": "401872922", "date": "2026-09-13T17:00Z", "competitions": [{"date": "2026-09-13T17:00Z", "timeValid": true, "competitors": [{"homeAway": "home", "team": {"id": "30", "abbreviation": "JAX", "displayName": "Jacksonville Jaguars"}}, {"homeAway": "away", "team": {"id": "5", "abbreviation": "CLE", "displayName": "Cleveland Browns"}}]}]}]}')

class JacksonvilleScheduleTest(unittest.TestCase):
    def test_both_real_contracts_resolve_to_the_same_kickoff(self):
        events, failures = parse_scoreboard(
            PAYLOAD, league="NFL",
            source_url="https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=20260913",
            retrieved_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            date_bucket="20260913")
        self.assertEqual(failures, [])
        snapshot = ScheduleSnapshot(league="NFL")
        for event in events:
            snapshot.add(event)
        markets = {"KXNFLGAME-26SEP13CLEJAC-" + side: {
            "ticker": "KXNFLGAME-26SEP13CLEJAC-" + side,
            "event_ticker": "KXNFLGAME-26SEP13CLEJAC"}
            for side in ("CLE", "JAC")}
        resolver = StartResolver(league="NFL", schedule=snapshot)
        resolver.prime(markets)
        for market in markets.values():
            result = resolver.resolution(market)
            self.assertTrue(result.resolved, result)
            self.assertEqual(result.event.provider_event_id, "401872922")
            self.assertEqual(result.kickoff, datetime(2026, 9, 13, 17, tzinfo=timezone.utc))
        self.assertEqual(resolver.failure_counts(per="contracts"), {})
