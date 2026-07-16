"""Regression tests for the position-orphaning bugs.

Two ways the fleet has manufactured phantom orphans:

1. Coverage (GEN/APTV, fixed with paged fetching): an equity position whose
   opening order scrolled out of the most-recent-500 order window (flooded by
   high-frequency crypto orders) lost both its ownership tag and its entry
   timestamp — silently disabling stop-loss / max-hold exits. These tests
   replay >500 intervening orders after an open and assert that entry time and
   ownership still resolve, and that a position with no order at all is
   detectable as an orphan.

2. Fail-open fetch (2026-07-15 timeout storm): three Alpaca ReadTimeouts made
   the order fetch return an EMPTY set, which the ownership map treated as "no
   orders exist" — the whole 9-position book resolved to unowned and the
   accountant alert-flagged every position as orphaned in one cycle. The
   fail-safe tests assert a failed fetch reads as UNKNOWN: retries first, then
   last-known-good map, quiet quarantine, and an orphan sweep that stands down
   instead of paging about the entire book.

Run: python -m unittest test_orphan_resolution -v
"""
import datetime
import unittest
from unittest import mock

import requests
from alpaca.trading.enums import AssetClass

import accountant
import utils
import tiered_hold


UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)


def reset_ownership_state():
    """Clear utils' module-global ownership/fetch-health state between tests."""
    utils._ownership_cache = {}
    utils._ownership_cache_time = 0
    utils._ownership_map_degraded = False
    utils._last_order_fetch_failure = 0.0
    utils._fallback_log_times.clear()


class FakeOrder:
    """Minimal stand-in for an alpaca Order object."""
    _seq = 0

    def __init__(self, symbol, client_order_id, submitted_at, filled_at=None):
        FakeOrder._seq += 1
        self.id = f"oid-{FakeOrder._seq}"
        self.symbol = symbol
        self.client_order_id = client_order_id
        self.submitted_at = submitted_at
        self.filled_at = filled_at


class FakeTradingClient:
    """Mimics Alpaca get_orders pagination: newest-first, `limit`-capped,
    `until` (exclusive on submitted_at) to page backwards."""

    def __init__(self, orders):
        # Newest first, like Alpaca's default direction.
        self._orders = sorted(orders, key=lambda o: o.submitted_at, reverse=True)
        self.call_count = 0

    def get_orders(self, filter=None):
        self.call_count += 1
        until = getattr(filter, "until", None)
        limit = getattr(filter, "limit", 500) or 500
        pool = self._orders
        if until is not None:
            pool = [o for o in pool if o.submitted_at < until]
        return pool[:limit]


def build_flooded_history():
    """One old GEN + APTV open, then 2500 recent crypto orders on top."""
    orders = []
    # Aged equity opens (~3 weeks back), filled.
    gen_open = NOW - datetime.timedelta(days=21)
    aptv_open = NOW - datetime.timedelta(days=22)
    orders.append(FakeOrder("GEN", "trend_bot-GEN-1781099900", gen_open, filled_at=gen_open))
    orders.append(FakeOrder("APTV", "trend_bot-APTV-1781024438", aptv_open, filled_at=aptv_open))
    # 2500 crypto orders in the last ~2 days -> buries the equity opens far past
    # a single limit=500 window.
    for i in range(2500):
        ts = NOW - datetime.timedelta(minutes=i)
        orders.append(FakeOrder("BTC/USD", f"crypto_grid-BTC/USD-{i}", ts, filled_at=ts))
    return orders


class AgedOrderResolutionTest(unittest.TestCase):
    def setUp(self):
        reset_ownership_state()
        self.client = FakeTradingClient(build_flooded_history())

    def test_single_window_would_miss_aged_open(self):
        """Documents the bug: one limit=500 page never reaches the GEN open."""
        from alpaca.trading.requests import GetOrdersRequest
        page = self.client.get_orders(filter=GetOrdersRequest(status="all", limit=500))
        symbols = {o.symbol for o in page}
        self.assertNotIn("GEN", symbols)
        self.assertNotIn("APTV", symbols)

    def test_entry_times_resolve_for_aged_positions(self):
        entry_times = utils.get_position_entry_times(self.client, held_symbols=["GEN", "APTV"])
        self.assertIn("GEN", entry_times)
        self.assertIn("APTV", entry_times)
        # And they must be usable as a hold-duration proxy (~3 weeks).
        gen_hours = (NOW - entry_times["GEN"]).total_seconds() / 3600.0
        self.assertGreater(gen_hours / 24.0, 20)

    def test_ownership_resolves_from_real_tag(self):
        owner_map = utils._build_order_based_map(self.client, held_symbols=["GEN", "APTV"])
        self.assertEqual(owner_map.get("GEN"), "trend_bot")
        self.assertEqual(owner_map.get("APTV"), "trend_bot")

    def test_pagination_stops_early_when_covered(self):
        """Covering exactly the held symbols shouldn't walk all 2500 orders."""
        utils.get_position_entry_times(self.client, held_symbols=["GEN", "APTV"])
        # 21-22 days of buried opens -> a handful of 500-pages, not unbounded.
        self.assertLessEqual(self.client.call_count, 12)

    def test_max_hold_backstop_would_fire(self):
        """A 21-day short scores into a hold tier whose max_hold_days is exceeded."""
        entry_times = utils.get_position_entry_times(self.client, held_symbols=["GEN"])
        hours_held = (NOW - entry_times["GEN"]).total_seconds() / 3600.0
        # Short at 24.82 now ~26.46 -> losing; still, any tier with an overnight
        # allowance caps at <= 7 days, and 21 days blows past it.
        score = tiered_hold.calculate_hold_score(
            "trend_short", 26.46, 24.82,
            {"adx": 30.0, "ema_trend_intact": True}, "SIDEWAYS", 15.0,
            hours_held=hours_held,
        )
        tier = tiered_hold.get_hold_tier(score, "trend_short")
        max_days = tiered_hold.max_hold_days_for_tier(tier)
        if max_days is not None:
            self.assertGreaterEqual(hours_held / 24.0, max_days)


class OrphanSignatureTest(unittest.TestCase):
    """A position with no order history at all is the true orphan signature."""

    def setUp(self):
        reset_ownership_state()

    def test_untracked_symbol_has_no_owner_or_entry(self):
        # History contains GEN only; ORPH is held but never ordered by any bot.
        gen_open = NOW - datetime.timedelta(days=5)
        client = FakeTradingClient([
            FakeOrder("GEN", "trend_bot-GEN-1", gen_open, filled_at=gen_open),
        ])
        held = ["GEN", "ORPH"]
        owner_map = utils._build_order_based_map(client, held_symbols=held)
        entry_times = utils.get_position_entry_times(client, held_symbols=held)

        # GEN is durably resolvable...
        self.assertEqual(owner_map.get("GEN"), "trend_bot")
        self.assertIn("GEN", entry_times)
        # ...ORPH is not — this is what the accountant orphan sweep alerts on.
        self.assertNotIn("ORPH", owner_map)
        self.assertNotIn("ORPH", entry_times)


class AssignedStockOwnershipTest(unittest.TestCase):
    """Assignment turns a wheel option into bare stock with no tagged order of
    its own. The resolver must attribute that stock to the wheel via the
    option order's root — and never hand untagged stock to a default owner
    (trend_bot liquidated wheel-assigned PAAS on 2026-07-06 through the old
    `else trend_bot` fallback)."""

    def setUp(self):
        reset_ownership_state()

    def test_assigned_stock_resolves_to_wheel_via_option_root(self):
        t = NOW - datetime.timedelta(days=4)
        client = FakeTradingClient([
            FakeOrder("PAAS260702P00048000",
                      "wheel_bot-PAAS260702P00048000-1782000000", t, filled_at=t),
        ])
        owner_map = utils._build_order_based_map(client, held_symbols=["PAAS"])
        self.assertEqual(owner_map.get("PAAS"), "wheel_bot")

    def test_direct_equity_tag_outranks_root_inference(self):
        opt_t = NOW - datetime.timedelta(days=1)
        eq_t = NOW - datetime.timedelta(days=10)
        client = FakeTradingClient([
            FakeOrder("SLB260717P00050000",
                      "wheel_bot-SLB260717P00050000-1783000000", opt_t, filled_at=opt_t),
            FakeOrder("SLB", "trend_bot-SLB-1782000000", eq_t, filled_at=eq_t),
        ])
        owner_map = utils._build_order_based_map(client, held_symbols=["SLB"])
        # Wheel's option activity is newer, but an explicit order on the bare
        # symbol is the stronger claim: trend's shares stay trend's.
        self.assertEqual(owner_map.get("SLB"), "trend_bot")
        # The contract itself still resolves to the wheel.
        self.assertEqual(owner_map.get("SLB260717P00050000"), "wheel_bot")

    def test_untagged_stock_resolves_to_no_owner(self):
        client = FakeTradingClient([])
        owner = utils.get_bot_owner("ORPH", AssetClass.US_EQUITY, client)
        # None = unowned/quarantined: every bot filters holdings by its own
        # name, so nothing manages (or liquidates) this position.
        self.assertIsNone(owner)


class TimeoutTradingClient:
    """FakeTradingClient wrapper whose get_orders raises ReadTimeout on chosen
    call attempts — attempts 1..fail_first_n, and every attempt >= fail_from.
    Mimics the 2026-07-15 storm (13:30:09–13:30:52, three ReadTimeouts)."""

    def __init__(self, orders, fail_first_n=0, fail_from=None):
        self._inner = FakeTradingClient(orders)
        self.fail_first_n = fail_first_n
        self.fail_from = fail_from
        self.attempts = 0

    def get_orders(self, filter=None):
        self.attempts += 1
        if self.attempts <= self.fail_first_n or (
                self.fail_from is not None and self.attempts >= self.fail_from):
            raise requests.exceptions.ReadTimeout(
                "HTTPSConnectionPool(host='paper-api.alpaca.markets', port=443): "
                "Read timed out. (read timeout=30)")
        return self._inner.get_orders(filter=filter)


class FetchFailureFailSafeTest(unittest.TestCase):
    """2026-07-15 regression: a failed order fetch must read as UNKNOWN, never
    as 'no orders exist'. The old code returned an empty list on a ReadTimeout,
    the map was rebuilt from nothing (and cached!), and every held position —
    including ETSY/FTNT opened the day before — resolved to unowned at once."""

    def setUp(self):
        reset_ownership_state()
        # Retry backoff must not actually sleep in tests.
        self.sleep_patch = mock.patch("time.sleep")
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        reset_ownership_state()

    def _seed_good_map(self):
        """Warm the cache from a healthy fetch, then age it past 60s freshness
        so the next build is forced to hit the (about to fail) API."""
        t = NOW - datetime.timedelta(days=1)
        good = FakeTradingClient([
            FakeOrder("ETSY", "trend_bot-ETSY-1", t, filled_at=t),
            FakeOrder("FTNT", "trend_bot-FTNT-2", t, filled_at=t),
        ])
        owner_map = utils._build_order_based_map(good, held_symbols=["ETSY", "FTNT"])
        self.assertEqual(owner_map.get("ETSY"), "trend_bot")
        utils._ownership_cache_time -= 61

    def test_transient_timeouts_recover_via_retry(self):
        """Two timeouts then a clean page: retries absorb the blip entirely."""
        t = NOW - datetime.timedelta(days=1)
        client = TimeoutTradingClient(
            [FakeOrder("GEN", "trend_bot-GEN-1", t, filled_at=t)], fail_first_n=2)
        owner_map = utils._build_order_based_map(client, held_symbols=["GEN"])
        self.assertEqual(owner_map.get("GEN"), "trend_bot")
        self.assertFalse(utils.ownership_map_degraded())
        self.assertTrue(utils.order_history_healthy())

    def test_storm_serves_last_known_good_map_not_empty(self):
        self._seed_good_map()
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        owner_map = utils._build_order_based_map(storm, held_symbols=["ETSY", "FTNT"])
        # Yesterday's longs keep yesterday's owner — not 'no bot tag'.
        self.assertEqual(owner_map.get("ETSY"), "trend_bot")
        self.assertEqual(owner_map.get("FTNT"), "trend_bot")
        self.assertTrue(utils.ownership_map_degraded())
        self.assertFalse(utils.order_history_healthy())

    def test_storm_never_caches_an_empty_map(self):
        self._seed_good_map()
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        utils._build_order_based_map(storm, held_symbols=["ETSY"])
        # The stale-but-real map must still be the cache, not a hollow rebuild.
        self.assertEqual(utils._ownership_cache.get("ETSY"), "trend_bot")

    def test_storm_with_no_cache_raises_instead_of_empty_map(self):
        """Fresh process mid-storm: 'ownership is unknowable' must stay loud."""
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        with self.assertRaises(utils.OrderFetchError):
            utils._build_order_based_map(storm, held_symbols=["ETSY"])
        self.assertTrue(utils.ownership_map_degraded())
        self.assertFalse(utils.order_history_healthy())

    def test_degraded_lookup_quarantines_quietly(self):
        """A symbol missing from the stale fallback map is UNKNOWN: unowned
        (no bot trades it) but with no ownership_fallback metric/log page."""
        self._seed_good_map()
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        with mock.patch.object(utils, "_note_ownership_fallback") as note:
            self.assertIsNone(utils.get_bot_owner("MYST", AssetClass.US_EQUITY, storm))
            note.assert_not_called()
            # Known symbols still resolve normally from the stale map.
            self.assertEqual(
                utils.get_bot_owner("ETSY", AssetClass.US_EQUITY, storm), "trend_bot")

    def test_failure_cooldown_stops_api_hammering(self):
        """Per-position lookups inside the cooldown must not re-run a full
        retries-x-timeout fetch each — that would stall the caller's cycle."""
        self._seed_good_map()
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        utils._build_order_based_map(storm, held_symbols=["ETSY"])
        attempts_after_failure = storm.attempts
        self.assertEqual(attempts_after_failure, utils.ORDER_FETCH_ATTEMPTS)
        utils._build_order_based_map(storm, held_symbols=["ETSY"])
        utils._build_order_based_map(storm)
        self.assertEqual(storm.attempts, attempts_after_failure)

    def test_clean_fetch_after_storm_restores_fresh_map(self):
        self._seed_good_map()
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        utils._build_order_based_map(storm, held_symbols=["ETSY"])
        self.assertTrue(utils.ownership_map_degraded())
        # Storm passes: age the failure beyond the cooldown; next build refetches.
        utils._last_order_fetch_failure -= utils.OWNERSHIP_FETCH_FAIL_COOLDOWN + 1
        utils._ownership_cache_time -= 61
        t = NOW - datetime.timedelta(hours=2)
        clean = FakeTradingClient(
            [FakeOrder("PANW", "trend_bot-PANW-9", t, filled_at=t)])
        owner_map = utils._build_order_based_map(clean, held_symbols=["PANW"])
        self.assertEqual(owner_map.get("PANW"), "trend_bot")
        self.assertFalse(utils.ownership_map_degraded())

    def test_entry_times_salvage_partial_pages(self):
        """Storm begins on page 2: page 1's coverage is salvaged (newest-first,
        so a covered symbol's most recent fill is correct), the un-fetched tail
        stays unknown rather than being fabricated."""
        t_etsy = NOW - datetime.timedelta(days=1)
        orders = [FakeOrder("ETSY", "trend_bot-ETSY-1", t_etsy, filled_at=t_etsy)]
        for i in range(520):
            ts = NOW - datetime.timedelta(days=2, minutes=i)
            orders.append(FakeOrder("BTC/USD", f"crypto_grid-BTC/USD-{i}", ts, filled_at=ts))
        gen_t = NOW - datetime.timedelta(days=20)
        orders.append(FakeOrder("GEN", "trend_bot-GEN-2", gen_t, filled_at=gen_t))

        client = TimeoutTradingClient(orders, fail_from=2)
        entry_times = utils.get_position_entry_times(client, held_symbols=["ETSY", "GEN"])
        self.assertIn("ETSY", entry_times)
        self.assertNotIn("GEN", entry_times)
        self.assertFalse(utils.order_history_healthy())


class FakePosition:
    """Minimal stand-in for an alpaca Position."""

    def __init__(self, symbol, asset_class=AssetClass.US_EQUITY):
        self.symbol = symbol
        self.asset_class = asset_class
        self.side = "long"
        self.qty = "10"
        self.unrealized_pl = "12.34"


class OrphanSweepStanddownTest(unittest.TestCase):
    """The accountant's sweep must stand down — no orphan alerts, no orphan
    metrics — whenever the order-history fetch behind the map failed, and keep
    alerting on real orphans when history is healthy. On 2026-07-15 the sweep
    fired 'no bot tag in order history' for all 9 held positions at 13:30:52,
    forty seconds after the first of three fetch ReadTimeouts."""

    def setUp(self):
        reset_ownership_state()
        accountant._orphan_alert_times.clear()
        self.sleep_patch = mock.patch("time.sleep")
        self.sleep_patch.start()
        self.metrics = []
        self.alerts = []
        self.metric_patch = mock.patch.object(
            accountant, "log_metric",
            side_effect=lambda measurement, tags, fields:
                self.metrics.append((measurement, tags, fields)))
        self.alert_patch = mock.patch.object(
            accountant, "send_overseer", side_effect=self.alerts.append)
        self.metric_patch.start()
        self.alert_patch.start()

    def tearDown(self):
        self.alert_patch.stop()
        self.metric_patch.stop()
        self.sleep_patch.stop()
        accountant._orphan_alert_times.clear()
        reset_ownership_state()

    def _metrics_named(self, name):
        return [m for m in self.metrics if m[0] == name]

    def test_real_orphan_still_alerts_when_healthy(self):
        t = NOW - datetime.timedelta(days=1)
        client = FakeTradingClient(
            [FakeOrder("ETSY", "trend_bot-ETSY-1", t, filled_at=t)])
        accountant.detect_orphans(
            [FakePosition("ETSY"), FakePosition("ORPH")], client)
        orphaned = self._metrics_named("orphan_position")
        self.assertEqual(len(orphaned), 1)
        self.assertEqual(orphaned[0][1]["symbol"], "ORPH")
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("ORPH", self.alerts[0])

    def test_fetch_storm_with_cached_map_stands_down(self):
        """The 2026-07-15 shape: healthy book, warm map, then a timeout storm.
        Zero orphan pages for the 9-position book; one visible skip metric."""
        t = NOW - datetime.timedelta(days=1)
        good = FakeTradingClient([
            FakeOrder("ETSY", "trend_bot-ETSY-1", t, filled_at=t),
            FakeOrder("FTNT", "trend_bot-FTNT-2", t, filled_at=t),
        ])
        utils._build_order_based_map(good, held_symbols=["ETSY", "FTNT"])
        utils._ownership_cache_time -= 61

        storm = TimeoutTradingClient([], fail_first_n=10**9)
        accountant.detect_orphans(
            [FakePosition(s) for s in ("ETSY", "FTNT", "T", "PANW", "OKTA")], storm)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._metrics_named("orphan_position"), [])
        self.assertEqual(len(self._metrics_named("orphan_sweep")), 1)

    def test_fetch_storm_with_no_cache_stands_down(self):
        storm = TimeoutTradingClient([], fail_first_n=10**9)
        accountant.detect_orphans([FakePosition("ETSY")], storm)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._metrics_named("orphan_position"), [])
        self.assertEqual(len(self._metrics_named("orphan_sweep")), 1)

    def test_entry_time_fetch_failure_stands_down(self):
        """Map fetches clean, entry-times fetch hits the storm right after:
        no entry_time_missing spam for the whole book either."""
        t = NOW - datetime.timedelta(days=1)
        client = TimeoutTradingClient(
            [FakeOrder("ETSY", "trend_bot-ETSY-1", t, filled_at=t)], fail_from=2)
        accountant.detect_orphans([FakePosition("ETSY")], client)
        self.assertEqual(self.alerts, [])
        self.assertEqual(self._metrics_named("entry_time_missing"), [])
        self.assertEqual(self._metrics_named("orphan_position"), [])
        self.assertEqual(len(self._metrics_named("orphan_sweep")), 1)


if __name__ == "__main__":
    unittest.main()
