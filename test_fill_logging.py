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


if __name__ == "__main__":
    unittest.main()
