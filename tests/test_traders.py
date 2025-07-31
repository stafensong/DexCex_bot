import asyncio
import json
import os
import sys
from decimal import Decimal, ROUND_DOWN

from dotenv import load_dotenv
from web3 import Web3
import ccxt.async_support as ccxt

# --- 導入專案模組 ---
from cex_binance_trader import CexTrader
from dex_uniswap_trader import DexTrader
from CexDex_tri_bot import RPC_URLS

load_dotenv()

# ==============================================================================
# |                         核心交易執行函式 (修正版)                            |
# ==============================================================================

async def execute_test_trade(
    cex_trader: CexTrader, 
    dex_trader: DexTrader, 
    chain: str, 
    path: str, 
    cex_symbol: str, 
    cex_amount: float,
    dex_amount: float,
    token_in: dict,
    token_out: dict,
    pool_fee: int
):
    """
    直接根據手動輸入的參數執行CEX和DEX的交易。
    (現在 CEX 和 DEX 有各自的 amount)
    """
    print("\n" + "="*50)
    print(f"🔥 [MANUAL TEST TRIGGERED] 🔥")
    print(f"Path: {path} | Chain: {chain}")
    print(f"CEX Action: {path.split('_')[0]} {cex_amount:.6f} {cex_symbol.split('/')[0]}")
    print(f"DEX Action: Swap {dex_amount:.6f} {token_in['symbol']} for {token_out['symbol']}")
    print("="*50 + "\n")

    # --- 1. 建立 CEX 和 DEX 任務 ---
    cex_task = None
    dex_task = None

    cex_side = 'sell' if path == "DEX_BUY_CEX_SELL" else 'buy'

    # 建立任務
    cex_task = asyncio.create_task(
        cex_trader.execute_order(symbol=cex_symbol, side=cex_side, amount=cex_amount)
    )
    dex_task = asyncio.create_task(
        dex_trader.execute_swap(
            chain=chain,
            protocol='uniswap', # 假設為 uniswap
            token_in=token_in,
            token_out=token_out,
            amount=dex_amount,
            fee=pool_fee
        )
    )

    # --- 2. 同時執行並處理結果 ---
    print("Tasks created. Executing with asyncio.gather...")
    results = await asyncio.gather(cex_task, dex_task, return_exceptions=True)
    
    cex_result, dex_result = results

    print("\n--- TRADE EXECUTION RESULTS ---")
    
    if isinstance(cex_result, Exception):
        print(f"❌ CEX leg FAILED: {cex_result}")
    elif cex_result:
        print(f"✅ CEX leg SUCCEEDED. Order ID: {cex_result.get('id')}")
    else:
        print("❌ CEX leg FAILED with no specific order info.")

    if isinstance(dex_result, Exception):
        print(f"❌ DEX leg FAILED: {dex_result}")
    elif dex_result:
        print(f"✅ DEX leg SUCCEEDED. Transaction Hash: {dex_result}")
    else:
        print("❌ DEX leg FAILED with no specific transaction info.")
        
    print("---------------------------------")

    # --- 3. 處理部分成功的情況 ---
    cex_success = not isinstance(cex_result, Exception) and cex_result
    dex_success = not isinstance(dex_result, Exception) and dex_result

    if cex_success and not dex_success:
        print("\n! CRITICAL WARNING: CEX SUCCEEDED, DEX FAILED! MANUAL HEDGE REQUIRED!")
    elif not cex_success and dex_success:
        print("\n! CRITICAL WARNING: DEX SUCCEEDED, CEX FAILED! MANUAL HEDGE REQUIRED!")
    elif not cex_success and not dex_success:
        print("\nBoth legs of the trade failed. No exposure.")
    else:
        print("\n✅✅✅ Test execution fully successful on both legs! ✅✅✅")


# ==============================================================================
# |                             主函式 (修正版)                                  |
# ==============================================================================

async def main():
    print("--- Manual Arbitrage Test Script ---")
    
    # --- 讀取 ABI 和 Token 地址映射 ---
    try:
        with open('token_mapping.json', 'r') as f:
            token_map = json.load(f)
        with open('./ABI/erc20.json', 'r') as f:
            ERC20_ABI = json.load(f)
    except FileNotFoundError as e:
        print(f"[Fatal Error] Could not open required file: {e}. Please ensure both token_mapping.json and ABI/erc20.json exist.")
        return

    # --- 獲取使用者輸入 ---
    chain = input("Enter the chain (e.g., base, arbitrum): ").lower()
    if chain not in RPC_URLS:
        print(f"Invalid chain. Available options: {list(RPC_URLS.keys())}")
        return

    # 初始化 Web3 Provider
    w3 = Web3(Web3.HTTPProvider(RPC_URLS[chain]))
    if not w3.is_connected():
        print(f"Failed to connect to the RPC for {chain}.")
        return
    print(f"Connected to {chain} chain.")

    user_input_symbol = input("Enter the trading pair (e.g., WETH/USDC): ").upper()
    
    path = input("Enter the trade path (CEX_BUY_DEX_SELL or DEX_BUY_CEX_SELL): ").upper()
    if path not in ["CEX_BUY_DEX_SELL", "DEX_BUY_CEX_SELL"]:
        print("Invalid path. Please enter one of the two valid options.")
        return

    try:
        cex_amount = float(input("Enter the trade amount (in base asset, e.g., 0.1): "))
    except ValueError:
        print("Invalid amount. Please enter a number.")
        return
        
    try:
        pool_fee = int(input("Enter the DEX pool fee tier (e.g., 3000 for 0.3%): "))
    except ValueError:
        print("Invalid fee. Please enter an integer.")
        return

    # --- 初始化 Traders 和獲取 CEX 價格 ---
    temp_exchange = None
    try:
        print("\n[Info] Initializing traders and fetching CEX price...")
        cex_trader = CexTrader(api_key=os.getenv('BINANCE_API_KEY'), api_secret=os.getenv('BINANCE_API_SECRET'))
        dex_trader = DexTrader(rpc_url_mapping=RPC_URLS, private_key=os.getenv('PRIVATE_KEY'))
        
        # 修正 CEX symbol: WETH -> ETH
        cex_trade_symbol = user_input_symbol.replace('WETH', 'ETH')
        
        # 獲取 CEX 即時價格
        ticker = await cex_trader.exchange.fetch_ticker(cex_trade_symbol)
        cex_price = ticker['last']
        print(f"Fetched CEX price for {cex_trade_symbol}: {cex_price}")
        
        print("[Info] Traders initialized.")
    except Exception as e:
        print(f"Failed to initialize traders or fetch price: {e}")
        if 'cex_trader' in locals() and hasattr(cex_trader, 'close'):
            await cex_trader.close()
        return

    # --- 根據輸入設定交易參數 ---
    try:
        base_asset_symbol, stable_coin_symbol = user_input_symbol.split('/')
        
        def get_token_details(token_symbol):
            token_address = w3.to_checksum_address(token_map[chain][token_symbol])
            contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
            decimals = contract.functions.decimals().call()
            return {'address': token_address, 'symbol': token_symbol, 'decimals': decimals}

        base_asset = get_token_details(base_asset_symbol)
        stable_coin = get_token_details(stable_coin_symbol)

        if path == "CEX_BUY_DEX_SELL":
            token_in = base_asset
            token_out = stable_coin
            dex_amount = cex_amount # DEX賣出同等數量的 base asset
        else: # DEX_BUY_CEX_SELL
            token_in = stable_coin
            token_out = base_asset
            # DEX買入base asset, 需要計算應花費多少 stable coin
            dex_amount = cex_amount * cex_price
            print(f"Calculated DEX input amount: {dex_amount:.6f} {stable_coin['symbol']}")


    except KeyError as e:
        print(f"[Fatal Error] Token '{e.args[0]}' not found in `token_mapping.json` for the '{chain}' chain.")
        return
    except Exception as e:
        print(f"An error occurred while preparing parameters: {e}")
        return

    # --- 執行測試 ---
    try:
        await execute_test_trade(
            cex_trader,
            dex_trader,
            chain,
            path,
            cex_trade_symbol,
            cex_amount,
            dex_amount,
            token_in,
            token_out,
            pool_fee
        )
    except Exception as e:
        print(f"\n[Fatal Error] An unexpected error occurred during execution: {e}")
    finally:
        if 'cex_trader' in locals() and hasattr(cex_trader, 'close'):
            await cex_trader.close()
        print("\n--- Test finished ---")
        sys.exit(0)


if __name__ == "__main__":
    if not all(os.path.exists(f) for f in ['cex_binance_trader.py', 'dex_uniswap_trader.py', 'token_mapping.json']):
        print("[Fatal Error] One of the required files (`cex_binance_trader.py`, `dex_uniswap_trader.py`, `token_mapping.json`) is missing.")
        sys.exit(1)
        
    asyncio.run(main())