"""Regression: the live bot_config.json is checked against what the code reads.

--- Why this exists -------------------------------------------------------

`bot_config.json` is gitignored and lives on the host, so NO config change ever
arrives by deploy — anything a release adds to `bot_config.template.json` is a
manual step on the Beelink, every time. Nothing verified that the live file and
the code agreed, and every read is a `.get(key, default)`, so a missing key is
silent. Two of those defaults are actively unsafe:

    global_settings.vix absent       -> 15.0, BELOW every gate. The VIX
                                        kill-switch reads a calm market.
    cfo_settings.unallocated_reserve -> 0.0, so every budget computes on FULL
      absent                            equity instead of equity minus reserve.

Found by auditing the shipped template itself, which was missing `vix`: a fresh
install traded as though the market were calm until market_analyst's first
successful fetch — up to 15 minutes of a disabled kill-switch that looks
exactly like a working one. The template now seeds the analyst's own blind-state
values instead (CRITICAL_VOLATILITY / 25.0 / data_stale), because a config that
has never had a successful fetch IS the stale case.

The expected shape lives in fleet_registry, not here and not in fleet_doctor,
because the registry already owns the bot_config contract — so a newly
registered bot's keys are checked the moment it is registered (rule 2: the
registry is the only bot list).
"""
import json
import unittest

import fleet_registry


def full_config():
    """A config defining everything the code reads."""
    with open("bot_config.template.json") as f:
        return json.load(f)


def paths(findings):
    return [f[0] for f in findings]


class TemplateIsCompleteTest(unittest.TestCase):
    """The shipped bootstrap must not itself be missing keys."""

    def test_the_template_defines_every_key_the_code_reads(self):
        self.assertEqual(fleet_registry.missing_config_keys(full_config()), [])

    def test_the_template_boots_into_the_fail_safe_not_a_calm_market(self):
        gs = full_config()["global_settings"]
        # A brand-new config has had no successful SPY+VIX fetch. Publishing
        # SIDEWAYS/15.0 would un-gate the fleet on a reading nobody measured.
        self.assertEqual(gs["market_condition"], "CRITICAL_VOLATILITY")
        self.assertTrue(gs["data_stale"])
        self.assertGreater(gs["vix"], 22, "seeded VIX must clear the wheel/crypto gate")
        self.assertLess(gs["vix"], 28, "but stay below the full kill, per the analyst")

    def test_every_registered_bot_has_a_template_entry(self):
        bots = full_config()["bots"]
        for name in fleet_registry.BOTS:
            self.assertIn(name, bots, f"{name} is registered but absent from the template")


class MissingKeyDetectionTest(unittest.TestCase):

    def test_absent_vix_is_critical(self):
        cfg = full_config()
        del cfg["global_settings"]["vix"]
        found = fleet_registry.missing_config_keys(cfg)
        self.assertIn("global_settings.vix", paths(found))
        entry = next(f for f in found if f[0] == "global_settings.vix")
        self.assertEqual(entry[1], 15.0)
        self.assertEqual(entry[2], "critical")

    def test_absent_unallocated_reserve_is_critical(self):
        cfg = full_config()
        del cfg["cfo_settings"]["unallocated_reserve"]
        entry = next(f for f in fleet_registry.missing_config_keys(cfg)
                     if f[0] == "cfo_settings.unallocated_reserve")
        self.assertEqual((entry[1], entry[2]), (0.0, "critical"))

    def test_absent_market_condition_is_critical(self):
        cfg = full_config()
        del cfg["global_settings"]["market_condition"]
        entry = next(f for f in fleet_registry.missing_config_keys(cfg)
                     if f[0] == "global_settings.market_condition")
        self.assertEqual(entry[2], "critical")

    def test_a_registered_bot_absent_from_config_is_critical(self):
        cfg = full_config()
        del cfg["bots"]["crypto_grid"]
        entry = next(f for f in fleet_registry.missing_config_keys(cfg)
                     if f[0] == "bots.crypto_grid")
        self.assertEqual(entry[2], "critical")

    def test_absent_entries_enabled_is_reported(self):
        # The live case: the grid silently opens nothing.
        cfg = full_config()
        del cfg["bots"]["crypto_grid"]["entries_enabled"]
        entry = next(f for f in fleet_registry.missing_config_keys(cfg)
                     if f[0] == "bots.crypto_grid.entries_enabled")
        self.assertEqual((entry[1], entry[2]), (False, "warn"))

    def test_per_bot_keys_come_from_the_registry(self):
        # Rule 2: no per-bot list outside fleet_registry. A bot declaring a
        # config key gets checked without touching the checker.
        self.assertIn("entries_enabled",
                      fleet_registry.BOTS["crypto_grid"]["config_keys"])
        self.assertIn("force_close_symbols",
                      fleet_registry.BOTS["wheel_bot"]["config_keys"])

    def test_critical_findings_sort_first(self):
        cfg = full_config()
        del cfg["bots"]["wheel_bot"]["force_close_symbols"]   # warn
        del cfg["global_settings"]["vix"]                     # critical
        found = fleet_registry.missing_config_keys(cfg)
        self.assertEqual(found[0][2], "critical")

    def test_an_empty_config_reports_everything_without_raising(self):
        found = fleet_registry.missing_config_keys({})
        self.assertTrue(any(f[0] == "global_settings.vix" for f in found))
        self.assertTrue(any(f[0].startswith("bots.") for f in found))

    def test_none_is_handled(self):
        self.assertTrue(fleet_registry.missing_config_keys(None))


class RuntimeDriftIsNotAnErrorTest(unittest.TestCase):
    """Keys the live file gains at runtime must not be flagged as wrong.

    The live config legitimately accumulates state the template never had.
    This check is one-directional by design; flagging that drift would make it
    noise, and a check that fires on normal operation trains you to ignore it.
    """

    def test_runtime_only_keys_are_not_reported(self):
        cfg = full_config()
        cfg["global_settings"].update({
            "vix_source": "cboe",
            "regime_updated": "2026-09-11T16:00:00+00:00",
        })
        cfg["bots"]["wheel_bot"]["force_close_symbols"] = ["PAAS"]
        self.assertEqual(fleet_registry.missing_config_keys(cfg), [])

    def test_a_stray_config_bot_is_reported_separately(self):
        cfg = full_config()
        cfg["bots"]["condor_bot"] = {"status": "active", "allocation": 0.0}
        self.assertEqual(fleet_registry.unregistered_config_bots(cfg), ["condor_bot"])
        # ...and not as a missing key.
        self.assertNotIn("bots.condor_bot", paths(fleet_registry.missing_config_keys(cfg)))

    def test_the_accountant_entry_is_not_stray(self):
        # A real bots{} entry for an infra process, not a strategy.
        self.assertEqual(fleet_registry.unregistered_config_bots(full_config()), [])


if __name__ == "__main__":
    unittest.main()
