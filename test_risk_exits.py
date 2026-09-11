"""Regression: a hold decision never disables a stop loss.

--- The defect this pins ---------------------------------------------------

Both equity bots evaluated the tiered-hold EOD policy BEFORE their risk
exits. From 15:30 ET, a position scoring HOLD_OVERNIGHT or HOLD_SWING took
an early `return` (survivor fell into an if/elif chain whose first branch was
the EOD one, which amounts to the same thing) — so for the rest of the
session, and straight through the overnight gap, that position had:

    no stop loss, no take profit, no signal exit.

tiered_hold.OVERNIGHT_STOPS defines stop_loss_pct/trailing_stop_pct for
exactly these tiers, but nothing reads them (still true), so nothing
downstream covered the gap either. The tier that means "this position is
worth carrying overnight" was the tier that removed its protection for the
one period where a gap can happen.

Tiered hold decides the EOD SWEEP. It was never meant to decide whether risk
management applies. These tests fix the ordering.
"""
import ast
import datetime as dt
import sys
import types
import unittest
from unittest import mock


def _stub_ta():
    """`ta` is sdist-only and fails to build on some toolchains (a known
    tech-debt item). The bots only touch it inside their cycle bodies, so a
    stub keeps this suite runnable anywhere; a real install is used when
    present."""
    try:
        import ta  # noqa: F401
        return
    except Exception:
        pass
    sys.modules.setdefault("ta", types.ModuleType("ta"))
    for name, attrs in (("ta.momentum", ["RSIIndicator"]),
                        ("ta.trend", ["EMAIndicator", "ADXIndicator"])):
        mod = types.ModuleType(name)
        for attr in attrs:
            setattr(mod, attr, object)
        sys.modules.setdefault(name, mod)


_stub_ta()

import pandas as pd          # noqa: E402

import survivor_bot          # noqa: E402
import tiered_hold           # noqa: E402
import trend_bot             # noqa: E402
import utils                 # noqa: E402


class FakePosition:
    def __init__(self, symbol, qty, entry, side="long", asset_class="us_equity"):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = str(entry)
        self.side = side
        self.asset_class = asset_class


class FakeTrade:
    def __init__(self, price):
        self.price = price


def _survivor_at(time_str, live_price, entry=100.0, tier="HOLD_SWING"):
    """Run survivor_bot.manage_position and return every submitted reason."""
    bot = survivor_bot.bot
    submitted = []
    with mock.patch.object(bot, "pending_symbols", set()), \
         mock.patch.object(bot, "time_str", time_str), \
         mock.patch.object(bot, "is_eod_eval", "15:30" <= time_str < "15:45"), \
         mock.patch.object(bot, "is_eod_close", time_str >= "15:45"), \
         mock.patch.object(bot, "regime", "SIDEWAYS"), \
         mock.patch.object(bot, "vix", 15.0), \
         mock.patch.object(bot, "hours_held", return_value=2.0), \
         mock.patch.object(bot, "submit",
                           side_effect=lambda *a, **k: submitted.append(k.get("reason", ""))), \
         mock.patch.object(utils, "live_equity_price", return_value=live_price), \
         mock.patch.object(tiered_hold, "get_hold_tier", return_value=tier):
        survivor_bot.manage_position("AAPL", FakePosition("AAPL", 10, entry), rsi=50.0,
                                     price=live_price)
    return submitted


def _trend_at(time_str, price, entry=100.0, tier="HOLD_SWING", side="long"):
    """Run trend_bot.manage_position and return every submitted reason."""
    bot = trend_bot.bot
    submitted = []
    latest = {"ema_fast": 101.0, "ema_slow": 100.0}
    with mock.patch.object(bot, "pending_symbols", set()), \
         mock.patch.object(bot, "time_str", time_str), \
         mock.patch.object(bot, "is_eod_eval", "15:30" <= time_str < "15:45"), \
         mock.patch.object(bot, "is_eod_close", time_str >= "15:45"), \
         mock.patch.object(bot, "regime", "SIDEWAYS"), \
         mock.patch.object(bot, "vix", 15.0), \
         mock.patch.object(bot, "hours_held", return_value=2.0), \
         mock.patch.object(bot, "submit",
                           side_effect=lambda *a, **k: submitted.append(k.get("reason", ""))), \
         mock.patch.object(utils, "get_bot_owner", return_value="trend_bot"), \
         mock.patch.object(utils, "live_equity_price", return_value=price), \
         mock.patch.object(tiered_hold, "get_hold_tier", return_value=tier):
        trend_bot.manage_position("AAPL", FakePosition("AAPL", 10, entry, side=side),
                                  latest, price, local_adx=30.0,
                                  bull_cross=True, bear_cross=False)
    return submitted


class SurvivorRiskExitTest(unittest.TestCase):
    """survivor: -3% stop, +5% target, RSI>70."""

    def test_stop_loss_fires_during_the_eod_eval_window(self):
        # 15:30-15:45 with a HOLD tier used to `return` before any risk check.
        reasons = _survivor_at("15:35", live_price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"a held-overnight position ignored its -10% stop: {reasons}")

    def test_stop_loss_fires_after_the_eod_close_window(self):
        reasons = _survivor_at("15:50", live_price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"a held-overnight position ignored its -10% stop: {reasons}")

    def test_take_profit_fires_during_the_hold_window(self):
        reasons = _survivor_at("15:35", live_price=120.0)
        self.assertTrue(any("Take Profit" in r for r in reasons), reasons)

    def test_hold_still_overrides_the_eod_sweep_when_no_risk_exit_fires(self):
        # The feature is intact: a flat position tiered HOLD is NOT liquidated.
        reasons = _survivor_at("15:50", live_price=100.5)
        self.assertEqual(reasons, [], f"held position was swept anyway: {reasons}")

    def test_close_eod_tier_still_liquidates(self):
        reasons = _survivor_at("15:50", live_price=100.5, tier="CLOSE_EOD")
        self.assertTrue(any("EOD Liquidation" in r for r in reasons), reasons)

    def test_intraday_stop_is_unaffected(self):
        reasons = _survivor_at("11:00", live_price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons), reasons)


class TrendRiskExitTest(unittest.TestCase):
    """trend: -5% stop, +8% target, crossover exits."""

    def test_stop_loss_fires_during_the_eod_eval_window(self):
        reasons = _trend_at("15:35", price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"a held-overnight position ignored its -10% stop: {reasons}")

    def test_stop_loss_fires_after_the_eod_close_window(self):
        reasons = _trend_at("15:50", price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"a held-overnight position ignored its -10% stop: {reasons}")

    def test_short_stop_loss_fires_during_the_hold_window(self):
        # A short gapping UP is the loss case that overnight holds expose.
        reasons = _trend_at("15:35", price=110.0, side="short")
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"a held-overnight short ignored its stop: {reasons}")

    def test_take_profit_fires_during_the_hold_window(self):
        reasons = _trend_at("15:35", price=120.0)
        self.assertTrue(any("Take Profit" in r for r in reasons), reasons)

    def test_hold_still_overrides_the_eod_sweep_when_no_risk_exit_fires(self):
        reasons = _trend_at("15:50", price=100.5)
        self.assertEqual(reasons, [], f"held position was swept anyway: {reasons}")

    def test_close_eod_tier_still_liquidates(self):
        reasons = _trend_at("15:50", price=100.5, tier="CLOSE_EOD")
        self.assertTrue(any("EOD Liquidation" in r for r in reasons), reasons)


class OvernightStopsStillUnwiredTest(unittest.TestCase):
    """Documents the remaining gap rather than pretending it is closed."""

    def test_overnight_stop_percentages_are_defined_but_unread(self):
        self.assertIn("HOLD_OVERNIGHT", tiered_hold.OVERNIGHT_STOPS)
        self.assertIn("stop_loss_pct", tiered_hold.OVERNIGHT_STOPS["HOLD_OVERNIGHT"])
        # The bots enforce their OWN stop percentages now, which is what closes
        # the hole. tiered_hold's per-tier stops remain unwired — if that ever
        # changes, this test should be replaced by one that exercises them.
        # Checked against the parsed tree, not the raw text: a comment naming
        # the constant is documentation, not a read.
        for module in (survivor_bot, trend_bot):
            with open(module.__file__) as f:
                tree = ast.parse(f.read(), filename=module.__file__)
            reads = [n for n in ast.walk(tree)
                     if (isinstance(n, ast.Attribute) and n.attr == "OVERNIGHT_STOPS")
                     or (isinstance(n, ast.Name) and n.id == "OVERNIGHT_STOPS")]
            self.assertEqual(reads, [],
                             f"{module.__name__} now reads OVERNIGHT_STOPS — "
                             f"update this test to cover the wiring.")


# ---------------------------------------------------------------------------
# Stale bars must not suppress risk management (PR #22 review, finding 3).
#
# The first fix moved risk exits ahead of the hold branch inside
# manage_position. The freshness guard then reintroduced the same failure one
# layer up: get_data_alpaca returned None for a stale frame, cycle() did
# `continue`, and manage_position was never called at all. A stale history
# feed plus a perfectly healthy live quote therefore suppressed stop losses,
# take profits, max-hold and EOD liquidation on every held position.
#
# These tests drive cycle(), not manage_position — the bug lived in the
# caller, so a test that calls manage_position directly cannot see it.
# ---------------------------------------------------------------------------

def _bars(rows=300, price=100.0):
    idx = pd.to_datetime(
        [dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc) - dt.timedelta(minutes=15 * i)
         for i in range(rows - 1, -1, -1)], utc=True).tz_convert("America/New_York")
    return pd.DataFrame({"open": price, "high": price, "low": price,
                         "close": price, "volume": 1.0}, index=idx)


def _survivor_cycle(indicators_ok, live_price, entry=100.0, time_str="11:00",
                    tier="HOLD_SWING", df=None):
    bot = survivor_bot.bot
    submitted = []
    pos = FakePosition("AAPL", 10, entry)
    frame = df if df is not None else _bars()
    with mock.patch.object(survivor_bot, "get_segregated_targets", return_value=({}, set())), \
         mock.patch.object(survivor_bot, "get_data_alpaca", return_value=(frame, indicators_ok)), \
         mock.patch.object(survivor_bot, "get_trend_sma", return_value=50.0), \
         mock.patch.object(bot, "my_symbols", return_value=["AAPL"]), \
         mock.patch.object(bot, "pos_dict", {"AAPL": pos}), \
         mock.patch.object(bot, "pending_symbols", set()), \
         mock.patch.object(bot, "in_cooldown", return_value=False), \
         mock.patch.object(bot, "time_str", time_str), \
         mock.patch.object(bot, "is_eod_eval", "15:30" <= time_str < "15:45"), \
         mock.patch.object(bot, "is_eod_close", time_str >= "15:45"), \
         mock.patch.object(bot, "regime", "SIDEWAYS"), \
         mock.patch.object(bot, "vix", 15.0), \
         mock.patch.object(bot, "hours_held", return_value=2.0), \
         mock.patch.object(utils, "live_equity_price", return_value=live_price), \
         mock.patch.object(tiered_hold, "get_hold_tier", return_value=tier), \
         mock.patch.object(bot, "submit",
                           side_effect=lambda *a, **k: submitted.append(k.get("reason", ""))):
        survivor_bot.cycle(bot)
    return submitted


def _trend_cycle(indicators_ok, live_price, entry=100.0, time_str="11:00",
                 tier="HOLD_SWING", side="long", df=None):
    bot = trend_bot.bot
    submitted = []
    pos = FakePosition("AAPL", 10, entry, side=side)
    frame = df if df is not None else _bars()
    with mock.patch.object(bot, "targets", return_value={}), \
         mock.patch.object(trend_bot, "get_data_alpaca", return_value=(frame, indicators_ok)), \
         mock.patch.object(bot, "my_symbols", return_value=["AAPL"]), \
         mock.patch.object(bot, "pos_dict", {"AAPL": pos}), \
         mock.patch.object(bot, "positions", []), \
         mock.patch.object(bot, "pending_symbols", set()), \
         mock.patch.object(bot, "in_cooldown", return_value=False), \
         mock.patch.object(bot, "time_str", time_str), \
         mock.patch.object(bot, "is_eod_eval", "15:30" <= time_str < "15:45"), \
         mock.patch.object(bot, "is_eod_close", time_str >= "15:45"), \
         mock.patch.object(bot, "regime", "SIDEWAYS"), \
         mock.patch.object(bot, "vix", 15.0), \
         mock.patch.object(bot, "hours_held", return_value=2.0), \
         mock.patch.object(utils, "get_bot_owner", return_value="trend_bot"), \
         mock.patch.object(utils, "live_equity_price", return_value=live_price), \
         mock.patch.object(tiered_hold, "get_hold_tier", return_value=tier), \
         mock.patch.object(bot, "submit",
                           side_effect=lambda *a, **k: submitted.append(k.get("reason", ""))):
        trend_bot.cycle(bot)
    return submitted


class StaleBarsStillManagePositionsTest(unittest.TestCase):

    def test_survivor_stop_fires_on_stale_bars_with_a_fresh_quote(self):
        reasons = _survivor_cycle(indicators_ok=False, live_price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"stale bars suppressed the stop loss: {reasons}")

    def test_trend_stop_fires_on_stale_bars_with_a_fresh_quote(self):
        reasons = _trend_cycle(indicators_ok=False, live_price=90.0)
        self.assertTrue(any("Stop Loss" in r for r in reasons),
                        f"stale bars suppressed the stop loss: {reasons}")

    def test_trend_short_stop_fires_on_stale_bars(self):
        reasons = _trend_cycle(indicators_ok=False, live_price=110.0, side="short")
        self.assertTrue(any("Stop Loss" in r for r in reasons), reasons)

    def test_survivor_eod_liquidation_survives_stale_bars(self):
        reasons = _survivor_cycle(indicators_ok=False, live_price=100.0,
                                  time_str="15:50", tier="CLOSE_EOD")
        self.assertTrue(any("EOD Liquidation" in r for r in reasons),
                        f"stale bars suppressed EOD liquidation: {reasons}")

    def test_trend_eod_liquidation_survives_stale_bars(self):
        reasons = _trend_cycle(indicators_ok=False, live_price=100.0,
                               time_str="15:50", tier="CLOSE_EOD")
        self.assertTrue(any("EOD Liquidation" in r for r in reasons), reasons)

    def test_survivor_take_profit_fires_on_stale_bars(self):
        reasons = _survivor_cycle(indicators_ok=False, live_price=120.0)
        self.assertTrue(any("Take Profit" in r for r in reasons), reasons)

    def test_no_frame_at_all_still_manages_a_held_position(self):
        # Insufficient indicator history / empty response: df is None, but the
        # live quote is good, so risk exits must still run.
        bot = survivor_bot.bot
        submitted = []
        pos = FakePosition("AAPL", 10, 100.0)
        with mock.patch.object(survivor_bot, "get_segregated_targets", return_value=({}, set())), \
             mock.patch.object(survivor_bot, "get_data_alpaca", return_value=(None, False)), \
             mock.patch.object(bot, "my_symbols", return_value=["AAPL"]), \
             mock.patch.object(bot, "pos_dict", {"AAPL": pos}), \
             mock.patch.object(bot, "pending_symbols", set()), \
             mock.patch.object(bot, "in_cooldown", return_value=False), \
             mock.patch.object(bot, "time_str", "11:00"), \
             mock.patch.object(bot, "is_eod_eval", False), \
             mock.patch.object(bot, "is_eod_close", False), \
             mock.patch.object(bot, "regime", "SIDEWAYS"), \
             mock.patch.object(bot, "vix", 15.0), \
             mock.patch.object(bot, "hours_held", return_value=2.0), \
             mock.patch.object(utils, "live_equity_price", return_value=90.0), \
             mock.patch.object(tiered_hold, "get_hold_tier", return_value="HOLD_SWING"), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda *a, **k: submitted.append(k.get("reason", ""))):
            survivor_bot.cycle(bot)
        self.assertTrue(any("Stop Loss" in r for r in submitted), submitted)

    def test_stale_bars_still_block_new_entries(self):
        # The guard must keep doing its job on the ENTRY side.
        bot = survivor_bot.bot
        entered = []
        with mock.patch.object(survivor_bot, "get_segregated_targets",
                               return_value=({"AAPL": {"confidence": 0.9}}, set())), \
             mock.patch.object(survivor_bot, "get_data_alpaca", return_value=(_bars(), False)), \
             mock.patch.object(bot, "my_symbols", return_value=[]), \
             mock.patch.object(bot, "pos_dict", {}), \
             mock.patch.object(bot, "pending_symbols", set()), \
             mock.patch.object(bot, "in_cooldown", return_value=False), \
             mock.patch.object(bot, "time_str", "11:00"), \
             mock.patch.object(survivor_bot, "try_entry",
                               side_effect=lambda *a, **k: entered.append(a)):
            survivor_bot.cycle(bot)
        self.assertEqual(entered, [], "stale bars were used to open a position")

    def test_no_price_at_all_refuses_to_guess(self):
        # Stale bars AND no live quote: there is no validated price, so the
        # bot must stand down loudly rather than risk-manage off an old close.
        reasons = _survivor_cycle(indicators_ok=False, live_price=None)
        self.assertEqual(reasons, [],
                         "traded on an unvalidated price when no quote was available")


class IndicatorExitsAreSuppressedWhenBlindTest(unittest.TestCase):
    """Only the bar-derived decisions stop; the price-derived ones continue."""

    def test_survivor_rsi_exit_needs_indicators(self):
        # RSI 80 would normally trigger "RSI Overbought" — but with stale bars
        # that RSI is weeks old, so the exit must not fire on it.
        bot = survivor_bot.bot
        reasons = []
        with mock.patch.object(bot, "pending_symbols", set()), \
             mock.patch.object(bot, "time_str", "11:00"), \
             mock.patch.object(bot, "is_eod_eval", False), \
             mock.patch.object(bot, "is_eod_close", False), \
             mock.patch.object(bot, "regime", "SIDEWAYS"), \
             mock.patch.object(bot, "vix", 15.0), \
             mock.patch.object(bot, "hours_held", return_value=2.0), \
             mock.patch.object(utils, "live_equity_price", return_value=100.0), \
             mock.patch.object(tiered_hold, "get_hold_tier", return_value="HOLD_SWING"), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda *a, **k: reasons.append(k.get("reason", ""))):
            survivor_bot.manage_position("AAPL", FakePosition("AAPL", 10, 100.0),
                                         rsi=80.0, price=None, indicators_ok=False)
        self.assertEqual(reasons, [], f"acted on a stale RSI: {reasons}")

    def test_trend_crossover_exit_needs_indicators(self):
        bot = trend_bot.bot
        reasons = []
        with mock.patch.object(bot, "pending_symbols", set()), \
             mock.patch.object(bot, "time_str", "11:00"), \
             mock.patch.object(bot, "is_eod_eval", False), \
             mock.patch.object(bot, "is_eod_close", False), \
             mock.patch.object(bot, "regime", "SIDEWAYS"), \
             mock.patch.object(bot, "vix", 15.0), \
             mock.patch.object(bot, "hours_held", return_value=2.0), \
             mock.patch.object(utils, "get_bot_owner", return_value="trend_bot"), \
             mock.patch.object(utils, "live_equity_price", return_value=100.0), \
             mock.patch.object(tiered_hold, "get_hold_tier", return_value="HOLD_SWING"), \
             mock.patch.object(bot, "submit",
                               side_effect=lambda *a, **k: reasons.append(k.get("reason", ""))):
            trend_bot.manage_position("AAPL", FakePosition("AAPL", 10, 100.0),
                                      latest=None, price=None, local_adx=None,
                                      bull_cross=False, bear_cross=True,
                                      indicators_ok=False)
        self.assertEqual(reasons, [], f"acted on a stale crossover: {reasons}")


if __name__ == "__main__":
    unittest.main()
