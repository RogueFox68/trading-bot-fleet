import datetime as dt
import tempfile
import unittest
from types import SimpleNamespace

import strategy_advisor as advisor


UTC = dt.timezone.utc


def fill(bot, symbol, side, qty, price, days_ago=0, multiplier=1):
    return {
        "bot": bot,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "multiplier": multiplier,
        "notional": qty * price * multiplier,
        "filled_at": dt.datetime(2026, 7, 9, tzinfo=UTC) - dt.timedelta(days=days_ago),
        "client_order_id": f"{bot}-{symbol}-1",
        "adverse_slippage_bps": None,
    }


class StrategyAdvisorAttributionTest(unittest.TestCase):
    def test_bot_from_client_order_id(self):
        self.assertEqual(advisor.bot_from_client_order_id("trend_bot-AAPL-123"), "trend_bot")
        self.assertEqual(advisor.bot_from_client_order_id("moon_bot-BTCUSD-123"), "moon_bot")
        self.assertIsNone(advisor.bot_from_client_order_id("manual-AAPL-123"))

    def test_order_to_fill_ignores_unfilled_and_unknown_orders(self):
        order = SimpleNamespace(
            client_order_id="trend_bot-AAPL-123",
            symbol="AAPL",
            side="buy",
            filled_avg_price=100,
            filled_qty=2,
            filled_at=dt.datetime(2026, 7, 9, tzinfo=UTC),
            submitted_at=None,
            limit_price=99,
        )
        out = advisor.fill_from_order(order)
        self.assertEqual(out["bot"], "trend_bot")
        self.assertAlmostEqual(out["adverse_slippage_bps"], 101.01010101010101)

        order.filled_avg_price = None
        self.assertIsNone(advisor.fill_from_order(order))
        order.filled_avg_price = 100
        order.client_order_id = "manual-AAPL-123"
        self.assertIsNone(advisor.fill_from_order(order))


class StrategyAdvisorMetricsTest(unittest.TestCase):
    def test_realized_pnl_handles_longs_shorts_and_options(self):
        rows = [
            fill("trend_bot", "AAA", "buy", 10, 10),
            fill("trend_bot", "AAA", "sell", 10, 12),
            fill("trend_bot", "BBB", "sell", 5, 20),
            fill("trend_bot", "BBB", "buy", 5, 18),
            fill("wheel_bot", "AAA260731P00045000", "sell", 1, 1.00, multiplier=100),
            fill("wheel_bot", "AAA260731P00045000", "buy", 1, 0.40, multiplier=100),
        ]

        metrics = advisor.realized_metrics(rows)
        self.assertEqual(metrics["trend_bot"]["realized_pl"], 30)
        self.assertEqual(metrics["trend_bot"]["closed_trades"], 2)
        self.assertEqual(metrics["trend_bot"]["win_rate"], 1.0)
        self.assertEqual(metrics["wheel_bot"]["realized_pl"], 60)

    def test_drawdown_tracks_realized_curve(self):
        rows = [
            fill("survivor_bot", "AAA", "buy", 10, 10),
            fill("survivor_bot", "AAA", "sell", 10, 8),
            fill("survivor_bot", "BBB", "buy", 10, 10),
            fill("survivor_bot", "BBB", "sell", 10, 13),
        ]
        metrics = advisor.realized_metrics(rows)
        self.assertEqual(metrics["survivor_bot"]["realized_pl"], 10)
        self.assertEqual(metrics["survivor_bot"]["max_drawdown"], 20)

    def test_regime_context_buckets_vix_and_macro(self):
        context = advisor.regime_context({
            "global_settings": {
                "market_condition": "BEAR_TREND",
                "vix": 27.2,
                "macro_climate": "MACRO_BEAR",
                "sector_rotation": "DEFENSIVE",
            }
        })
        self.assertEqual(context["vix_band"], "HIGH_VIX")
        self.assertEqual(context["regime_key"], "BEAR_TREND|HIGH_VIX|MACRO_BEAR|DEFENSIVE")


class StrategyAdvisorAllocationTest(unittest.TestCase):
    def _config(self):
        return {
            "bots": {b: {"allocation": 0.0} for b in advisor.fleet_registry.BOTS},
            "cfo_settings": {
                "base_allocations": {
                    "trend_bot": 0.28,
                    "survivor_bot": 0.20,
                    "wheel_bot": 0.38,
                    "crypto_grid": 0.05,
                    "moon_bot": 0.04,
                },
                "minimum_reserves": {
                    "trend_bot": 0.10,
                    "survivor_bot": 0.10,
                    "wheel_bot": 0.05,
                    "crypto_grid": 0.02,
                    "moon_bot": 0.02,
                },
            },
            "global_settings": {
                "market_condition": "SIDEWAYS",
                "vix": 16.5,
                "macro_climate": "MACRO_BULL",
            },
        }

    def test_no_change_when_evidence_is_thin(self):
        report = advisor.build_strategy_report([], {}, {}, 100000, self._config(),
                                               now=dt.datetime(2026, 7, 9, tzinfo=UTC))
        rec = report["recommendation"]
        self.assertEqual(rec["action"], "no_change")
        self.assertEqual(rec["current_allocations"], rec["recommended_allocations"])
        self.assertIn("insufficient_evidence", rec["reason_codes"])
        self.assertIn("SIDEWAYS|LOW_VIX|MACRO_BULL|UNKNOWN", report["macro_scorecard"])

    def test_recommends_small_shift_from_loser_to_winner(self):
        rows = []
        for i in range(3):
            rows += [
                fill("trend_bot", f"WIN{i}", "buy", 10, 10, days_ago=i),
                fill("trend_bot", f"WIN{i}", "sell", 10, 12, days_ago=i),
                fill("wheel_bot", f"BAD{i}260731P00045000", "sell", 1, 1.0, days_ago=i, multiplier=100),
                fill("wheel_bot", f"BAD{i}260731P00045000", "buy", 1, 2.0, days_ago=i, multiplier=100),
            ]
        report = advisor.build_strategy_report(
            rows,
            unrealized_by_bot={"trend_bot": 50, "wheel_bot": -50},
            allocation_by_bot={"trend_bot": 5000, "wheel_bot": 5000},
            equity=100000,
            config_data=self._config(),
            now=dt.datetime(2026, 7, 9, tzinfo=UTC),
            influx_realized_by_bot={"trend_bot": 10, "wheel_bot": -10},
            influx_window_days=60,
        )
        rec = report["recommendation"]
        self.assertEqual(rec["action"], "recommend_shift")
        self.assertEqual(rec["recommended_allocations"]["trend_bot"], 0.30)
        self.assertEqual(rec["recommended_allocations"]["wheel_bot"], 0.36)
        self.assertFalse(rec["writes_effective_budgets"])
        comparison = report["source_comparison"]
        self.assertEqual(comparison["window_days"], {"alpaca_ledger": 60, "influx": 60})
        self.assertEqual(comparison["bots"]["trend_bot"]["influx_realized_pl"], 10)
        self.assertGreater(comparison["bots"]["trend_bot"]["delta"], 0)

    def test_report_can_be_written(self):
        report = advisor.build_strategy_report([], {}, {}, 100000, self._config(),
                                               now=dt.datetime(2026, 7, 9, tzinfo=UTC))
        with tempfile.NamedTemporaryFile() as f:
            advisor.write_strategy_report(report, path=f.name)
            self.assertGreater(len(f.read()), 0)


def event(etype, symbol, qty, day, eid="ev-1"):
    return {"activity_type": etype, "symbol": symbol, "qty": qty,
            "date": day, "id": eid}


class StrategyAdvisorOptionEventTest(unittest.TestCase):
    """Expirations and assignments have no order behind them; without synthetic
    fills the ledger never realizes expired-worthless premium (the wheel's most
    common winning outcome) and skews the wheel's score low."""

    def test_expired_short_put_realizes_premium(self):
        fills = [fill("wheel_bot", "AAA260731P00045000", "sell", 1, 1.00,
                      days_ago=10, multiplier=100)]
        synth, unmatched = advisor.synthetic_fills_from_events(
            [event("OPEXP", "AAA260731P00045000", 1, "2026-07-05")], fills)
        self.assertEqual(unmatched, 0)
        self.assertEqual(len(synth), 1)
        self.assertEqual(synth[0]["side"], "buy")
        self.assertEqual(synth[0]["price"], 0.0)
        metrics = advisor.realized_metrics(fills + synth)
        self.assertEqual(metrics["wheel_bot"]["realized_pl"], 100.0)
        self.assertEqual(metrics["wheel_bot"]["win_rate"], 1.0)

    def test_expired_long_option_realizes_loss(self):
        fills = [fill("trend_bot", "AAA260731C00050000", "buy", 1, 0.50,
                      days_ago=10, multiplier=100)]
        synth, _ = advisor.synthetic_fills_from_events(
            [event("OPEXP", "AAA260731C00050000", 1, "2026-07-05")], fills)
        self.assertEqual(synth[0]["side"], "sell")
        metrics = advisor.realized_metrics(fills + synth)
        self.assertEqual(metrics["trend_bot"]["realized_pl"], -50.0)

    def test_assigned_short_put_realizes_premium_and_books_stock_at_strike(self):
        fills = [fill("wheel_bot", "AAA260731P00045000", "sell", 1, 1.00,
                      days_ago=10, multiplier=100)]
        synth, unmatched = advisor.synthetic_fills_from_events(
            [event("OPASN", "AAA260731P00045000", 1, "2026-07-05")], fills)
        self.assertEqual(unmatched, 0)
        legs = {(s["symbol"], s["side"]): s for s in synth}
        self.assertIn(("AAA260731P00045000", "buy"), legs)
        stock = legs[("AAA", "buy")]
        self.assertEqual(stock["qty"], 100)
        self.assertEqual(stock["price"], 45.0)
        self.assertEqual(stock["multiplier"], 1)
        # a later stock sale pairs against the strike-priced basis
        exit_fill = fill("wheel_bot", "AAA", "sell", 100, 47.0, days_ago=1)
        metrics = advisor.realized_metrics(fills + synth + [exit_fill])
        self.assertEqual(metrics["wheel_bot"]["realized_pl"], 100.0 + 200.0)

    def test_assigned_covered_call_sells_stock_at_strike(self):
        fills = [
            fill("wheel_bot", "AAA", "buy", 100, 44.0, days_ago=20),
            fill("wheel_bot", "AAA260731C00045000", "sell", 1, 0.80,
                 days_ago=10, multiplier=100),
        ]
        synth, _ = advisor.synthetic_fills_from_events(
            [event("OPASN", "AAA260731C00045000", 1, "2026-07-05")], fills)
        stock = [s for s in synth if s["symbol"] == "AAA"][0]
        self.assertEqual(stock["side"], "sell")
        self.assertEqual(stock["price"], 45.0)
        metrics = advisor.realized_metrics(fills + synth)
        # +80 call premium, +100 stock gain (45 strike - 44 basis) x 100 shares
        self.assertEqual(metrics["wheel_bot"]["realized_pl"], 180.0)

    def test_event_without_open_lot_is_skipped_and_counted(self):
        synth, unmatched = advisor.synthetic_fills_from_events(
            [event("OPEXP", "AAA260731P00045000", 1, "2026-07-05")], [])
        self.assertEqual(synth, [])
        self.assertEqual(unmatched, 1)

    def test_event_qty_capped_at_open_qty(self):
        fills = [fill("wheel_bot", "AAA260731P00045000", "sell", 1, 1.00,
                      days_ago=10, multiplier=100)]
        synth, unmatched = advisor.synthetic_fills_from_events(
            [event("OPEXP", "AAA260731P00045000", 3, "2026-07-05")], fills)
        self.assertEqual(len(synth), 1)
        self.assertEqual(synth[0]["qty"], 1)
        self.assertEqual(unmatched, 1)

    def test_event_before_open_does_not_close_anything(self):
        fills = [fill("wheel_bot", "AAA260731P00045000", "sell", 1, 1.00,
                      days_ago=1, multiplier=100)]
        synth, unmatched = advisor.synthetic_fills_from_events(
            [event("OPEXP", "AAA260731P00045000", 1, "2026-07-01")], fills)
        self.assertEqual(synth, [])
        self.assertEqual(unmatched, 1)


if __name__ == "__main__":
    unittest.main()
