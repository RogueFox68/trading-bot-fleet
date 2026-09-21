"""Entity-resolution regressions.

The load-bearing assertions are the REJECTIONS. A matcher that resolves a
little more is not better -- every extra resolution it makes on thin evidence
is a trade on the wrong game with a confident fair probability attached.

These assert only that an input does or does not resolve, never WHICH reject
reason fired, because the reason differs between the rapidfuzz and difflib
backends while the safety property does not.
"""

import unittest
from datetime import datetime, timedelta

from core.matcher import (
    ROSTERS, canonical_event_id, match_event, parse_kalshi_game_ticker,
    resolve_team,
)

START = datetime(2026, 11, 17, 18, 0)


class ResolutionTest(unittest.TestCase):
    def test_full_names_resolve(self):
        for league, name, expected in (
            ("NFL", "Buffalo Bills", "BUF"), ("NFL", "Kansas City Chiefs", "KC"),
            ("NFL", "San Francisco 49ers", "SF"), ("MLB", "New York Yankees", "NYY"),
            ("MLB", "Los Angeles Dodgers", "LAD"),
        ):
            self.assertEqual(resolve_team(name, league).abbreviation, expected)

    def test_historical_names_still_resolve(self):
        """Feeds carry stale franchise names; the archive is full of them."""
        self.assertEqual(resolve_team("Cleveland Indians", "MLB").abbreviation, "CLE")
        self.assertEqual(resolve_team("Oakland Raiders", "NFL").abbreviation, "LV")
        self.assertEqual(resolve_team("St. Louis Rams", "NFL").abbreviation, "LAR")

    def test_exact_abbreviation_is_identity(self):
        self.assertEqual(resolve_team("KC", "NFL").abbreviation, "KC")

    def test_unknown_league_rejects(self):
        self.assertFalse(resolve_team("Buffalo Bills", "XFL").resolved)

    def test_empty_name_rejects(self):
        self.assertFalse(resolve_team("   ", "NFL").resolved)


class AmbiguityTest(unittest.TestCase):
    """Shared-city names must not resolve. This is the whole safety property."""

    def test_bare_city_with_two_franchises_rejects(self):
        for league, city in (("MLB", "New York"), ("NFL", "New York"),
                             ("NFL", "Los Angeles"), ("MLB", "Chicago"),
                             ("MLB", "Los Angeles")):
            self.assertFalse(
                resolve_team(city, league).resolved,
                f"{city!r} has two {league} franchises and must not resolve",
            )

    def test_unrelated_name_rejects(self):
        for name in ("Sharks", "Manchester United", "zzzzz"):
            self.assertFalse(resolve_team(name, "NFL").resolved)


class CrossLeagueCollisionTest(unittest.TestCase):
    def test_same_abbreviation_different_franchise(self):
        """MIA is a live abbreviation in both leagues, for different teams."""
        self.assertEqual(resolve_team("Miami Dolphins", "NFL").abbreviation, "MIA")
        self.assertEqual(resolve_team("Miami Marlins", "MLB").abbreviation, "MIA")

    def test_rosters_genuinely_collide(self):
        """Pins the premise: if these stopped overlapping, league-scoping
        would look like dead weight to a future reader."""
        shared = set(ROSTERS["NFL"]) & set(ROSTERS["MLB"])
        self.assertGreaterEqual(len(shared), 10)
        self.assertIn("MIA", shared)

    def test_wrong_league_does_not_borrow(self):
        self.assertFalse(resolve_team("Miami Marlins", "NFL").resolved)


class MatchEventTest(unittest.TestCase):
    def test_both_teams_required(self):
        self.assertTrue(match_event("NFL", "Buffalo Bills", "Kansas City Chiefs", START).matched)
        half = match_event("NFL", "Buffalo Bills", "New York", START)
        self.assertFalse(half.matched)
        self.assertIsNotNone(half.reject_reason)

    def test_start_time_must_agree(self):
        ok = match_event("NFL", "Buffalo Bills", "Kansas City Chiefs", START,
                         exchange_start=START + timedelta(minutes=5))
        self.assertTrue(ok.matched)
        drifted = match_event("NFL", "Buffalo Bills", "Kansas City Chiefs", START,
                              exchange_start=START + timedelta(hours=3))
        self.assertFalse(drifted.matched)

    def test_same_team_both_sides_rejects(self):
        self.assertFalse(
            match_event("NFL", "Buffalo Bills", "Buffalo Bills", START).matched)

    def test_event_id_is_stable_and_ordered(self):
        a = match_event("NFL", "Buffalo Bills", "Kansas City Chiefs", START)
        self.assertEqual(a.event_id, "NFL_20261117_BUF_KC")
        # Home/away order is part of the identity, not incidental.
        b = match_event("NFL", "Kansas City Chiefs", "Buffalo Bills", START)
        self.assertNotEqual(a.event_id, b.event_id)

    def test_same_teams_different_week_are_different_events(self):
        """A divisional rematch is the failure case fuzzy title matching hits."""
        a = canonical_event_id("NFL", START, "BUF", "KC")
        b = canonical_event_id("NFL", START + timedelta(days=56), "BUF", "KC")
        self.assertNotEqual(a, b)


class TickerParseTest(unittest.TestCase):
    def test_parses_game_ticker(self):
        p = parse_kalshi_game_ticker("KXNFLGAME-24NOV17-BUF-KC")
        self.assertEqual((p["series"], p["a"], p["b"]), ("KXNFLGAME", "BUF", "KC"))

    def test_unexpected_shape_returns_none(self):
        """A format change must surface as unmatched coverage, never as
        mis-parsed teams."""
        for bad in ("KXNFLGAME-BUF-KC", "garbage", "", "KXNFLGAME-24NOV17-BUF"):
            self.assertIsNone(parse_kalshi_game_ticker(bad))


if __name__ == "__main__":
    unittest.main()
