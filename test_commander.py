"""Regression tests for commander's fleet watchdog alerting.

The 2026-09 storm: every bot reported "CRASHED" to Discord over and over, which
buried the one process that was genuinely failing. Three separate defects:

  1. No throttle. The watchdog loop is 60s, so ANY process that stayed down
     alerted 60x/hour — and a fleet-wide event alerted for all nine at once.
  2. No distinction between a crash and a clean stop. A process the analyst
     paused (VIX > 28) and then reactivated is 'stopped', not 'errored'; on the
     next cycle commander announced the whole fleet as CRASHED.
  3. The alert carried no diagnosis — no restart count, no error-log tail — so
     it could not be told apart from the noise it was drowning in.

Run: python -m unittest test_commander -v
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

import commander


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def proc(name, status, restarts=0, err_log=None):
    return {
        "name": name,
        "pm2_env": {"status": status, "restart_time": restarts,
                    "pm_err_log_path": err_log},
        "monit": {"memory": 100 * 1024 * 1024, "cpu": 1.0},
    }


CONFIG_ACTIVE = {"bots": {
    "wheel_bot": {"script": "wheel_bot.py", "status": "active"},
    "trend_bot": {"script": "trend_bot.py", "status": "active"},
}}


class _Channel:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class ManageFleetTests(unittest.TestCase):
    def setUp(self):
        commander._last_down_alert.clear()
        commander.PAUSED_BY_COMMANDER.clear()
        self.addCleanup(commander._last_down_alert.clear)
        self.addCleanup(commander.PAUSED_BY_COMMANDER.clear)

        self.channel = _Channel()
        p = mock.patch.object(commander.bot, "fetch_channel",
                              new=mock.AsyncMock(return_value=self.channel))
        p.start(); self.addCleanup(p.stop)

        self.run_mock = mock.patch.object(commander.subprocess, "run").start()
        self.addCleanup(mock.patch.stopall)

        cid = mock.patch.object(commander, "CHANNEL_ID", "123")
        cid.start(); self.addCleanup(cid.stop)

    # --- 1. throttling ---------------------------------------------------
    def test_repeated_down_cycles_alert_once(self):
        """A bot that stays errored must not re-alert every 60s watchdog tick."""
        cfg = {"bots": {"wheel_bot": {"script": "wheel_bot.py", "status": "active"}}}
        procs = [proc("wheel_bot", "errored", restarts=10)]
        for _ in range(10):
            run(commander.manage_fleet(procs, cfg))
        self.assertEqual(len(self.channel.sent), 1)
        # ...but it IS restarted every cycle; only the shouting is throttled.
        self.assertEqual(self.run_mock.call_count, 10)

    def test_throttle_is_per_bot_not_global(self):
        procs = [proc("wheel_bot", "errored"), proc("trend_bot", "errored")]
        run(commander.manage_fleet(procs, CONFIG_ACTIVE))
        self.assertEqual(len(self.channel.sent), 2)

    def test_throttle_expires(self):
        procs = [proc("wheel_bot", "errored")]
        run(commander.manage_fleet(procs, CONFIG_ACTIVE))
        commander._last_down_alert["wheel_bot"] -= commander.DOWN_ALERT_COOLDOWN + 1
        run(commander.manage_fleet(procs, CONFIG_ACTIVE))
        self.assertEqual(len(self.channel.sent), 2)

    def test_recovery_clears_the_throttle(self):
        """After a bot comes back, the NEXT genuine failure alerts immediately
        instead of being swallowed by the old cooldown."""
        run(commander.manage_fleet([proc("wheel_bot", "errored")], CONFIG_ACTIVE))
        run(commander.manage_fleet([proc("wheel_bot", "online")], CONFIG_ACTIVE))
        run(commander.manage_fleet([proc("wheel_bot", "errored")], CONFIG_ACTIVE))
        self.assertEqual(len(self.channel.sent), 2)

    # --- 2. crash vs. clean stop ----------------------------------------
    def test_commander_paused_bot_resumes_silently(self):
        """The storm's root cause: analyst pauses the fleet on VIX>28, then
        reactivates it, and every bot got announced as CRASHED."""
        paused_cfg = {"bots": {"wheel_bot": {"script": "wheel_bot.py",
                                             "status": "paused"}}}
        run(commander.manage_fleet([proc("wheel_bot", "online")], paused_cfg))
        self.assertIn("wheel_bot", commander.PAUSED_BY_COMMANDER)
        self.assertEqual(self.channel.sent, [])

        # Analyst flips it back to active; the process is 'stopped'.
        run(commander.manage_fleet([proc("wheel_bot", "stopped")], CONFIG_ACTIVE))
        self.assertEqual(self.channel.sent, [], "resume must not report a crash")
        self.assertNotIn("wheel_bot", commander.PAUSED_BY_COMMANDER)

    def test_errored_is_worded_as_a_crash(self):
        run(commander.manage_fleet([proc("wheel_bot", "errored", restarts=10)],
                                   CONFIG_ACTIVE))
        msg = self.channel.sent[0]
        self.assertIn("CRASHED", msg)
        self.assertIn("10 restarts", msg)

    def test_stopped_is_not_called_a_crash(self):
        run(commander.manage_fleet([proc("wheel_bot", "stopped")], CONFIG_ACTIVE))
        msg = self.channel.sent[0]
        self.assertIn("NOT RUNNING", msg)
        self.assertNotIn("CRASHED", msg)

    def test_missing_process_is_started_without_an_alert(self):
        run(commander.manage_fleet([], CONFIG_ACTIVE))
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.run_mock.call_count, 2)

    # --- 3. the alert says why ------------------------------------------
    def test_alert_carries_the_error_log_tail(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write("Traceback (most recent call last):\n"
                    "  File \"/app/code/trend_bot.py\", line 23\n"
                    "ModuleNotFoundError: No module named 'ta'\n")
            path = f.name
        self.addCleanup(os.unlink, path)

        run(commander.manage_fleet([proc("trend_bot", "errored", err_log=path)],
                                   {"bots": {"trend_bot": {"script": "trend_bot.py",
                                                           "status": "active"}}}))
        self.assertIn("ModuleNotFoundError", self.channel.sent[0])

    def test_missing_error_log_still_alerts(self):
        run(commander.manage_fleet(
            [proc("wheel_bot", "errored", err_log="/nonexistent/x.log")],
            CONFIG_ACTIVE))
        self.assertEqual(len(self.channel.sent), 1)

    def test_discord_failure_is_logged_not_swallowed(self):
        """No bare `except: pass` — a watchdog that cannot reach Discord is
        exactly what the error registry exists for."""
        with mock.patch.object(commander.bot, "fetch_channel",
                               new=mock.AsyncMock(side_effect=RuntimeError("no channel"))), \
             mock.patch.object(commander.registry, "log_error") as le:
            run(commander.manage_fleet([proc("wheel_bot", "errored")], CONFIG_ACTIVE))
            le.assert_called_once()
        # The restart still happens even though the alert failed.
        self.assertTrue(self.run_mock.called)


class ProcessMetricsTests(unittest.TestCase):
    def test_memory_and_cpu_come_from_monit(self):
        """`pm2 jlist` reports live usage under 'monit', not 'pm2_env' — reading
        the wrong key is what left bot_monitor.memory flat at 0 in Grafana."""
        captured = {}
        with mock.patch.object(commander.requests, "post",
                               side_effect=lambda *a, **k: captured.update(k) or
                               mock.Mock(status_code=204)):
            commander.log_process_to_influx(proc("wheel_bot", "online", restarts=2))
        self.assertIn("memory=104857600", captured.get("data", ""))


if __name__ == "__main__":
    unittest.main()
