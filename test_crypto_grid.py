"""Regression: the Grid ledger only ever moves on a confirmed broker fill.

--- The defects this pins --------------------------------------------------

1. SUBMISSION IS NOT EXECUTION. `bot.submit()` returns an order, not an
   outcome: pending, rejected, canceled or partially filled are all normal.
   The first lot ledger recorded the REQUESTED quantity at the SUBMIT price
   when no fill was present, and retired a whole lot whenever a sell returned
   any order object. So an unfilled $50 buy became a real 0.5-unit lot with a
   fabricated cost basis, and a 1-unit sell that filled 0.25 erased the other
   0.75 from the books while the coins stayed in the account — where
   crypto_grid's own sell path, or moon_bot, could reach them.

2. A FAILED READ IS NOT A FLAT POSITION. `account_qty()` returned 0.0 for any
   exception, and reconciliation treated that as authoritative and PERSISTED
   it. One timeout retired every lot in the file. Before the ledger existed a
   failed read merely skipped a sell; afterwards it destroyed durable state.

3. FIFO WAS CLAIMED, NOT ENFORCED. Selecting the oldest *qualifying* lot skips
   underwater ones — specific-lot selection. Buy 1 at $200 then 1 at $90, sell
   at $120: execution books +$30 against the $90 lot while the accountant's
   FIFO books -$80 against the $200 lot. Same trade, two answers.

4. A PRICE CHECK IN FRONT OF A MARKET ORDER IS NOT A FLOOR. Spread, slippage
   and fees all land after the check. The floor has to be the order's limit.

5. A shared-symbol cancel helper cancelled EVERY open order on the symbol,
   including moon_bot's.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import crypto_grid


class FakeAPIError(Exception):
    def __init__(self, status_code):
        super().__init__(f"api error {status_code}")
        self.status_code = status_code


class FakeOrder:
    """An Alpaca order as the reconciler sees it."""
    def __init__(self, order_id="o1", filled_qty=0.0, filled_avg_price=0.0, status="new"):
        self.id = order_id
        self.filled_qty = str(filled_qty)
        self.filled_avg_price = str(filled_avg_price) if filled_avg_price else None
        self.status = status


def state_with(lots=None, pending=None):
    st = crypto_grid._empty_state()
    for sym, book in (lots or {}).items():
        st["lots"][sym] = [dict(l) for l in book]
    st["pending"] = dict(pending or {})
    return st


def lot(lot_id, qty, price):
    return {"lot_id": lot_id, "qty": qty, "price": price, "opened_at": "t0"}


def pending_buy(symbol="BTC/USD", qty=0.5, lot_id="L1", applied=0.0, price=100.0):
    return {"symbol": symbol, "side": "buy", "requested_qty": qty,
            "applied_qty": applied, "lot_id": lot_id, "limit_price": price,
            "submitted_at": 0.0}


def pending_sell(symbol="BTC/USD", qty=1.0, lot_id="L1", applied=0.0, price=120.0):
    return {"symbol": symbol, "side": "sell", "requested_qty": qty,
            "applied_qty": applied, "lot_id": lot_id, "limit_price": price,
            "submitted_at": 0.0}


def reconcile_against(state, orders):
    """Run reconcile_pending with the broker returning `orders` by id."""
    with mock.patch.object(crypto_grid.bot.trading_client, "get_order_by_id",
                           side_effect=lambda oid: orders[str(oid)]):
        return crypto_grid.reconcile_pending(state)


class BuyLifecycleTest(unittest.TestCase):
    """A lot exists only once the broker says the coins do."""

    def test_pending_buy_creates_no_lot(self):
        # THE bug: an unfilled order used to become inventory at submit price.
        st = state_with(pending={"o1": pending_buy()})
        reconcile_against(st, {"o1": FakeOrder("o1", filled_qty=0.0, status="new")})
        self.assertEqual(st["lots"]["BTC/USD"], [],
                         "a pending buy invented inventory")
        self.assertIn("o1", st["pending"], "the order stopped being tracked")

    def test_zero_fill_rejection_leaves_no_lot(self):
        st = state_with(pending={"o1": pending_buy()})
        reconcile_against(st, {"o1": FakeOrder("o1", filled_qty=0.0, status="rejected")})
        self.assertEqual(st["lots"]["BTC/USD"], [])
        self.assertNotIn("o1", st["pending"])

    def test_delayed_full_fill_books_the_broker_price(self):
        st = state_with(pending={"o1": pending_buy(qty=0.5, price=100.0)})
        # Cycle 1: nothing yet.
        reconcile_against(st, {"o1": FakeOrder("o1", 0.0, 0.0, "new")})
        self.assertEqual(st["lots"]["BTC/USD"], [])
        # Cycle 2: filled, at a price that is NOT the submit price.
        reconcile_against(st, {"o1": FakeOrder("o1", 0.5, 123.45, "filled")})
        book = st["lots"]["BTC/USD"]
        self.assertEqual(len(book), 1)
        self.assertAlmostEqual(book[0]["qty"], 0.5)
        self.assertAlmostEqual(book[0]["price"], 123.45)
        self.assertNotIn("o1", st["pending"])

    def test_partial_then_canceled_buy_keeps_only_what_filled(self):
        st = state_with(pending={"o1": pending_buy(qty=1.0)})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 100.0, "partially_filled")})
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.25)
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 100.0, "canceled")})
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.25)
        self.assertNotIn("o1", st["pending"])

    def test_repeated_reconciliation_is_idempotent(self):
        st = state_with(pending={"o1": pending_buy(qty=0.5)})
        order = FakeOrder("o1", 0.5, 100.0, "partially_filled")
        for _ in range(5):
            reconcile_against(st, {"o1": order})
        self.assertEqual(len(st["lots"]["BTC/USD"]), 1)
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.5)

    def test_a_pending_buy_is_recovered_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = state_with(pending={"o1": pending_buy(qty=0.5)})
                crypto_grid.save_state(st)
                reloaded = crypto_grid.load_state()          # "restart"
                self.assertIn("o1", reloaded["pending"])
                reconcile_against(reloaded, {"o1": FakeOrder("o1", 0.5, 100.0, "filled")})
                self.assertAlmostEqual(reloaded["lots"]["BTC/USD"][0]["qty"], 0.5)

    def test_an_unreadable_in_flight_order_suspends_entries(self):
        st = state_with(pending={"o1": pending_buy()})
        crypto_grid.resume_entries()
        with mock.patch.object(crypto_grid.bot.trading_client, "get_order_by_id",
                               side_effect=TimeoutError("boom")):
            crypto_grid.reconcile_pending(st)
        self.assertTrue(crypto_grid._entries_suspended)
        self.assertIn("o1", st["pending"], "an unreadable order was dropped")
        crypto_grid.resume_entries()


class SellLifecycleTest(unittest.TestCase):
    """A sell removes only what the broker confirms it sold."""

    def test_partial_sell_keeps_the_remainder(self):
        # THE bug: any returned order object used to retire the whole lot.
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 120.0, "partially_filled")})
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.75)

    def test_partial_then_canceled_sell_retains_the_unsold_remainder(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 120.0, "partially_filled")})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 120.0, "canceled")})
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.75)
        self.assertNotIn("o1", st["pending"])

    def test_full_fill_removes_the_lot(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        reconcile_against(st, {"o1": FakeOrder("o1", 1.0, 120.0, "filled")})
        self.assertEqual(st["lots"]["BTC/USD"], [])

    def test_repeated_reconciliation_does_not_double_subtract(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        order = FakeOrder("o1", 0.25, 120.0, "partially_filled")
        for _ in range(5):
            reconcile_against(st, {"o1": order})
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 0.75)

    def test_a_lot_with_a_resting_sell_is_not_offered_again(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        self.assertIsNone(crypto_grid.sellable_lot(st, "BTC/USD", 999.0)[1])


class FifoSelectionTest(unittest.TestCase):
    """Strict FIFO — the execution ledger and the reports agree."""

    def test_oldest_underwater_lot_blocks_the_sale(self):
        # The divergence case: an earlier version sold the $90 lot for +$30
        # while the accountant booked the $200 lot for -$80.
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 200.0), lot("L2", 1.0, 90.0)]})
        idx, chosen = crypto_grid.sellable_lot(st, "BTC/USD", 120.0)
        self.assertIsNone(chosen,
                          "skipped the oldest lot — that is specific-lot, not FIFO")

    def test_oldest_lot_is_sold_when_it_clears(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 90.0), lot("L2", 1.0, 95.0)]})
        idx, chosen = crypto_grid.sellable_lot(st, "BTC/USD", 120.0)
        self.assertEqual((idx, chosen["price"]), (0, 90.0))

    def test_boundary_hug_does_not_sell(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 0.5, 100.0)]})
        self.assertIsNone(crypto_grid.sellable_lot(st, "BTC/USD", 100.1)[1])

    def test_repeated_boundary_crossings_produce_no_sells(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 0.5, 100.0)]})
        sells = sum(1 for p in (100.05, 99.95, 100.10, 99.90, 100.02) * 20
                    if crypto_grid.sellable_lot(st, "BTC/USD", p)[1] is not None)
        self.assertEqual(sells, 0)

    def test_an_honest_zone_move_clears_the_floor(self):
        zone_pct = (2 * crypto_grid.GRID_WIDTH_PCT) / crypto_grid.GRID_LEVELS
        self.assertGreater(zone_pct, crypto_grid.REQUIRED_SPREAD_PCT)
        st = state_with(lots={"BTC/USD": [lot("L1", 0.5, 100.0)]})
        self.assertIsNotNone(
            crypto_grid.sellable_lot(st, "BTC/USD", 100.0 * (1 + zone_pct))[1])


class ExecutableFloorTest(unittest.TestCase):
    """The floor is the order's limit price, not a check before a market order."""

    def test_sell_is_a_limit_order_at_or_above_the_floor(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        captured = {}

        def fake_submit(order_data, **kwargs):
            captured["req"] = order_data
            return FakeOrder("o9")

        with mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "submit", side_effect=fake_submit):
            crypto_grid.grid_sell("BTC/USD", 120.0, 5, st, held=1.0, known=True)

        req = captured["req"]
        self.assertTrue(hasattr(req, "limit_price"), "grid sell is not a limit order")
        self.assertGreaterEqual(float(req.limit_price),
                                crypto_grid.sell_floor({"price": 100.0}) - 0.01)
        self.assertIn("o9", st["pending"])
        # Still one full lot: nothing leaves until a fill confirms it.
        self.assertAlmostEqual(st["lots"]["BTC/USD"][0]["qty"], 1.0)

    def test_sell_is_capped_by_the_shared_account_position(self):
        st = state_with(lots={"ETH/USD": [lot("L1", 1.0, 100.0)]})
        captured = {}
        with mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda od, **k: captured.setdefault("req", od) or FakeOrder("o1")):
            crypto_grid.grid_sell("ETH/USD", 200.0, 5, st, held=0.2, known=True)
        self.assertAlmostEqual(float(captured["req"].qty), 0.2)

    def test_an_unknown_position_read_blocks_selling(self):
        st = state_with(lots={"ETH/USD": [lot("L1", 1.0, 100.0)]})
        submitted = []
        with mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a)):
            crypto_grid.grid_sell("ETH/USD", 200.0, 5, st, held=0.0, known=False)
        self.assertEqual(submitted, [], "sold into an unreadable position")


class PositionReadTest(unittest.TestCase):
    """A failed read is unknown, not flat — and unknown may not retire lots."""

    def _account_qty_with(self, side_effect):
        with mock.patch.object(crypto_grid.bot.trading_client, "get_open_position",
                               side_effect=side_effect):
            return crypto_grid.account_qty("BTC/USD")

    def test_timeout_is_unknown(self):
        qty, known = self._account_qty_with(TimeoutError("read timed out"))
        self.assertFalse(known)

    def test_http_500_is_unknown(self):
        qty, known = self._account_qty_with(FakeAPIError(500))
        self.assertFalse(known)

    def test_404_is_a_confirmed_flat_position(self):
        qty, known = self._account_qty_with(FakeAPIError(404))
        self.assertTrue(known)
        self.assertEqual(qty, 0.0)

    def test_unknown_read_preserves_every_lot(self):
        # THE bug: one timeout retired the whole ledger and persisted it.
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        changed = crypto_grid.reconcile_lots(st, "BTC/USD", held=0.0, known=False)
        self.assertFalse(changed)
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 1.0)

    def test_confirmed_shortfall_does_retire_lots(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 90.0), lot("L2", 1.0, 95.0)]})
        self.assertTrue(crypto_grid.reconcile_lots(st, "BTC/USD", held=1.0, known=True))
        self.assertEqual(len(st["lots"]["BTC/USD"]), 1)
        self.assertEqual(st["lots"]["BTC/USD"][0]["price"], 95.0)


class LedgerPersistenceTest(unittest.TestCase):

    def setUp(self):
        crypto_grid.resume_entries()

    def tearDown(self):
        crypto_grid.resume_entries()

    def test_truncated_json_suspends_entries_rather_than_reading_flat(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with open(path, "w") as f:
                f.write('{"version": 2, "lots": {"BTC/USD": [{"qty": 1.0,')
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                crypto_grid.load_state()
        self.assertTrue(crypto_grid._entries_suspended,
                        "a corrupt ledger was treated as a tradable empty one")

    def test_missing_file_is_a_legitimate_flat_start(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(crypto_grid, "STATE_FILE", os.path.join(d, "nope.json")):
                st = crypto_grid.load_state()
        self.assertEqual(st["lots"]["BTC/USD"], [])
        self.assertFalse(crypto_grid._entries_suspended)

    def test_a_failed_write_suspends_entries(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        with mock.patch.object(crypto_grid, "STATE_FILE", "/proc/nonexistent/grid.json"):
            ok = crypto_grid.save_state(st)
        self.assertFalse(ok)
        self.assertTrue(crypto_grid._entries_suspended,
                        "a lost write left the bot trading on an unpersisted ledger")

    def test_save_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                            pending={"o1": pending_sell(lot_id="L1")})
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                self.assertTrue(crypto_grid.save_state(st))
                self.assertFalse(os.path.exists(path + ".tmp"))
                back = crypto_grid.load_state()
            self.assertAlmostEqual(back["lots"]["BTC/USD"][0]["qty"], 1.0)
            self.assertIn("o1", back["pending"])

    def test_suspended_entries_block_buys(self):
        st = state_with()
        submitted = []
        crypto_grid.suspend_entries("test")
        try:
            with mock.patch.object(crypto_grid.bot, "submit",
                                   side_effect=lambda *a, **k: submitted.append(a)):
                crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        finally:
            crypto_grid.resume_entries()
        self.assertEqual(submitted, [])


class BudgetTest(unittest.TestCase):
    """One symbol may not spend the whole allocation, nor the same dollars twice."""

    def test_budget_is_divided_across_symbols(self):
        with mock.patch.object(crypto_grid.utils, "get_budget_dollars", return_value=3000.0), \
             mock.patch.object(crypto_grid.bot, "equity", 100000.0):
            self.assertAlmostEqual(crypto_grid.per_symbol_budget(),
                                   3000.0 / len(crypto_grid.SYMBOLS))

    def test_fail_closed_budget_stays_zero(self):
        with mock.patch.object(crypto_grid.utils, "get_budget_dollars", return_value=0.0), \
             mock.patch.object(crypto_grid.bot, "equity", 100000.0):
            self.assertEqual(crypto_grid.per_symbol_budget(), 0.0)

    def test_outstanding_buys_are_reserved(self):
        # Without reservation, several in-flight buys each see the same room.
        st = state_with(pending={"o1": pending_buy(qty=5.0, price=100.0)})
        self.assertAlmostEqual(crypto_grid.outstanding_buy_notional(st, "BTC/USD"), 500.0)

    def test_a_pending_buy_blocks_a_second_one_over_budget(self):
        st = state_with(pending={"o1": pending_buy(qty=10.0, price=100.0)})
        submitted = []
        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a)):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        self.assertEqual(submitted, [], "spent budget already committed to a pending order")

    def test_a_slice_never_overshoots_the_remaining_share(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 9.6, 100.0)]})
        captured = {}
        with mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: captured.setdefault("qty", qty)), \
             mock.patch.object(crypto_grid.bot, "submit", return_value=FakeOrder("o1")):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        self.assertAlmostEqual(captured["qty"] * 100.0, 40.0, places=6)


class CancelScopeTest(unittest.TestCase):
    """Never cancel another bot's order on a shared symbol."""

    def test_only_this_bots_orders_are_cancelled(self):
        class O:
            def __init__(self, oid, coid, side):
                self.id, self.client_order_id, self.side = oid, coid, side

        orders = [O("1", "crypto_grid-BTCUSD-1", crypto_grid.OrderSide.SELL),
                  O("2", "moon_bot-BTCUSD-2", crypto_grid.OrderSide.SELL),
                  O("3", None, crypto_grid.OrderSide.SELL)]
        cancelled = []
        with mock.patch.object(crypto_grid.bot.trading_client, "get_orders", return_value=orders), \
             mock.patch.object(crypto_grid.bot.trading_client, "cancel_order_by_id",
                               side_effect=cancelled.append):
            crypto_grid.cancel_my_open_orders("BTC/USD", side=crypto_grid.OrderSide.SELL)
        self.assertEqual(cancelled, ["1"], "cancelled an order belonging to another bot")


class UnledgeredInventoryTest(unittest.TestCase):
    """Migration: coins with no lot are reported, not adopted."""

    def setUp(self):
        crypto_grid._unledgered_reported.clear()

    def test_unledgered_holdings_are_reported_once(self):
        st = state_with()
        with mock.patch.object(crypto_grid.logger, "warning") as warn:
            crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, True, st)
            crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, True, st)
        self.assertEqual(warn.call_count, 1)

    def test_an_unknown_read_reports_nothing(self):
        st = state_with()
        with mock.patch.object(crypto_grid.logger, "warning") as warn:
            crypto_grid.report_unledgered_inventory("ETH/USD", 0.0, False, st)
        warn.assert_not_called()

    def test_unledgered_coins_are_not_adopted_as_lots(self):
        st = state_with()
        crypto_grid.report_unledgered_inventory("ETH/USD", 1.454, True, st)
        self.assertEqual(st["lots"]["ETH/USD"], [])
        self.assertIsNone(crypto_grid.sellable_lot(st, "ETH/USD", 99999.0)[1])


class MoonLifecycleTest(unittest.TestCase):
    """moon_bot shares these coins, so it needs the same fill discipline."""

    def setUp(self):
        try:
            import crypto_breakout
        except Exception as e:  # pragma: no cover
            self.skipTest(f"crypto_breakout unavailable: {e}")
        self.moon = crypto_breakout

    def _reconcile(self, state, orders):
        with mock.patch.object(self.moon.bot.trading_client, "get_order_by_id",
                               side_effect=lambda oid: orders[str(oid)]):
            return self.moon.reconcile_pending(state)

    def test_pending_buy_records_no_quantity(self):
        st = self.moon._empty_state()
        st["pending"]["o1"] = {"symbol": "ETH/USD", "side": "buy",
                               "requested_qty": 3.944, "applied_qty": 0.0}
        self._reconcile(st, {"o1": FakeOrder("o1", 0.0, 0.0, "new")})
        self.assertEqual(self.moon.my_qty(st, "ETH/USD"), 0.0,
                         "an unfilled buy became tracked inventory")

    def test_partial_sell_keeps_the_remainder(self):
        st = self.moon._empty_state()
        st["qty"]["ETH/USD"] = 3.944
        st["pending"]["o1"] = {"symbol": "ETH/USD", "side": "sell",
                               "requested_qty": 3.944, "applied_qty": 0.0}
        self._reconcile(st, {"o1": FakeOrder("o1", 1.0, 1800.0, "partially_filled")})
        self.assertAlmostEqual(self.moon.my_qty(st, "ETH/USD"), 2.944,
                               msg="a partial sell zeroed the whole coin")

    def test_rejected_sell_leaves_the_ledger_alone(self):
        st = self.moon._empty_state()
        st["qty"]["ETH/USD"] = 3.944
        st["pending"]["o1"] = {"symbol": "ETH/USD", "side": "sell",
                               "requested_qty": 3.944, "applied_qty": 0.0}
        self._reconcile(st, {"o1": FakeOrder("o1", 0.0, 0.0, "rejected")})
        self.assertAlmostEqual(self.moon.my_qty(st, "ETH/USD"), 3.944)

    def test_v1_flat_state_file_is_carried_forward(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "moon.json")
            with open(path, "w") as f:
                json.dump({"ETH/USD": 3.944}, f)
            with mock.patch.object(self.moon, "STATE_FILE", path):
                st = self.moon.load_state()
        self.assertAlmostEqual(self.moon.my_qty(st, "ETH/USD"), 3.944)

    def test_entry_size_is_clipped_to_remaining_budget(self):
        captured = {}
        bot = self.moon.bot
        with mock.patch.object(self.moon, "get_donchian_levels",
                               return_value=(100.0, 50.0, 150.0)), \
             mock.patch.object(self.moon, "load_state", return_value=self.moon._empty_state()), \
             mock.patch.object(self.moon, "reconcile_pending", return_value=False), \
             mock.patch.object(self.moon, "save_state"), \
             mock.patch.object(self.moon.utils, "get_available_budget", return_value=1000.0), \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: captured.setdefault(sym, qty)), \
             mock.patch.object(bot, "submit", return_value=None):
            self.moon.cycle(bot)
        # 10% of $100k is $10,000, but only $1,000 of budget remains.
        self.assertAlmostEqual(captured["BTC/USD"] * 150.0, 1000.0, places=2)


if __name__ == "__main__":
    unittest.main()
