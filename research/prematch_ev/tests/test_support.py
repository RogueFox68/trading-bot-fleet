"""Sport support: what this code can do, checked BEFORE anything is paid for.

THE DEFECT THIS CLOSES. `--sport NBA` and `--sport NHL` were accepted by
argparse, survived preflight, and failed at JOIN time -- after every snapshot
had been bought -- because neither has a team roster and the join maps
bookmaker team NAMES onto exchange team CODES. Reproduced: an NCAA-shaped
slate joins 0 contracts with `sharp_teams_unresolvable` and
`unknown_exchange_code`.

That is the `regions=us` defect from round 1 wearing different clothes: a run
that costs credits and returns nothing usable, where the only symptom is an
empty result that reads like an absent edge.

`ready` here means the JOIN can work. It deliberately does NOT mean data
exists at any lead time -- listing lead times and historical sharp coverage
are separate questions that only a live audit answers, and conflating them is
how "we could not see it" becomes "there was nothing there".
"""

import unittest
from datetime import datetime, timedelta, timezone

from collect import Ledger, SupportReport, join_markets, support_report
from core.matcher import ROSTERS, league_is_supported, supported_leagues
from data.odds_history import SPORT_KEYS, SharpQuote
import run_study

UTC = timezone.utc


class SupportedLeagueTest(unittest.TestCase):
    def test_only_leagues_with_a_roster_are_supported(self):
        self.assertEqual(set(supported_leagues()), set(ROSTERS))
        for league in supported_leagues():
            self.assertTrue(ROSTERS[league], f"{league} roster is empty")

    def test_the_cli_offers_sports_that_are_not_join_ready(self):
        """Not a bug to fix by deleting them -- a gap to REPORT. The odds
        provider covers them; this code cannot resolve their teams yet."""
        offered = set(SPORT_KEYS)
        ready = set(supported_leagues())
        self.assertTrue(offered - ready,
                        "if this ever empties, delete this test, do not weaken it")
        for league in offered - ready:
            self.assertFalse(league_is_supported(league))

    def test_ncaa_football_is_not_supported_at_all(self):
        """Neither an odds key nor a roster. The gap list starts here."""
        report = support_report("NCAAF", "KXNCAAFGAME")
        self.assertFalse(report.ready)
        self.assertIsNone(report.odds_key)
        self.assertEqual(report.roster_teams, 0)
        self.assertEqual(len(report.blockers()), 2)


class SupportReportTest(unittest.TestCase):
    def test_nfl_is_join_ready(self):
        report = support_report("NFL", "KXNFLGAME")
        self.assertTrue(report.ready)
        self.assertEqual(report.odds_key, "americanfootball_nfl")
        self.assertEqual(report.roster_teams, 32)
        self.assertEqual(report.blockers(), [])

    def test_nfl_carries_the_missing_alias_caveat(self):
        """MLB needed AZ->ARI. An empty alias map is an unverified assumption,
        not evidence there are none -- and a missing alias shows up as a team
        contributing no data, never as an error."""
        caveats = " ".join(support_report("NFL", "KXNFLGAME").caveats())
        self.assertIn("NO exchange-code aliases", caveats)
        self.assertIn("AZ->ARI", caveats)

    def test_a_series_without_a_dated_fee_schedule_is_flagged(self):
        self.assertFalse(support_report("NFL", "KXNFLGAME").fee_schedule)
        self.assertIn("no dated fee schedule",
                      " ".join(support_report("NFL", "KXNFLGAME").caveats()))
        self.assertTrue(support_report("MLB", "KXMLBGAME").fee_schedule)

    def test_a_roster_less_sport_reports_its_blocker(self):
        for league in ("NBA", "NHL"):
            report = support_report(league, f"KX{league}GAME")
            self.assertFalse(report.ready, league)
            self.assertTrue(any("roster" in b for b in report.blockers()), league)

    def test_ready_does_not_claim_data_exists(self):
        """The distinction the whole MLB result turned on."""
        report = SupportReport("NFL", "americanfootball_nfl", 32, 0,
                               "KXNFLGAME", False)
        self.assertTrue(report.ready)
        self.assertEqual(report.blockers(), [])


class RefusedBeforeSpendTest(unittest.TestCase):
    """The gate has to fire where nothing has been spent yet."""

    def _survey(self, sport, series):
        args = run_study.parse_args([
            "--sport", sport, "--series", series,
            "--from", "2026-09-01", "--to", "2026-09-02", "--cache-dir", ""])
        return run_study.survey(args)

    def test_a_roster_less_sport_fails_coverage_with_no_cutoffs(self):
        markets, cutoffs, _, coverage, _, _, matrix = self._survey("NBA", "KXNBAGAME")
        self.assertFalse(coverage.complete)
        self.assertEqual(markets, {})
        self.assertEqual(cutoffs, [], "nothing may be queued for purchase")
        self.assertEqual(len(matrix.statuses), 0)
        self.assertIn("roster", str(coverage))

    def test_the_refusal_names_the_cost_it_prevents(self):
        _, _, _, coverage, _, _, _ = self._survey("NBA", "KXNBAGAME")
        self.assertIn("AFTER paying", str(coverage))

    def test_support_exits_nonzero_for_an_unsupported_sport(self):
        code = run_study.main(["--support", "--sport", "NBA", "--series", "KXNBAGAME"])
        self.assertEqual(code, 1)

    def test_support_exits_zero_for_a_ready_sport(self):
        code = run_study.main(["--support", "--sport", "NFL", "--series", "KXNFLGAME"])
        self.assertEqual(code, 0)

    def test_support_costs_nothing_and_needs_no_window(self):
        """It reads the code. No api key, no dates, no network."""
        self.assertEqual(
            run_study.main(["--support", "--sport", "NFL", "--series", "KXNFLGAME"]), 0)


class JoinFailsWithoutARosterTest(unittest.TestCase):
    """The failure the gate now prevents, reproduced so it stays reproduced."""

    def _quote(self, home, away):
        start = datetime(2026, 9, 21, 17, 0, tzinfo=UTC)
        return SharpQuote(snapshot=start - timedelta(hours=1), commence_time=start,
                          away_name=away, home_name=home, away_price=120,
                          home_price=-140, book="pinnacle", provider_event_id="e1",
                          last_update=start - timedelta(hours=1, minutes=2))

    def test_an_ncaa_slate_joins_nothing_and_says_why(self):
        ev = "KXNCAAFGAME-26SEP211300BAMAUGA"
        markets = {
            f"{ev}-{code}": {"ticker": f"{ev}-{code}", "event_ticker": ev,
                             "result": "yes" if code == "BAMA" else "no",
                             "open_time": "2026-09-01T00:00:00Z",
                             "settlement_ts": "2026-09-21T23:00:00Z"}
            for code in ("BAMA", "UGA")
        }
        led = Ledger()
        led.count("contracts", 2, unit="contracts")
        joined = join_markets(
            markets, {"e1": [self._quote("Alabama Crimson Tide", "Georgia Bulldogs")]},
            "NCAAF", led)
        self.assertEqual(joined, [])
        self.assertIn("sharp_teams_unresolvable", led.rejections)

    def test_the_same_slate_shape_joins_fine_for_a_supported_league(self):
        """Proving the shape is not the problem -- the roster is. Identity
        comes from the YES suffixes, so the parser is league-agnostic."""
        ev = "KXNFLGAME-26SEP211300BUFKC"
        markets = {
            f"{ev}-{code}": {"ticker": f"{ev}-{code}", "event_ticker": ev,
                             "result": "yes" if code == "BUF" else "no",
                             "open_time": "2026-09-01T00:00:00Z",
                             "settlement_ts": "2026-09-21T23:00:00Z"}
            for code in ("BUF", "KC")
        }
        led = Ledger()
        led.count("contracts", 2, unit="contracts")
        joined = join_markets(
            markets, {"e1": [self._quote("Kansas City Chiefs", "Buffalo Bills")]},
            "NFL", led)
        self.assertEqual(len(joined), 2, f"rejections: {dict(led.rejections)}")
        self.assertEqual({j.yes_participant for j in joined}, {"BUF", "KC"})


if __name__ == "__main__":
    unittest.main()
