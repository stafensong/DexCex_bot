import time
import datetime
import json
import sys
import os
import asyncio
import redis # Import the top-level redis library for exceptions
import redis.asyncio as aioredis # Use an alias for the async version
from web3 import Web3
from abc import ABC, abstractmethod
import ccxt.async_support as ccxt

# --- Import all configurations from config.py ---
from config import *
# --- Import shared functions ---
from dashboard_publisher import publish_message, get_redis_client

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
# |                           STRATEGY INTERFACE                              |
# ==============================================================================

class ArbitrageStrategy(ABC):
    def __init__(self, w3, pool_info):
        self.w3 = w3
        self.pool_info = pool_info
        self.config = {}
        self.chain = pool_info['chain']
        self.protocol = pool_info['protocol']
        self.pool_address = self.w3.to_checksum_address(pool_info['poolAddress'])
        self.pool_fee = int(pool_info['feeTier'])
        
        quoter_address = CONTRACT_ADDRESSES.get(self.protocol, {}).get(self.chain, {}).get('quoter_v2')
        if not quoter_address:
            raise ValueError(f"Quoter V2 address not found for protocol '{self.protocol}' on chain '{self.chain}'")
        self.quoter_contract = self.w3.eth.contract(address=quoter_address, abi=QUOTER_V2_ABI)
        
        self._configure()

    def _get_token_decimals(self, token_info: dict) -> int:
        symbol = token_info['symbol']
        if symbol in TOKEN_DECIMALS: return TOKEN_DECIMALS[symbol]
        try:
            token_address = self.w3.to_checksum_address(token_info['address'])
            token_contract = self.w3.eth.contract(address=token_address, abi=ERC20_DECIMALS_ABI)
            decimals = token_contract.functions.decimals().call()
            TOKEN_DECIMALS[symbol] = decimals
            return decimals
        except Exception as e:
            return 18

    async def _get_gas_cost_in_usd(self, cex_exchange):
        router_address_str = CONTRACT_ADDRESSES.get(self.protocol, {}).get(self.chain, {}).get('router')
        if not router_address_str:
            print(f"[Warning] Router address not found for {self.protocol} on {self.chain}. Cannot estimate gas.")
            return float('inf')

        router_address = self.w3.to_checksum_address(router_address_str)
        router_contract = self.w3.eth.contract(address=router_address, abi=ROUTER_ABI)

        token0 = self.pool_info['token0']
        token1 = self.pool_info['token1']
        
        estimated_gas_used = 0

        # --- FINAL FIX: Try estimating gas in both directions (0->1 and 1->0) ---
        # Attempt 1: Simulate swap from token0 to token1
        try:
            token0_decimals = self._get_token_decimals(token0)
            amount_in_0 = 10**token0_decimals # Simulate with 1 full token
            
            swap_params_0 = {
                'tokenIn': self.w3.to_checksum_address(token0['address']),
                'tokenOut': self.w3.to_checksum_address(token1['address']),
                'fee': self.pool_fee,
                'recipient': GAS_ESTIMATION_DUMMY_ADDRESS,
                'deadline': int(time.time()) + 600,
                'amountIn': amount_in_0,
                'amountOutMinimum': 0,
                'sqrtPriceLimitX96': 0
            }
            tx_build_0 = router_contract.functions.exactInputSingle(swap_params_0).build_transaction({
                'from': GAS_ESTIMATION_DUMMY_ADDRESS, 'nonce': 0
            })
            estimated_gas_used = await asyncio.to_thread(
                self.w3.eth.estimate_gas, {'to': router_address, 'data': tx_build_0['data']}
            )
        except Exception:
            # If the first attempt fails, try the other direction
            try:
                token1_decimals = self._get_token_decimals(token1)
                amount_in_1 = 10**token1_decimals # Simulate with 1 full token

                swap_params_1 = {
                    'tokenIn': self.w3.to_checksum_address(token1['address']),
                    'tokenOut': self.w3.to_checksum_address(token0['address']),
                    'fee': self.pool_fee,
                    'recipient': GAS_ESTIMATION_DUMMY_ADDRESS,
                    'deadline': int(time.time()) + 600,
                    'amountIn': amount_in_1,
                    'amountOutMinimum': 0,
                    'sqrtPriceLimitX96': 0
                }
                tx_build_1 = router_contract.functions.exactInputSingle(swap_params_1).build_transaction({
                    'from': GAS_ESTIMATION_DUMMY_ADDRESS, 'nonce': 0
                })
                estimated_gas_used = await asyncio.to_thread(
                    self.w3.eth.estimate_gas, {'to': router_address, 'data': tx_build_1['data']}
                )
            except Exception as e1:
                # If both directions fail, log the error and return infinity
                error_str = str(e1)
                if 'execution reverted' in error_str and len(error_str) > 120:
                    error_str = error_str[:120] + "..."
                print(f"[Warning] Gas estimation failed in both directions for pool {self.pool_info['poolAddress']}. Final error: {error_str}")
                return float('inf')

        # If we have a successful gas estimate, calculate the cost in USD
        try:
            gas_price_wei = self.w3.eth.gas_price
            
            if self.chain == 'bsc':
                gas_token_symbol = 'BNB/USDT'
                gas_cost_native = self.w3.from_wei(gas_price_wei * estimated_gas_used, 'ether')
            else: # Default to ETH-based chains
                gas_token_symbol = 'ETH/USDT'
                gas_cost_native = self.w3.from_wei(gas_price_wei * estimated_gas_used, 'ether')

            ticker = await cex_exchange.fetch_ticker(gas_token_symbol)
            native_token_price_usd = ticker['last']
            return float(gas_cost_native) * native_token_price_usd
        except Exception as e:
            print(f"[Warning] Could not fetch native token price to calculate gas fee. Error: {e}")
            return float('inf')

    def _get_quote_with_slippage(self, token_in_address, token_out_address, amount_in_wei):
        try:
            quote_params = {
                'tokenIn': token_in_address,
                'tokenOut': token_out_address,
                'amountIn': amount_in_wei,
                'fee': self.pool_fee,
                'sqrtPriceLimitX96': 0
            }
            amount_out, _, _, _ = self.quoter_contract.functions.quoteExactInputSingle(quote_params).call()
            return amount_out
        except Exception:
            return 0

    @abstractmethod
    def _configure(self):
        pass

    @abstractmethod
    async def evaluate(self, cex_exchange):
        pass

# ... (UsdBasedStrategy and AltWethTriangularStrategy classes remain the same) ...
class UsdBasedStrategy(ArbitrageStrategy):
    def _configure(self):
        token0, token1 = self.pool_info['token0'], self.pool_info['token1']
        if token0['symbol'] in STABLECOINS:
            self.base_asset, self.stable_coin = token1, token0
        else:
            self.base_asset, self.stable_coin = token0, token1
        
        self.base_decimals = self._get_token_decimals(self.base_asset)
        self.stable_decimals = self._get_token_decimals(self.stable_coin)
        self.base_address = self.w3.to_checksum_address(self.base_asset['address'])
        self.stable_address = self.w3.to_checksum_address(self.stable_coin['address'])
        self.config['cex_symbol'] = self.pool_info['cex_symbol']

    async def evaluate(self, cex_exchange):
        log_data = {"tvl": self.pool_info['tvlUSD']}
        try:
            order_book = await cex_exchange.fetch_order_book(self.config['cex_symbol'], limit=1)
            if not order_book or not order_book['bids'] or not order_book['asks']:
                return {"profitable": False, "message": "CEX order book is empty."}
            cex_bid = order_book['bids'][0][0]
            cex_ask = order_book['asks'][0][0]
            log_data.update({"cex_ask": cex_ask, "cex_bid": cex_bid})
        except Exception as e:
            return {"profitable": False, "message": f"Could not fetch CEX order book for {self.config['cex_symbol']}"}

        stable_amount_in_wei = int(TRADE_VOLUME_USD * (10**self.stable_decimals))
        dex_amount_out_alt = self._get_quote_with_slippage(self.stable_address, self.base_address, stable_amount_in_wei)
        if dex_amount_out_alt > 0:
            dex_effective_price = TRADE_VOLUME_USD / (dex_amount_out_alt / (10**self.base_decimals))
            log_data["dex_buy_price"] = dex_effective_price

            price_impact = abs(dex_effective_price - (1/cex_ask)) / (1/cex_ask) * 100 if cex_ask != 0 else 0
            if price_impact > SLIPPAGE_THRESHOLD_PERCENT:
                 return {"profitable": False, "message": f"Slippage ({price_impact:.2f}%) exceeds threshold.", **log_data}

            if dex_effective_price < cex_bid:
                profit_margin = (cex_bid - dex_effective_price) / dex_effective_price * 100
                return {"profitable": True, "path": "DEX_BUY_CEX_SELL", "dex_price": dex_effective_price, "profit_margin_percent": profit_margin, "message": f"Potential direct arbitrage! Margin: {profit_margin:.2f}%", **log_data}

        alt_amount_in_wei = int((TRADE_VOLUME_USD / cex_ask) * (10**self.base_decimals)) if cex_ask > 0 else 0
        if alt_amount_in_wei > 0:
            dex_amount_out_stable = self._get_quote_with_slippage(self.base_address, self.stable_address, alt_amount_in_wei)
            if dex_amount_out_stable > 0:
                stable_received = dex_amount_out_stable / (10**self.stable_decimals)
                dex_effective_price = stable_received / (TRADE_VOLUME_USD / cex_ask)
                log_data["dex_sell_price"] = dex_effective_price

                price_impact = abs(dex_effective_price - cex_ask) / cex_ask * 100 if cex_ask != 0 else 0
                if price_impact > SLIPPAGE_THRESHOLD_PERCENT:
                    return {"profitable": False, "message": f"Slippage ({price_impact:.2f}%) exceeds threshold.", **log_data}

                if dex_effective_price > cex_ask:
                    profit_margin = (dex_effective_price - cex_ask) / cex_ask * 100
                    return {"profitable": True, "path": "CEX_BUY_DEX_SELL", "dex_price": dex_effective_price, "profit_margin_percent": profit_margin, "message": f"Potential direct arbitrage! Margin: {profit_margin:.2f}%", **log_data}

        return {"profitable": False, "message": "No direct arbitrage opportunity found.", **log_data}

class AltWethTriangularStrategy(ArbitrageStrategy):
    def _configure(self):
        token0, token1 = self.pool_info['token0'], self.pool_info['token1']
        native_symbol = 'WBNB' if self.chain == 'bsc' else 'WETH'
        
        if native_symbol in token0['symbol'].upper():
            self.alt_token, self.base_token = token1, token0
        else:
            self.alt_token, self.base_token = token0, token1
        
        self.alt_decimals = self._get_token_decimals(self.alt_token)
        self.base_decimals = self._get_token_decimals(self.base_token)
        self.alt_address = self.w3.to_checksum_address(self.alt_token['address'])
        self.base_address = self.w3.to_checksum_address(self.base_token['address'])
        self.config['cex_alt_stable_symbol'] = self.pool_info['cex_symbol']
        stable_coin = self.config['cex_alt_stable_symbol'].split('/')[1]
        
        cex_base_symbol = 'BNB' if self.chain == 'bsc' else 'ETH'
        self.config['cex_eth_stable_symbol'] = f"{cex_base_symbol}/{stable_coin}"

    async def evaluate(self, cex_exchange):
        log_data = {"tvl": self.pool_info['tvlUSD']}
        try:
            alt_stable_ob = await cex_exchange.fetch_order_book(self.config['cex_alt_stable_symbol'], limit=1)
            eth_stable_ob = await cex_exchange.fetch_order_book(self.config['cex_eth_stable_symbol'], limit=1)
            
            if not all([alt_stable_ob, eth_stable_ob, alt_stable_ob['bids'], alt_stable_ob['asks'], eth_stable_ob['bids'], eth_stable_ob['asks']]):
                return {"profitable": False, "message": "CEX order book is empty for triangular arb."}

            cex_alt_ask = alt_stable_ob['asks'][0][0]
            cex_alt_bid = alt_stable_ob['bids'][0][0]
            cex_eth_ask = eth_stable_ob['asks'][0][0]
            cex_eth_bid = eth_stable_ob['bids'][0][0]

            cex_implied_ask_price = cex_alt_ask / cex_eth_bid
            cex_implied_bid_price = cex_alt_bid / cex_eth_ask

            log_data.update({
                "cex_implied_ask": cex_implied_ask_price,
                "cex_implied_bid": cex_implied_bid_price
            })

        except Exception as e:
            return {"profitable": False, "message": f"Could not fetch CEX order books for triangular arb: {e}"}

        alt_amount_in_wei = int((TRADE_VOLUME_USD / cex_alt_ask) * (10**self.alt_decimals)) if cex_alt_ask > 0 else 0
        if alt_amount_in_wei > 0:
            dex_amount_out_weth = self._get_quote_with_slippage(self.alt_address, self.base_address, alt_amount_in_wei)
            if dex_amount_out_weth > 0:
                weth_received = dex_amount_out_weth / (10**self.base_decimals)
                dex_effective_price = weth_received / (alt_amount_in_wei / (10**self.alt_decimals))
                log_data["dex_sell_price"] = dex_effective_price

                price_impact = abs(dex_effective_price - cex_implied_bid_price) / cex_implied_bid_price * 100 if cex_implied_bid_price != 0 else 0
                if price_impact > SLIPPAGE_THRESHOLD_PERCENT:
                    return {"profitable": False, "message": f"Slippage ({price_impact:.2f}%) exceeds threshold.", **log_data}

                if dex_effective_price > cex_implied_ask_price:
                    profit_margin = (dex_effective_price - cex_implied_ask_price) / cex_implied_ask_price * 100
                    return {"profitable": True, "path": "CEX_BUY_DEX_SELL_TRIANGULAR", "dex_price": dex_effective_price, "profit_margin_percent": profit_margin, "message": f"Potential triangular opportunity! Margin: {profit_margin:.2f}%", **log_data}

        return {"profitable": False, "message": "No triangular arbitrage opportunity found.", **log_data}

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
# |                                 MAIN                                      |
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
    while True: # Add a loop for automatic reconnection
        try:
            # Create a new, dedicated client for this subscriber
            client = aioredis.Redis(host=redis_host, port=redis_port, auto_close_connection_pool=False, decode_responses=True)
            await client.ping() # Verify connection
            print("[Info] Redis listener connected successfully with a dedicated client.")

            pubsub = client.pubsub()
            await pubsub.subscribe("hot_pools_channel")
            
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=None) # Block until message
                if message and message.get('type') == 'message':
                    try:
                        new_hot_pools = json.loads(message['data'])
                        # Update the list in-place to ensure the main loop sees the change
                        hot_pools_ref.clear()
                        hot_pools_ref.extend(new_hot_pools)
                        print(f"[Redis Listener] Received and updated {len(new_hot_pools)} hot pools.")
                        # REMOVED: await publish_message(...) to prevent task crashes
                    except json.JSONDecodeError:
                        print("[Error] Could not decode hot pools message from Redis.")
        except redis.exceptions.ConnectionError as e:
            print(f"[Error] Redis connection lost in hot pool listener: {e}. Reconnecting in 10 seconds...")
        except asyncio.CancelledError:
            print("[Info] Redis listener task cancelled.")
            break # Exit the loop if cancelled
        except Exception as e:
            print(f"[Error] An unexpected error occurred in the hot pool listener: {e}. Reconnecting in 10 seconds...")
        finally:
            # Gracefully close the dedicated connection before retrying
            if pubsub:
                await pubsub.close()
            if client:
                await client.close()
            print("[Info] Redis listener connection closed.")
        
        await asyncio.sleep(10) # Wait before attempting to reconnect


async def main():
    DEX_POOLS_FILE = "target_pools_Dex.json"
    print(f"--- Bootstrapping Arbitrage Bot ---")

    hot_pools_from_redis = []

    try:
        with open(DEX_POOLS_FILE, 'r') as f:
            dex_data = json.load(f)
        if not dex_data.get('pools'): 
            raise ValueError("No pools found in DEX pools file.")
        
        print("[Info] Initializing Binance exchange and loading markets...")
        cex_exchange = ccxt.binance()
        await cex_exchange.load_markets()
        
        # Pass Redis connection details to the listener
        listener_task = asyncio.create_task(redis_hot_pool_listener(hot_pools_from_redis, REDIS_HOST, 6379))
        
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
            print("\n[Warning] No initial bots could be initialized. Bot will rely on hot targets.")

        monitored_pools = {bot.pool_info['poolAddress'] for bot in all_bots}
        print(f"\n--- Starting Monitoring Loop ---")
        print(f"Initial monitored pairs: {sorted([bot.cex_symbol for bot in all_bots])}")

        cycle_num = 0
        while True:
            cycle_num += 1
            
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
            if not hot_pools_from_redis:
                log_to_file(cycle_start_message)
            
            tasks = []
            for bot in bots_to_scan:
                task = asyncio.create_task(bot.run_check())
                tasks.append(task)
                await asyncio.sleep(DELAY_BETWEEN_TASKS_SECONDS)

            await asyncio.gather(*tasks)

            print(f"\n--- Completed Scan Cycle #{cycle_num}. Pausing for {cooldown_this_cycle} seconds. ---")
            await asyncio.sleep(cooldown_this_cycle)

    except (FileNotFoundError, ValueError, KeyError, ConnectionError) as e:
        print(f"\n[Fatal Error] Could not start bot: {e}")
    except (KeyboardInterrupt, SystemExit):
        print("\nExecution stopped by user. Goodbye!")
    finally:
        if 'listener_task' in locals() and not listener_task.done():
            listener_task.cancel()
        if 'cex_exchange' in locals() and hasattr(cex_exchange, 'close'):
            await cex_exchange.close()
        sys.exit(0)

if __name__ == "__main__":
    if not all(os.path.exists(f) for f in ['config.py', 'dashboard_publisher.py']):
        print("[Fatal Error] Core files `config.py` or `dashboard_publisher.py` not found.")
        sys.exit(1)
    asyncio.run(main())