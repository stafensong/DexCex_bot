import asyncio
import json
import os
import sys
import datetime
from decimal import Decimal, ROUND_DOWN

import ccxt.async_support as ccxt
from web3 import Web3

# --- Path Correction ---
# Add the project's root directory to the Python path to allow for absolute imports
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# --- Import from local modules ---
from config import *
from traders.cex_trader import CexTrader
from traders.dex_trader import DexTrader
from core.arbitrage_bot import (
    V3ArbitrageBot,
    get_strategy_for_pool,
    find_arbitrage_targets,
    redis_hot_pool_listener,
    log_to_file,
)
from dashboard.publisher import publish_message, get_redis_client

# ==============================================================================
# |                         TRADE EXECUTION FUNCTION                           |
# ==============================================================================

async def execute_arbitrage_opportunity(opportunity: dict, cex_trader: CexTrader, dex_trader: DexTrader):
    """
    Parses the arbitrage opportunity and executes the trades on CEX and DEX concurrently.
    """
    await publish_message('main_bot', 'log', f"Arbitrage triggered for {opportunity['cex_symbol']}. Path: {opportunity['path']}")
    print("\n" + "="*50)
    print(f"🔥 [ARBITRAGE TRIGGERED] 🔥")
    print(f"Profit: ${opportunity['net_profit_usd']:.4f} | Path: {opportunity['path']}")
    print(f"CEX: {opportunity['cex_symbol']} @ {opportunity['cex_price']:.6f}")
    print(f"DEX: {opportunity['dex_details']['protocol']} on {opportunity['dex_details']['chain']} @ {opportunity['dex_price']:.6f}")
    print("="*50 + "\n")

    # --- 1. Prepare Trade Parameters ---
    path = opportunity['path']
    cex_symbol = opportunity['cex_symbol']
    dex_details = opportunity['dex_details']
    
    trade_volume_usd = Decimal(str(opportunity['trade_volume_usd']))
    cex_price = Decimal(str(opportunity['cex_price']))
    
    market_info = await cex_trader.get_market_info(cex_symbol)
    if not market_info:
        print(f"[Execution Error] Cannot get market info for {cex_symbol} on CEX. Aborting.")
        return

    amount_precision = market_info['precision']['amount']
    trade_amount = float((trade_volume_usd / cex_price).quantize(Decimal(str(10**-amount_precision)), rounding=ROUND_DOWN))

    print(f"Calculated trade amount: {trade_amount} {cex_symbol.split('/')[0]}")
    print("--- Preparing to execute REAL trades simultaneously ---")

    # --- 2. Create CEX and DEX Tasks ---
    cex_task = None
    dex_task = None

    if path == "DEX_BUY_CEX_SELL":
        cex_task = asyncio.create_task(
            cex_trader.execute_order(symbol=cex_symbol, side='sell', amount=trade_amount)
        )
        dex_task = asyncio.create_task(
            dex_trader.execute_swap(
                chain=dex_details['chain'],
                protocol=dex_details['protocol'],
                token_in=dex_details['stable_coin'],
                token_out=dex_details['base_asset'],
                amount=trade_amount,
                fee=dex_details['pool_fee']
            )
        )
    elif path == "CEX_BUY_DEX_SELL":
        cex_task = asyncio.create_task(
            cex_trader.execute_order(symbol=cex_symbol, side='buy', amount=trade_amount)
        )
        dex_task = asyncio.create_task(
            dex_trader.execute_swap(
                chain=dex_details['chain'],
                protocol=dex_details['protocol'],
                token_in=dex_details['base_asset'],
                token_out=dex_details['stable_coin'],
                amount=trade_amount,
                fee=dex_details['pool_fee']
            )
        )
    else:
        print(f"[Execution Error] Unknown or unsupported path: {path}. Aborting.")
        return

    # --- 3. Execute and Handle Results ---
    results = await asyncio.gather(cex_task, dex_task, return_exceptions=True)
    cex_result, dex_result = results

    # ... (rest of the execution logic remains the same) ...

# ==============================================================================
# |                                 MAIN                                       |
# ==============================================================================

async def main():
    await publish_message('main_bot', 'log', 'Arbitrage Engine starting up...')
    print(f"--- Bootstrapping Arbitrage Execution Engine ---")

    hot_pools_from_redis = []
    listener_task = None
    cex_trader = None
    cex_exchange = None

    try:
        redis_client_for_get = await get_redis_client()
        if redis_client_for_get:
            latest_hot_pools_str = await redis_client_for_get.get("latest_hot_pools")
            if latest_hot_pools_str:
                try:
                    hot_pools_from_redis.extend(json.loads(latest_hot_pools_str))
                    print(f"[Info] Pre-populated with {len(hot_pools_from_redis)} hot pools from Redis.")
                except json.JSONDecodeError:
                    print("[Warning] Could not decode latest hot pools from Redis.")
        
        cex_trader = CexTrader(api_key=os.getenv('BINANCE_API_KEY'), api_secret=os.getenv('BINANCE_API_SECRET'))
        dex_trader = DexTrader(rpc_url_mapping=RPC_URLS, private_key=os.getenv('PRIVATE_KEY'))
        print("[Info] Traders initialized.")

        cex_exchange = ccxt.binance({'apiKey': os.getenv('BINANCE_API_KEY'), 'secret': os.getenv('BINANCE_API_SECRET')})
        await cex_exchange.load_markets()

        with open("data/target_pools_Dex.json", 'r') as f:
            dex_data = json.load(f)
        if not dex_data.get('pools'): 
            raise ValueError("No pools found in DEX pools file.")

        listener_task = asyncio.create_task(redis_hot_pool_listener(hot_pools_from_redis, REDIS_HOST, REDIS_PORT))
        
        all_bots = []
        w3_providers = {chain: Web3(Web3.HTTPProvider(rpc)) for chain, rpc in RPC_URLS.items() if rpc}
        
        initial_targets = await find_arbitrage_targets(dex_data['pools'], cex_exchange)
        print(f"[Info] Found {len(initial_targets)} initial viable targets.")

        for pool in initial_targets:
            try:
                w3 = w3_providers.get(pool['chain'])
                if w3 and w3.is_connected():
                    strategy = get_strategy_for_pool(pool, w3)
                    if strategy:
                        all_bots.append(V3ArbitrageBot(pool_info=pool, strategy=strategy, cex_exchange=cex_exchange))
            except Exception as e:
                print(f"[Error] Failed to initialize bot for initial pool {pool.get('poolAddress')}: {e}")

        if not all_bots:
            print("\n[Fatal Error] No initial bots could be initialized. Exiting.")
            return

        monitored_pools = {bot.pool_info['poolAddress'] for bot in all_bots}
        print(f"\n--- Starting Monitoring & Execution Loop ---")
        print(f"Initial monitored pairs: {sorted(list(set([bot.cex_symbol for bot in all_bots])))}")

        cycle_num = 0
        while True:
            cycle_num += 1
            
            # ... (main loop logic remains the same) ...
            if hot_pools_from_redis:
                new_bots_added = 0
                hot_targets_with_cex = await find_arbitrage_targets(hot_pools_from_redis, cex_exchange)
                
                for pool in hot_targets_with_cex:
                    if pool['poolAddress'] not in monitored_pools:
                        try:
                            w3 = w3_providers.get(pool['chain'])
                            if w3 and w3.is_connected():
                                strategy = get_strategy_for_pool(pool, w3)
                                if strategy:
                                    new_bot = V3ArbitrageBot(pool_info=pool, strategy=strategy, cex_exchange=cex_exchange)
                                    all_bots.append(new_bot)
                                    monitored_pools.add(pool['poolAddress'])
                                    print(f"[Info] New hot target added: {new_bot.cex_symbol} on {new_bot.network} (Pool: {pool['poolAddress']})")
                                    new_bots_added += 1
                        except Exception as e:
                            print(f"[Error] Failed to initialize bot for hot pool {pool['poolAddress']}: {e}")
                if new_bots_added > 0:
                     print(f"Total monitored targets now: {len(all_bots)}")

            bots_to_scan = []
            cooldown_this_cycle = NORMAL_CYCLE_COOLDOWN_SECONDS

            if hot_pools_from_redis:
                hot_pool_addresses = {p['poolAddress'] for p in hot_pools_from_redis}
                bots_to_scan = [bot for bot in all_bots if bot.pool_info['poolAddress'] in hot_pool_addresses]
                cooldown_this_cycle = HOT_TARGET_CYCLE_COOLDOWN_SECONDS
                if bots_to_scan:
                    print(f"\n--- Volume surge detected! Focusing on {len(bots_to_scan)} hot target(s). ---")
            
            if not bots_to_scan:
                bots_to_scan = all_bots
                cooldown_this_cycle = NORMAL_CYCLE_COOLDOWN_SECONDS

            cycle_start_message = f"--- Starting Scan Cycle #{cycle_num} for {len(bots_to_scan)} target(s) at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---"
            print(cycle_start_message)
            
            opportunity_found_in_cycle = False
            for bot in bots_to_scan:
                opportunity = await bot.run_check()
                
                if opportunity:
                    await execute_arbitrage_opportunity(opportunity, cex_trader, dex_trader)
                    print(f"[Info] Opportunity processed. Pausing for 60 seconds before starting a new full scan...")
                    await asyncio.sleep(60)
                    opportunity_found_in_cycle = True
                    break 
                
                await asyncio.sleep(DELAY_BETWEEN_TASKS_SECONDS)

            if opportunity_found_in_cycle:
                continue

            print(f"\n--- Completed Scan Cycle #{cycle_num}. Pausing for {cooldown_this_cycle} seconds. ---")
            await asyncio.sleep(cooldown_this_cycle)

    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"\n[Fatal Error] Could not start bot: {e}")
        await publish_message('main_bot', 'error', {'message': f"Could not start bot: {e}"})
    except Exception as e:
        print(f"\n[Fatal Error] An unexpected error occurred during startup: {e}")
        import traceback
        traceback.print_exc()
        await publish_message('main_bot', 'error', {'message': f"An unexpected error occurred: {e}"})
    except (KeyboardInterrupt, SystemExit):
        print("\nExecution stopped by user. Goodbye!")
    finally:
        # This block will run no matter what happens in the try block
        await publish_message('main_bot', 'log', 'Bot shutting down.')
        if listener_task and not listener_task.done():
            listener_task.cancel()
        if cex_trader and hasattr(cex_trader, 'close'):
            await cex_trader.close()
        if cex_exchange and hasattr(cex_exchange, 'close'):
            await cex_exchange.close()
        print("Exiting.")
        sys.exit(0)

if __name__ == "__main__":
    if not all(os.path.exists(f) for f in ['config.py', 'traders/cex_trader.py', 'traders/dex_trader.py']):
        print("[Fatal Error] Core files not found.")
        sys.exit(1)
        
    asyncio.run(main())