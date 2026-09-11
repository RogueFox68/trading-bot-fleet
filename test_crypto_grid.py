"""Regression: a grid sell is a round trip, not a boundary tick.

--- The defect this pins ---------------------------------------------------

The old bot bought on any downward zone crossing and sold on any upward one,
each at whatever price prevailed when the crossing was noticed. The grid's
"spacing" constrained the ZONE INDEX, never the transaction prices. Price
oscillating around a single boundary therefore produced buy/sell pairs cents
apart, each pair losing the round-trip fee, indefinitely — while the order
count climbed and the dashboard showed a busy, apparently profitable bot.
365 filled crypto_grid orders since July 13 sit behind that logic.

Nothing in the old code ever compared a sell price to the price it was
closing against, so there was no place the loss could be noticed.

Second defect: the per-symbol budget check compared ONE symbol's value
against the bot's WHOLE budget, so all three symbols could each spend it —
3x the allocation, with every individual check reading as compliant.

Third defect: `held_qty()` returned the shared account position. crypto_grid
and moon_bot hold the same coins, so the grid could (and did) sell moon's
inventory; an account-level FIFO reconstruction matched moon-origin ETH lots
against grid sells 113 times.
"""
import sys
import types
import unittest
from unittest import mock

import crypto_grid


class SellableLotTest(unittest.TestCase):
    """The guard that turns a zone rise into an actual profitable close."""

    def test_boundary_hug_does_not_sell(self):
        # THE bug, in one test: buy at 100, price ticks up 0.1% across the
        # same zone boundary. The old bot sold here and ate the fee.
        lots = {"BTC/USD": [{"qty": 0.5, "price": 100.0, "opened_at": "t0"}]}
        idx, lot = crypto_grid.sellable_lot(lots, "BTC/USD", 100.1)
        self.assertIsNone(lot, "a 0.1% move cleared the cost guard")

    def test_a_move_below_round_trip_cost_does_not_sell(self):
        lots = {"BTC/USD": [{"qty": 0.5, "price": 100.0, "opened_at": "t0"}]}
        just_under = 100.0 * (1 + crypto_grid.REQUIRED_SPREAD_PCT) - 0.01
        self.assertIsNone(crypto_grid.sellable_lot(lots, "BTC/USD", just_under)[1])

    def test_a_move_clearing_costs_plus_profit_sells(self):
        lots = {"BTC/USD": [{"qty": 0.5, "price": 100.0, "opened_at": "t0"}]}
        clears = 100.0 * (1 + crypto_grid.REQUIRED_SPREAD_PCT) + 0.01
        idx, lot = crypto_grid.sellable_lot(lots, "BTC/USD", clears)
        self.assertEqual(idx, 0)
        self.assertEqual(lot["price"], 100.0)

    def test_an_honest_zone_to_zone_move_clears_the_guard(self):
        # One zone is (2 * GRID_WIDTH_PCT) / GRID_LEVELS. The guard must not
        # be so wide that it blocks the strategy it is protecting.
        zone_pct = (2 * crypto_grid.GRID_WIDTH_PCT) / crypto_grid.GRID_LEVELS
        self.assertGreater(zone_pct, crypto_grid.REQUIRED_SPREAD_PCT)
        lots = {"BTC/USD": [{"qty": 0.5, "price": 100.0, "opened_at": "t0"}]}
        self.assertIsNotNone(
            crypto_grid.sellable_lot(lots, "BTC/USD", 100.0 * (1 + zone_pct))[1])

    def test_oldest_qualifying_lot_is_chosen(self):
        # FIFO, matching the accountant's and advisor's pairing order.
        lots = {"BTC/USD": [
            {"qty": 1.0, "price": 90.0, "opened_at": "t0"},
            {"qty": 1.0, "price": 95.0, "opened_at": "t1"},
        ]}
        idx, lot = crypto_grid.sellable_lot(lots, "BTC/USD", 120.0)
        self.assertEqual((idx, lot["price"]), (0, 90.0))

    def test_skips_underwater_lots_to_reach_a_qualifying_one(self):
        lots = {"BTC/USD": [
            {"qty": 1.0, "price": 200.0, "opened_at": "t0"},   # underwater
            {"qty": 1.0, "price": 90.0, "opened_at": "t1"},    # qualifies
        ]}
        idx, lot = crypto_grid.sellable_lot(lots, "BTC/USD", 120.0)
        self.assertEqual((idx, lot["price"]), (1, 90.0))

    def test_no_lots_means_no_sell(self):
        self.assertIsNone(crypto_grid.sellable_lot({"BTC/USD": []}, "BTC/USD", 999.0)[1])

    def test_required_spread_exceeds_assumed_round_trip_cost(self):
        self.assertGreater(crypto_grid.REQUIRED_SPREAD_PCT,
                           crypto_grid.ROUND_TRIP_COST_PCT)


class ChurnSimulationTest(unittest.TestCase):
    """Price oscillating around one boundary must not generate trades."""

    def test_repeated_boundary_crossings_produce_no_sells(self):
        lots = {"BTC/USD": [{"qty": 0.5, "price": 100.0, "opened_at": "t0"}]}
        sells = 0
        for price in (100.05, 99.95, 100.10, 99.90, 100.02) * 20:
            if crypto_grid.sellable_lot(lots, "BTC/USD", price)[1] is not None:
                sells += 1
        self.assertEqual(sells, 0, "boundary churn still generates sells")


class LedgerTest(unittest.TestCase):
    """The grid's own inventory, isolated from moon_bot's shared coins."""

    def test_ledger_qty_and_value(self):
        lots = {"ETH/USD": [{"qty": 1.0, "price": 10.0}, {"qty": 2.0, "price": 20.0}]}
        self.assertEqual(crypto_grid.ledger_qty(lots, "ETH/USD"), 3.0)
        self.assertEqual(crypto_grid.ledger_value(lots, "ETH/USD", 5.0), 15.0)

    def test_ledger_ignores_coins_the_grid_did_not_buy(self):
        # moon_bot's 3.944 ETH sits in the same Alpaca position. The grid's
        # inventory is its ledger, not the account.
        lots = {"ETH/USD": [{"qty": 0.5, "price": 100.0}]}
        self.assertEqual(crypto_grid.ledger_qty(lots, "ETH/USD"), 0.5)

    def test_sell_is_capped_by_the_shared_account_position(self):
        # The ledger claims 1.0 but the account only holds 0.2 — selling the
        # full lot would reach into somebody else's coins (or fail).
        lots = {"ETH/USD": [{"qty": 1.0, "price": 100.0, "opened_at": "t0"}]}
        submitted = []
        with mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a)), \
             mock.patch.object(crypto_grid, "cancel_open_orders_for_symbol"), \
             mock.patch.object(crypto_grid.bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: {"qty": qty}), \
             mock.patch.object(crypto_grid, "save_lots"):
            crypto_grid.grid_sell("ETH/USD", 200.0, 5, lots, held=0.2)
        self.assertEqual(len(submitted), 1)
        self.assertAlmostEqual(submitted[0][0]["qty"], 0.2)

    def test_reconcile_trims_oldest_lots_first(self):
        lots = {"BTC/USD": [
            {"qty": 1.0, "price": 90.0, "opened_at": "t0"},
            {"qty": 1.0, "price": 95.0, "opened_at": "t1"},
        ]}
        self.assertTrue(crypto_grid.reconcile_lots(lots, "BTC/USD", account_qty=1.0))
        self.assertEqual(len(lots["BTC/USD"]), 1)
        self.assertEqual(lots["BTC/USD"][0]["price"], 95.0)

    def test_reconcile_is_a_noop_when_the_account_covers_the_ledger(self):
        lots = {"BTC/USD": [{"qty": 1.0, "price": 90.0, "opened_at": "t0"}]}
        self.assertFalse(crypto_grid.reconcile_lots(lots, "BTC/USD", account_qty=5.0))
        self.assertEqual(len(lots["BTC/USD"]), 1)

    def test_reconcile_to_zero_empties_the_book(self):
        lots = {"BTC/USD": [{"qty": 1.0, "price": 90.0, "opened_at": "t0"}]}
        crypto_grid.reconcile_lots(lots, "BTC/USD", account_qty=0.0)
        self.assertEqual(lots["BTC/USD"], [])


class PerSymbolBudgetTest(unittest.TestCase):
    """One symbol may not spend the whole bot's allocation."""

    def test_budget_is_divided_across_symbols(self):
        with mock.patch.object(crypto_grid.utils, "get_budget_dollars", return_value=3000.0), \
             mock.patch.object(crypto_grid.bot, "equity", 100000.0):
            self.assertAlmostEqual(crypto_grid.per_symbol_budget(),
                                   3000.0 / len(crypto_grid.SYMBOLS))

    def test_fail_closed_budget_stays_zero(self):
        with mock.patch.object(crypto_grid.utils, "get_budget_dollars", return_value=0.0), \
             mock.patch.object(crypto_grid.bot, "equity", 100000.0):
            self.assertEqual(crypto_grid.per_symbol_budget(), 0.0)

    def test_buy_is_blocked_once_the_symbol_fills_its_share(self):
        # Ledger already holds $1000 of BTC against a $1000 per-symbol share.
        lots = {"BTC/USD": [{"qty": 10.0, "price": 100.0, "opened_at": "t0"}]}
        submitted = []
        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a)):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, lots)
        self.assertEqual(submitted, [], "symbol exceeded its share of the budget")

    def test_a_slice_never_overshoots_the_remaining_share(self):
        # $960 of a $1000 share used: the slice must be $40, not $50.
        lots = {"BTC/USD": [{"qty": 9.6, "price": 100.0, "opened_at": "t0"}]}
        captured = {}

        class FakeOrder:
            filled_qty = 0
            filled_avg_price = 0

        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_open_orders_for_symbol"), \
             mock.patch.object(crypto_grid, "save_lots"), \
             mock.patch.object(crypto_grid.bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: captured.setdefault("qty", qty)), \
             mock.patch.object(crypto_grid.bot, "submit", return_value=FakeOrder()):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, lots)
        self.assertAlmostEqual(captured["qty"] * 100.0, 40.0, places=6)


class BuyRecordsBasisTest(unittest.TestCase):
    """Every buy must leave a lot, or the sell guard has nothing to test."""

    def test_lot_records_the_broker_fill_price(self):
        lots = {"BTC/USD": []}

        class FilledOrder:
            filled_qty = "0.4"
            filled_avg_price = "123.45"

        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_open_orders_for_symbol"), \
             mock.patch.object(crypto_grid, "save_lots"), \
             mock.patch.object(crypto_grid.bot, "market_order", return_value=object()), \
             mock.patch.object(crypto_grid.bot, "submit", return_value=FilledOrder()):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, lots)
        self.assertEqual(len(lots["BTC/USD"]), 1)
        self.assertAlmostEqual(lots["BTC/USD"][0]["price"], 123.45)
        self.assertAlmostEqual(lots["BTC/USD"][0]["qty"], 0.4)

    def test_unfilled_order_falls_back_to_the_submit_price(self):
        lots = {"BTC/USD": []}

        class PendingOrder:
            filled_qty = 0
            filled_avg_price = None

        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_open_orders_for_symbol"), \
             mock.patch.object(crypto_grid, "save_lots"), \
             mock.patch.object(crypto_grid.bot, "market_order", return_value=object()), \
             mock.patch.object(crypto_grid.bot, "submit", return_value=PendingOrder()):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, lots)
        self.assertAlmostEqual(lots["BTC/USD"][0]["price"], 100.0)

    def test_a_refused_order_records_no_lot(self):
        # bot.submit returns None on failure; a phantom lot would let a later
        # sell "close" inventory that was never bought.
        lots = {"BTC/USD": []}
        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_open_orders_for_symbol"), \
             mock.patch.object(crypto_grid.bot, "market_order", return_value=object()), \
             mock.patch.object(crypto_grid.bot, "submit", return_value=None):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, lots)
        self.assertEqual(lots["BTC/USD"], [])


class UnledgeredInventoryTest(unittest.TestCase):
    """Migration: coins with no lot are reported, not adopted."""

    def setUp(self):
        crypto_grid._unledgered_reported.clear()

    def test_unledgered_holdings_are_reported_once(self):
        lots = {"ETH/USD": []}
        with mock.patch.object(crypto_grid.logger, "warning") as warn:
            crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, lots)
            crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, lots)
        self.assertEqual(warn.call_count, 1)

    def test_a_fully_ledgered_symbol_is_silent(self):
        lots = {"ETH/USD": [{"qty": 1.0, "price": 100.0, "opened_at": "t0"}]}
        with mock.patch.object(crypto_grid.logger, "warning") as warn:
            crypto_grid.report_unledgered_inventory("ETH/USD", 1.0, lots)
        warn.assert_not_called()

    def test_unledgered_coins_are_not_adopted_as_lots(self):
        # Adopting them would put a made-up cost basis into the sell guard.
        lots = {"ETH/USD": []}
        crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, lots)
        self.assertEqual(lots["ETH/USD"], [])
        self.assertIsNone(crypto_grid.sellable_lot(lots, "ETH/USD", 99999.0)[1])


class MoonBudgetClipTest(unittest.TestCase):
    """moon_bot's 10%-of-equity target must be clipped to the CFO budget."""

    def test_entry_size_is_clipped_to_remaining_budget(self):
        try:
            import crypto_breakout
        except Exception as e:  # pragma: no cover
            self.skipTest(f"crypto_breakout unavailable: {e}")

        captured = {}
        bot = crypto_breakout.bot
        with mock.patch.object(crypto_breakout, "get_donchian_levels",
                               return_value=(100.0, 50.0, 150.0)), \
             mock.patch.object(crypto_breakout, "load_state", return_value={}), \
             mock.patch.object(crypto_breakout, "save_state"), \
             mock.patch.object(crypto_breakout.utils, "get_available_budget",
                               return_value=1000.0), \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: captured.setdefault(sym, qty)), \
             mock.patch.object(bot, "submit", return_value=None):
            crypto_breakout.cycle(bot)
        # 10% of $100k is $10,000, but only $1,000 of budget remains.
        self.assertAlmostEqual(captured["BTC/USD"] * 150.0, 1000.0, places=2)


if __name__ == "__main__":
    unittest.main()
