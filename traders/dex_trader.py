import os
import time
import json
import asyncio
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv

class DexTrader:
    """
    A class to interact with DEXes like Uniswap or PancakeSwap, designed for the arbitrage bot.
    Handles multi-chain connections and asynchronous operations.
    """

    def __init__(self, rpc_url_mapping: dict, private_key: str):
        """
        Initializes the DexTrader.

        Args:
            rpc_url_mapping (dict): A dictionary mapping chain names (e.g., 'base') to their RPC URLs.
            private_key (str): The private key of the wallet to be used for trading.
        """
        if not private_key:
            raise ValueError("DEX private key not provided.")
        
        self.w3_instances = self._initialize_w3_instances(rpc_url_mapping)
        self.private_key = private_key
        self.user_address = Web3().eth.account.from_key(private_key).address
        
        # Load ABIs
        self.erc20_abi = self._load_abi('ABI/erc20.json')
        self.router_abi = self._load_abi('ABI/router.json')
        self.quoter_abi = self._load_abi('ABI/quoter.json')

        # Centralized contract addresses
        self.contract_addresses = {
            'uniswap_base': {'router': '0x2626664c2603336E57B271c5C0b26F421741e481', 'quoter': '0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a'},
            'uniswap_arbitrum': {'router': '0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45', 'quoter': '0x61fFE014bA17989E743c5F6cB21bF9697530B21e'},
            'pancakeswap_bsc': {'router': '0x13f4EA83D0bd40E75C8222255bc855a974568Dd4', 'quoter': '0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997'}
        }
        print(f"[Info] DexTrader initialized for address: {self.user_address}")

    def _load_abi(self, path: str):
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"[Fatal Error] ABI file not found at {path}.")
            raise

    def _initialize_w3_instances(self, rpc_urls: dict) -> dict:
        instances = {}
        for chain, url in rpc_urls.items():
            if not url:
                print(f"[Warning] No RPC URL for chain '{chain}', skipping.")
                continue
            w3 = Web3(Web3.HTTPProvider(url))
            if 'bsc' in chain:
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware(), layer=0)
            if not w3.is_connected():
                raise ConnectionError(f"Failed to connect to RPC for chain '{chain}' at {url}")
            instances[chain] = w3
            print(f"[Info] Connected to DEX chain: {chain} (Chain ID: {w3.eth.chain_id})")
        return instances

    def _get_w3(self, chain: str) -> Web3:
        w3 = self.w3_instances.get(chain)
        if not w3:
            raise ValueError(f"No active Web3 instance for chain '{chain}'.")
        return w3

    async def _approve_token(self, w3: Web3, token_address: str, router_address: str, amount_in_wei: int):
        token_contract = w3.eth.contract(address=token_address, abi=self.erc20_abi)
        
        allowance = await asyncio.to_thread(
            token_contract.functions.allowance(self.user_address, router_address).call
        )
        if allowance >= amount_in_wei:
            print(f"Sufficient allowance for {token_address} on chain {w3.eth.chain_id}.")
            return True

        print(f"Approving {amount_in_wei} of {token_address} for router {router_address}...")
        
        tx_params = {
            'from': self.user_address,
            'gas': 100000,
            'gasPrice': await asyncio.to_thread(getattr, w3.eth, 'gas_price'),
            'nonce': await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address),
            'chainId': w3.eth.chain_id
        }
        
        tx = await asyncio.to_thread(
            token_contract.functions.approve(router_address, amount_in_wei * 2).build_transaction, tx_params
        )
        
        signed_tx = w3.eth.account.sign_transaction(tx, self.private_key)
        tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed_tx.raw_transaction)
        
        print(f"Approval TX sent: {w3.to_hex(tx_hash)}. Waiting for receipt...")
        try:
            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=180)
            if receipt.status == 1:
                print("✅ Approval successful.")
                return True
            else:
                print(f"❌ Approval failed. Receipt: {receipt}")
                return False
        except Exception as e:
            print(f"Error waiting for approval receipt: {e}")
            return False

    async def execute_swap(self, chain: str, protocol: str, token_in: dict, token_out: dict, amount: float, fee: int, slippage: float = 0.5):
        """
        Executes a token swap on a specific DEX.

        Args:
            chain (str): The blockchain (e.g., 'base', 'arbitrum').
            protocol (str): The DEX protocol (e.g., 'uniswap', 'pancakeswap').
            token_in (dict): Dictionary with 'address' and 'decimals' for the input token.
            token_out (dict): Dictionary with 'address' for the output token.
            amount (float): The amount of token_in to swap.
            fee (int): The pool fee tier (e.g., 3000 for 0.3%).
            slippage (float): Slippage tolerance in percent.

        Returns:
            str: The transaction hash if successful, otherwise None.
        """
        w3 = self._get_w3(chain)
        config_key = f"{protocol}_{chain}"
        addresses = self.contract_addresses.get(config_key)
        if not addresses:
            print(f"[Error] No contract addresses found for {config_key}")
            return None

        router_address = w3.to_checksum_address(addresses['router'])
        quoter_address = w3.to_checksum_address(addresses['quoter'])
        token_in_address = w3.to_checksum_address(token_in['address'])
        token_out_address = w3.to_checksum_address(token_out['address'])
        
        amount_in_wei = int(amount * (10**token_in['decimals']))

        # Get quote
        quoter_contract = w3.eth.contract(address=quoter_address, abi=self.quoter_abi)
        try:
            quote_params = {'tokenIn': token_in_address, 'tokenOut': token_out_address, 'amountIn': amount_in_wei, 'fee': fee, 'sqrtPriceLimitX96': 0}
            quote_amount_out = await asyncio.to_thread(quoter_contract.functions.quoteExactInputSingle(quote_params).call)
            amount_out_minimum = int(quote_amount_out[0] * (1 - (slippage / 100)))
        except Exception as e:
            print(f"❌ Could not get quote: {e}")
            return None

        # Approve
        approved = await self._approve_token(w3, token_in_address, router_address, amount_in_wei)
        if not approved:
            print("Swap aborted due to approval failure.")
            return None

        # Execute
        print("Executing swap...")
        router_contract = w3.eth.contract(address=router_address, abi=self.router_abi)
        swap_params = {
            'tokenIn': token_in_address, 'tokenOut': token_out_address, 'fee': fee,
            'recipient': self.user_address, 'deadline': int(time.time()) + 600,
            'amountIn': amount_in_wei, 'amountOutMinimum': amount_out_minimum, 'sqrtPriceLimitX96': 0
        }
        
        try:
            tx_params = {
                'from': self.user_address, 'gas': 300000,
                'nonce': await asyncio.to_thread(w3.eth.get_transaction_count, self.user_address),
                'chainId': w3.eth.chain_id
            }
            tx = await asyncio.to_thread(router_contract.functions.exactInputSingle(swap_params).build_transaction, tx_params)
            
            signed_tx = w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed_tx.raw_transaction)
            
            print(f"Swap TX sent: {w3.to_hex(tx_hash)}. Waiting for receipt...")
            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=180)
            
            if receipt.status == 1:
                print(f"✅ Swap successful! Gas used: {receipt.gasUsed}")
                return w3.to_hex(tx_hash)
            else:
                print(f"❌ Swap failed. Receipt: {receipt}")
                return None
        except Exception as e:
            print(f"❌ An unexpected error occurred during swap execution: {e}")
            return None

# --- Example Usage ---
async def main_example():
    load_dotenv()
    rpc_urls = {'base': os.getenv('BASE_RPC_URL'), 'arbitrum': os.getenv('ARBITRUM_RPC_URL')}
    private_key = os.getenv("WALLET_PRIVATE_KEY")

    trader = DexTrader(rpc_url_mapping=rpc_urls, private_key=private_key)

    # Example: Swap 0.001 WETH for USDC on Arbitrum
    chain = 'arbitrum'
    protocol = 'uniswap'
    weth_arbitrum = {'address': '0x82aF49447D8a07e3bd95BD0d56f35241523fBab1', 'decimals': 18}
    usdc_arbitrum = {'address': '0xaf88d065e77c8cC2239327C5EDb3A432268e5831', 'decimals': 6}
    
    await trader.execute_swap(
        chain=chain,
        protocol=protocol,
        token_in=weth_arbitrum,
        token_out=usdc_arbitrum,
        amount=0.001,
        fee=500, # 0.05%
        slippage=0.5
    )

if __name__ == '__main__':
    asyncio.run(main_example())
