"""Regression tests for the position-orphaning bug.

An equity position whose opening order scrolled out of the most-recent-500 order
window (flooded by high-frequency crypto orders) used to lose both its ownership
tag and its entry timestamp — silently disabling stop-loss / max-hold exits.
These tests replay >500 intervening orders after an open and assert that entry
time and ownership still resolve, and that a position with no order at all is
detectable as an orphan.

Run: python -m unittest test_orphan_resolution -v
"""
import datetime
import unittest

import utils
import tiered_hold


UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC)


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
        # Ownership cache is module-global; clear it between tests.
        utils._ownership_cache = {}
        utils._ownership_cache_time = 0
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
        utils._ownership_cache = {}
        utils._ownership_cache_time = 0

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
        utils._ownership_cache = {}
        utils._ownership_cache_time = 0

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
        from alpaca.trading.enums import AssetClass
        client = FakeTradingClient([])
        owner = utils.get_bot_owner("ORPH", AssetClass.US_EQUITY, client)
        # None = unowned/quarantined: every bot filters holdings by its own
        # name, so nothing manages (or liquidates) this position.
        self.assertIsNone(owner)


if __name__ == "__main__":
    unittest.main()
