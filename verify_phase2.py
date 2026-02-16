
import json
import os
import utils
from logger import get_logger

# Setup Dummy Data
DUMMY_TARGETS = {
    "trend_targets": [
        "NVDA", # Legacy String
        {"symbol": "TSLA", "confidence": 0.85}, # New Object
        {"symbol": "COIN", "confidence": 0.45}
    ],
    "survivor_targets": [
        {"symbol": "TQQQ", "confidence": 0.90}
    ],
    "wheel_targets": [],
    "condor_targets": []
}

TEST_FILE = "test_active_targets.json"

def test_schema_parsing():
    print("--- PHASE 2 SCHEMA VERIFICATION ---")
    
    # 1. Create Dummy File
    with open(TEST_FILE, 'w') as f:
        json.dump(DUMMY_TARGETS, f)
    print("Created dummy target file.")
    
    # 2. Test Utils Loader
    print("\n[Test] Loading via utils.get_targets_with_freshness_check...")
    try:
        raw_trend = utils.get_targets_with_freshness_check(TEST_FILE, "trend_targets", [])
        print(f"   Loaded {len(raw_trend)} items.")
    except Exception as e:
        print(f"   FAILED to load: {e}")
        return

    # 3. Test Parsing
    print("\n[Test] Parsing Items...")
    for item in raw_trend:
        sym, conf = utils.parse_target(item)
        print(f"   Input: {str(item):<40} -> Sym: {sym:<6} | Conf: {conf}")
        
        if sym == "NVDA" and conf != 0.5: 
            print("   Legacy String default confidence check FAILED")
        if sym == "TSLA" and conf != 0.85:
            print("   Object confidence check FAILED")
            
    print("\nVerification Complete.")
    
    # Cleanup
    if os.path.exists(TEST_FILE):
        os.remove(TEST_FILE)

if __name__ == "__main__":
    test_schema_parsing()
