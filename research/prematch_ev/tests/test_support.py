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

from collect import (
    Ledger, SupportReport, join_markets, market_start_time,
    parse_event_body_start, support_report,
)
from core.matcher import (
    ROSTERS, league_has_schedule, league_is_supported, parse_kalshi_game_ticker,
    start_source, supported_leagues,
)
from data.odds_history import SPORT_KEYS, SharpQuote
import run_study

UTC = timezone.utc


# REAL tickers, read off live KXNFLGAME contracts during PR #27's review.
# Note what is NOT here: a time. `26SEP14DENKC` is a date and two teams.
#
# The synthetic `KXNFLGAME-26SEP211300BUFKC` used in an earlier round had
# `1300` in the middle because I put it there. It parsed a start perfectly and
# proved nothing -- the fourth time in this study that a fixture built from an
# assumption agreed with the assumption. These are copied, not composed.
REAL_NFL_TICKERS = (
    "KXNFLGAME-26SEP14DENKC-KC",
    "KXNFLGAME-26SEP13DALNYG-NYG",
)
# From the sampled KC contract. Every one of these is the END of the contract,
# on the day AFTER kickoff. None of them is a start.
KC_END_OF_GAME_FIELDS = {
    "close_time": "2026-09-15T03:15:19Z",
    "expected_expiration_time": "2026-09-15T03:15:00Z",
    "settlement_ts": "2026-09-15T03:21:19.283583Z",
}


class NflTickerHasNoKickoffTest(unittest.TestCase):
    """Structural parsing is league-agnostic. Deriving a START is not.

    I claimed otherwise, on the strength of a ticker I had invented.
    """

    def test_the_structural_parse_still_works(self):
        for ticker in REAL_NFL_TICKERS:
            parsed = parse_kalshi_game_ticker(ticker)
            self.assertIsNotNone(parsed, ticker)
            self.assertEqual(parsed["series"], "KXNFLGAME")
            self.assertTrue(parsed["yes_participant"])

    def test_but_no_start_can_be_derived(self):
        for ticker in REAL_NFL_TICKERS:
            event = parse_kalshi_game_ticker(ticker)["event_ticker"]
            self.assertIsNone(parse_event_body_start(event),
                              f"{event} has no kickoff to find")

    def test_the_mlb_body_does_carry_one(self):
        """The contrast that makes this a per-league fact, not a parser bug."""
        event = parse_kalshi_game_ticker(
            "KXMLBGAME-26SEP152140MIAAZ-MIA")["event_ticker"]
        self.assertIsNotNone(parse_event_body_start(event))

    def test_end_of_contract_fields_are_never_a_start(self):
        """close_time, expected_expiration_time and settlement_ts are all on
        the day AFTER kickoff. A study whose lead times count back from the
        final whistle is measuring the wrong thing precisely."""
        market = {"ticker": REAL_NFL_TICKERS[0],
                  "event_ticker": "KXNFLGAME-26SEP14DENKC",
                  **KC_END_OF_GAME_FIELDS}
        self.assertIsNone(market_start_time(market),
                          "a start was inferred from an end-of-contract field")

    def test_nfl_start_source_is_external_not_the_ticker(self):
        """The ticker still carries no kickoff. An EXTERNAL source supplies it.

        This test used to assert NFL had no start source at all, which was
        true of the code and never of the world. What has to stay pinned is
        the underlying fact -- the body has a date and no time -- and that the
        two sources are kept apart, because they carry different warranties.
        """
        self.assertEqual(start_source("NFL"), "external_schedule")
        self.assertTrue(league_has_schedule("NFL"))
        self.assertEqual(start_source("MLB"), "event_ticker")
        self.assertTrue(league_has_schedule("MLB"))
        # the reason the external source is needed at all
        self.assertIsNone(parse_event_body_start("KXNFLGAME-26SEP14DENKC"))

    def test_the_two_start_sources_carry_different_warranties(self):
        """An external schedule cannot date a decision; a ticker can."""
        from core.matcher import start_source_kind, start_source_provenance
        self.assertEqual(start_source_kind("MLB"), "ticker")
        self.assertEqual(start_source_provenance("MLB"),
                         "verified_at_decision_time")
        self.assertEqual(start_source_kind("NFL"), "external")
        self.assertEqual(start_source_provenance("NFL"), "unverified")


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
        self.assertIsNone(report.start_source)
        # Three, not two: odds key, roster, AND schedule source.
        self.assertEqual(len(report.blockers()), 3)


class SupportReportTest(unittest.TestCase):
    def test_nfl_is_collectable_through_an_external_schedule(self):
        """Both halves, from different sources -- and the distinction kept.

        These were one flag, and NFL passed it on 32 teams and a valid odds
        key while every contract failed on no_readable_start_time. The flags
        stayed separate; what changed is that the schedule half is now
        satisfied, by an ADAPTER rather than by the ticker.
        """
        report = support_report("NFL", "KXNFLGAME")
        self.assertTrue(report.roster_ready)
        self.assertTrue(report.schedule_ready)
        self.assertTrue(report.ready)
        self.assertTrue(report.schedule_is_external)
        self.assertEqual(report.odds_key, "americanfootball_nfl")
        self.assertEqual(report.roster_teams, 32)

    def test_nfl_is_collectable_but_NOT_point_in_time_capable(self):
        """The third question, and the one an exit code cannot carry.

        A run over NFL can size availability and cost. It cannot certify a
        backtest, because the schedule it dated its decisions with was fetched
        afterwards. COLLECTABLE says nothing about that, so the report says it
        separately rather than letting readiness imply it.
        """
        report = support_report("NFL", "KXNFLGAME")
        self.assertTrue(report.ready)
        self.assertFalse(report.point_in_time_capable)
        self.assertTrue(support_report("MLB", "KXMLBGAME").point_in_time_capable)

    def test_the_nfl_caveat_states_the_exploratory_limit_and_the_two_confusions(self):
        caveats = " ".join(support_report("NFL", "KXNFLGAME").caveats()).lower()
        self.assertIn("historical_schedule_as_of=unverified", caveats)
        self.assertIn("cannot certify", caveats)
        self.assertIn("point-in-time", caveats)
        # adapter support is not runtime coverage
        self.assertIn("runtime coverage", caveats)

    def test_an_unscheduled_league_still_names_the_shortcut_it_refuses(self):
        """The end-of-contract warning belongs to leagues with NO source.

        NFL no longer carries it, because NFL now has a real one. NCAAF does,
        and the three field names stay in the blocker so the shortcut is
        visibly closed rather than merely unused.
        """
        blockers = " ".join(support_report("NCAAF", "KXNCAAFGAME").blockers())
        self.assertIn("scheduled-start source", blockers)
        for field in ("close_time", "expected_expiration_time", "settlement_ts"):
            self.assertIn(field, blockers)

    def test_mlb_is_both(self):
        report = support_report("MLB", "KXMLBGAME")
        self.assertTrue(report.roster_ready)
        self.assertTrue(report.schedule_ready)
        self.assertTrue(report.ready)
        self.assertEqual(report.blockers(), [])

    def test_nfl_carries_the_missing_alias_caveat(self):
        """MLB needed AZ->ARI. An empty alias map is an unverified assumption,
        not evidence there are none -- and a missing alias shows up as a team
        contributing no data, never as an error."""
        from unittest.mock import patch
        from core.matcher import EXCHANGE_CODE_ALIASES
        with patch.dict(EXCHANGE_CODE_ALIASES, {"NFL": {}}):
            caveats = " ".join(support_report("NFL", "KXNFLGAME").caveats())
        self.assertIn("NO exchange-code aliases", caveats)
        self.assertIn("AZ->ARI", caveats)

    def test_a_series_without_a_dated_fee_schedule_is_flagged(self):
        self.assertFalse(support_report("NCAAF", "KXNCAAFGAME").fee_schedule)
        self.assertIn("no dated fee schedule", " ".join(
            support_report("NCAAF", "KXNCAAFGAME").caveats()))
        self.assertTrue(support_report("MLB", "KXMLBGAME").fee_schedule)
        # NFL was this test's example until its dated schedule was read
        # (2026-09-25, PR #27 comment 5833183856) and recorded.
        nfl = support_report("NFL", "KXNFLGAME")
        self.assertTrue(nfl.fee_schedule)
        self.assertNotIn("no dated fee schedule", " ".join(nfl.caveats()))

    def test_a_roster_less_sport_reports_its_blocker(self):
        for league in ("NBA", "NHL"):
            report = support_report(league, f"KX{league}GAME")
            self.assertFalse(report.ready, league)
            self.assertTrue(any("roster" in b for b in report.blockers()), league)

    def test_ready_does_not_claim_data_exists(self):
        """The distinction the whole MLB result turned on."""
        report = SupportReport("MLB", "baseball_mlb", 30, 1, "KXMLBGAME",
                               True, "event_ticker")
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
        result = self._survey("NBA", "KXNBAGAME")
        self.assertFalse(result.coverage.complete)
        self.assertEqual(result.markets, {})
        self.assertEqual(result.cutoffs, [],
                         "nothing may be queued for purchase")
        self.assertEqual(len(result.matrix.statuses), 0)
        self.assertIn("roster", str(result.coverage))

    def test_the_refusal_names_the_cost_it_prevents(self):
        result = self._survey("NBA", "KXNBAGAME")
        self.assertIn("AFTER paying", str(result.coverage))

    def test_support_exits_nonzero_for_an_unsupported_sport(self):
        code = run_study.main(["--support", "--sport", "NBA", "--series", "KXNBAGAME"])
        self.assertEqual(code, 1)

    def test_support_exits_zero_for_a_collectable_sport(self):
        code = run_study.main(["--support", "--sport", "MLB", "--series", "KXMLBGAME"])
        self.assertEqual(code, 0)

    def test_support_exits_zero_for_nfl_now_that_it_has_a_schedule(self):
        """Roster-ready is still not collectable on its own.

        This assertion has now moved twice: NFL was the READY example, then
        the NOT-ready example when the flags were separated, and now it is
        ready again on the strength of an external adapter. The flag that
        matters did not move -- COLLECTABLE still needs both halves -- and the
        exploratory limit rides in the caveats, where an exit code cannot
        flatten it into pass or fail.
        """
        code = run_study.main(["--support", "--sport", "NFL", "--series", "KXNFLGAME"])
        self.assertEqual(code, 0)
        report = support_report("NFL", "KXNFLGAME")
        self.assertTrue(report.caveats(),
                        "a collectable-but-exploratory league must say so")

    def test_support_costs_nothing_and_needs_no_window(self):
        """It reads the code. No api key, no dates, no network."""
        self.assertEqual(
            run_study.main(["--support", "--sport", "MLB", "--series", "KXMLBGAME"]), 0)


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
