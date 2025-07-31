import os
import json
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ==============================================================================
# |                                 CONFIG                                     |
# ==============================================================================

# --- General Settings ---
TRADE_VOLUME_USD = 100.00 # Example trade volume for profit calculation
CEX_TAKER_FEE_RATE = 0.00075
SLIPPAGE_THRESHOLD_PERCENT = 1.0
MIN_TVL_USD = 100_000
LOW_LIQUIDITY_MAX_TRADE_USD = 5_000
OPTIMAL_SLIPPAGE_TARGET = 0.3

# --- Scanning Speed & Cooldown ---
NORMAL_CYCLE_COOLDOWN_SECONDS = 60  # Normal cooldown
HOT_TARGET_CYCLE_COOLDOWN_SECONDS = 5 # Cooldown for hot targets
DELAY_BETWEEN_TASKS_SECONDS = 1.5 # Delay between starting each pair scan

# --- File-based linking ---
LOG_FILE = "data/arbitrage_log.txt"
VOLUME_LOG_FILE = "data/volume_log.txt"

# --- Redis Configuration ---
REDIS_HOST = 'localhost'
REDIS_PORT = 6379
REDIS_CHANNEL = "bot_dashboard_channel"

# --- Volume Scanner Configuration ---
VOLUME_THRESHOLD_RATIO = 1.5
KLINE_INTERVAL = '1h'
CYCLE_COOLDOWN_SECONDS = 600 # Cooldown for the volume scanner itself
ERROR_COOLDOWN_SECONDS = 60 # Cooldown after an error
CONCURRENT_REQUEST_LIMIT = 100
TARGET_QUOTE_CURRENCIES = ["USDT", "USDC"]


# --- Strategy Specific Thresholds ---
PROFIT_THRESHOLD_USD = 3.00

# --- Blacklist for high-competition CEX pairs ---
CEX_BLACKLIST = {'ETHUSDT', 'BTCUSDT', 'ETHUSDC', 'BTCUSDC', 'TUSDT', 'TUSDC'}

# --- RPC & ABIs ---
RPC_URLS = {
    'base': os.getenv('BASE_RPC_URL'),
    'arbitrum': os.getenv('ARBITRUM_RPC_URL'),
    'bsc': os.getenv('BSC_RPC_URL'),
}
# Add Router addresses for gas estimation
CONTRACT_ADDRESSES = {
    'uniswap': {
        'base': {
            'quoter_v2': '0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a',
            'router': '0x2626664c2603336E57B271c5C0b26F421741e481', # SwapRouter02
        },
        'arbitrum': {
            'quoter_v2': '0x61fFE014bA17989E743c5F6cB21bF9697530B21e',
            'router': '0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45', # SwapRouter02
        },
        'bsc': {
            'quoter_v2': '0x78D78E420Da98ad378D7799bE8f4AF69033EB077',
            'router': '0xB971eF87ede563556b2ED4b1C0b0019111Dd85d2', # SwapRouter02
        }
    },
    'pancakeswap': {
        'bsc': {
            'quoter_v2': '0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997',
            'router': '0x13f4EA83D0bd40E75C8222255bc855a974568Dd4', # PancakeSwap Smart Router
        }
    }
}

# Load ABIs from files
try:
    with open('./ABI/quoter.json') as f:
        QUOTER_V2_ABI = json.load(f)
    with open('./ABI/erc20.json') as f:
        ERC20_DECIMALS_ABI = json.load(f)
    # Load Router ABI for gas estimation
    with open('./ABI/router.json') as f:
        ROUTER_ABI = json.load(f)
except FileNotFoundError as e:
    print(f"[Config Error] ABI file not found. Please ensure the ABI directory is correct. Error: {e}")
    exit(1)


# A cache for token decimals
TOKEN_DECIMALS = {'WETH': 18, 'ETH': 18, 'WBNB': 18, 'BNB': 18, 'USDC': 6, 'USDT': 6, 'DAI': 18}
STABLECOINS = {"USDC", "USDT", "DAI"}

# A dummy address for gas estimation, as from address doesn't impact gas units
GAS_ESTIMATION_DUMMY_ADDRESS = "0x0000000000000000000000000000000000000001"
