import asyncio
import os
import ccxt.async_support as ccxt
from dotenv import load_dotenv
import datetime
import json
from dashboard_publisher import publish_message, get_redis_client

# --- Configuration ---
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
VOLUME_THRESHOLD_RATIO = 1.5
KLINE_INTERVAL = '1h'
CYCLE_COOLDOWN_SECONDS = 600
ERROR_COOLDOWN_SECONDS = 60 # Cooldown after an error
CONCURRENT_REQUEST_LIMIT = 100
TARGET_QUOTE_CURRENCIES = ["USDT", "USDC"]
DEX_POOLS_FILE = "target_pools_Dex.json"
# HOT_POOLS_FILE = "hot_pools.json" # No longer used, replaced by Redis
VOLUME_LOG_FILE = "volume_log.txt"

# ==============================================================================
# |                           HELPER FUNCTIONS                                 |
# ==============================================================================

def log_to_volume_file(message: str):
    """Appends a message to the volume log file."""
    try:
        with open(VOLUME_LOG_FILE, 'a') as f:
            f.write(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"[Error] Failed to write to volume log file: {e}")

async def fetch_kline_data(exchange, symbol: str):
    """Fetches k-line data for a single trading pair."""
    try:
        klines = await exchange.fetch_ohlcv(symbol, timeframe=KLINE_INTERVAL, limit=2)
        if len(klines) < 2:
            return None
        prev_kline, current_kline = klines[0], klines[1]
        return {
            "symbol": symbol,
            "prev_volume": float(prev_kline[5]),
            "current_volume": float(current_kline[5]),
        }
    except Exception:
        return None

def build_cex_to_dex_map(dex_pools: list) -> dict:
    """Builds a map from CEX symbols to a list of corresponding DEX pools."""
    mapping = {}
    for pool in dex_pools:
        token0_sym, token1_sym = pool['token0']['symbol'], pool['token1']['symbol']
        alt_symbol = None
        if token0_sym in TARGET_QUOTE_CURRENCIES:
            alt_symbol = token1_sym
        elif token1_sym in TARGET_QUOTE_CURRENCIES:
            alt_symbol = token0_sym
        elif 'WETH' in token0_sym.upper():
            alt_symbol = token1_sym
        elif 'WETH' in token1_sym.upper():
            alt_symbol = token0_sym

        if alt_symbol:
            for q_currency in TARGET_QUOTE_CURRENCIES:
                cex_symbol = f"{alt_symbol}/{q_currency}"
                if cex_symbol not in mapping:
                    mapping[cex_symbol] = []
                mapping[cex_symbol].append(pool)
    return mapping

# ==============================================================================
# |                                MAIN LOGIC                                  |
# ==============================================================================

async def main():
    """
    Main entry point for the scanner. Handles initialization, the main loop, and cleanup.
    """
    await publish_message('volume_scanner', 'log', 'Scanner starting up...')
    log_to_volume_file("--- Binance Volume Surge Scanner Initializing ---")

    exchange = ccxt.binance({
        'apiKey': API_KEY, 'secret': API_SECRET, 'options': {'defaultType': 'spot'}
    })

    try:
        # --- Initialization ---
        try:
            with open(DEX_POOLS_FILE, 'r') as f:
                dex_pools = json.load(f).get('pools', [])
            if not dex_pools:
                log_to_volume_file(f"[Fatal Error] No pools in {DEX_POOLS_FILE}. Exiting.")
                await publish_message('volume_scanner', 'error', {'message': f"No pools found in {DEX_POOLS_FILE}"})
                return
            log_to_volume_file(f"Loaded {len(dex_pools)} pools from {DEX_POOLS_FILE}.")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log_to_volume_file(f"[Fatal Error] Could not read/parse {DEX_POOLS_FILE}: {e}. Exiting.")
            await publish_message('volume_scanner', 'error', {'message': f"Could not read or parse {DEX_POOLS_FILE}: {e}"})
            return

        cex_to_dex_map = build_cex_to_dex_map(dex_pools)
        log_to_volume_file(f"Built map for {len(cex_to_dex_map)} CEX symbols.")

        log_to_volume_file("Fetching exchange markets...")
        await exchange.load_markets()
        target_symbols = [s for s in cex_to_dex_map.keys() if s in exchange.markets]
        log_to_volume_file(f"Monitoring {len(target_symbols)} CEX pairs.")
        await publish_message('volume_scanner', 'log', f"Monitoring {len(target_symbols)} CEX pairs.")

        # --- Main Loop ---
        cycle_count = 0
        while True:
            cycle_count += 1
            try:
                log_to_volume_file(f"--- Starting Scan Cycle #{cycle_count} ---")

                semaphore = asyncio.Semaphore(CONCURRENT_REQUEST_LIMIT)
                async def fetch_with_semaphore(symbol):
                    async with semaphore:
                        return await fetch_kline_data(exchange, symbol)

                tasks = [fetch_with_semaphore(s) for s in target_symbols]
                results = await asyncio.gather(*tasks)
                
                log_to_volume_file("Scan complete. Analyzing results...")
                hot_pools = []
                for res in filter(None, results):
                    if res["prev_volume"] > 0 and res["current_volume"] > (VOLUME_THRESHOLD_RATIO * res["prev_volume"]):
                        log_to_volume_file(f"[!!] Surge on {res['symbol']} ({res['current_volume'] / res['prev_volume']:.2f}x)")
                        if res['symbol'] in cex_to_dex_map:
                            hot_pools.extend(cex_to_dex_map[res['symbol']])

                unique_hot_pools_list = list({p['poolAddress']: p for p in hot_pools}.values())

                # --- Publish to Redis and also set a key for state persistence ---
                redis_client = await get_redis_client()
                if redis_client:
                    message = json.dumps(unique_hot_pools_list)
                    await redis_client.set("latest_hot_pools", message)
                    await redis_client.publish("hot_pools_channel", message)
                    log_to_volume_file(f"Published and set {len(unique_hot_pools_list)} hot pools in Redis.")
                # -----------------------------------------------------------------

                if unique_hot_pools_list:
                    log_to_volume_file(f"Found {len(unique_hot_pools_list)} hot pools. Published to dashboard.")
                    await publish_message('volume_scanner', 'found_hot_pools', unique_hot_pools_list)
                else:
                    log_to_volume_file("No significant surges detected.")

                log_to_volume_file(f"--- Cycle #{cycle_count} finished. Cooling down for {CYCLE_COOLDOWN_SECONDS}s ---")
                await asyncio.sleep(CYCLE_COOLDOWN_SECONDS)

            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                log_to_volume_file(f"[Warning] A network or exchange error occurred in cycle #{cycle_count}: {e}")
                await publish_message('volume_scanner', 'warning', {'message': f"Scanner network error: {e}"})
                log_to_volume_file(f"Cooling down for {ERROR_COOLDOWN_SECONDS}s before retrying...")
                await asyncio.sleep(ERROR_COOLDOWN_SECONDS)
            except Exception as e:
                log_to_volume_file(f"[Fatal Error] An unexpected error occurred in the main loop: {e}")
                await publish_message('volume_scanner', 'error', {'message': str(e)})
                raise # Re-raise the exception to terminate the program

    except asyncio.CancelledError:
        log_to_volume_file("--- Scanner task cancelled. ---")
    finally:
        log_to_volume_file("--- Shutting down scanner ---")
        await publish_message('volume_scanner', 'log', 'Scanner shutting down.')
        if exchange:
            await exchange.close()
        print("Scanner shutdown complete.")

# ==============================================================================
# |                             ENTRY POINT                                    |
# ==============================================================================

if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("[Fatal Error] Please set BINANCE_API_KEY and BINANCE_API_SECRET in your .env file.")
    else:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nScanner stopped by user.")