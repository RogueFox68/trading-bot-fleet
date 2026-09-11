"""Regression: period P&L is FIFO against real opening inventory.

--- The defects this pins --------------------------------------------------

1. accountant.calculate_realized_pl used an AVERAGE-COST approximation:
   average every buy in the window into one cost, multiply by total quantity
   sold. That is only right on a book that is flat at both ends of the window.
   It is wrong in three separate ways otherwise:

     * a sell whose matching buy predated the window was costed against the
       average of unrelated LATER buys — or, with no buys in the window,
       contributed nothing at all;
     * inventory still held at the window's end was costed as if sold,
       because the average absorbed it;
     * shorts were mispriced outright; the formula assumes buy-then-sell.

   crypto_grid — hundreds of turns a month, never flat — is precisely the
   book that breaks worst, and it is the book the dashboard was reporting
   several thousand dollars of profit on.

   The substring test that classified rows was also incomplete: `grid_sweep`
   contains neither "buy" nor "sell", so every sweep silently vanished from
   realized P&L while its proceeds stayed in the equity curve.

2. strategy_advisor.build_window_metrics filtered fills to the window and
   FIFO-paired only those. A sell whose opening buy predated the cutoff had
   no lot to close, so realized_metrics appended it as a fresh SHORT lot: the
   sale booked ZERO realized P&L and any later in-window buy was scored as
   covering a short that never existed.

3. The same advisor added the LIFETIME unrealized P&L, unchanged, into the
   5d, 20d and 60d windows alike — counting one position's whole run-up three
   times and attributing months of drift to the last five days. On the
   2026-09-11 snapshot that was ~$4,914 of crypto unrealized (on positions
   the broker reports with a NEGATIVE cost basis) in every window, and it is
   the main reason the advisor recommended moving another 2% of the account
   out of Trend and into Grid.
"""
import datetime as dt
import unittest
from unittest import mock

import pandas as pd

import accountant
import strategy_advisor as advisor

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def row(days_ago, action, price, qty, symbol="BTC/USD", measurement="crypto_trades"):
    return {
        "time": (NOW - dt.timedelta(days=days_ago)).timestamp(),
        "symbol": symbol,
        "price": price,
        "qty": qty,
        "action": action,
        "bot_type": measurement,
    }


def frame(*rows):
    return pd.DataFrame(list(rows))


def fill(days_ago, bot, side, price, qty, symbol="BTC/USD", multiplier=1):
    return {
        "bot": bot,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "multiplier": multiplier,
        "notional": price * qty * multiplier,
        "filled_at": NOW - dt.timedelta(days=days_ago),
        "client_order_id": f"{bot}-{symbol}-{days_ago}",
        "adverse_slippage_bps": None,
    }


class AccountantFifoTest(unittest.TestCase):

    def test_pre_window_buy_is_matched_against_its_real_basis(self):
        # Bought at 100 forty days ago, sold at 110 today. The 30d window must
        # book +10, not +110 (no basis) and not 0 (dropped).
        df = frame(row(40, "grid_buy", 100.0, 1.0),
                   row(1, "grid_sell", 110.0, 1.0))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["crypto_grid"], 10.0)

    def test_unsold_inventory_is_not_booked_as_profit(self):
        # Two buys, one sell. Average-cost costed the sale at the average of
        # BOTH buys; FIFO closes the first lot only.
        df = frame(row(10, "grid_buy", 100.0, 1.0),
                   row(9, "grid_buy", 200.0, 1.0),
                   row(8, "grid_sell", 150.0, 1.0))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["crypto_grid"], 50.0)   # 150 - 100
        # The average-cost formula produced 150 - ((100+200)/2) = 0.
        self.assertNotAlmostEqual(scores["crypto_grid"], 0.0)

    def test_boundary_churn_books_the_loss_it_actually_made(self):
        # 20 round trips, each buying at 100.00 and selling at 100.05, the
        # pattern the old grid generated. Average-cost reported these as
        # break-even-ish in aggregate; each pair is +0.05 gross before fees.
        rows = []
        for i in range(20):
            rows.append(row(20 - i * 0.5, "grid_buy", 100.00, 1.0))
            rows.append(row(20 - i * 0.5 - 0.1, "grid_sell", 100.05, 1.0))
        scores = accountant.calculate_realized_pl(frame(*rows), window_days=30, now=NOW)
        self.assertAlmostEqual(scores["crypto_grid"], 20 * 0.05, places=6)

    def test_grid_sweep_counts_as_a_sell(self):
        # `grid_sweep` matched neither substring, so it vanished entirely.
        df = frame(row(10, "grid_buy", 100.0, 1.0),
                   row(5, "grid_sweep", 120.0, 1.0))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["crypto_grid"], 20.0)

    def test_lifecycle_events_are_not_paired_as_trades(self):
        df = frame(row(10, "sell_put", 2.0, 1.0, "PAAS260918P00015000", "wheel_trades"),
                   row(5, "expired", 0.0, 1.0, "PAAS260918P00015000", "wheel_trades"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        # The expiry is a state change, not a buy; the short stays open.
        self.assertAlmostEqual(scores["wheel_bot"], 0.0)

    def test_option_premium_scales_by_the_contract_multiplier(self):
        # Sold a put at $2.00/share, bought it back at $0.50 => $150/contract.
        df = frame(row(10, "sell_put", 2.0, 1.0, "PAAS260918P00015000", "wheel_trades"),
                   row(5, "buy_close", 0.5, 1.0, "PAAS260918P00015000", "wheel_trades"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["wheel_bot"], 150.0)

    def test_closes_outside_the_window_are_not_counted(self):
        df = frame(row(60, "grid_buy", 100.0, 1.0),
                   row(45, "grid_sell", 200.0, 1.0))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["crypto_grid"], 0.0)

    def test_per_symbol_books_do_not_cross(self):
        df = frame(row(10, "grid_buy", 100.0, 1.0, "BTC/USD"),
                   row(9, "grid_sell", 150.0, 1.0, "ETH/USD"),
                   row(8, "grid_buy", 120.0, 1.0, "ETH/USD"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        # The ETH sell opens a short at 150, covered at 120 => +30. The BTC
        # buy is untouched. Crossing the books would have closed BTC at 150.
        self.assertAlmostEqual(scores["crypto_grid"], 30.0)

    def test_empty_and_malformed_frames_are_safe(self):
        self.assertEqual(accountant.calculate_realized_pl(pd.DataFrame()), {})
        self.assertEqual(
            accountant.calculate_realized_pl(pd.DataFrame([{"bot_type": "crypto_trades"}])), {})

    def test_window_and_lookback_are_paired_in_one_place(self):
        # realized_pl_for_window exists so no caller fetches exactly its
        # window and silently reintroduces the missing-basis bug.
        captured = {}

        def fake_query(days=30):
            captured["days"] = days
            return pd.DataFrame()

        with mock.patch.object(accountant, "query_influx_trades", fake_query):
            accountant.realized_pl_for_window(30)
        self.assertEqual(captured["days"], 30 + accountant.INVENTORY_LOOKBACK_DAYS)


class AdvisorOpeningInventoryTest(unittest.TestCase):

    def _windows(self, fills, unrealized, days):
        return advisor.build_window_metrics(
            fills, unrealized, {"crypto_grid": 1000.0, "trend_bot": 1000.0},
            equity=74000.0, window_days=days, now=NOW)

    def test_pre_window_buy_gives_the_in_window_sell_a_basis(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 1.0),
                 fill(2, "crypto_grid", "sell", 110.0, 1.0)]
        got = self._windows(fills, {}, 5)["crypto_grid"]
        self.assertAlmostEqual(got["realized_pl"], 10.0)
        self.assertEqual(got["closed_trades"], 1)

    def test_without_opening_inventory_the_sale_used_to_book_nothing(self):
        # Documents the old behavior explicitly: the sell became a short lot.
        fills = [fill(2, "crypto_grid", "sell", 110.0, 1.0)]
        metrics, residual = advisor.realized_metrics(fills)
        self.assertAlmostEqual(metrics["crypto_grid"]["realized_pl"], 0.0)
        self.assertEqual(metrics["crypto_grid"]["closed_trades"], 0)
        self.assertTrue(residual[("crypto_grid", "BTC/USD")][0]["qty"] < 0)

    def test_opening_inventory_reconstructs_the_book_at_the_cutoff(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 2.0),
                 fill(35, "crypto_grid", "sell", 120.0, 1.0)]
        opening = advisor.opening_inventory(fills, NOW - dt.timedelta(days=5))
        book = opening[("crypto_grid", "BTC/USD")]
        self.assertEqual(len(book), 1)
        self.assertAlmostEqual(book[0]["qty"], 1.0)
        self.assertAlmostEqual(book[0]["price"], 100.0)


class AdvisorUnrealizedScopingTest(unittest.TestCase):

    def _windows(self, fills, unrealized, days):
        return advisor.build_window_metrics(
            fills, unrealized, {"crypto_grid": 1000.0},
            equity=74000.0, window_days=days, now=NOW)

    def test_an_old_position_does_not_credit_a_short_window(self):
        # One lot opened 40 days ago, still held, carrying $5,000 unrealized.
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        sixty = self._windows(fills, {"crypto_grid": 5000.0}, 60)["crypto_grid"]
        self.assertAlmostEqual(five["unrealized_pl"], 0.0,
                               msg="a 40-day-old lot credited the 5-day window")
        self.assertAlmostEqual(sixty["unrealized_pl"], 5000.0)

    def test_the_raw_lifetime_figure_is_still_reported(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["lifetime_unrealized_pl"], 5000.0)

    def test_unrealized_splits_between_old_and_new_lots(self):
        # Half the open notional opened inside the window => half the credit.
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0),
                 fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 4000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["unrealized_pl"], 2000.0)

    def test_a_window_that_opened_everything_gets_the_whole_figure(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 4000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["unrealized_pl"], 4000.0)

    def test_no_open_position_credits_nothing(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 1.0),
                 fill(2, "crypto_grid", "sell", 110.0, 1.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["unrealized_pl"], 0.0)

    def test_total_pl_uses_the_scoped_figure(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["total_pl"], five["realized_pl"] + 0.0)


class NegativeBasisFlagTest(unittest.TestCase):
    """A negative-cost-basis position must be flagged, not silently scored."""

    def test_report_carries_the_flagged_symbols(self):
        report = advisor.build_strategy_report(
            fills=[], unrealized_by_bot={}, allocation_by_bot={}, equity=74000.0,
            config_data={}, now=NOW, negative_basis_positions=["ETHUSD", "SOLUSD"])
        assumptions = report["assumptions"]
        self.assertEqual(assumptions["negative_basis_positions"], ["ETHUSD", "SOLUSD"])
        self.assertIn("not a trustworthy input", assumptions["negative_basis_caveat"])

    def test_clean_book_carries_no_caveat(self):
        report = advisor.build_strategy_report(
            fills=[], unrealized_by_bot={}, allocation_by_bot={}, equity=74000.0,
            config_data={}, now=NOW)
        self.assertEqual(report["assumptions"]["negative_basis_positions"], [])
        self.assertIsNone(report["assumptions"]["negative_basis_caveat"])


if __name__ == "__main__":
    unittest.main()
