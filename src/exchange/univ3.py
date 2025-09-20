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
        logger.info(f"UniV3DexClient 初始化，地址: {self.user_address}，支援的鏈: {list(self.w3_instances.keys())}")

    def _get_w3(self, chain: str) -> Web3:
        if chain not in self.w3_instances:
            raise ValueError(f"不支援的鏈或 RPC URL 未設定: {chain}")
        return self.w3_instances[chain]
    
    def _get_token_details(self, symbol: str, chain: str) -> 'TokenDetails':
        if symbol not in self.tokens_config or chain not in self.tokens_config[symbol]:
            raise ValueError(f"在 {chain} 上找不到代幣 {symbol} 的地址設定。")
        return self.tokens_config[symbol][chain]

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
        """使用 QuoterV2 合約獲取鏈上報價（quoteExactInputSingle，tuple 參數版）。"""
        w3 = self._get_w3(pair.dex_chain)
        quoter_address = Web3.to_checksum_address(
            self.dex_config.uniswap_v3[pair.dex_chain].quoter_v2
        )
        quoter_contract = w3.eth.contract(address=quoter_address, abi=self.quoter_abi)

        try:
            # 1) 決定 tokenIn / tokenOut 與 amountIn（皆為 exact input 模式）
            if side == 'sell':  # 賣 base，換 quote
                token_in_details = self._get_token_details(pair.base, pair.dex_chain)
                token_out_details = self._get_token_details(pair.quote, pair.dex_chain)
                amount_in = int(size * (10 ** token_in_details.decimals))
            else:  # 買 base，用 quote 當投入去換 base（仍是 exact input 模式）
                token_in_details = self._get_token_details(pair.quote, pair.dex_chain)
                token_out_details = self._get_token_details(pair.base, pair.dex_chain)
                amount_in = int(size * (10 ** token_in_details.decimals))

            token_in = Web3.to_checksum_address(token_in_details.address)
            token_out = Web3.to_checksum_address(token_out_details.address)
            fee = int(pair.dex_pool_fee)  # 確保是 int (uint24)

            # 2) QuoterV2 參數用 tuple 順序對應 ABI：
            # (address tokenIn, address tokenOut, uint256 amountIn, uint24 fee, uint160 sqrtPriceLimitX96)
            params_tuple = (token_in, token_out, int(amount_in), fee, 0)

            # 3) 呼叫：V2 會回傳 (amountOut, sqrtPriceX96After, initializedTicksCrossed, gasEstimate)
            out = await asyncio.to_thread(lambda: quoter_contract.functions
                                        .quoteExactInputSingle(params_tuple)
                                        .call())
            amount_out_wei = out[0]

            # 4) 換算與價格
            amount_out = Decimal(amount_out_wei) / (10 ** token_out_details.decimals)
            if size == 0:
                return None

            # 每 1 token_in 可換多少 token_out（exact input）
            effective_price = amount_out / size # Decimal

            return Quote(
                pair=pair,
                side=side,
                price=effective_price,
                size=size,
                venue="DEX",
                timestamp=time.time(),
            )

        except Exception as e:
            # 若看到 "sequence of length 3" 幾乎就是 ABI/參數打包問題或 fee 型別問題
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
