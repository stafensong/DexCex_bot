import time
import datetime
import json
import sys
import os
import asyncio
import redis
import redis.asyncio as aioredis
from web3 import Web3
import ccxt.async_support as ccxt

# --- Import from local modules ---
from config import *
from dashboard.publisher import publish_message
from core.strategies import ArbitrageStrategy, UsdBasedStrategy, AltWethTriangularStrategy

# ==============================================================================
# |                           LOGGING FUNCTION                                |
# ==============================================================================

def log_to_file(message: str):
    """Appends a message to the specified log file."""
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(message + "\n")
    except Exception as e:
        print(f"[Error] Failed to write to log file: {e}")

# ==============================================================================
# |                             ARBITRAGE BOT                                  |
# ==============================================================================

class V3ArbitrageBot:
    def __init__(self, pool_info: dict, strategy: ArbitrageStrategy, cex_exchange: ccxt.Exchange):
        self.pool_info = pool_info
        self.strategy = strategy
        self.network = pool_info['chain']
        self.protocol = pool_info['protocol']
        self.cex_exchange = cex_exchange
        self.cex_symbol = strategy.config.get('cex_symbol') or strategy.config.get('cex_alt_stable_symbol')

    async def run_check(self):
        result = await self.strategy.evaluate(self.cex_exchange)
        if result.get("profitable"):
            # NOTE: Gas cost is now calculated within the strategy object itself
            gas_fee_usd = await self.strategy._get_gas_cost_in_usd(self.cex_exchange)
            if gas_fee_usd == float('inf'):
                return None

            trade_volume_usd = TRADE_VOLUME_USD
            profit_before_fees_usd = trade_volume_usd * (result['profit_margin_percent'] / 100)
            cex_fee_usd = trade_volume_usd * CEX_TAKER_FEE_RATE
            net_profit_usd = profit_before_fees_usd - cex_fee_usd - gas_fee_usd

            if net_profit_usd < PROFIT_THRESHOLD_USD:
                return None

            dex_price = result.get("dex_price", 0)
            cex_price = result.get("cex_ask", 0) if "CEX_BUY" in result.get("path", "") else result.get("cex_bid", 0)
            if "TRIANGULAR" in result.get("path", ""):
                 cex_price = result.get("cex_implied_ask", 0) if "CEX_BUY" in result.get("path", "") else result.get("cex_implied_bid", 0)

            log_entry = (
                f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [OPPORTUNITY FOUND] "
                f"({self.protocol.upper()} on {self.network.upper()}) "
                f"{result['message']} | Pool: {self.pool_info['poolAddress']} | "
                f"DEX Price: {dex_price:.6f} | CEX Price: {cex_price:.6f} | "
                f"Gas Fee: ${gas_fee_usd:.4f} | "
                f"Trade Volume: ${trade_volume_usd:.2f} | Net Profit: ${net_profit_usd:.4f}"
            )
            print(log_entry)
            log_to_file(log_entry)

            opportunity_data = {
                "profitable": True,
                "path": result.get("path"),
                "cex_symbol": self.cex_symbol,
                "cex_price": cex_price,
                "dex_price": dex_price,
                "trade_volume_usd": trade_volume_usd,
                "net_profit_usd": net_profit_usd,
                "gas_fee_usd": gas_fee_usd,
                "dex_details": {
                    "protocol": self.protocol,
                    "chain": self.network,
                    "pool_address": self.pool_info['poolAddress'],
                    "pool_fee": self.strategy.pool_fee,
                    "base_asset": self.strategy.base_asset,
                    "stable_coin": getattr(self.strategy, 'stable_coin', None),
                    "alt_token": getattr(self.strategy, 'alt_token', None),
                    "base_token": getattr(self.strategy, 'base_token', None),
                }
            }
            
            await publish_message('main_bot', 'opportunity_found', opportunity_data)
            return opportunity_data
        return None

# ==============================================================================
# |                           HELPER FUNCTIONS                                |
# ==============================================================================

def get_strategy_for_pool(pool_info: dict, w3: Web3) -> ArbitrageStrategy:
    pool_type = pool_info.get('poolType')
    if pool_type in ('WETH_STABLE', 'ALT_STABLE'):
        return UsdBasedStrategy(w3, pool_info)
    elif pool_type == 'ALT_WETH':
        return AltWethTriangularStrategy(w3, pool_info)
    return None

async def find_arbitrage_targets(dex_pools: list, exchange: ccxt.Exchange) -> list:
    print("--- Searching for all viable arbitrage targets with on-chain verification ---")
    arbitrage_targets = []
    if not exchange.markets:
        await exchange.load_markets()
    cex_markets = exchange.markets

    w3_providers = {chain: Web3(Web3.HTTPProvider(rpc)) for chain, rpc in RPC_URLS.items()}

    for pool in dex_pools:
        if float(pool.get('tvlUSD', 0)) < MIN_TVL_USD:
            continue

        pool_type = pool.get('poolType')
        if not pool_type or pool_type not in ['WETH_STABLE', 'ALT_STABLE', 'ALT_WETH']:
            continue

        token0, token1 = pool['token0'], pool['token1']
        chain = pool['chain']
        w3 = w3_providers.get(chain)
        if not w3 or not w3.is_connected():
            continue

        alt_token_info = None
        if pool_type == 'ALT_WETH':
            alt_token_info = token1 if 'WETH' in token0['symbol'].upper() else token0
        elif pool_type == 'ALT_STABLE':
            alt_token_info = token0 if token1['symbol'] in STABLECOINS else token1
        else:
            continue

        try:
            token_contract = w3.eth.contract(address=w3.to_checksum_address(alt_token_info['address']), abi=ERC20_DECIMALS_ABI)
            on_chain_symbol = token_contract.functions.symbol().call()
        except Exception:
            continue

        found_cex_symbol = None
        for stable in ['USDT', 'USDC']:
            cex_symbol = f"{on_chain_symbol}/{stable}"
            cex_symbol = cex_symbol.replace('WETH', 'ETH')
            
            blacklist_format_symbol = cex_symbol.replace('/', '')
            if cex_symbol in cex_markets and blacklist_format_symbol not in CEX_BLACKLIST:
                found_cex_symbol = cex_symbol
                break

        if found_cex_symbol:
            pool['cex_symbol'] = found_cex_symbol
            arbitrage_targets.append(pool)
            
    return arbitrage_targets

async def redis_hot_pool_listener(hot_pools_ref: list, redis_host: str, redis_port: int):
    """
    A listener that subscribes to the hot pools channel on Redis
    and updates the shared list object in-place.
    This function creates its own dedicated Redis connection.
    """
    print("[Info] Starting Redis hot pool listener...")
    client = None
    pubsub = None
    while True:
        try:
            client = aioredis.Redis(host=redis_host, port=redis_port, auto_close_connection_pool=False, decode_responses=True)
            await client.ping()
            print("[Info] Redis listener connected successfully with a dedicated client.")

            pubsub = client.pubsub()
            await pubsub.subscribe("hot_pools_channel")
            
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None)
                if message and message.get('type') == 'message':
                    try:
                        new_hot_pools = json.loads(message['data'])
                        hot_pools_ref.clear()
                        hot_pools_ref.extend(new_hot_pools)
                        print(f"[Redis Listener] Received and updated {len(new_hot_pools)} hot pools.")
                    except json.JSONDecodeError:
                        print("[Error] Could not decode hot pools message from Redis.")
        except redis.exceptions.ConnectionError as e:
            print(f"[Error] Redis connection lost in hot pool listener: {e}. Reconnecting in 10 seconds...")
        except asyncio.CancelledError:
            print("[Info] Redis listener task cancelled.")
            break
        except Exception as e:
            print(f"[Error] An unexpected error occurred in the hot pool listener: {e}. Reconnecting in 10 seconds...")
        finally:
            if pubsub:
                await pubsub.close()
            if client:
                await client.close()
            print("[Info] Redis listener connection closed.")
        
        await asyncio.sleep(10)