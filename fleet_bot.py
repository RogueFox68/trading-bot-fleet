"""Shared runner for fleet bots.

Absorbs the ~400 lines of scaffolding every bot used to copy-paste —
clients, Discord webhook, market-hours gate, regime/VIX read, targets
loading, ownership priming, pending-order tracking, budget gate, EOD
windows, failed-symbol cooldowns, tagged order submission, and the
main-loop exception wrapper — so a bot script is just its strategy.

Usage (see new_bot_template.py for a full example):

    bot = FleetBot("survivor_bot")

    def cycle(bot):
        targets = bot.targets("survivor_targets")
        ...decide...
        bot.submit(order_request, reason="Bought Dip", notify="msg")

    bot.run(cycle)

Deliberately a helper library, not a framework: the bot owns its loop
body, the runner owns the plumbing. Strategy semantics (indicators,
entry/exit rules, sizing constants) stay in the bot script.
"""
import datetime
import json
import os
import time

import pytz
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from alpaca.data.historical import StockHistoricalDataClient

import config
import fleet_registry
import utils
from logger import get_logger, registry

TARGET_FILE = "active_targets.json"
CONFIG_FILE = "bot_config.json"
FAILED_SYMBOL_COOLDOWN = 3600  # seconds to skip a symbol after an order error
EASTERN = pytz.timezone('America/New_York')


class FleetBot:
    def __init__(self, name, loop_seconds=60, market_hours=True):
        if name not in fleet_registry.BOTS:
            raise ValueError(f"{name} is not in fleet_registry.BOTS - add it there first")
        self.name = name
        self.reg = fleet_registry.BOTS[name]
        self.loop_seconds = loop_seconds
        self.market_hours = market_hours

        self.logger = get_logger(name)
        self.trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
        self.data_client = StockHistoricalDataClient(config.API_KEY, config.SECRET_KEY)
        self.webhook = getattr(config, self.reg["webhook"], "")

        self.failed_symbols = {}

        # Per-cycle context, populated by refresh()
        self.account = None
        self.equity = 0.0
        self.positions = []
        self.pos_dict = {}
        self.entry_times = {}
        self.pending_symbols = set()
        self.regime = "SIDEWAYS"
        self.vix = 15.0
        self.capital_crunch = False
        self.budget_ok = True
        self.time_str = ""
        self.is_eod_eval = False        # 15:30-15:45 ET: tiered-hold scoring window
        self.is_eod_close = False       # 15:45+ ET: liquidation window
        self.is_eod_skip_entry = False  # 14:00+ ET: no new entries

    # ---- Discord -------------------------------------------------------
    def notify(self, msg):
        if not self.webhook or "YOUR" in self.webhook:
            return
        try:
            requests.post(self.webhook, json={"content": msg})
        except Exception as e:
            registry.log_error(self.name, "send_discord", e, context=str(msg)[:50])
            self.logger.error(f"Discord webhook failed: {e}")

    # ---- Cooldowns -----------------------------------------------------
    def in_cooldown(self, symbol):
        t = self.failed_symbols.get(symbol)
        if t is None:
            return False
        if (time.time() - t) < FAILED_SYMBOL_COOLDOWN:
            return True
        del self.failed_symbols[symbol]  # cooldown elapsed, allow a retry
        return False

    def cooldown(self, symbol):
        self.failed_symbols[symbol] = time.time()

    # ---- Orders --------------------------------------------------------
    def tag(self, symbol):
        """client_order_id in the fleet's ownership format."""
        return f"{self.name}-{symbol.replace('/', '')}-{int(time.time())}"

    def market_order(self, symbol, qty, side, tif=TimeInForce.DAY):
        return MarketOrderRequest(symbol=symbol, qty=qty, side=side,
                                  time_in_force=tif, client_order_id=self.tag(symbol))

    def submit(self, order_data, reason="", action=None, notify=None):
        """Submit through the shared safety gates; on error, log + cooldown.

        Returns the (possibly still-pending) order, or None on failure.
        """
        try:
            order = utils.submit_and_log_order(self.trading_client, order_data,
                                               self.logger, reason=reason, log_action=action)
            if notify:
                self.notify(notify)
            return order
        except Exception as e:
            self.logger.error(f"    [!] Order error {getattr(order_data, 'symbol', '?')}: {e}")
            self.cooldown(getattr(order_data, 'symbol', ''))
            return None

    # ---- Targets / config ----------------------------------------------
    def targets(self, strategy_key, fallback=None):
        """Validated {symbol: {confidence, ...}} for a strategy bucket."""
        return utils.load_and_validate_targets(TARGET_FILE, strategy_key,
                                               {} if fallback is None else fallback)

    def _read_global_settings(self):
        try:
            with open(CONFIG_FILE, 'r') as f:
                gs = json.load(f).get('global_settings', {})
            self.regime = gs.get('market_condition', 'SIDEWAYS')
            self.vix = gs.get('vix', 15.0)
            self.capital_crunch = gs.get('CAPITAL_CRUNCH', False)
        except Exception as e:
            registry.log_error(self.name, "read_config", e)
            self.logger.error(f"[!] Config load error: {e}")
            self.regime, self.vix, self.capital_crunch = "SIDEWAYS", 15.0, False

    # ---- Per-cycle context ----------------------------------------------
    def refresh(self):
        """Fetch account, positions, ownership, pending orders, regime, EOD flags."""
        self._read_global_settings()

        self.account = self.trading_client.get_account()
        self.equity = float(self.account.portfolio_value)
        self.positions = self.trading_client.get_all_positions()
        self.pos_dict = {p.symbol: p for p in self.positions}

        # Prime the ownership cache with full order-history coverage for held
        # equities, so aged positions (opening order buried behind a crypto
        # order flood) still resolve owner + entry time this cycle.
        equity_syms = [p.symbol for p in self.positions if p.asset_class == AssetClass.US_EQUITY]
        utils.prime_ownership(self.trading_client, equity_syms)
        self.entry_times = utils.get_position_entry_times(self.trading_client,
                                                          held_symbols=equity_syms)

        open_orders = self.trading_client.get_orders(filter=GetOrdersRequest(status="open"))
        self.pending_symbols = {o.symbol for o in open_orders}

        now_et = datetime.datetime.now(EASTERN)
        self.time_str = now_et.strftime('%H:%M')
        self.is_eod_eval = "15:30" <= self.time_str < "15:45"
        self.is_eod_close = self.time_str >= "15:45"
        self.is_eod_skip_entry = self.time_str >= "14:00"

        # Batch budget check once per cycle to prevent log spam
        self.budget_ok = utils.check_budget(self.name, self.trading_client)

    def my_symbols(self):
        """Equity symbols this bot owns (by order-tag resolution)."""
        return [p.symbol for p in self.positions
                if p.asset_class == AssetClass.US_EQUITY
                and utils.get_bot_owner(p.symbol, p.asset_class, self.trading_client) == self.name]

    def hours_held(self, symbol):
        """Hours since the position's entry fill, or None if unresolvable."""
        entry_dt = self.entry_times.get(symbol)
        if entry_dt is None:
            return None
        return (datetime.datetime.now(datetime.timezone.utc) - entry_dt).total_seconds() / 3600.0

    # ---- Sizing ----------------------------------------------------------
    def size_position(self, price, confidence, risk_per_trade, size_mult=1.0,
                      max_position_pct=0.10):
        """Confidence-scaled share count, capped by CFO budget, max position
        %, and RegT buying power. Returns (qty, order_cost); qty 0 = skip."""
        scaler = 0.5 + confidence
        risk_amt = self.equity * risk_per_trade * scaler * float(size_mult)

        available_budget = utils.get_available_budget(self.name, self.trading_client)
        risk_amt = min(risk_amt, available_budget)

        qty = int(risk_amt / price)
        max_qty = int((self.equity * max_position_pct) / price)
        qty = min(qty, max_qty)

        order_cost = float(qty) * float(price)
        buying_power = float(self.account.regt_buying_power)
        if order_cost > buying_power:
            self.logger.info(f"    [SKIP] Cost ${order_cost:.0f} > RegT Buying Power ${buying_power:.0f}")
            return 0, order_cost
        return qty, order_cost

    # ---- Main loop --------------------------------------------------------
    def _market_open(self):
        try:
            return self.trading_client.get_clock().is_open
        except Exception as e:
            registry.log_error(self.name, "get_clock", e)
            return True  # matches legacy behavior: trade on through a clock hiccup

    def run(self, cycle):
        self.logger.info(f"--- {self.name.upper()} STARTED (fleet_bot runner) ---")
        while True:
            try:
                if self.market_hours and not self._market_open():
                    self.logger.info("Market Closed. Sleeping 15m...")
                    time.sleep(900)
                    continue

                self.refresh()
                cycle(self)
                time.sleep(self.loop_seconds)

            except Exception as e:
                registry.log_error(self.name, "main_loop", e)
                self.logger.error(f"{self.name} error: {e}", exc_info=True)
                time.sleep(60)
