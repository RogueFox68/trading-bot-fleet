"""Regression: the daily loss cap must not trap the fleet inside its losses.

--- The defect this pins ---------------------------------------------------

`utils.submit_and_log_order` rejected EVERY order once the day's P&L breached
MAX_DAILY_LOSS, before it had looked at what the order does. So a fleet down
$5,000 could no longer sell a losing long, cover a short, or buy back a short
option — the exact actions that stop the loss growing. A circuit breaker that
locks you inside the position is the opposite of a risk control, and the EOD
exit reordering did nothing about it: the block happened below the bots, in
the shared submit path.

`exposure_increasing_qty` is the shared classifier. The loss cap and the
notional/exposure caps both use it, so the two cannot drift apart about what
"closing" means.

Also covered: pending BUY orders reserve their capital against the CFO budget.
Crypto was missing from that reservation entirely and equity MARKET buys
slipped through it (no limit_price), so several unfilled entries could each
see the same headroom and spend it.
"""
import unittest
from unittest import mock

from alpaca.trading.enums import AssetClass, OrderSide
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest

import utils


class FakePosition:
    def __init__(self, symbol, qty, current_price=100.0):
        self.symbol = symbol
        self.qty = str(qty)
        self.current_price = str(current_price)


def order(symbol, qty, side):
    return MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force="day")


class ExposureClassifierTest(unittest.TestCase):

    def test_selling_a_long_opens_nothing(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("AAPL", 10, OrderSide.SELL),
                                          [FakePosition("AAPL", 10)]), 0.0)

    def test_partial_close_opens_nothing(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("AAPL", 4, OrderSide.SELL),
                                          [FakePosition("AAPL", 10)]), 0.0)

    def test_covering_a_short_opens_nothing(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("AAPL", 10, OrderSide.BUY),
                                          [FakePosition("AAPL", -10)]), 0.0)

    def test_buying_to_close_a_short_option_opens_nothing(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("PAAS260918P00015000", 1, OrderSide.BUY),
                                          [FakePosition("PAAS260918P00015000", -1)]), 0.0)

    def test_a_fresh_long_opens_everything(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("AAPL", 10, OrderSide.BUY), []), 10.0)

    def test_an_over_sell_only_opens_the_flipping_part(self):
        self.assertEqual(
            utils.exposure_increasing_qty(order("AAPL", 15, OrderSide.SELL),
                                          [FakePosition("AAPL", 10)]), 5.0)

    def test_crypto_symbol_spelling_is_normalised(self):
        # Alpaca reports the position as BTCUSD while orders use BTC/USD; a
        # mismatch here would read every crypto close as a fresh short.
        self.assertEqual(
            utils.exposure_increasing_qty(order("BTC/USD", 0.5, OrderSide.SELL),
                                          [FakePosition("BTCUSD", 1.0)]), 0.0)


class DailyLossCapTest(unittest.TestCase):
    """Breached cap: entries blocked, exits allowed."""

    def setUp(self):
        utils._uncontained_override_warned = False
        self.addCleanup(setattr, utils, "_uncontained_override_warned", False)

    def _submit(self, order_data, positions):
        account = mock.Mock(equity="60000", last_equity="70000")   # -$10k today
        client = mock.Mock()
        client.get_account.return_value = account
        client.get_all_positions.return_value = positions
        client.submit_order.return_value = mock.Mock(
            id="o1", symbol=getattr(order_data, "symbol", ""),
            side=OrderSide.SELL, qty="1",
            status=mock.Mock(value="accepted"), type=mock.Mock(value="market"))
        with mock.patch.object(utils, "assert_order_allowed_here"), \
             mock.patch.object(utils, "_log_fill_to_influx"), \
             mock.patch.object(utils, "_safety_price_client",
                               return_value=mock.Mock(**{
                                   "get_stock_latest_trade.return_value": {
                                       getattr(order_data, "symbol", ""): mock.Mock(price=100.0)}})), \
             mock.patch("time.sleep"):
            return utils.submit_and_log_order(client, order_data, utils.logger)

    def test_the_cap_still_blocks_a_new_entry(self):
        with self.assertRaises(Exception) as ctx:
            self._submit(order("AAPL", 10, OrderSide.BUY), [])
        self.assertIn("Daily Loss Cap", str(ctx.exception))

    def test_a_long_exit_is_allowed_through(self):
        # THE bug: this raised, so a losing long could not be sold.
        self._submit(order("AAPL", 10, OrderSide.SELL), [FakePosition("AAPL", 10)])

    def test_a_short_cover_is_allowed_through(self):
        self._submit(order("AAPL", 10, OrderSide.BUY), [FakePosition("AAPL", -10)])

    def test_an_option_buy_to_close_is_allowed_through(self):
        self._submit(order("PAAS260918P00015000", 1, OrderSide.BUY),
                     [FakePosition("PAAS260918P00015000", -1)])

    def test_a_crypto_close_is_allowed_through(self):
        self._submit(order("BTC/USD", 0.5, OrderSide.SELL), [FakePosition("BTCUSD", 1.0)])

    def test_an_over_sell_that_flips_into_a_short_is_still_blocked(self):
        with self.assertRaises(Exception) as ctx:
            self._submit(order("AAPL", 50, OrderSide.SELL), [FakePosition("AAPL", 10)])
        self.assertIn("Daily Loss Cap", str(ctx.exception))


class PendingCapitalReservationTest(unittest.TestCase):
    """Unfilled buys must hold their dollars, whatever the asset class."""

    def test_limit_price_prices_the_order(self):
        o = mock.Mock(limit_price="50", notional=None, filled_avg_price=None,
                      qty="10", symbol="AAPL")
        self.assertAlmostEqual(utils._pending_unit_price(o, []), 50.0)

    def test_dollar_notional_prices_the_order(self):
        o = mock.Mock(limit_price=None, notional="500", filled_avg_price=None,
                      qty="10", symbol="AAPL")
        self.assertAlmostEqual(utils._pending_unit_price(o, []), 50.0)

    def test_a_partial_fill_prices_the_remainder(self):
        o = mock.Mock(limit_price=None, notional=None, filled_avg_price="48.5",
                      qty="10", symbol="AAPL")
        self.assertAlmostEqual(utils._pending_unit_price(o, []), 48.5)

    def test_a_held_position_prices_a_market_order(self):
        # The crypto case: a market BTC buy with no limit and no fill yet.
        o = mock.Mock(limit_price=None, notional=None, filled_avg_price=None,
                      qty="0.5", symbol="BTC/USD")
        self.assertAlmostEqual(
            utils._pending_unit_price(o, [FakePosition("BTCUSD", 1.0, current_price=60000.0)]),
            60000.0)

    def test_an_unpriceable_order_reports_zero_rather_than_guessing(self):
        o = mock.Mock(limit_price=None, notional=None, filled_avg_price=None,
                      qty="0.5", symbol="BTC/USD")
        self.assertEqual(utils._pending_unit_price(o, []), 0.0)

    def test_a_pending_crypto_buy_consumes_budget(self):
        # Previously the pending loop skipped crypto entirely, so an unfilled
        # entry reserved nothing and the next one saw the same headroom.
        pending = mock.Mock(
            client_order_id="crypto_grid-BTCUSD-1", asset_class=AssetClass.CRYPTO,
            side=OrderSide.BUY, qty="0.5", filled_qty="0", limit_price="1000",
            notional=None, filled_avg_price=None, symbol="BTC/USD", id="o1")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000")
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [pending]
        with mock.patch.object(utils, "get_budget_dollars", return_value=1000.0):
            ok, budget, used = utils.check_budget_details("crypto_grid", client)
        self.assertAlmostEqual(used, 500.0, msg="a pending crypto buy reserved nothing")

    def test_an_unpriceable_pending_buy_fails_closed(self):
        """A fresh quantity-based MARKET buy has nothing to price it by.

        No limit_price, no notional, no partial fill, no existing position —
        precisely the new-entry case. Warning and continuing reserved ZERO, so
        the next symbol saw the same headroom and spent it again. Refusing
        further exposure is the honest answer until the order resolves.
        """
        pending = mock.Mock(
            client_order_id="moon_bot-BTCUSD-1", asset_class=AssetClass.CRYPTO,
            side=OrderSide.BUY, qty="0.5", filled_qty="0", limit_price=None,
            notional=None, filled_avg_price=None, symbol="BTC/USD", id="o1")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000")
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [pending]
        with mock.patch.object(utils, "get_budget_dollars", return_value=1000.0):
            ok, budget, used = utils.check_budget_details("moon_bot", client)
        self.assertFalse(ok, "an unpriceable pending buy left headroom for another entry")
        self.assertEqual(utils.get_available_budget("moon_bot", client), 0.0)

    def test_a_second_symbol_cannot_reuse_the_first_entrys_dollars(self):
        # Two different new symbols, first market buy accepted and completely
        # unfilled, no existing positions.
        pending = mock.Mock(
            client_order_id="moon_bot-BTCUSD-1", asset_class=AssetClass.CRYPTO,
            side=OrderSide.BUY, qty="0.5", filled_qty="0", limit_price=None,
            notional=None, filled_avg_price=None, symbol="BTC/USD", id="o1")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000")
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [pending]
        with mock.patch.object(utils, "get_budget_dollars", return_value=1000.0):
            # The ETH/USD entry asks the same question and must be refused.
            self.assertFalse(utils.check_budget("moon_bot", client))

    def test_an_equity_market_buy_is_priced_from_a_quote(self):
        # Equities have a price client, so they resolve rather than fail closed.
        pending = mock.Mock(
            client_order_id="trend_bot-AAPL-1", asset_class=AssetClass.US_EQUITY,
            side=OrderSide.BUY, qty="10", filled_qty="0", limit_price=None,
            notional=None, filled_avg_price=None, symbol="AAPL", id="o2")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000")
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [pending]
        quote_client = mock.Mock(**{
            "get_stock_latest_trade.return_value": {"AAPL": mock.Mock(price=50.0)}})
        with mock.patch.object(utils, "get_budget_dollars", return_value=1000.0), \
             mock.patch.object(utils, "_safety_price_client", return_value=quote_client):
            ok, budget, used = utils.check_budget_details("trend_bot", client)
        self.assertTrue(ok)
        self.assertAlmostEqual(used, 500.0)

    def test_a_pending_equity_market_buy_consumes_budget(self):
        pending = mock.Mock(
            client_order_id="trend_bot-AAPL-1", asset_class=AssetClass.US_EQUITY,
            side=OrderSide.BUY, qty="10", filled_qty="0", limit_price=None,
            notional="500", filled_avg_price=None, symbol="AAPL", id="o2")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000")
        client.get_all_positions.return_value = []
        client.get_orders.return_value = [pending]
        with mock.patch.object(utils, "get_budget_dollars", return_value=1000.0):
            ok, budget, used = utils.check_budget_details("trend_bot", client)
        self.assertAlmostEqual(used, 500.0,
                               msg="an equity MARKET buy reserved nothing (no limit_price)")


if __name__ == "__main__":
    unittest.main()
