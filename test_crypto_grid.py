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
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import crypto_grid
import utils


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


def lot(lot_id, qty, price, disposed=0.0):
    """A confirmed lot. Quantity held = acquired - disposed; never stored net."""
    return {"lot_id": lot_id, "acquired_qty": qty, "disposed_qty": disposed,
            "price": price, "opened_at": "t0"}


def held(state, symbol="BTC/USD", index=0):
    return crypto_grid.lot_qty(state["lots"][symbol][index])


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
        changed, _settled = crypto_grid.reconcile_pending(state)
        return changed


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
        self.assertAlmostEqual(crypto_grid.lot_qty(book[0]), 0.5)
        self.assertAlmostEqual(book[0]["price"], 123.45)
        self.assertNotIn("o1", st["pending"])

    def test_partial_then_canceled_buy_keeps_only_what_filled(self):
        st = state_with(pending={"o1": pending_buy(qty=1.0)})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 100.0, "partially_filled")})
        self.assertAlmostEqual(held(st), 0.25)
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 100.0, "canceled")})
        self.assertAlmostEqual(held(st), 0.25)
        self.assertNotIn("o1", st["pending"])

    def test_repeated_reconciliation_is_idempotent(self):
        st = state_with(pending={"o1": pending_buy(qty=0.5)})
        order = FakeOrder("o1", 0.5, 100.0, "partially_filled")
        for _ in range(5):
            reconcile_against(st, {"o1": order})
        self.assertEqual(len(st["lots"]["BTC/USD"]), 1)
        self.assertAlmostEqual(held(st), 0.5)

    def test_a_pending_buy_is_recovered_across_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = state_with(pending={"o1": pending_buy(qty=0.5)})
                crypto_grid.save_state(st)
                reloaded = crypto_grid.load_state()          # "restart"
                self.assertIn("o1", reloaded["pending"])
                reconcile_against(reloaded, {"o1": FakeOrder("o1", 0.5, 100.0, "filled")})
                self.assertAlmostEqual(held(reloaded), 0.5)

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
        self.assertAlmostEqual(held(st), 0.75)

    def test_partial_then_canceled_sell_retains_the_unsold_remainder(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 120.0, "partially_filled")})
        reconcile_against(st, {"o1": FakeOrder("o1", 0.25, 120.0, "canceled")})
        self.assertAlmostEqual(held(st), 0.75)
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
        self.assertAlmostEqual(held(st), 0.75)

    def test_a_lot_with_a_resting_sell_is_not_offered_again(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"o1": pending_sell(qty=1.0, lot_id="L1")})
        self.assertIsNone(crypto_grid.sellable_lot(st, "BTC/USD", 999.0)[1])


class InterleavedBuySellCancelTest(unittest.TestCase):
    """A buy update must never resurrect coins a sell already disposed of.

    Reported sequence: a buy fills 0.5 of 1 and stays pending; a sell takes
    that 0.5, emptying the lot; the buy is then CANCELED still reporting
    cumulative filled_qty=0.5. Restating the lot's quantity from that figure
    recreated a 0.5-coin lot for coins that were already gone — and on a
    shared symbol, a later sell would reach moon_bot's inventory to cover it.

    Fixed by never storing a net quantity: `acquired_qty` is the buy's to
    restate, `disposed_qty` is the sells', and held = acquired - disposed.
    """

    def test_a_canceled_partial_buy_does_not_resurrect_sold_coins(self):
        st = state_with(pending={"b1": pending_buy(qty=1.0, lot_id="L1")})
        # Buy fills 0.5, still open.
        reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "partially_filled")})
        self.assertAlmostEqual(held(st), 0.5)

        # That 0.5 is sold and fully fills.
        st["pending"]["s1"] = pending_sell(qty=0.5, lot_id="L1")
        reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "partially_filled"),
                               "s1": FakeOrder("s1", 0.5, 120.0, "filled")})
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 0.0)

        # Buy is canceled, still reporting cumulative filled_qty=0.5.
        reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "canceled")})
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 0.0,
                               msg="a canceled partial buy resurrected sold coins")
        self.assertEqual(st["lots"]["BTC/USD"], [],
                         "an exhausted lot outlived its terminal buy")

    def test_the_same_sequence_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = state_with(pending={"b1": pending_buy(qty=1.0, lot_id="L1")})
                reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "partially_filled")})
                st["pending"]["s1"] = pending_sell(qty=0.5, lot_id="L1")
                reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "partially_filled"),
                                       "s1": FakeOrder("s1", 0.5, 120.0, "filled")})
                crypto_grid.save_state(st)

                reloaded = crypto_grid.load_state()          # restart
                reconcile_against(reloaded, {"b1": FakeOrder("b1", 0.5, 100.0, "canceled")})
        self.assertAlmostEqual(crypto_grid.ledger_qty(reloaded, "BTC/USD"), 0.0,
                               msg="the disposal was lost across a restart")

    def test_a_lot_with_a_pending_buy_is_not_offered_for_sale(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 0.5, 100.0)]},
                        pending={"b1": pending_buy(qty=1.0, lot_id="L1")})
        self.assertIsNone(crypto_grid.sellable_lot(st, "BTC/USD", 999.0)[1],
                          "offered coins out of an unfinished acquisition")

    def test_v2_net_qty_state_migrates_forward(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with open(path, "w") as f:
                json.dump({"version": 2, "pending": {}, "lots": {
                    "BTC/USD": [{"lot_id": "L1", "qty": 0.4, "price": 100.0}]}}, f)
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = crypto_grid.load_state()
        self.assertAlmostEqual(held(st), 0.4)


class StaleSnapshotTest(unittest.TestCase):
    """A confirmed fill outranks a position snapshot taken before it settled.

    FleetBot.refresh() caches `bot.positions` BEFORE cycle() runs. moon_bot
    compared its ledger against that snapshot, so a buy that filled between
    the snapshot and the order read was measured against a position list
    captured while it was still unfilled: the ledger looked like it
    over-stated reality, the coin was zeroed, and with a triggered trailing
    stop no exit was submitted. Reproduced on 68173cb as
    qty={'ETH/USD': 0.0}, pending={}, stop submissions=0.
    """

    def setUp(self):
        try:
            import crypto_breakout
        except Exception as e:  # pragma: no cover
            self.skipTest(f"crypto_breakout unavailable: {e}")
        self.moon = crypto_breakout

    def _filled_buy(self):
        o = FakeOrder("b1", 1.0, 1800.0, "filled")
        o.client_order_id = "moon_bot-ETHUSD-1"
        o.symbol = "ETH/USD"
        o.filled_at = None
        o.canceled_at = o.updated_at = o.submitted_at = None
        return o

    def _run_cycle(self, position_read, price=1000.0):
        """cycle() with a buy that settles during it, against `position_read`."""
        moon, bot = self.moon, self.moon.bot
        state = moon._empty_state()
        state["pending"]["b1"] = {"symbol": "ETH/USD", "side": "buy",
                                  "requested_qty": 1.0, "applied_qty": 0.0}
        submitted = []
        with mock.patch.object(utils, "_post_influx_line", return_value=True), \
             mock.patch.object(moon, "get_donchian_levels",
                               return_value=(5000.0, 3000.0, price)), \
             mock.patch.object(moon, "load_state", return_value=state), \
             mock.patch.object(moon, "save_state"), \
             mock.patch.object(moon.utils, "account_position_qty",
                               side_effect=lambda c, s, b="": position_read), \
             mock.patch.object(bot.trading_client, "get_order_by_id",
                               return_value=self._filled_buy()), \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: {"sym": sym, "qty": qty}), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda od, **k: submitted.append(od) or FakeOrder("x")):
            moon.cycle(bot)
        return state, submitted

    def test_a_just_settled_buy_is_not_erased_by_a_lagging_read(self):
        # The position endpoint has not caught up: it still reports flat.
        state, submitted = self._run_cycle(position_read=(0.0, True))
        self.assertAlmostEqual(self.moon.my_qty(state, "ETH/USD"), 1.0,
                               msg="a confirmed 1 ETH buy was erased by a stale read")
        self.assertEqual(submitted, [], "bought again on top of a holding it owned")

    def test_an_unreadable_position_preserves_the_ledger(self):
        state, _ = self._run_cycle(position_read=(0.0, False))
        self.assertAlmostEqual(self.moon.my_qty(state, "ETH/USD"), 1.0)

    def test_the_holding_survives_save_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "moon.json")
            with mock.patch.object(self.moon, "STATE_FILE", path):
                state, _ = self._run_cycle(position_read=(0.0, True))
                self.moon.save_state(state)
                reloaded = self.moon.load_state()
        self.assertAlmostEqual(self.moon.my_qty(reloaded, "ETH/USD"), 1.0)

    def test_the_stop_fires_once_an_authoritative_read_confirms_the_coin(self):
        # Next cycle: the position endpoint now agrees, and price is below the
        # 10-day low, so the trailing stop must submit.
        moon, bot = self.moon, self.moon.bot
        state = moon._empty_state()
        state["qty"]["ETH/USD"] = 1.0
        submitted = []
        with mock.patch.object(moon, "get_donchian_levels",
                               return_value=(5000.0, 3000.0, 1000.0)), \
             mock.patch.object(moon, "load_state", return_value=state), \
             mock.patch.object(moon, "reconcile_pending", return_value=(False, set())), \
             mock.patch.object(moon, "save_state"), \
             mock.patch.object(moon.utils, "account_position_qty", return_value=(1.0, True)), \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: {"sym": sym, "qty": qty}), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda od, **k: submitted.append(od) or FakeOrder("x")):
            moon.cycle(bot)
        self.assertEqual(len(submitted), 1, "the trailing stop did not fire")
        self.assertAlmostEqual(submitted[0]["qty"], 1.0)

    def test_a_triggered_stop_on_an_unreadable_position_is_loud_not_silent(self):
        moon, bot = self.moon, self.moon.bot
        state = moon._empty_state()
        state["qty"]["ETH/USD"] = 1.0
        submitted = []
        with mock.patch.object(moon, "get_donchian_levels",
                               return_value=(5000.0, 3000.0, 1000.0)), \
             mock.patch.object(moon, "load_state", return_value=state), \
             mock.patch.object(moon, "reconcile_pending", return_value=(False, set())), \
             mock.patch.object(moon, "save_state"), \
             mock.patch.object(moon.utils, "account_position_qty", return_value=(0.0, False)), \
             mock.patch.object(moon.registry, "log_error") as logged, \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda od, **k: submitted.append(od)):
            moon.cycle(bot)
        self.assertEqual(submitted, [], "sold into a position it could not read")
        self.assertTrue(logged.called, "an unverifiable live stop passed silently")
        self.assertAlmostEqual(self.moon.my_qty(state, "ETH/USD"), 1.0)


class GridSettledSymbolTest(unittest.TestCase):
    """The grid needs the same guard: a lagging read is not a disposal."""

    def test_a_symbol_that_settled_this_cycle_defers_external_adjustment(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        self.assertFalse(
            crypto_grid.reconcile_lots(st, "BTC/USD", held=0.0, known=True,
                                       settled={"BTC/USD"}))
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 1.0)

    def test_reconcile_pending_reports_the_symbols_it_settled(self):
        st = state_with(pending={"b1": pending_buy(qty=0.5, lot_id="L1")})
        with mock.patch.object(utils, "_post_influx_line", return_value=True), \
             mock.patch.object(crypto_grid.bot.trading_client, "get_order_by_id",
                               return_value=FakeOrder("b1", 0.5, 100.0, "filled")):
            changed, settled = crypto_grid.reconcile_pending(st)
        self.assertTrue(changed)
        self.assertEqual(settled, {"BTC/USD"})

    def test_a_quiet_symbol_is_still_adjusted(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        self.assertTrue(
            crypto_grid.reconcile_lots(st, "BTC/USD", held=0.6, known=True, settled=set()))
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 0.6)


class FillsReachInfluxTest(unittest.TestCase):
    """Grid fills must land in the trade measurement, limit orders included.

    `submit_and_log_order` only polls and logs MARKET orders; a resting LIMIT
    order returns unlogged. crypto_grid is `reconciled=False` in the registry,
    so `reconcile_fills` never visits it either. The moment grid exits became
    limit orders, completed SELLS had no path into `crypto_trades` while BUYS
    still did — the accountant would have seen a book that only ever bought.
    """

    def test_a_terminal_sell_writes_exactly_one_fill_row(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"s1": pending_sell(qty=1.0, lot_id="L1")})
        order = FakeOrder("s1", 1.0, 120.0, "filled")
        order.filled_at = None
        order.client_order_id = "crypto_grid-BTCUSD-1"
        order.symbol = "BTC/USD"
        with mock.patch.object(crypto_grid.utils, "log_confirmed_fill") as logged:
            reconcile_against(st, {"s1": order})
        self.assertEqual(logged.call_count, 1)
        self.assertEqual(logged.call_args.kwargs["action"], "grid_sell")

    def test_a_terminal_buy_writes_exactly_one_fill_row(self):
        st = state_with(pending={"b1": pending_buy(qty=0.5, lot_id="L1")})
        with mock.patch.object(crypto_grid.utils, "log_confirmed_fill") as logged:
            reconcile_against(st, {"b1": FakeOrder("b1", 0.5, 100.0, "filled")})
        self.assertEqual(logged.call_count, 1)
        self.assertEqual(logged.call_args.kwargs["action"], "grid_buy")

    def test_an_open_order_writes_nothing_yet(self):
        # An in-flight partial may still fill completely and be logged at its
        # broker filled_at; writing now would double-count it.
        st = state_with(pending={"b1": pending_buy(qty=1.0, lot_id="L1")})
        with mock.patch.object(crypto_grid.utils, "log_confirmed_fill") as logged:
            reconcile_against(st, {"b1": FakeOrder("b1", 0.25, 100.0, "partially_filled")})
        logged.assert_not_called()

    def test_log_confirmed_fill_routes_full_and_partial_fills_apart(self):
        full = FakeOrder("o1", 1.0, 120.0, "filled")
        full.filled_at = object()
        with mock.patch.object(utils, "_log_fill_to_influx") as full_path, \
             mock.patch.object(utils, "log_terminal_partial_fill") as partial_path:
            utils.log_confirmed_fill(full, utils.logger, action="grid_sell")
        full_path.assert_called_once()
        partial_path.assert_not_called()

        part = FakeOrder("o2", 0.25, 120.0, "canceled")
        part.filled_at = None
        with mock.patch.object(utils, "_log_fill_to_influx") as full_path, \
             mock.patch.object(utils, "log_terminal_partial_fill") as partial_path:
            utils.log_confirmed_fill(part, utils.logger, action="grid_sell")
        full_path.assert_not_called()
        partial_path.assert_called_once()

    def test_a_zero_fill_writes_nothing(self):
        rejected = FakeOrder("o3", 0.0, 0.0, "rejected")
        with mock.patch.object(utils, "_log_fill_to_influx") as full_path, \
             mock.patch.object(utils, "log_terminal_partial_fill") as partial_path:
            self.assertEqual(utils.log_confirmed_fill(rejected, utils.logger), 0)
        full_path.assert_not_called()
        partial_path.assert_not_called()


class ExternalVsOrderReconciliationTest(unittest.TestCase):
    """One sale must not be subtracted by both reconcilers.

    The order read and the position read are separate network calls, and a
    fill landing between them is ordinary. The position read then sees the
    coins gone and books an "external" disposal; the next cycle's order read
    reports the same quantity as newly filled and books it again. Reordering
    the reads does not help — they are not atomic either way. So external
    adjustment waits until the symbol has no unsettled order.
    """

    def test_a_fill_between_the_two_reads_is_counted_once(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"s1": pending_sell(qty=1.0, lot_id="L1")})
        # Cycle N: the order read shows nothing filled...
        reconcile_against(st, {"s1": FakeOrder("s1", 0.0, 0.0, "new")})
        # ...then 0.4 fills, and the POSITION read sees 0.6 remaining.
        crypto_grid.reconcile_lots(st, "BTC/USD", held=0.6, known=True)
        # Cycle N+1: the order now reports cumulative filled_qty=0.4.
        reconcile_against(st, {"s1": FakeOrder("s1", 0.4, 120.0, "filled")})
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 0.6,
                               msg="the same sale was subtracted twice")

    def test_the_ledger_still_reads_0_6_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                                pending={"s1": pending_sell(qty=1.0, lot_id="L1")})
                reconcile_against(st, {"s1": FakeOrder("s1", 0.0, 0.0, "new")})
                crypto_grid.reconcile_lots(st, "BTC/USD", held=0.6, known=True)
                crypto_grid.save_state(st)

                reloaded = crypto_grid.load_state()        # restart
                reconcile_against(reloaded, {"s1": FakeOrder("s1", 0.4, 120.0, "filled")})
                crypto_grid.reconcile_lots(reloaded, "BTC/USD", held=0.6, known=True)
        self.assertAlmostEqual(crypto_grid.ledger_qty(reloaded, "BTC/USD"), 0.6)

    def test_a_genuine_external_sale_is_still_caught_once_quiet(self):
        # The feature this guard protects must still work with nothing pending.
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        self.assertTrue(crypto_grid.reconcile_lots(st, "BTC/USD", held=0.6, known=True))
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 0.6)

    def test_an_in_flight_symbol_is_left_to_the_order_reconciler(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                        pending={"s1": pending_sell(qty=1.0, lot_id="L1")})
        self.assertFalse(crypto_grid.reconcile_lots(st, "BTC/USD", held=0.0, known=True))
        self.assertAlmostEqual(crypto_grid.ledger_qty(st, "BTC/USD"), 1.0)


class FillDeliveryOutboxTest(unittest.TestCase):
    """A refused write is a queued delivery, not a lost trade.

    `_log_fill_to_influx` swallowed HTTP failures and `log_confirmed_fill`
    returned success anyway, while the reconcilers dropped the pending order
    regardless. Crypto is excluded from reconcile_fills, so a 503 lost the
    row permanently with no backfill path.
    """

    def _order(self, oid="s1", qty=1.0, price=120.0, status="filled", stamped=True):
        o = FakeOrder(oid, qty, price, status)
        o.client_order_id = "crypto_grid-BTCUSD-1"
        o.symbol = "BTC/USD"
        o.filled_at = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc) if stamped else None
        return o

    def test_a_refused_write_reports_failure_and_queues(self):
        outbox = []
        with mock.patch.object(utils, "_post_influx_line", return_value=False):
            wrote = utils.log_confirmed_fill(self._order(), utils.logger,
                                             action="grid_sell", outbox=outbox)
        self.assertEqual(wrote, 0, "a refused write reported success")
        self.assertEqual(len(outbox), 1)

    def test_a_successful_write_queues_nothing(self):
        outbox = []
        with mock.patch.object(utils, "_post_influx_line", return_value=True):
            wrote = utils.log_confirmed_fill(self._order(), utils.logger,
                                             action="grid_sell", outbox=outbox)
        self.assertEqual(wrote, 1)
        self.assertEqual(outbox, [])

    def test_a_terminal_partial_also_queues(self):
        outbox = []
        with mock.patch.object(utils, "_post_influx_line", return_value=False):
            utils.log_confirmed_fill(self._order(qty=0.25, status="canceled", stamped=False),
                                     utils.logger, action="grid_sell", outbox=outbox)
        self.assertEqual(len(outbox), 1)

    def test_the_outbox_survives_restart_and_recovers_one_row(self):
        # 503, restart, then a successful flush producing exactly one row.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "grid.json")
            with mock.patch.object(crypto_grid, "STATE_FILE", path):
                st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]},
                                pending={"s1": pending_sell(qty=1.0, lot_id="L1")})
                with mock.patch.object(utils, "_post_influx_line", return_value=False):
                    reconcile_against(st, {"s1": self._order()})
                self.assertEqual(len(st["outbox"]), 1)
                # Trading state settled regardless of the delivery failure.
                self.assertNotIn("s1", st["pending"])
                crypto_grid.save_state(st)

                reloaded = crypto_grid.load_state()          # restart
                self.assertEqual(len(reloaded["outbox"]), 1)

                posted = []
                with mock.patch.object(utils, "_post_influx_line",
                                       side_effect=lambda line, lg, context="": posted.append(line) or True):
                    utils.flush_fill_outbox(reloaded["outbox"], crypto_grid.logger)
        self.assertEqual(len(posted), 1, "recovery did not produce exactly one row")
        self.assertEqual(reloaded["outbox"], [])

    def test_a_still_down_influx_keeps_the_row_queued(self):
        outbox = ["crypto_trades,symbol=BTC/USD price=1,action=\"grid_sell\",qty=1 1"]
        with mock.patch.object(utils, "_post_influx_line", return_value=False):
            self.assertEqual(utils.flush_fill_outbox(outbox, utils.logger), 0)
        self.assertEqual(len(outbox), 1)

    def test_the_outbox_is_bounded(self):
        outbox = [f"line{i}" for i in range(utils.FILL_OUTBOX_MAX)]
        utils._stash(outbox, "newest", utils.logger)
        self.assertEqual(len(outbox), utils.FILL_OUTBOX_MAX)
        self.assertEqual(outbox[-1], "newest")


class TerminalPartialIdentityTest(unittest.TestCase):
    """Submit-poll and reconciler must stamp one terminal partial identically.

    The market branch of submit_and_log_order called _log_fill_to_influx,
    which stamps a no-filled_at partial with time.time_ns(); the reconciler
    called log_terminal_partial_fill, which uses a deterministic synthetic
    time. Two timestamps means two rows for one trade — not an idempotent
    overwrite. This asserts the resulting QUANTITY, not merely that each
    helper repeats itself.
    """

    def _terminal_partial(self):
        o = FakeOrder("o7", 0.25, 120.0, "canceled")
        o.client_order_id = "crypto_grid-BTCUSD-9"
        o.symbol = "BTC/USD"
        o.filled_at = None
        o.canceled_at = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)
        o.side = crypto_grid.OrderSide.SELL
        return o

    def test_submit_poll_and_reconciler_write_the_same_point(self):
        order = self._terminal_partial()
        lines = []
        with mock.patch.object(utils, "_post_influx_line",
                               side_effect=lambda line, lg, context="": lines.append(line) or True):
            # What the submission poll now does...
            utils.log_confirmed_fill(order, utils.logger, action="grid_sell")
            # ...and what the crypto reconciler does for the same order.
            utils.log_confirmed_fill(order, utils.logger, action="grid_sell")

        self.assertEqual(len(lines), 2, "expected two write attempts")
        stamps = {line.rsplit(" ", 1)[1] for line in lines}
        self.assertEqual(len(stamps), 1,
                         f"one terminal partial produced two timestamps: {stamps}")
        self.assertEqual(lines[0], lines[1],
                         "the two paths render the same fill differently")

    def test_the_quantity_is_not_doubled_across_the_two_paths(self):
        order = self._terminal_partial()
        rows = {}

        def capture(line, lg, context=""):
            body, stamp = line.rsplit(" ", 1)
            rows[stamp] = body          # same stamp => overwrite, like InfluxDB
            return True

        with mock.patch.object(utils, "_post_influx_line", side_effect=capture):
            utils.log_confirmed_fill(order, utils.logger, action="grid_sell")
            utils.log_confirmed_fill(order, utils.logger, action="grid_sell")

        self.assertEqual(len(rows), 1, "one trade landed as two rows")
        total = sum(float(b.split("qty=")[1].split(",")[0]) for b in rows.values())
        self.assertAlmostEqual(total, 0.25, msg=f"quantity doubled: {total}")


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
        self.assertAlmostEqual(held(st), 1.0)

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
            self.assertAlmostEqual(held(back), 1.0)
            self.assertIn("o1", back["pending"])

    def test_suspended_entries_block_buys(self):
        st = state_with()
        submitted = []
        crypto_grid.suspend_entries("test")
        try:
            with mock.patch.object(crypto_grid.bot, "bot_settings", {"entries_enabled": True}), \
                 mock.patch.object(crypto_grid.bot, "submit",
                                   side_effect=lambda *a, **k: submitted.append(a)):
                crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        finally:
            crypto_grid.resume_entries()
        self.assertEqual(submitted, [])


class EntriesLeverTest(unittest.TestCase):
    """New entries are fail-closed until an operator turns them on."""

    def _buy_with_settings(self, settings):
        st = state_with()
        submitted = []
        with mock.patch.object(crypto_grid.bot, "bot_settings", settings), \
             mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "budget_ok", True), \
             mock.patch.object(crypto_grid.bot, "account", mock.Mock(buying_power="100000")), \
             mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "market_order", return_value=object()), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a) or FakeOrder("o1")):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        return submitted

    def test_absent_key_means_disabled(self):
        self.assertEqual(self._buy_with_settings({}), [])

    def test_explicit_false_means_disabled(self):
        self.assertEqual(self._buy_with_settings({"entries_enabled": False}), [])

    def test_explicit_true_enables_entries(self):
        self.assertEqual(len(self._buy_with_settings({"entries_enabled": True})), 1)

    def test_selling_is_unaffected_by_the_lever(self):
        # Existing inventory must always be able to wind down.
        st = state_with(lots={"BTC/USD": [lot("L1", 1.0, 100.0)]})
        submitted = []
        with mock.patch.object(crypto_grid.bot, "bot_settings", {}), \
             mock.patch.object(crypto_grid, "cancel_my_open_orders"), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a) or FakeOrder("o2")):
            crypto_grid.grid_sell("BTC/USD", 120.0, 5, st, held=1.0, known=True)
        self.assertEqual(len(submitted), 1, "the entries lever blocked an exit")


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
        with mock.patch.object(crypto_grid.bot, "bot_settings", {"entries_enabled": True}), \
             mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
             mock.patch.object(crypto_grid.bot, "regime", "SIDEWAYS"), \
             mock.patch.object(crypto_grid.bot, "capital_crunch", False), \
             mock.patch.object(crypto_grid.bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(a)):
            crypto_grid.grid_buy("BTC/USD", 100.0, 3, st)
        self.assertEqual(submitted, [], "spent budget already committed to a pending order")

    def test_a_slice_never_overshoots_the_remaining_share(self):
        st = state_with(lots={"BTC/USD": [lot("L1", 9.6, 100.0)]})
        captured = {}
        with mock.patch.object(crypto_grid.bot, "bot_settings", {"entries_enabled": True}), \
             mock.patch.object(crypto_grid, "per_symbol_budget", return_value=1000.0), \
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
            changed, _settled = self.moon.reconcile_pending(state)
            return changed

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

    def test_a_readable_position_lets_the_trailing_stop_fire(self):
        """The symbol-spelling wipe, now handled inside account_position_qty.

        A raw-symbol position dict looked up with a slash symbol missed every
        position, so `total_held` came back 0, the reconcile decided the
        ledger over-stated reality and zeroed the coin, and the trailing-stop
        branch was never reached. The shared helper tries both spellings.
        """
        moon = self.moon
        bot = moon.bot
        state = moon._empty_state()
        state["qty"]["ETH/USD"] = 1.0
        submitted = []

        with mock.patch.object(moon, "get_donchian_levels",
                               return_value=(5000.0, 3000.0, 1000.0)), \
             mock.patch.object(moon, "load_state", return_value=state), \
             mock.patch.object(moon, "reconcile_pending", return_value=(False, set())), \
             mock.patch.object(moon, "save_state"), \
             mock.patch.object(moon.utils, "account_position_qty",
                               return_value=(1.0, True)), \
             mock.patch.object(bot, "equity", 100000.0), \
             mock.patch.object(bot, "positions", []), \
             mock.patch.object(bot, "budget_ok", True), \
             mock.patch.object(bot, "account", mock.Mock(buying_power="500000")), \
             mock.patch.object(bot, "market_order",
                               side_effect=lambda sym, qty, side, tif: {"qty": qty}), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda od, **k: submitted.append(od) or FakeOrder("o1")):
            moon.cycle(bot)

        self.assertEqual(len(submitted), 1, "the trailing stop never submitted an exit")
        self.assertAlmostEqual(submitted[0]["qty"], 1.0)
        self.assertAlmostEqual(moon.my_qty(state, "ETH/USD"), 1.0,
                               msg="the ledger was zeroed before the fill confirmed it")

    def test_entry_size_is_clipped_to_remaining_budget(self):
        captured = {}
        bot = self.moon.bot
        with mock.patch.object(self.moon, "get_donchian_levels",
                               return_value=(100.0, 50.0, 150.0)), \
             mock.patch.object(self.moon, "load_state", return_value=self.moon._empty_state()), \
             mock.patch.object(self.moon, "reconcile_pending", return_value=(False, set())), \
             mock.patch.object(self.moon.utils, "account_position_qty", return_value=(0.0, True)), \
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
