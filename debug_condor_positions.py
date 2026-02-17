
import config
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass

def debug_condor_positions():
    print("--- CONDOR BOT DEBUGGER ---")
    
    # 1. Connect
    try:
        trading_client = TradingClient(config.API_KEY, config.SECRET_KEY, paper=config.PAPER)
        account = trading_client.get_account()
        print(f"Connected to Account: {account.account_number} (Status: {account.status})")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    # 2. Fetch Raw Positions
    print("\n[1] Fetching All Positions...")
    positions = trading_client.get_all_positions()
    print(f"Raw Count: {len(positions)}")
    
    # 3. Simulate Condor Logic (NEW)
    print("\n[2] Simulating New Logic (Leg Counting)...")
    condor_positions = 0
    active_tickers = set()
    root_leg_counts = {}
    
    for p in positions:
        print(f"   - Checking {p.symbol} ({p.asset_class})... ", end="")
        
        if p.asset_class == AssetClass.US_OPTION:
            root = p.symbol
            # Extract root symbol logic...
            match = False
            for i, char in enumerate(p.symbol):
                if char.isdigit():
                    root = p.symbol[:i]
                    match = True
                    break
            
            if match:
                active_tickers.add(root)
                root_leg_counts[root] = root_leg_counts.get(root, 0) + 1
                print(f"MATCH! Root: {root}")
            else:
                print("No regex match.")
        else:
            print("Not an option.")

    # Only count roots with 2+ legs as "Condor Positions"
    condor_positions = sum(1 for c in root_leg_counts.values() if c >= 2)

    print("\n[3] Results:")
    print(f"   Condor Slots Used (>=2 legs): {condor_positions}")
    print(f"   Busy Roots (Total): {len(active_tickers)}")
    print(f"   Root Leg Counts: {root_leg_counts}")
    
    print("\nInterpretation:")
    print(f"   Status: {condor_positions}/3 Condors Active.")
    if condor_positions == 0 and len(positions) > 0:
        print("   SUCCESS: Single legs are ignored for the limit!")

if __name__ == "__main__":
    debug_condor_positions()
