import asyncio
import json
import time
from decimal import Decimal
from typing import Optional, Dict

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from loguru import logger

from ..core.config import DexConfig, NetworkConfig, SecretsConfig
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
    def __init__(self, dex_config: DexConfig, net_config: NetworkConfig, secrets: SecretsConfig):
        self.dex_config = dex_config
        self.net_config = net_config
        self.secrets = secrets
        self.user_address = Web3.to_checksum_address(Web3().eth.account.from_key(secrets.dex_wallet_private_key.get_secret_value()).address)
        
        self.w3_instances: Dict[str, Web3] = {}
        for chain, rpc_url in net_config.rpc_urls.items():
            if rpc_url:
                w3 = Web3(Web3.HTTPProvider(rpc_url))
                # 處理 PoA 鏈 (如 BSC, Polygon)
                if 'bsc' in chain or 'polygon' in chain:
                    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                self.w3_instances[chain] = w3

        # 載入 ABIs
        self.quoter_abi = _load_abi("ABI/quoter.json")
        self.router_abi = _load_abi("ABI/router.json")
        self.erc20_abi = _load_abi("ABI/erc20.json")
        logger.info(f"UniV3DexClient 初始化，地址: {self.user_address}，支援的鏈: {list(self.w3_instances.keys())}")

    def _get_w3(self, chain: str) -> Web3:
        if chain not in self.w3_instances:
            raise ValueError(f"不支援的鏈或 RPC URL 未設定: {chain}")
        return self.w3_instances[chain]

    async def get_quote(self, pair: MarketPair, size: Decimal, side: str) -> Optional[Quote]:
        # 待實現: 呼叫 QuoterV2 合約的 quoteExactInputSingle
        mock_price = Decimal("3000.0") if pair.base == "WETH" else Decimal("1.0")
        if side == "buy":
            mock_price *= Decimal("1.0005")
        else:
            mock_price *= Decimal("0.9995")
        return Quote(pair=pair, side=side, price=mock_price, size=size, venue="DEX", timestamp=0)

    async def _approve_token(self, w3: Web3, token_address: str, router_address: str):
        token_contract = w3.eth.contract(address=token_address, abi=self.erc20_abi)
        amount_to_approve = 2**256 - 1 # Approve max

        allowance = await asyncio.to_thread(
            token_contract.functions.allowance(self.user_address, router_address).call
        )
        if allowance >= amount_to_approve // 2: # 檢查是否已有足夠的授權
            logger.debug(f"Token {token_address} 已有足夠授權。")
            return True

        logger.info(f"正在為 Router {router_address} 授權 Token {token_address}...")
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address)
        tx_params = {
            'from': self.user_address,
            'nonce': nonce,
            'gasPrice': await asyncio.to_thread(getattr, w3.eth, 'gas_price')
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
        w3 = self._get_w3(params.pair.dex_chain)
        router_address = Web3.to_checksum_address(self.dex_config.uniswap_v3[params.pair.dex_chain].router)
        
        # 根據交易方向確定 token_in 和 token_out
        # TODO: 這部分需要從 config 或一個中心化的 token list 獲取地址和精度
        # 這裡暫時硬編碼 WETH 和 USDT/USDC 的地址和精度
        token_map = {
            "WETH": {"address": self.dex_config.uniswap_v3[params.pair.dex_chain].weth, "decimals": 18},
            "USDT": {"address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
            "USDC": {"address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6}
        }

        if params.side == 'sell': # 賣 base, 買 quote
            token_in = token_map[params.pair.base]
            token_out = token_map[params.pair.quote]
        else: # 買 base, 賣 quote
            token_in = token_map[params.pair.quote]
            token_out = token_map[params.pair.base]

        token_in_addr = Web3.to_checksum_address(token_in['address'])
        token_out_addr = Web3.to_checksum_address(token_out['address'])
        amount_in_wei = int(params.size * (10**token_in['decimals']))

        # 1. 授權 Token
        await self._approve_token(w3, token_in_addr, router_address)

        # 2. 準備交易
        logger.info(f"準備 DEX 交換: {params.side} {params.size} {params.pair.base} on {params.pair.dex_chain}")
        router_contract = w3.eth.contract(address=router_address, abi=self.router_abi)
        nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address)

        # TODO: 實現更精確的 amountOutMinimum 計算
        amount_out_minimum = 0

        swap_tx_params = {
            'tokenIn': token_in_addr,
            'tokenOut': token_out_addr,
            'fee': params.pair.dex_pool_fee,
            'recipient': self.user_address,
            'deadline': int(time.time()) + 600,
            'amountIn': amount_in_wei,
            'amountOutMinimum': amount_out_minimum,
            'sqrtPriceLimitX96': 0
        }

        tx_params = {
            'from': self.user_address,
            'nonce': nonce,
            'gasPrice': await asyncio.to_thread(getattr, w3.eth, 'gas_price')
        }

        # 3. 建立、簽署、發送交易
        try:
            swap_tx = await asyncio.to_thread(
                router_contract.functions.exactInputSingle(swap_tx_params).build_transaction, tx_params
            )
            signed_tx = w3.eth.account.sign_transaction(swap_tx, self.secrets.dex_wallet_private_key.get_secret_value())
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed_tx.rawTransaction)
            logger.info(f"DEX Swap 交易已發送: {tx_hash.hex()}")

            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=self.net_config.max_pending_seconds)
            
            success = receipt['status'] == 1
            if success:
                logger.success(f"DEX Swap 成功。Tx: {tx_hash.hex()}")
            else:
                logger.error(f"DEX Swap 失敗。Tx: {tx_hash.hex()}, 收據: {receipt}")

            return DexTxReceipt(
                tx_hash=tx_hash.hex(),
                status=success,
                block_number=receipt['blockNumber'],
                gas_used=receipt['gasUsed'],
                effective_gas_price=receipt['effectiveGasPrice'],
                # TODO: 從日誌中解析真實的成交價和數量
                avg_fill_price=params.price,
                filled_size=params.size
            )

        except Exception as e:
            logger.error(f"DEX 交換執行失敗: {e}", exc_info=True)
            raise
