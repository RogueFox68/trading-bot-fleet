"""Regression tests for fill-row stamping and wheel close pricing.

_log_fill_to_influx must stamp rows at the broker fill time floored to the
millisecond: Alpaca's single-order (get_order_by_id) and order-list
(get_orders) endpoints serialize filled_at with sub-µs differences, so raw-ns
stamps turned the submit-time write and the reconciled write into duplicate
points ~1µs apart (inflated SUM(qty) for PAAS/T/BABA on 2026-07-06).

marketable_close_price must cross the spread: the midpoint-only close limit
(with a spread-width veto) is why every 6/30-7/06 roll-close died unfilled
and the PAAS put rode DTE-0 into assignment.

Run: python -m unittest test_fill_logging -v
"""
import datetime
import unittest
from types import SimpleNamespace
from unittest import mock

import utils

UTC = datetime.timezone.utc


class FakeFilledOrder:
    def __init__(self, filled_at):
        self.client_order_id = "trend_bot-PAAS-1783346453"
        self.symbol = "PAAS"
        self.side = utils.OrderSide.SELL
        self.filled_avg_price = 45.6916
        self.filled_qty = 100
        self.filled_at = filled_at


def captured_ns(order, reason=""):
    """Run _log_fill_to_influx against a mocked InfluxDB and return the
    nanosecond timestamp it stamped on the line-protocol write."""
    with mock.patch.object(utils.requests, "post") as post:
        post.return_value = mock.Mock(status_code=204)
        utils._log_fill_to_influx(order, utils.logger, reason=reason)
        line = post.call_args.kwargs["data"]
    return int(line.rsplit(" ", 1)[1])


class MsFlooredTimestampTest(unittest.TestCase):
    def test_submicrosecond_serializations_land_on_the_same_point(self):
        # The observed failure: the two endpoints round the same fill to
        # adjacent microseconds. Both must floor to the same ms stamp so the
        # second write overwrites the first instead of duplicating it.
        a = datetime.datetime(2026, 7, 6, 14, 0, 55, 123456, tzinfo=UTC)
        b = datetime.datetime(2026, 7, 6, 14, 0, 55, 123457, tzinfo=UTC)
        self.assertEqual(captured_ns(FakeFilledOrder(a), reason="Bearish Crossover"),
                         captured_ns(FakeFilledOrder(b)))

    def test_stamp_is_millisecond_aligned(self):
        t = datetime.datetime(2026, 7, 6, 14, 0, 55, 987654, tzinfo=UTC)
        self.assertEqual(captured_ns(FakeFilledOrder(t)) % 1_000_000, 0)

    def test_distinct_fills_stay_distinct(self):
        a = datetime.datetime(2026, 7, 6, 14, 0, 55, 123000, tzinfo=UTC)
        b = datetime.datetime(2026, 7, 6, 14, 0, 57, 500000, tzinfo=UTC)
        self.assertNotEqual(captured_ns(FakeFilledOrder(a)),
                            captured_ns(FakeFilledOrder(b)))


class MarketableClosePriceTest(unittest.TestCase):
    """The ladder must reach the ask even on spreads the entry guard rejects."""

    def test_ladder_escalates_from_midpoint_to_the_ask(self):
        import wheel_bot
        # 27% spread — calculate_smart_price refuses this ("spread too wide"),
        # which is exactly how PAAS could never be closed on expiry day.
        quote = mock.Mock(bid_price=3.8, ask_price=5.2)
        self.assertEqual(wheel_bot.marketable_close_price(quote, 0.0), 4.5)
        self.assertEqual(wheel_bot.marketable_close_price(quote, 0.5), 4.85)
        self.assertEqual(wheel_bot.marketable_close_price(quote, 1.0), 5.2)

    def test_no_ask_returns_none(self):
        import wheel_bot
        quote = mock.Mock(bid_price=0.0, ask_price=0.0)
        self.assertIsNone(wheel_bot.marketable_close_price(quote, 1.0))


# --- reconcile_fills must page, not read a single 500-order window ---
class _ReconcileOrder:
    def __init__(self, symbol, client_order_id, submitted_at, filled_at, side):
        self.id = client_order_id  # unique -> feeds _fetch_orders_covering dedup
        self.symbol = symbol
        self.client_order_id = client_order_id
        self.submitted_at = submitted_at
        self.filled_at = filled_at
        self.side = side
        self.filled_avg_price = 1.23
        self.filled_qty = 1


class _ReconcileClient:
    """Alpaca get_orders stand-in honoring status/after/until/limit, newest-first."""
    def __init__(self, orders):
        self._orders = sorted(orders, key=lambda o: o.submitted_at, reverse=True)
        self.call_count = 0

    def get_orders(self, filter=None):
        self.call_count += 1
        after = getattr(filter, "after", None)
        until = getattr(filter, "until", None)
        limit = getattr(filter, "limit", 500) or 500
        pool = self._orders
        if after is not None:
            pool = [o for o in pool if o.submitted_at > after]
        if until is not None:
            pool = [o for o in pool if o.submitted_at < until]
        return pool[:limit]


class ReconcileFillsPagingTest(unittest.TestCase):
    """A reconciled bot's late fill buried behind a crypto-order flood must still
    be reconciled — the same bounded-window trap that orphaned aged positions."""

    def _client(self):
        now = datetime.datetime.now(UTC)
        # Wheel option filled an hour ago but SUBMITTED 3 days ago, so it sorts 3
        # days deep — below every newer order.
        orders = [_ReconcileOrder(
            "PAAS260731P00045000", "wheel_bot-PAAS260731P00045000-1780000000",
            now - datetime.timedelta(days=3), now - datetime.timedelta(hours=1),
            utils.OrderSide.SELL)]
        # 2000 crypto orders in the last ~33h bury it far past a single 500 window.
        for i in range(2000):
            ts = now - datetime.timedelta(minutes=i)
            orders.append(_ReconcileOrder(
                "BTC/USD", f"crypto_grid-BTC/USD-{i}", ts, ts, utils.OrderSide.BUY))
        return _ReconcileClient(orders)

    def test_single_500_window_would_miss_it(self):
        """Documents the bug: the newest 500 closed orders are all crypto."""
        from alpaca.trading.requests import GetOrdersRequest
        page = self._client().get_orders(
            filter=GetOrdersRequest(status="closed", limit=500))
        self.assertTrue(page and all(o.symbol == "BTC/USD" for o in page))

    def test_aged_reconciled_fill_is_paged_and_written(self):
        client = self._client()
        written = []
        with mock.patch.object(
                utils, "_log_fill_to_influx",
                side_effect=lambda o, lg, action=None, reason="": written.append(o.symbol)):
            n = utils.reconcile_fills(client, utils.logger)
        self.assertGreater(client.call_count, 1)                 # actually paged
        self.assertIn("PAAS260731P00045000", written)            # aged fill caught
        self.assertNotIn("BTC/USD", written)                     # crypto not reconciled
        self.assertEqual(n, 1)


# --- close_option_position must not re-buy the original qty after a partial ---
class _CloseOrder:
    def __init__(self, oid, status_value, filled_qty):
        self.id = oid
        self.status = SimpleNamespace(value=status_value)
        self.filled_qty = filled_qty


class _CloseClient:
    """Drives the close ladder: each submitted rung maps to a scripted plan
    (poll status + cumulative filled_qty). A canceled rung reports 'canceled'."""
    def __init__(self, plans):
        self.plans = plans      # one dict per rung, in submission order
        self.by_oid = {}
        self.canceled = set()
        self.submitted = []     # (oid, qty submitted)

    def register(self, oid):
        self.by_oid[oid] = self.plans[len(self.by_oid)]

    def get_order_by_id(self, oid):
        plan = self.by_oid[oid]
        status = "canceled" if oid in self.canceled else plan["poll"]
        return _CloseOrder(oid, status, plan["filled_qty"])

    def cancel_order_by_id(self, oid):
        self.canceled.add(oid)


def _make_submit(client):
    def fake_submit(tc, req, logger, reason=""):
        oid = f"o{len(client.by_oid) + 1}"
        client.submitted.append((oid, int(req.qty)))
        client.register(oid)
        return SimpleNamespace(id=oid)
    return fake_submit


class ClosePositionPartialFillTest(unittest.TestCase):
    def _run(self, qty, plans):
        import wheel_bot
        client = _CloseClient(plans)
        active = SimpleNamespace(symbol="PAAS260731P00045000", qty=str(-qty))
        quote = mock.Mock(bid_price=1.0, ask_price=1.2)
        with mock.patch.object(wheel_bot, "trading_client", client), \
             mock.patch.object(wheel_bot, "get_option_data", return_value=quote), \
             mock.patch.object(wheel_bot.utils, "submit_and_log_order",
                               side_effect=_make_submit(client)), \
             mock.patch.object(wheel_bot.time, "sleep"):
            price = wheel_bot.close_option_position(active, "rollcls", "test")
        return client, price

    def test_partial_fill_resubmits_only_remaining(self):
        # Short 5: rung 1 partially fills 2 then times out; rung 2 fills the 3 left.
        client, price = self._run(5, [
            {"poll": "partially_filled", "filled_qty": 2},
            {"poll": "filled", "filled_qty": 3},
        ])
        self.assertEqual(client.submitted[0][1], 5)   # rung 1 tried the full 5
        self.assertEqual(client.submitted[1][1], 3)   # rung 2 tried ONLY the 3 left
        self.assertIsNotNone(price)

    def test_no_partial_keeps_full_qty(self):
        # Rung 1 never fills (no partial) -> rung 2 re-sends the full 5.
        client, price = self._run(5, [
            {"poll": "new", "filled_qty": 0},
            {"poll": "filled", "filled_qty": 5},
        ])
        self.assertEqual(client.submitted[0][1], 5)
        self.assertEqual(client.submitted[1][1], 5)
        self.assertIsNotNone(price)

    def test_partials_across_rungs_complete_close(self):
        # 5 = 2 (rung 1) + 3 (rung 2 partial) -> fully closed, no escalation to ask.
        client, price = self._run(5, [
            {"poll": "partially_filled", "filled_qty": 2},
            {"poll": "partially_filled", "filled_qty": 3},
        ])
        self.assertEqual([q for _, q in client.submitted], [5, 3])
        self.assertEqual(len(client.submitted), 2)    # no third rung needed
        self.assertIsNotNone(price)


if __name__ == "__main__":
    unittest.main()
