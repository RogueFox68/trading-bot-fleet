import json
import logging
from datetime import datetime, timedelta
from alpaca_trade_api.rest import REST, OptionAPI

# --- Configuration & Setup ---
LOG_FILE = "volatility_bot.log"
CONFIG_FILE = "bot_config.json"
EARNINGS_FILE = "earnings_calendar.json"

logging.basicConfig(
    filename=LOG_FILE, 
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class VolatilityBot:
    def __init__(self, api_key, secret_key, base_url):
        self.api = REST(api_key, secret_key, base_url)
        self.opt_api = OptionAPI(api_key, secret_key, base_url)
        self.allocation = 0.05  # 5% of account equity per earnings event

    def load_config(self):
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)

    def get_earnings_targets(self):
        """Reads the calendar produced by the Analysis Node."""
        try:
            with open(EARNINGS_FILE, 'r') as f:
                return json.load(f)
        except FileNotFoundError:
            logging.error("Earnings calendar missing! Corsair node may be down.")
            return []

    def find_contract_by_delta(self, symbol, exp_date, option_type, target_delta):
        """
        Finds the contract closest to a specific Delta.
        option_type: 'call' or 'put'
        target_delta: e.g., 0.20 for short leg, 0.10 for long leg
        """
        contracts = self.opt_api.get_option_contracts(symbol=symbol, expiration_date=exp_date)
        # In a real implementation, you'd pull the Greeks via Alpaca's snapshot API
        # For this logic: Filter contracts -> Get Snapshot -> Find closest Delta
        best_contract = None
        min_diff = float('inf')

        for c in contracts:
            if c.type == option_type.upper():
                snapshot = self.api.get_option_snapshot(c.symbol)
                delta = snapshot.greeks.delta if snapshot.greeks else 0
                # Delta is negative for puts; use absolute value for comparison
                diff = abs(abs(delta) - target_delta)
                if diff < min_diff:
                    min_diff = diff
                    best_contract = c
        return best_contract

    def execute_credit_spreads(self, symbol, exp_date):
        """Executes the Bull Put and Bear Call spreads."""
        # 1. Identify Legs
        short_put = self.find_contract_by_delta(symbol, exp_date, 'put', 0.20)
        long_put  = self.find_contract_by_delta(symbol, exp_date, 'put', 0.10)
        short_call = self.find_contract_by_delta(symbol, exp_date, 'call', 0.20)
        long_call  = self.find_contract_by_delta(symbol, exp_date, 'call', 0.10)

        if not all([short_put, long_put, short_call, long_call]):
            logging.error(f"Could not find all legs for {symbol}. Skipping.")
            return

        # 2. Calculate Quantity based on allocation
        account = self.api.get_account()
        equity = float(account.equity)
        risk_amount = equity * self.allocation
        # Simplified: Divide risk by the width of the spreads (Strike Diff * 100)
        # For this example, we assume a fixed quantity or calculated based on margin
        qty = 1 

        # 3. Execution Sequence: Buy Wings First (Safety), then Sell Inner Legs
        try:
            # Longs first to cap risk immediately
            self.api.submit_order(symbol=long_put.symbol, qty=qty, side='buy', type='market', time_in_force='day')
            self.api.submit_order(symbol=long_call.symbol, qty=qty, side='buy', type='market', time_in_force='day')
            # Shorts second to collect premium
            self.api.submit_order(symbol=short_put.symbol, qty=qty, side='sell', type='market', time_in_force='day')
            self.api.submit_order(symbol=short_call.symbol, qty=qty, side='sell', type='market', time_in_force='day')
            logging.info(f"Successfully deployed Vol Crush spreads for {symbol}")
        except Exception as e:
            logging.error(f"Execution failed for {symbol}: {e}")

    def close_earnings_positions(self, symbol):
        """Closes all options tied to a specific ticker."""
        positions = self.api.list_positions()
        for pos in positions:
            if symbol in pos.symbol: # Basic check to see if the option is for this stock
                self.api.submit_order(
                    symbol=pos.symbol, 
                    qty=int(pos.qty), 
                    side='sell' if pos.side == 'long' else 'buy', 
                    type='market', 
                    time_in_force='day'
                )
        logging.info(f"Harvested IV crush for {symbol}")

    def run(self):
        config = self.load_config()
        if config.get("emergency_stop") or config.get("regime") == "CRITICAL_VOLATILITY":
            logging.warning("Bot paused: Emergency Stop active or Critical Volatility.")
            return

        targets = self.get_earnings_targets()
        today = datetime.now().date()

        for target in targets:
            report_date = datetime.strptime(target['report_date'], '%Y-%m-%d').date()
            symbol = target['ticker']
            # Find the expiration immediately following report date
            exp_date = target['nearest_expiration'] 

            # ENTRY LOGIC: T-minus 2 days
            if report_date - today == timedelta(days=2):
                logging.info(f"Entering volatility trade for {symbol}")
                self.execute_credit_spreads(symbol, exp_date)

            # EXIT LOGIC: T+1 (Morning After)
            if today - report_date == timedelta(days=1):
                logging.info(f"Exiting volatility trade for {symbol}")
                self.close_earnings_positions(symbol)

# --- Execution Block ---
if __name__ == "__main__":
    bot = VolatilityBot(api_key="...", secret_key="...", base_url="...")
    bot.run()
