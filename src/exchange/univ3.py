import asyncio
import json
import time
from decimal import Decimal
from typing import Optional, Dict

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from loguru import logger

from ..core.config import DexConfig, NetworkConfig, SecretsConfig, TokenDetails
from ..core.types import MarketPair, Quote, DexSwapParams, DexTxReceipt
from .dex_base import DexClient

# --- ABI 輔助函數 ---
def _load_abi(path: str) -> Dict:
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"ABI 檔案未找到: {path}")
        raise

class UniV3DexClient(DexClient):
    def __init__(self, dex_config: DexConfig, net_config: NetworkConfig, secrets: SecretsConfig, tokens_config: Dict[str, Dict[str, 'TokenDetails']]):
        self.dex_config = dex_config
        self.net_config = net_config
        self.secrets = secrets
        self.tokens_config = tokens_config  # Directly use the passed dictionary
        self.user_address = Web3.to_checksum_address(Web3().eth.account.from_key(secrets.dex_wallet_private_key.get_secret_value()).address)
        
        self.w3_instances: Dict[str, Web3] = {}
        for chain, rpc_url in net_config.rpc_urls.items():
            if rpc_url:
                w3 = Web3(Web3.HTTPProvider(rpc_url))
                if chain in ["bsc", "polygon", "base"]:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                self.w3_instances[chain] = w3

        self.quoter_abi = _load_abi("ABI/quoter.json")
        self.router_abi = _load_abi("ABI/router.json")
        self.erc20_abi = _load_abi("ABI/erc20.json")
        try:
            self.factory_abi = _load_abi("ABI/factory.json")
        except Exception:
            self.factory_abi = None
            logger.debug("未載入 Uniswap V3 factory ABI，部分功能將不可用。")
        self._factory_contracts: Dict[str, 'web3.contract.Contract'] = {}
        logger.info(f"UniV3DexClient 初始化，地址: {self.user_address}，支援的鏈: {list(self.w3_instances.keys())}")

    def _get_w3(self, chain: str) -> Web3:
        if chain not in self.w3_instances:
            raise ValueError(f"不支援的鏈或 RPC URL 未設定: {chain}")
        return self.w3_instances[chain]

    def _get_factory_contract(self, chain: str):
        if not self.factory_abi:
            raise ValueError("未載入 factory ABI，無法檢查池子存在與否。")
        if chain not in self.dex_config.uniswap_v3:
            raise ValueError(f"在設定中找不到鏈 {chain} 的 DEX 合約資訊。")

        contracts = self.dex_config.uniswap_v3[chain]
        if not getattr(contracts, 'factory', None):
            raise ValueError(f"鏈 {chain} 未設定 Uniswap V3 factory 地址。")

        if chain not in self._factory_contracts:
            factory_address = Web3.to_checksum_address(contracts.factory)
            factory_contract = self._get_w3(chain).eth.contract(address=factory_address, abi=self.factory_abi)
            self._factory_contracts[chain] = factory_contract
        return self._factory_contracts[chain]

    def _get_token_details(self, symbol: str, chain: str) -> 'TokenDetails':
        if symbol not in self.tokens_config or chain not in self.tokens_config[symbol]:
            raise ValueError(f"在 {chain} 上找不到代幣 {symbol} 的地址設定。")
        return self.tokens_config[symbol][chain]

    async def get_pool_address(self, base_symbol: str, quote_symbol: str, chain: str, fee: int) -> Optional[str]:
        """Return the pool address for the given token pair and fee tier if it exists."""
        try:
            factory = self._get_factory_contract(chain)
        except ValueError as exc:
            logger.debug(f"取用 factory 合約失敗: {exc}")
            return None

        try:
            base = self._get_token_details(base_symbol, chain)
            quote = self._get_token_details(quote_symbol, chain)
        except ValueError as exc:
            logger.debug(f"取得代幣資訊失敗: {exc}")
            return None

        token_a = Web3.to_checksum_address(base.address)
        token_b = Web3.to_checksum_address(quote.address)

        def _call() -> str:
            return factory.functions.getPool(token_a, token_b, int(fee)).call()

        try:
            pool_address = await asyncio.to_thread(_call)
        except Exception as exc:
            logger.warning(f"查詢 Uniswap pool 失敗 ({base_symbol}/{quote_symbol} {chain} fee={fee}): {exc}")
            return None

        if pool_address and int(pool_address, 16) != 0:
            return Web3.to_checksum_address(pool_address)
        return None

    async def get_balance(self, asset: str, chain: str) -> Decimal:
        """查詢特定代幣在特定鏈上的錢包餘額。"""
        w3 = self._get_w3(chain)
        try:
            # 對於原生代幣 (e.g., ETH on mainnet)
            if asset == self.net_config.native_token.get(chain):
                balance_wei = await asyncio.to_thread(w3.eth.get_balance, self.user_address)
                balance = Decimal(balance_wei) / Decimal(10**18)
                logger.debug(f"在 {chain} 上的原生代幣 {asset} 餘額: {balance}")
                return balance

            # 對於 ERC20 代幣
            token_details = self._get_token_details(asset, chain)
            token_address = Web3.to_checksum_address(token_details.address)
            token_contract = w3.eth.contract(address=token_address, abi=self.erc20_abi)
            
            balance_wei = await asyncio.to_thread(
                token_contract.functions.balanceOf(self.user_address).call
            )
            
            balance = Decimal(balance_wei) / (10**token_details.decimals)
            logger.debug(f"在 {chain} 上的 {asset} 餘額: {balance}")
            return balance

        except ValueError as e:
            logger.error(f"查詢餘額失敗: {e}")
            return Decimal("-1")
        except Exception as e:
            logger.error(f"查詢 {asset} on {chain} 餘額時發生未知錯誤: {e}")
            return Decimal("-1")

    async def get_quote(self, pair: MarketPair, size: Decimal, side: str) -> Optional[Quote]:
        """
        使用 Uniswap V3 QuoterV2：
        - 賣單(sell base)：quoteExactInputSingle（base -> quote）
        - 買單(buy base)：quoteExactOutputSingle（quote -> base）
        一律回傳 DEX 價格 = quote per base（便於和 CEX 直接比較）
        """
        w3 = self._get_w3(pair.dex_chain)
        quoter_address = Web3.to_checksum_address(self.dex_config.uniswap_v3[pair.dex_chain].quoter_v2)
        quoter = w3.eth.contract(address=quoter_address, abi=self.quoter_abi)

        try:
            fee = int(pair.dex_pool_fee)

            # --- Token Details Resolution ---
            # Prioritize dynamically passed details, otherwise fallback to config
            if pair.base_address and pair.base_decimals is not None:
                base_addr = pair.base_address
                base_decimals = pair.base_decimals
            else:
                base_details = self._get_token_details(pair.base, pair.dex_chain)
                base_addr = base_details.address
                base_decimals = base_details.decimals

            if pair.quote_address and pair.quote_decimals is not None:
                quote_addr = pair.quote_address
                quote_decimals = pair.quote_decimals
            else:
                quote_details = self._get_token_details(pair.quote, pair.dex_chain)
                quote_addr = quote_details.address
                quote_decimals = quote_details.decimals
            # --- End Token Details Resolution ---

            if side == 'sell':
                # ---- 賣 base：exact input ----
                token_in  = Web3.to_checksum_address(base_addr)
                token_out = Web3.to_checksum_address(quote_addr)
                amount_in = int(Decimal(size) * (10 ** base_decimals))
                if amount_in <= 0: return None

                params_in = (token_in, token_out, int(amount_in), fee, 0)
                out = await asyncio.to_thread(lambda: quoter.functions.quoteExactInputSingle(params_in).call())
                amount_out_wei = out[0]

                quote_out = Decimal(amount_out_wei) / (Decimal(10) ** quote_decimals)
                if size == 0: return None
                price_q_per_b = quote_out / Decimal(size)

            else: # side == 'buy'
                # ---- 買 base：exact output ----
                token_in  = Web3.to_checksum_address(quote_addr)
                token_out = Web3.to_checksum_address(base_addr)
                amount_out = int(Decimal(size) * (10 ** base_decimals))
                if amount_out <= 0: return None

                params_out = (token_in, token_out, int(amount_out), fee, 0)
                out = await asyncio.to_thread(lambda: quoter.functions.quoteExactOutputSingle(params_out).call())
                amount_in_wei = out[0]

                quote_in = Decimal(amount_in_wei) / (Decimal(10) ** quote_decimals)
                if size == 0: return None
                price_q_per_b = quote_in / Decimal(size)

            # 合理性過濾
            if not (Decimal("1e-18") < price_q_per_b < Decimal("1e18")):
                 logger.debug(f"丟棄不合理 DEX 價: {price_q_per_b} ({pair.symbol} on {pair.dex_chain}, side={side})")
                 return None

            return Quote(
                pair=pair,
                side=side,
                price=price_q_per_b,
                size=size,
                venue="DEX",
                timestamp=time.time(),
            )

        except Exception as e:
            logger.warning(f"獲取 DEX 報價失敗 ({pair.symbol} on {pair.dex_chain}): {e}")
            return None

    async def _approve_token(self, w3: Web3, token_address: str, router_address: str, required_amount: int):
        token_contract = w3.eth.contract(address=token_address, abi=self.erc20_abi)
        
        allowance = await asyncio.to_thread(
            token_contract.functions.allowance(self.user_address, router_address).call
        )
        if allowance >= required_amount:
            logger.debug(f"Token {token_address} 已有足夠授權。")
            return True

        logger.info(f"正在為 Router {router_address} 授權 Token {token_address}...")
        amount_to_approve = 2**256 - 1 # Approve max
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address)
        
        tx_params = {
            'from': self.user_address,
            'nonce': nonce,
            'gasPrice': await asyncio.to_thread(w3.eth.get_gas_price),
        }
        
        approve_tx = await asyncio.to_thread(
            token_contract.functions.approve(router_address, amount_to_approve).build_transaction, tx_params
        )
        
        signed_tx = w3.eth.account.sign_transaction(approve_tx, self.secrets.dex_wallet_private_key.get_secret_value())
        tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed_tx.rawTransaction)
        logger.info(f"Approve 交易已發送: {tx_hash.hex()}")
        
        receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=self.net_config.max_pending_seconds)
        if receipt['status'] == 1:
            logger.success(f"Token {token_address} 授權成功。")
            return True
        else:
            logger.error(f"Token {token_address} 授權失敗。收據: {receipt}")
            return False

    async def execute_swap(self, params: DexSwapParams) -> DexTxReceipt:
        w3 = self._get_w3(params.chain)
        router_address = Web3.to_checksum_address(self.dex_config.uniswap_v3[params.chain].router)
        token_in_addr = Web3.to_checksum_address(params.token_in_address)
        token_out_addr = Web3.to_checksum_address(params.token_out_address)
        amount_in_wei = int(params.amount_in * (10**params.token_in_decimals))

        await self._approve_token(w3, token_in_addr, router_address, amount_in_wei)

        logger.info(f"準備 DEX 交換 on {params.chain}: {params.amount_in} of token {params.token_in_address}")
        router_contract = w3.eth.contract(address=router_address, abi=self.router_abi)
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address)

        amount_out_minimum = 0 # 在生產環境中，應根據滑點嚴格計算

        swap_params_struct = {
            'tokenIn': token_in_addr,
            'tokenOut': token_out_addr,
            'fee': params.fee,
            'recipient': self.user_address,
            'deadline': int(time.time()) + 600,
            'amountIn': amount_in_wei,
            'amountOutMinimum': amount_out_minimum,
            'sqrtPriceLimitX96': 0
        }

        tx_params = {
            'from': self.user_address,
            'nonce': nonce,
            'gasPrice': await asyncio.to_thread(w3.eth.get_gas_price),
        }
        tx_params['gas'] = await asyncio.to_thread(router_contract.functions.exactInputSingle(swap_params_struct).estimate_gas, {'from': self.user_address, 'value': 0})

        try:
            swap_tx = await asyncio.to_thread(
                router_contract.functions.exactInputSingle(swap_params_struct).build_transaction, tx_params
            )
            signed_tx = w3.eth.account.sign_transaction(swap_tx, self.secrets.dex_wallet_private_key.get_secret_value())
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed_tx.rawTransaction)
            logger.info(f"DEX Swap 交易已發送: {tx_hash.hex()}")

            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=self.net_config.max_pending_seconds)
            
            if receipt['status'] == 1:
                logger.success(f"DEX Swap 成功。Tx: {tx_hash.hex()}")
                # TODO: 從交易日誌(Events)中解析真實的成交價和數量
                return DexTxReceipt(
                    tx_hash=tx_hash.hex(), status=1, block_number=receipt['blockNumber'],
                    gas_used=receipt['gasUsed'], effective_gas_price=receipt['effectiveGasPrice'],
                    avg_fill_price=Decimal(0), filled_size=Decimal(0) # Placeholder
                )
            else:
                logger.error(f"DEX Swap 失敗。Tx: {tx_hash.hex()}, 收據: {receipt}")
                raise Exception("DEX transaction reverted")

        except Exception as e:
            logger.error(f"DEX 交換執行失敗: {e}", exc_info=True)
            raise
