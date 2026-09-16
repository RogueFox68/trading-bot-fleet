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
import os
import tempfile
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
        opening, incomplete = advisor.opening_inventory(fills, NOW - dt.timedelta(days=5))
        self.assertEqual(incomplete, set())
        book = opening[("crypto_grid", "BTC/USD")]
        self.assertEqual(len(book), 1)
        self.assertAlmostEqual(book[0]["qty"], 1.0)
        self.assertAlmostEqual(book[0]["price"], 100.0)


class PeriodReturnAvailabilityTest(unittest.TestCase):
    """A window's return is reported only when it can actually be measured.

    The withdrawn approximation apportioned the LIFETIME unrealized figure by
    the share of open notional each window opened. It can reverse the sign:
    an old position up $100 and an equally sized new one down $50 net to +$50
    lifetime, and an equal split credits the new window +$25 when its actual
    change was -$50. That number was named total_pl and ranked the bots.
    """

    def _windows(self, fills, unrealized, days, excluded=()):
        return advisor.build_window_metrics(
            fills, unrealized, {"crypto_grid": 1000.0, "trend_bot": 1000.0},
            equity=74000.0, window_days=days, now=NOW, excluded_bots=excluded)

    def test_inventory_carried_in_makes_the_return_unmeasurable(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertFalse(five["period_return_available"])
        self.assertIsNone(five["total_pl"])
        self.assertIsNone(five["unrealized_pl"])
        self.assertEqual(five["period_return_unavailable_reason"],
                         "inventory_carried_into_window_and_no_opening_marks")

    def test_a_window_started_flat_reports_a_real_return(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 4000.0}, 5)["crypto_grid"]
        self.assertTrue(five["period_return_available"])
        self.assertAlmostEqual(five["unrealized_pl"], 4000.0)
        self.assertAlmostEqual(five["total_pl"], five["realized_pl"] + 4000.0)

    def test_the_sign_reversal_case_is_refused_not_approximated(self):
        # Old lot (opened 40d ago) and a new one, equal notional. The lifetime
        # figure nets to +50 while the new window is actually down.
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0),
                 fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 50.0}, 5)["crypto_grid"]
        self.assertFalse(five["period_return_available"],
                         "an unmeasurable return was reported as a number")
        self.assertIsNone(five["total_pl"])

    def test_old_losing_new_profitable_is_equally_refused(self):
        fills = [fill(40, "crypto_grid", "buy", 200.0, 10.0),
                 fill(2, "crypto_grid", "buy", 50.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": -500.0}, 5)["crypto_grid"]
        self.assertFalse(five["period_return_available"])

    def test_carried_inventory_in_a_different_asset_still_blocks_the_bot(self):
        # The bot, not the symbol, is what gets ranked.
        fills = [fill(40, "crypto_grid", "buy", 100.0, 1.0, symbol="ETH/USD"),
                 fill(2, "crypto_grid", "buy", 100.0, 1.0, symbol="BTC/USD")]
        five = self._windows(fills, {"crypto_grid": 100.0}, 5)["crypto_grid"]
        self.assertFalse(five["period_return_available"])

    def test_a_carried_short_position_also_blocks(self):
        fills = [fill(40, "trend_bot", "sell", 100.0, 10.0, symbol="TSLA"),
                 fill(35, "trend_bot", "buy", 90.0, 5.0, symbol="TSLA")]
        five = self._windows(fills, {"trend_bot": 200.0}, 5)["trend_bot"]
        self.assertFalse(five["period_return_available"])

    def test_option_lots_carry_their_contract_multiplier(self):
        # Without the multiplier an option lot's exposure is understated 100x,
        # which could read as "started flat" on a real open position.
        fills = [fill(40, "wheel_bot", "sell", 2.0, 1.0,
                      symbol="PAAS260918P00015000", multiplier=100)]
        opening, _ = advisor.opening_inventory(fills, NOW - dt.timedelta(days=5))
        notional = advisor._open_notional_by_bot(opening)
        self.assertAlmostEqual(notional["wheel_bot"], 200.0)

    def test_lifetime_unrealized_is_always_reported_and_labeled(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertAlmostEqual(five["lifetime_unrealized_pl"], 5000.0)

    def test_realized_only_is_still_reported_when_the_return_is_not(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 2.0),
                 fill(2, "crypto_grid", "sell", 110.0, 1.0)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertFalse(five["period_return_available"])
        self.assertAlmostEqual(five["realized_pl"], 10.0)

    def test_an_unmeasurable_bot_scores_nothing_and_is_unranked(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)] + [
            fill(d, "crypto_grid", "buy", 100.0, 1.0) for d in range(1, 4)]
        five = self._windows(fills, {"crypto_grid": 5000.0}, 5)["crypto_grid"]
        self.assertFalse(five["evidence_ok"])
        self.assertEqual(five["risk_adjusted_score"], 0.0)

    def test_an_excluded_bot_is_dropped_even_when_measurable(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        five = self._windows(fills, {"crypto_grid": 4000.0}, 5,
                             excluded=("crypto_grid",))["crypto_grid"]
        self.assertFalse(five["period_return_available"])
        self.assertEqual(five["period_return_unavailable_reason"],
                         "excluded_accounting_anomaly")

    def test_the_recommendation_names_the_unranked_bots(self):
        fills = [fill(40, "crypto_grid", "buy", 100.0, 10.0)]
        metrics = {f"{d}d": self._windows(fills, {"crypto_grid": 5000.0}, d)
                   for d in advisor.WINDOW_DAYS}
        rec = advisor.recommend_allocations(metrics, {}, advisor.regime_context({}))
        self.assertIn("crypto_grid", rec["unranked_bots"])
        self.assertTrue(any(r.startswith("period_return_unavailable:")
                            for r in rec["reason_codes"]))
        self.assertEqual(rec["action"], "no_change")


class OpeningInventoryCoverageTest(unittest.TestCase):
    """Incomplete history must report as incomplete, not as a clean book."""

    def test_a_close_with_no_open_marks_the_bot_incomplete(self):
        # The fetch window started mid-position: we see the exit, never the
        # entry. Booking that as a genuine new short is what let a pre-window
        # sale report its whole proceeds as profit.
        fills = [fill(90, "crypto_grid", "sell", 110.0, 1.0)]
        opening, incomplete = advisor.opening_inventory(fills, NOW - dt.timedelta(days=60))
        self.assertIn("crypto_grid", incomplete)

    def test_a_complete_book_is_not_flagged(self):
        fills = [fill(90, "crypto_grid", "buy", 100.0, 1.0),
                 fill(80, "crypto_grid", "sell", 110.0, 1.0)]
        opening, incomplete = advisor.opening_inventory(fills, NOW - dt.timedelta(days=60))
        self.assertEqual(incomplete, set())

    def test_incomplete_history_blocks_the_period_return(self):
        fills = [fill(90, "crypto_grid", "sell", 110.0, 1.0)]
        got = advisor.build_window_metrics(
            fills, {}, {}, equity=74000.0, window_days=60, now=NOW)["crypto_grid"]
        self.assertFalse(got["opening_inventory_complete"])
        self.assertFalse(got["period_return_available"])
        self.assertEqual(got["period_return_unavailable_reason"],
                         "incomplete_opening_history")

    def test_the_production_fetch_spans_more_than_the_longest_window(self):
        # THE bug: fetch_alpaca_fills defaulted to max(WINDOW_DAYS)=60, so the
        # 60d window's opening inventory was ALWAYS empty — the fix worked for
        # short windows and was silently absent on the one that matters most.
        self.assertGreater(advisor.LEDGER_LOOKBACK_DAYS, max(advisor.WINDOW_DAYS))
        captured = {}

        def fake_fetch(client, status=None, after=None, max_orders=None):
            captured["after"] = after
            return []

        with mock.patch.object(advisor, "utc_now", return_value=NOW), \
             mock.patch("utils._fetch_orders_covering", side_effect=fake_fetch):
            advisor.fetch_alpaca_fills(object())[0]
        span_days = (NOW - captured["after"]).days
        self.assertGreaterEqual(span_days, max(advisor.WINDOW_DAYS) + 90)

    def test_the_option_event_horizon_matches_the_fill_horizon(self):
        import inspect
        sig = inspect.signature(advisor.fetch_option_events)
        self.assertEqual(sig.parameters["lookback_days"].default,
                         advisor.LEDGER_LOOKBACK_DAYS)

    def test_hitting_the_fetch_cap_reports_incomplete(self):
        # Truncation used to be a log line and nothing else: the same plain
        # fill list came back, nothing downstream could act, and a truncated
        # history that happened to look flat was still ranked.
        def capped_fetch(client, status=None, after=None, max_orders=None):
            return [object()] * max_orders

        with mock.patch("utils._fetch_orders_covering", side_effect=capped_fetch), \
             mock.patch.object(advisor, "fills_from_orders", return_value=[]):
            _fills, complete = advisor.fetch_alpaca_fills(object(), max_orders=5)
        self.assertFalse(complete)

    def test_a_truncated_history_voids_every_period_return(self):
        # Even a book that looks perfectly flat: its starting inventory is
        # unverifiable, which is exactly the case that would be ranked.
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        got = advisor.build_window_metrics(
            fills, {"crypto_grid": 500.0}, {}, equity=74000.0, window_days=5,
            now=NOW, history_complete=False)["crypto_grid"]
        self.assertFalse(got["period_return_available"])
        self.assertIsNone(got["total_pl"])
        self.assertEqual(got["period_return_unavailable_reason"], "truncated_history_fetch")
        self.assertEqual(got["risk_adjusted_score"], 0.0)

    def test_a_truncated_history_withholds_the_recommendation(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0),
                 fill(2, "trend_bot", "buy", 50.0, 10.0)]
        with mock.patch.object(advisor, "fetch_alpaca_fills", return_value=(fills, False)), \
             mock.patch.object(advisor, "fetch_option_events", return_value=[]), \
             mock.patch.object(advisor, "utc_now", return_value=NOW), \
             mock.patch.object(advisor, "write_strategy_report"):
            report = advisor.generate_and_write_report(
                trading_client=object(), unrealized_by_bot={}, allocation_by_bot={},
                equity=74000.0, config_data={})
        self.assertFalse(report["assumptions"]["history_complete"])
        self.assertEqual(report["recommendation"]["action"], "no_change")
        self.assertIn("crypto_grid", report["recommendation"]["unranked_bots"])

    def test_a_complete_history_still_ranks_normally(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        got = advisor.build_window_metrics(
            fills, {"crypto_grid": 500.0}, {}, equity=74000.0, window_days=5,
            now=NOW, history_complete=True)["crypto_grid"]
        self.assertTrue(got["period_return_available"])

    def test_production_path_seeds_the_longest_window(self):
        # End to end through generate_and_write_report: a buy 90 days ago and a
        # sell yesterday must book +$10, not $0.
        fills = [fill(90, "crypto_grid", "buy", 100.0, 1.0),
                 fill(1, "crypto_grid", "sell", 110.0, 1.0)]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rec.json")
            with mock.patch.object(advisor, "fetch_alpaca_fills", return_value=(fills, True)), \
                 mock.patch.object(advisor, "fetch_option_events", return_value=[]), \
                 mock.patch.object(advisor, "utc_now", return_value=NOW):
                report = advisor.generate_and_write_report(
                    trading_client=object(), unrealized_by_bot={}, allocation_by_bot={},
                    equity=74000.0, config_data={}, path=path)
        longest = report["windows"][f"{max(advisor.WINDOW_DAYS)}d"]["crypto_grid"]
        self.assertAlmostEqual(longest["realized_pl"], 10.0,
                               msg="the 60d window still had no opening inventory")


class WheelMultiplierTest(unittest.TestCase):
    """The contract multiplier follows the INSTRUMENT, not the measurement.

    wheel_trades carries both option premium (per share, against a contract
    count) AND the wheel's own stock — assigned shares, and covered-call
    underlying. Scaling the whole measurement by 100 turned a 100-share PAAS
    round trip into a five-figure number.
    """

    def test_wheel_owned_stock_is_not_scaled_by_a_hundred(self):
        df = frame(row(10, "buy", 48.0, 100, "PAAS", "wheel_trades"),
                   row(5, "sell", 40.0, 100, "PAAS", "wheel_trades"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["wheel_bot"], -800.0)
        self.assertNotAlmostEqual(scores["wheel_bot"], -80000.0)

    def test_option_rows_in_the_same_measurement_still_scale(self):
        df = frame(row(10, "sell_put", 2.0, 1, "PAAS260918P00015000", "wheel_trades"),
                   row(5, "buy_close", 0.5, 1, "PAAS260918P00015000", "wheel_trades"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["wheel_bot"], 150.0)

    def test_both_instruments_in_one_window_are_summed_correctly(self):
        df = frame(row(10, "buy", 48.0, 100, "PAAS", "wheel_trades"),
                   row(9, "sell", 40.0, 100, "PAAS", "wheel_trades"),
                   row(8, "sell_put", 2.0, 1, "PAAS260918P00015000", "wheel_trades"),
                   row(7, "buy_close", 0.5, 1, "PAAS260918P00015000", "wheel_trades"))
        scores = accountant.calculate_realized_pl(df, window_days=30, now=NOW)
        self.assertAlmostEqual(scores["wheel_bot"], -800.0 + 150.0)


class ImpossibleBasisTest(unittest.TestCase):
    """A negative cost basis is normal on a SHORT. Only longs are anomalous."""

    @staticmethod
    def position(symbol, qty, cost_basis, side=None):
        p = mock.Mock()
        p.symbol, p.qty, p.cost_basis = symbol, str(qty), str(cost_basis)
        p.side = side if side is not None else ("short" if qty < 0 else "long")
        return p

    def test_a_short_put_is_not_an_anomaly(self):
        # Selling to open is a credit; this is routine wheel_bot operation.
        self.assertFalse(accountant._has_impossible_basis(
            self.position("PAAS260918P00015000", -1, -250.0)))

    def test_a_short_stock_position_is_not_an_anomaly(self):
        self.assertFalse(accountant._has_impossible_basis(
            self.position("TSLA", -100, -25000.0)))

    def test_a_long_with_a_negative_basis_is_an_anomaly(self):
        # The observed case: long ETH reported at roughly -$1,196.
        self.assertTrue(accountant._has_impossible_basis(
            self.position("ETHUSD", 1.454, -1196.0)))

    def test_an_ordinary_long_is_clean(self):
        self.assertFalse(accountant._has_impossible_basis(
            self.position("AAPL", 10, 2000.0)))

    def test_a_short_detected_by_qty_alone_is_not_an_anomaly(self):
        # Some option positions have been seen without a usable `side`.
        self.assertFalse(accountant._has_impossible_basis(
            self.position("PAAS260918P00015000", -1, -250.0, side="")))

    def test_an_unparseable_basis_is_not_an_anomaly(self):
        p = mock.Mock()
        p.symbol, p.qty, p.cost_basis, p.side = "AAPL", "10", None, "long"
        self.assertFalse(accountant._has_impossible_basis(p))


class AnomalyExcludesFromScoringTest(unittest.TestCase):
    """Flagging is not enough — the affected strategy must leave the ranking."""

    def _report(self, excluded=()):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        return advisor.build_strategy_report(
            fills=fills, unrealized_by_bot={"crypto_grid": 5000.0},
            allocation_by_bot={"crypto_grid": 1000.0}, equity=74000.0,
            config_data={}, now=NOW, negative_basis_positions=["ETHUSD"],
            excluded_bots=excluded)

    def test_an_anomalous_bot_loses_its_period_return(self):
        report = self._report(excluded=("crypto_grid",))
        window = report["windows"]["5d"]["crypto_grid"]
        self.assertTrue(window["excluded_from_scoring"])
        self.assertFalse(window["period_return_available"])
        self.assertIsNone(window["total_pl"])
        self.assertEqual(window["risk_adjusted_score"], 0.0)

    def test_the_same_book_scores_when_not_excluded(self):
        # Proves the exclusion is what changed the outcome, not the data.
        report = self._report(excluded=())
        window = report["windows"]["5d"]["crypto_grid"]
        self.assertTrue(window["period_return_available"])
        self.assertIsNotNone(window["total_pl"])

    def test_the_exclusion_is_recorded_in_the_report(self):
        report = self._report(excluded=("crypto_grid",))
        self.assertEqual(report["assumptions"]["excluded_from_scoring"], ["crypto_grid"])
        self.assertEqual(report["assumptions"]["negative_basis_positions"], ["ETHUSD"])

    def test_an_excluded_bot_cannot_receive_an_allocation_shift(self):
        report = self._report(excluded=("crypto_grid",))
        self.assertIn("crypto_grid", report["recommendation"]["unranked_bots"])
        self.assertEqual(report["recommendation"]["action"], "no_change")

    def test_a_resolved_anomaly_clears_the_report(self):
        fills = [fill(2, "crypto_grid", "buy", 100.0, 10.0)]
        report = advisor.build_strategy_report(
            fills=fills, unrealized_by_bot={"crypto_grid": 5000.0},
            allocation_by_bot={"crypto_grid": 1000.0}, equity=74000.0,
            config_data={}, now=NOW)
        self.assertEqual(report["assumptions"]["negative_basis_positions"], [])
        self.assertEqual(report["assumptions"]["excluded_from_scoring"], [])
        self.assertIsNone(report["assumptions"]["negative_basis_caveat"])


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


class SuspectPerformanceWriteTest(unittest.TestCase):
    """A flagged basis must not still be published as this bot's P&L.

    The 2026-09-15 live check: accounting_anomaly was being written every
    cycle with kind=negative_long_cost_basis, and bot_performance STILL
    reported crypto_grid at unrealized +5027.81 / total +5038.94 — derived
    from Alpaca's ETHUSD (qty 1.4156, avg_entry_price -1056.36, cost_basis
    -1495.40, unrealized_pl +4959.59) and a dust SOLUSD with the same defect.
    Account equity was correct; only per-bot attribution was wrong.

    A detector whose finding does not reach the number it invalidates is a
    log line, not a control (rule 21).
    """

    def point(self, bot="crypto_grid", realized=11.13, unrealized=5027.81,
              allocation=3464.19, suspect=True):
        return accountant.build_bot_performance_point(
            bot, realized, unrealized, allocation, suspect)

    def test_a_suspect_bot_publishes_no_unrealized_or_total(self):
        _, fields = self.point()
        self.assertNotIn("unrealized_pl", fields,
                         "the inflated unrealized figure is still being published")
        self.assertNotIn("total_pl", fields,
                         "total_pl carries the same bad basis through addition")

    def test_a_suspect_bot_still_publishes_realized_and_allocation(self):
        # Realized is FIFO over confirmed fills and allocation is
        # |market_value|; neither reads avg_entry_price, so both survive.
        _, fields = self.point()
        self.assertAlmostEqual(fields["realized_pl"], 11.13)
        self.assertAlmostEqual(fields["allocation"], 3464.19)

    def test_the_suspect_bot_is_tagged(self):
        tags, _ = self.point()
        self.assertEqual(tags["pl_suspect"], "true")
        self.assertEqual(tags["bot"], "crypto_grid")

    def test_a_clean_bot_is_unchanged_and_tagged_false(self):
        tags, fields = self.point(bot="trend_bot", realized=100.0,
                                  unrealized=25.0, allocation=9000.0,
                                  suspect=False)
        self.assertEqual(tags["pl_suspect"], "false",
                         "clean must be visible, not implied by absence")
        self.assertAlmostEqual(fields["unrealized_pl"], 25.0)
        self.assertAlmostEqual(fields["total_pl"], 125.0)
        self.assertAlmostEqual(fields["realized_pl"], 100.0)

    def test_the_withheld_bot_is_not_netted_down_to_a_partial_sum(self):
        """Netting the bad leg out and publishing the rest is rule 24's proxy."""
        _, fields = self.point(unrealized=68.22)   # as if ETH/SOL were removed
        self.assertNotIn("unrealized_pl", fields,
                         "a partial sum under the same field name still reads as P&L")

    def test_no_field_is_silently_renamed(self):
        """A clean row keeps exactly the four fields Grafana already plots."""
        _, fields = self.point(suspect=False)
        self.assertEqual(set(fields),
                         {"allocation", "realized_pl", "unrealized_pl", "total_pl"})


class SuspectWriteIsDrivenEndToEndTest(unittest.TestCase):
    """The flag reaches the write through the accountant's own plumbing.

    ImpossibleBasisTest pins the detector and SuspectPerformanceWriteTest pins
    the point builder; this drives detector -> anomalous_bots -> builder, so a
    future refactor cannot pass both while leaving them unconnected (rule 19:
    idempotence/agreement is a property of the system, not of a helper).
    """

    def test_the_observed_positions_withhold_their_owner(self):
        eth = ImpossibleBasisTest.position("ETHUSD", 1.4156, -1495.40)
        sol = ImpossibleBasisTest.position("SOLUSD", 0.0113, -65.0)
        clean = ImpossibleBasisTest.position("AAPL", 10, 2000.0)

        owners = {"ETHUSD": "crypto_grid", "SOLUSD": "crypto_grid",
                  "AAPL": "trend_bot"}
        anomalous = {owners[p.symbol] for p in (eth, sol, clean)
                     if accountant._has_impossible_basis(p)}

        self.assertEqual(anomalous, {"crypto_grid"})

        _, grid = accountant.build_bot_performance_point(
            "crypto_grid", 11.13, 5027.81, 3464.19, "crypto_grid" in anomalous)
        _, trend = accountant.build_bot_performance_point(
            "trend_bot", 100.0, 25.0, 9000.0, "trend_bot" in anomalous)

        self.assertNotIn("total_pl", grid, "crypto_grid's phantom total was published")
        self.assertAlmostEqual(trend["total_pl"], 125.0,
                               msg="an unaffected bot must not lose its P&L")


class AllocationsIgnorePerformanceTest(unittest.TestCase):
    """Phantom P&L must not be able to move real capital.

    bot_performance is currently WRITE-ONLY inside the fleet: the accountant
    writes it and only export_data.py (a manual CSV dump) reads it back. The
    CFO reallocator takes `allocation_stats`, which is built from
    abs(market_value) and never touches cost basis or P&L — so a negative
    basis could inflate the dashboard but not a budget.

    That is worth PINNING rather than re-deriving: it is the difference
    between a reporting bug and a capital-at-risk one, and it would be an
    easy thing to undo by "improving" the allocator to weight by performance.
    """

    def test_the_reallocator_takes_no_pl_input(self):
        import inspect
        params = set(inspect.signature(
            accountant.calculate_dynamic_allocations).parameters)
        self.assertEqual(
            params, {"equity", "allocation_stats", "regime", "vix", "config_data"},
            "calculate_dynamic_allocations grew a new input — if it is P&L "
            "derived, a negative cost basis can now move real allocations")
        for banned in ("realized", "unrealized", "performance", "pl", "total_pl"):
            self.assertNotIn(banned, params)
