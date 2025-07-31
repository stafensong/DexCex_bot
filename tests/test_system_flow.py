import asyncio
import json
import os
import subprocess
import sys
import time

# ==============================================================================
# |                             TEST CONFIG                                    |
# ==============================================================================

# --- Test Files ---
TARGET_POOLS_FILE = "target_pools_Dex.json"
HOT_POOLS_FILE = "hot_pools.json"
LOG_FILE = "arbitrage_log.txt"

# --- Test Data ---
# A realistic-looking test pool that the system will monitor.
# This pool will be used for both initial loading and for hot-pool injection.
TEST_POOL_DATA = {
    "pools": [
        {
            "poolAddress": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
            "feeTier": "500",
            "token0": {
                "symbol": "USDC",
                "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "decimals": 6
            },
            "token1": {
                "symbol": "WETH",
                "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
                "decimals": 18
            },
            "protocol": "uniswap",
            "chain": "base", # Using 'base' as an example chain
            "tvlUSD": "500000000",
            "poolType": "ALT_STABLE"
        }
    ]
}

# Expected string in the arbitrage log file when test mode is triggered
EXPECTED_LOG_ENTRY = "[OPPORTUNITY FOUND]"
EXPECTED_TRIGGER_MESSAGE = "[ARBITRAGE TRIGGERED]"
EXPECTED_SURGE_MESSAGE = "Volume surge detected!"

# ==============================================================================
# |                             TEST RUNNER                                    |
# ==============================================================================

async def run_test():
    """
    Runs the end-to-end integration test for the arbitrage bot system.
    """
    main_process = None
    test_passed = False
    
    # --- Setup Phase ---
    print("--- 1. SETUP: Preparing test environment ---")
    try:
        # Clean up old test files
        for f in [TARGET_POOLS_FILE, HOT_POOLS_FILE, LOG_FILE]:
            if os.path.exists(f):
                os.remove(f)
                print(f"Removed old test file: {f}")

        # Create fake target pools file
        with open(TARGET_POOLS_FILE, 'w') as f:
            json.dump(TEST_POOL_DATA, f)
        print(f"Created fake '{TARGET_POOLS_FILE}'")
        
        # Create an empty hot pools file to start
        with open(HOT_POOLS_FILE, 'w') as f:
            json.dump([], f)
        print(f"Created empty '{HOT_POOLS_FILE}'")

        # --- Execution Phase ---
        print("\n--- 2. EXECUTION: Starting main.py in test mode ---")
        
        # We use asyncio's subprocess tools for better async handling
        main_process = await asyncio.create_subprocess_exec(
            sys.executable, '-u', 'main.py', '--test-mode',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        print(f"main.py started with PID: {main_process.pid}")

        # --- Verification Phase ---
        print("\n--- 3. VERIFICATION: Monitoring main.py output ---")
        
        steps = {
            "normal_scan": False,
            # "hot_pool_injected": False, # Cannot be tested via file anymore
            # "surge_detected": False,    # Dependent on Redis
            "trigger_detected": False,
            "log_verified": False,
        }

        # Monitor stdout for up to 90 seconds
        try:
            async with asyncio.timeout(90.0):
                while True: # Loop indefinitely until success, timeout, or EOF
                    line_bytes = await main_process.stdout.readline()
                    if not line_bytes:
                        print("[TEST FAILED] main.py stdout stream ended prematurely.")
                        break
                    
                    line = line_bytes.decode('utf-8').strip()
                    if not line:
                        continue
                    
                    print(f"[main.py] {line}")

                    # Step 1: Verify normal scan starts
                    if "Starting Scan Cycle" in line and not steps["normal_scan"]:
                        steps["normal_scan"] = True
                        print("\n[TEST PASSED] Step 1/3: Normal scan cycle initiated.")
                        
                    # Step 2: Verify arbitrage trigger (This will now trigger in test mode without hot pools)
                    if EXPECTED_TRIGGER_MESSAGE in line and not steps["trigger_detected"]:
                        steps["trigger_detected"] = True
                        print("\n[TEST PASSED] Step 2/3: Arbitrage trigger message found in output.")

                        # Step 3: Verify log file
                        print("\n[TEST ACTION] Step 3/3: Verifying arbitrage_log.txt...")
                        time.sleep(1) # Give it a moment to flush to disk
                        if os.path.exists(LOG_FILE):
                            with open(LOG_FILE, 'r') as f:
                                log_content = f.read()
                            if EXPECTED_LOG_ENTRY in log_content and TEST_POOL_DATA['pools'][0]['poolAddress'] in log_content:
                                steps["log_verified"] = True
                                print("[TEST PASSED] Step 3/3: Log file contains correct entry.")
                            else:
                                print(f"[TEST FAILED] Log file content is incorrect: {log_content}")
                        else:
                            print("[TEST FAILED] Log file was not created.")
                        
                        # All steps are done, break the loop
                        break
        except asyncio.TimeoutError:
            print("\n[TEST FAILED] Timeout reached. main.py did not produce expected output in time.")

        # Final check
        if all(steps.values()):
            test_passed = True

    except Exception as e:
        print(f"\nAn unexpected error occurred during the test: {e}")
    
    finally:
        # --- Teardown Phase ---
        print("\n--- 4. TEARDOWN: Cleaning up test environment ---")
        if main_process and main_process.returncode is None:
            print(f"Terminating main.py process (PID: {main_process.pid})")
            main_process.terminate()
            await main_process.wait()

        for f in [TARGET_POOLS_FILE, HOT_POOLS_FILE, LOG_FILE]:
            if os.path.exists(f):
                os.remove(f)
                print(f"Cleaned up {f}")
        
        print("\n" + "="*50)
        if test_passed:
            print("✅✅✅  SYSTEM INTEGRATION TEST PASSED  ✅✅✅")
        else:
            print("❌❌❌  SYSTEM INTEGRATION TEST FAILED  ❌❌❌")
        print("="*50)


if __name__ == "__main__":
    # Ensure we are in the correct directory
    if not os.path.exists('main.py'):
        print("Error: This test script must be run from the root directory of the bot.")
        sys.exit(1)
    
    asyncio.run(run_test())
