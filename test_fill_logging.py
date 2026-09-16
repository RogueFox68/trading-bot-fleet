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
import json
import os
import shutil
import tempfile
import time
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
    def __init__(self, symbol, client_order_id, submitted_at, filled_at, side,
                 status="filled", filled_qty=1):
        self.id = client_order_id  # unique -> feeds _fetch_orders_covering dedup
        self.symbol = symbol
        self.client_order_id = client_order_id
        self.submitted_at = submitted_at
        self.filled_at = filled_at
        self.side = side
        self.status = SimpleNamespace(value=status)
        self.filled_avg_price = 1.23
        self.filled_qty = filled_qty
        self.canceled_at = submitted_at


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

    def test_reconcile_routes_full_and_terminal_partial(self):
        """Full fills take the filled_at path; a terminal partial-then-canceled
        order (no filled_at) is recovered via log_terminal_partial_fill."""
        now = datetime.datetime.now(UTC)
        full = _ReconcileOrder(
            "AAA260731P00045000", "wheel_bot-AAA-1",
            now - datetime.timedelta(hours=2), now - datetime.timedelta(hours=1),
            utils.OrderSide.BUY, status="filled")
        partial = _ReconcileOrder(
            "BBB260731P00045000", "wheel_bot-BBB-2",
            now - datetime.timedelta(hours=3), None,
            utils.OrderSide.BUY, status="canceled", filled_qty=2)
        crypto = _ReconcileOrder(
            "BTC/USD", "crypto_grid-9", now - datetime.timedelta(minutes=1),
            now - datetime.timedelta(minutes=1), utils.OrderSide.BUY)
        client = _ReconcileClient([full, partial, crypto])

        full_path, partial_path = [], []
        with mock.patch.object(
                utils, "_log_fill_to_influx",
                side_effect=lambda o, lg, action=None, reason="": full_path.append(o.symbol)), \
             mock.patch.object(
                utils, "log_terminal_partial_fill",
                side_effect=lambda o, lg, reason="": (partial_path.append(o.symbol), 1)[1]):
            n = utils.reconcile_fills(client, utils.logger)

        self.assertEqual(full_path, ["AAA260731P00045000"])      # full-fill path
        self.assertEqual(partial_path, ["BBB260731P00045000"])   # terminal-partial path
        self.assertNotIn("BTC/USD", full_path + partial_path)    # crypto excluded
        self.assertEqual(n, 2)


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
        # The runner owns the client now: patch it on wheel_bot.bot.
        with mock.patch.object(wheel_bot.bot, "trading_client", client), \
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

    def test_partial_rung_is_logged(self):
        """The canceled rung's partial fill is handed to log_terminal_partial_fill
        so it isn't lost from the P&L measurement."""
        import wheel_bot
        client = _CloseClient([
            {"poll": "partially_filled", "filled_qty": 2},
            {"poll": "filled", "filled_qty": 3},
        ])
        active = SimpleNamespace(symbol="PAAS260731P00045000", qty="-5")
        quote = mock.Mock(bid_price=1.0, ask_price=1.2)
        with mock.patch.object(wheel_bot.bot, "trading_client", client), \
             mock.patch.object(wheel_bot, "get_option_data", return_value=quote), \
             mock.patch.object(wheel_bot.utils, "submit_and_log_order",
                               side_effect=_make_submit(client)), \
             mock.patch.object(wheel_bot.utils, "log_terminal_partial_fill") as logp, \
             mock.patch.object(wheel_bot.time, "sleep"):
            wheel_bot.close_option_position(active, "rollcls", "test")
        self.assertEqual(logp.call_count, 1)                             # the rung-1 partial
        self.assertEqual(int(float(logp.call_args[0][0].filled_qty)), 2)  # 2 contracts


# --- log_terminal_partial_fill: recover canceled-rung partials, no double-count ---
class _TerminalOrder:
    def __init__(self, status="canceled", filled_qty=2, filled_avg_price=0.42,
                 filled_at=None, oid="ord-123", symbol="PAAS260731P00045000",
                 client_order_id="wheel_bot-PAAS260731P00045000-abc", side=None):
        self.symbol = symbol
        self.status = SimpleNamespace(value=status)
        self.filled_qty = filled_qty
        self.filled_avg_price = filled_avg_price
        self.filled_at = filled_at
        self.id = oid
        self.client_order_id = client_order_id
        self.side = side if side is not None else utils.OrderSide.BUY
        t = datetime.datetime(2026, 7, 7, 15, 0, 0, tzinfo=UTC)
        self.canceled_at = self.updated_at = self.submitted_at = t


def _capture_partial(order):
    with mock.patch.object(utils.requests, "post") as post:
        post.return_value = mock.Mock(status_code=204)
        n = utils.log_terminal_partial_fill(order, utils.logger)
        line = post.call_args.kwargs["data"] if post.called else None
    return n, line, post.call_count


class TerminalPartialFillTest(unittest.TestCase):
    def test_writes_row_for_terminal_partial(self):
        n, line, calls = _capture_partial(_TerminalOrder())
        self.assertEqual((n, calls), (1, 1))
        self.assertIn("wheel_trades,symbol=PAAS260731P00045000", line)
        self.assertIn('action="buy_close"', line)
        self.assertIn("qty=2", line)
        self.assertIn('fill_source="terminal_partial"', line)
        self.assertIn('source_order_id="ord-123"', line)

    def test_expired_partial_is_logged(self):
        n, _, calls = _capture_partial(_TerminalOrder(status="expired"))
        self.assertEqual((n, calls), (1, 1))

    def test_open_partial_is_never_logged(self):
        # Critical guardrail: an OPEN partially_filled may still fully fill and be
        # logged at filled_at — logging it here too would double-count.
        n, _, calls = _capture_partial(_TerminalOrder(status="partially_filled"))
        self.assertEqual((n, calls), (0, 0))

    def test_order_with_filled_at_left_to_full_fill_path(self):
        n, _, calls = _capture_partial(
            _TerminalOrder(status="canceled", filled_at=datetime.datetime.now(UTC)))
        self.assertEqual((n, calls), (0, 0))

    def test_zero_fill_writes_nothing(self):
        n, _, calls = _capture_partial(_TerminalOrder(filled_qty=0))
        self.assertEqual((n, calls), (0, 0))

    def test_stamp_is_deterministic_for_idempotent_reruns(self):
        _, line_a, _ = _capture_partial(_TerminalOrder())
        _, line_b, _ = _capture_partial(_TerminalOrder())
        self.assertEqual(line_a.rsplit(" ", 1)[1], line_b.rsplit(" ", 1)[1])


if __name__ == "__main__":
    unittest.main()


class ReasonSurvivesLateFillTest(unittest.TestCase):
    """A trade row keeps its reason even when the submit poll misses the fill.

    --- The defect this pins ----------------------------------------------

    On 2026-09-15 the day's entries logged reason=None: MSTR (trades, 13:32Z)
    and USFD (survivor_trades, 13:32Z). Every row on 9/14 had one ("Entry
    (Crossover)", "Bought Dip", "EOD Liquidation").

    It was NOT a dropped argument, and no commit caused it — HEAD was
    2026-09-11 and nothing landed on 9/14 or 9/15. `reason` propagates
    correctly on every submit-time path.

    The real cause: submit_and_log_order only logs a MARKET order that
    reaches `filled` inside its 5s poll. When the poll elapses it deliberately
    writes NOTHING (a now()-stamped partial would double-count against the
    reconciled row), so the row is created later by reconcile_fills — running
    in the ACCOUNTANT process, which called _log_fill_to_influx with no
    reason=. _log_fill_to_influx omits the field entirely when empty, so the
    row reads back as reason=None.

    The rows without a reason are therefore exactly the orders that took
    longer than 5s to fill. 13:32Z is 09:32 ET — two minutes after the open,
    when fill latency is worst. 9/14's orders simply filled inside the poll.
    This had been true since equity reconciliation landed on 2026-07-01.

    The reason is known in the BOT process and needed in the ACCOUNTANT
    process, so it has to be written down. These tests drive that handoff.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "order_reasons.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_remembered_reason_comes_back(self):
        utils.remember_order_reason("ord-1", "Entry (Crossover)", file_path=self.path)
        self.assertEqual(utils.load_order_reasons(self.path)["ord-1"],
                         "Entry (Crossover)")

    def test_a_missing_file_is_an_empty_map_not_an_error(self):
        self.assertEqual(utils.load_order_reasons(self.path + ".nope"), {})

    def test_a_corrupt_line_does_not_lose_the_others(self):
        utils.remember_order_reason("ord-1", "Bought Dip", file_path=self.path)
        with open(self.path, "a") as f:
            f.write("{not json\n\n")
        utils.remember_order_reason("ord-2", "EOD Liquidation", file_path=self.path)
        reasons = utils.load_order_reasons(self.path)
        self.assertEqual(reasons["ord-1"], "Bought Dip")
        self.assertEqual(reasons["ord-2"], "EOD Liquidation")

    def test_entries_past_the_reconcile_window_are_dropped(self):
        utils.remember_order_reason("old", "Entry (Crossover)", file_path=self.path)
        with open(self.path) as f:
            rec = json.loads(f.read().strip())
        rec["ts"] = time.time() - 40 * 86400
        with open(self.path, "w") as f:
            f.write(json.dumps(rec) + "\n")
        self.assertNotIn("old", utils.load_order_reasons(self.path))

    def test_an_empty_reason_is_not_recorded(self):
        self.assertFalse(utils.remember_order_reason("ord-1", "", file_path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_concurrent_writers_do_not_clobber_each_other(self):
        # Five bot processes share this file; append-only is why that is safe.
        for i in range(5):
            utils.remember_order_reason(f"ord-{i}", f"Entry ({i})", file_path=self.path)
        reasons = utils.load_order_reasons(self.path)
        self.assertEqual(len(reasons), 5)
        self.assertEqual(reasons["ord-3"], "Entry (3)")

    def test_the_file_is_compacted_once_it_grows_past_the_cap(self):
        over = utils.ORDER_REASON_MAX_LINES + 50
        now = time.time()
        with open(self.path, "w") as f:
            for i in range(over):
                f.write(json.dumps({"id": f"o{i}", "reason": "r", "ts": now}) + "\n")
        utils.load_order_reasons(self.path)
        with open(self.path) as f:
            remaining = [ln for ln in f if ln.strip()]
        self.assertLessEqual(len(remaining), utils.ORDER_REASON_MAX_LINES)
        # Newest are the ones kept — they are the ones still reconcilable.
        self.assertIn(f'"o{over - 1}"', remaining[-1])

    def test_reconcile_writes_the_remembered_reason(self):
        """The end-to-end case: a fill the submit poll never saw."""
        now = datetime.datetime.now(UTC)
        late = _ReconcileOrder(
            "MSTR", "trend_bot-MSTR-1",
            now - datetime.timedelta(minutes=30), now - datetime.timedelta(minutes=29),
            utils.OrderSide.SELL, status="filled")
        late.id = "late-order-1"
        client = _ReconcileClient([late])

        utils.remember_order_reason("late-order-1", "Entry (Crossover)",
                                    file_path=self.path)
        seen = {}
        with mock.patch.object(utils, "ORDER_REASON_FILE", self.path), \
             mock.patch.object(
                utils, "_log_fill_to_influx",
                side_effect=lambda o, lg, action=None, reason="", outbox=None:
                    seen.update({"reason": reason}) or 1):
            utils.reconcile_fills(client, utils.logger)

        self.assertEqual(seen.get("reason"), "Entry (Crossover)",
                         "the reconciled row lost the reason again")

    def test_an_unknown_order_reconciles_with_no_reason_rather_than_failing(self):
        now = datetime.datetime.now(UTC)
        o = _ReconcileOrder("USFD", "survivor_bot-USFD-1",
                            now - datetime.timedelta(minutes=30),
                            now - datetime.timedelta(minutes=29),
                            utils.OrderSide.BUY, status="filled")
        o.id = "never-recorded"
        seen = {}
        with mock.patch.object(utils, "ORDER_REASON_FILE", self.path), \
             mock.patch.object(
                utils, "_log_fill_to_influx",
                side_effect=lambda ord_, lg, action=None, reason="", outbox=None:
                    seen.update({"reason": reason}) or 1):
            utils.reconcile_fills(_ReconcileClient([o]), utils.logger)
        self.assertEqual(seen.get("reason"), "",
                         "an unknown order must degrade to today's behaviour, not raise")

    def test_the_submit_path_records_the_reason_before_polling(self):
        """It must be written even when the poll then elapses unfilled.

        This is THE case that needed it: the order never reaches `filled`
        inside the 5s poll, so submit_and_log_order writes no row at all and
        reconcile_fills creates it later, in another process.
        """
        from alpaca.trading.requests import MarketOrderRequest

        recorded = []
        pending = mock.Mock(
            id="sub-1", symbol="MSTR", side=utils.OrderSide.SELL, qty="10",
            status=mock.Mock(value="accepted"), type=mock.Mock(value="market"),
            filled_qty="0", filled_avg_price=None,
            client_order_id="trend_bot-MSTR-1")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000",
                                                    last_equity="100000")
        client.get_all_positions.return_value = []
        client.submit_order.return_value = pending
        client.get_order_by_id.return_value = pending   # never fills

        with mock.patch.object(utils, "remember_order_reason",
                               side_effect=lambda oid, r, file_path=None:
                                   recorded.append((oid, r))), \
             mock.patch.object(utils, "assert_order_allowed_here"), \
             mock.patch.object(utils, "_log_fill_to_influx"), \
             mock.patch.object(utils, "_safety_price_client",
                               return_value=mock.Mock(**{
                                   "get_stock_latest_trade.return_value": {
                                       "MSTR": mock.Mock(price=100.0)}})), \
             mock.patch("time.sleep"):
            utils.submit_and_log_order(
                client,
                MarketOrderRequest(symbol="MSTR", qty=10,
                                   side=utils.OrderSide.SELL, time_in_force="day"),
                utils.logger, reason="Entry (Crossover)")

        self.assertEqual(recorded, [("sub-1", "Entry (Crossover)")],
                         "the reason was not recorded at submit time, so a late "
                         "fill can never recover it")


class ReasonIsNotRecordedForUnreconciledBotsTest(unittest.TestCase):
    """Crypto bots log their own fills; their reasons would never be read."""

    def test_a_crypto_order_records_nothing(self):
        from alpaca.trading.requests import MarketOrderRequest
        recorded = []
        pending = mock.Mock(
            id="c-1", symbol="BTC/USD", side=utils.OrderSide.BUY, qty="1",
            status=mock.Mock(value="accepted"), type=mock.Mock(value="market"),
            filled_qty="0", filled_avg_price=None,
            client_order_id="crypto_grid-BTCUSD-1")
        client = mock.Mock()
        client.get_account.return_value = mock.Mock(equity="100000",
                                                    last_equity="100000")
        client.get_all_positions.return_value = []
        client.submit_order.return_value = pending
        client.get_order_by_id.return_value = pending

        with mock.patch.object(utils, "remember_order_reason",
                               side_effect=lambda oid, r, file_path=None:
                                   recorded.append((oid, r))), \
             mock.patch.object(utils, "assert_order_allowed_here"), \
             mock.patch.object(utils, "_log_fill_to_influx"), \
             mock.patch("time.sleep"):
            utils.submit_and_log_order(
                client,
                MarketOrderRequest(symbol="BTC/USD", qty=1,
                                   side=utils.OrderSide.BUY, time_in_force="gtc"),
                utils.logger, reason="grid entry")

        self.assertEqual(recorded, [],
                         "crypto is reconciled=False — nothing would ever read this")
