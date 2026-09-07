"""Regression tests for the fleet containment guard (utils.assert_order_allowed_here).

The 2026-09 duplicate-fleet incident: a pre-container host-level PM2 daemon,
dormant since containerisation, was resurrected by its `pm2-trader.service`
systemd unit when a long-deferred OS update finally rebooted the Beelink. It ran
the SAME live-mounted repo against the SAME config.py — so the same Alpaca
account — with restart counts in the thousands, while
`docker exec trading-fleet pm2 ls` showed nine healthy processes and zero
restarts. Both were true, of different PM2 daemons.

The alerting half of that is diagnosable now (commander._source_tag,
fleet_doctor's duplicate-fleet check). This guard covers the half that matters
more: a second fleet on one account submits orders tagged identically to the
real ones, which the ownership model cannot tell apart, while both copies race
on the same unlocked JSON state.

The load-bearing assertion in here is test_uncontained_never_reaches_the_broker:
the guard is worthless if it raises AFTER submit_order.

Run: python -m unittest test_containment -v
"""
import unittest
from unittest import mock

import utils


class _Account:
    last_equity = "100000"
    equity = "100000"


class _Order:
    id = "ord-1"
    symbol = "AAPL"
    qty = "1"
    status = mock.Mock(value="filled")
    type = mock.Mock(value="market")
    side = mock.Mock(value="buy")
    filled_qty = "1"
    filled_avg_price = "100"


class _Client:
    """Records whether the broker was reached at all."""

    def __init__(self):
        self.account_calls = 0
        self.submitted = []

    def get_account(self):
        self.account_calls += 1
        return _Account()

    def get_all_positions(self):
        return []

    def submit_order(self, order_data):
        self.submitted.append(order_data)
        return _Order()

    def get_order_by_id(self, _id):
        return _Order()


def _req(symbol="AAPL", qty=1):
    """A limit order: priced, so the safety gate needs no price lookup."""
    return mock.Mock(symbol=symbol, qty=qty, limit_price=10.0)


class DetectionTests(unittest.TestCase):
    """Deliberately generous: any one positive signal counts. A false 'no'
    would stop the real fleet trading, so it refuses only when nothing at all
    indicates a container."""

    def _only(self, present):
        return lambda path: path == present

    def test_dockerenv_marker(self):
        with mock.patch.object(utils.os.path, "exists",
                               self._only("/.dockerenv")):
            self.assertTrue(utils.running_in_fleet_container())

    def test_podman_marker(self):
        with mock.patch.object(utils.os.path, "exists",
                               self._only("/run/.containerenv")):
            self.assertTrue(utils.running_in_fleet_container())

    def test_fleet_code_dir(self):
        with mock.patch.object(utils.os.path, "exists", lambda p: False), \
             mock.patch.object(utils.os.path, "dirname",
                               lambda p: utils.FLEET_CONTAINER_CODE_DIR):
            self.assertTrue(utils.running_in_fleet_container())

    def test_cgroup_fallback(self):
        with mock.patch.object(utils.os.path, "exists", lambda p: False), \
             mock.patch("builtins.open",
                        mock.mock_open(read_data="0::/docker/abc123")):
            self.assertTrue(utils.running_in_fleet_container())

    def test_bare_host_is_not_a_container(self):
        with mock.patch.object(utils.os.path, "exists", lambda p: False), \
             mock.patch("builtins.open",
                        mock.mock_open(read_data="0::/user.slice")):
            self.assertFalse(utils.running_in_fleet_container())

    def test_unreadable_cgroup_is_not_fatal(self):
        with mock.patch.object(utils.os.path, "exists", lambda p: False), \
             mock.patch("builtins.open", side_effect=OSError("denied")):
            self.assertFalse(utils.running_in_fleet_container())


class GateTests(unittest.TestCase):
    def setUp(self):
        utils._uncontained_override_warned = False
        self.addCleanup(setattr, utils, "_uncontained_override_warned", False)

    def test_contained_passes(self):
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=True):
            utils.assert_order_allowed_here()   # must not raise

    def test_uncontained_raises(self):
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ, {}, clear=True):
            with self.assertRaises(utils.UncontainedFleetError):
                utils.assert_order_allowed_here()

    def test_error_names_the_override(self):
        """A gate nobody can deliberately bypass gets bypassed by editing the
        source, which is worse. The refusal has to say how."""
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ, {}, clear=True):
            try:
                utils.assert_order_allowed_here()
                self.fail("expected UncontainedFleetError")
            except utils.UncontainedFleetError as e:
                self.assertIn(utils.UNCONTAINED_OVERRIDE_ENV, str(e))

    def test_override_allows(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                with mock.patch.object(utils, "running_in_fleet_container",
                                       return_value=False), \
                     mock.patch.dict(
                         utils.os.environ,
                         {utils.UNCONTAINED_OVERRIDE_ENV: value}, clear=True):
                    utils.assert_order_allowed_here()

    def test_unset_and_falsey_values_do_not_override(self):
        for value in ("", "0", "no", "false"):
            with self.subTest(value=value):
                with mock.patch.object(utils, "running_in_fleet_container",
                                       return_value=False), \
                     mock.patch.dict(
                         utils.os.environ,
                         {utils.UNCONTAINED_OVERRIDE_ENV: value}, clear=True):
                    with self.assertRaises(utils.UncontainedFleetError):
                        utils.assert_order_allowed_here()

    def test_override_warns_once_not_per_order(self):
        """An every-order warning is the alert-storm mistake again."""
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ,
                             {utils.UNCONTAINED_OVERRIDE_ENV: "1"}, clear=True), \
             mock.patch.object(utils.logger, "warning") as warn:
            for _ in range(5):
                utils.assert_order_allowed_here()
        self.assertEqual(warn.call_count, 1)


class SubmitPathTests(unittest.TestCase):
    """The guard has to sit in front of the one place orders leave the fleet."""

    def setUp(self):
        utils._uncontained_override_warned = False
        self.addCleanup(setattr, utils, "_uncontained_override_warned", False)
        self.client = _Client()
        self.log = mock.Mock()

    def test_uncontained_never_reaches_the_broker(self):
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ, {}, clear=True):
            with self.assertRaises(utils.UncontainedFleetError):
                utils.submit_and_log_order(self.client, _req(), self.log)

        self.assertEqual(self.client.submitted, [],
                         "an uncontained fleet must not place orders")
        self.assertEqual(self.client.account_calls, 0,
                         "the guard must run before any broker call")

    def test_block_is_recorded_in_the_error_registry(self):
        """A rogue fleet blocked in silence is still a rogue fleet nobody
        knows is running — the block has to reach Grafana."""
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ, {}, clear=True), \
             mock.patch.object(utils.registry, "log_error") as le:
            with self.assertRaises(utils.UncontainedFleetError):
                utils.submit_and_log_order(self.client, _req(), self.log)
        le.assert_called_once()
        self.assertEqual(le.call_args[0][1], "containment_guard")

    def test_contained_submits_normally(self):
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=True), \
             mock.patch.object(utils, "_log_fill_to_influx"), \
             mock.patch.object(utils.time, "sleep"):
            utils.submit_and_log_order(self.client, _req(), self.log)
        self.assertEqual(len(self.client.submitted), 1)

    def test_override_submits_normally(self):
        with mock.patch.object(utils, "running_in_fleet_container",
                               return_value=False), \
             mock.patch.dict(utils.os.environ,
                             {utils.UNCONTAINED_OVERRIDE_ENV: "1"}, clear=True), \
             mock.patch.object(utils, "_log_fill_to_influx"), \
             mock.patch.object(utils.time, "sleep"):
            utils.submit_and_log_order(self.client, _req(), self.log)
        self.assertEqual(len(self.client.submitted), 1)


if __name__ == "__main__":
    unittest.main()
