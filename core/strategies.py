import time
import asyncio
from abc import ABC, abstractmethod

# --- Import configurations from config.py ---
from config import *

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

        try:
            token0_decimals = self._get_token_decimals(token0)
            amount_in_0 = 10**token0_decimals
            
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
            try:
                token1_decimals = self._get_token_decimals(token1)
                amount_in_1 = 10**token1_decimals

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
                error_str = str(e1)
                if 'execution reverted' in error_str and len(error_str) > 120:
                    error_str = error_str[:120] + "..."
                print(f"[Warning] Gas estimation failed in both directions for pool {self.pool_info['poolAddress']}. Final error: {error_str}")
                return float('inf')

        try:
            gas_price_wei = self.w3.eth.gas_price
            
            if self.chain == 'bsc':
                gas_token_symbol = 'BNB/USDT'
                gas_cost_native = self.w3.from_wei(gas_price_wei * estimated_gas_used, 'ether')
            else:
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