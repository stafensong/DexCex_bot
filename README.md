# DexCex: High-Frequency Arbitrage Bot

This is a sophisticated high-frequency trading bot designed to execute arbitrage strategies between Centralized Exchanges (CEX), like Binance, and Decentralized Exchanges (DEX), like Uniswap v3.

The bot is architected to be modular, extensible, and performant, utilizing modern Python features and robust libraries.

[Read this in Chinese (繁體中文)](README_zh-TW.md)

## Core Features

- **CEX & DEX Integration**: Connects to Binance for centralized order book data and trading, and to Uniswap v3-compatible DEXs across multiple chains for decentralized trading.
- **Advanced Opportunity Detection**: Implements logic for both direct arbitrage (e.g., `TOKEN/USDT` on CEX vs. `TOKEN/USDT` on DEX) and triangular/synthetic arbitrage (e.g., `TOKEN/USDT` on CEX vs. `TOKEN/WETH` on DEX).
- **Automated Discovery Engine**: A powerful, three-step workflow to automatically discover, validate, and trade on new arbitrage opportunities, removing the need for manual configuration.
- **Risk Management**: Includes configurable risk controls, such as daily loss limits and maximum notional size per trade.
- **State Persistence**: Maintains state for daily PnL and positions, ensuring resilience across restarts.
- **High Performance**: Built on `asyncio` and `uvloop` for high-throughput, low-latency operations.
- **Observability**: Integrated with Prometheus for metrics and provides structured, readable logging.

---

## Core Workflow: Automated Opportunity Discovery

The primary method for finding and executing trades is the automated discovery workflow. This process ensures that all opportunities are based on officially verified token contracts, thereby eliminating risks from fraudulent or "scam" tokens.

This is a three-step process.

### Step 1: Build the Authoritative Token List (One-time Setup)

This is the most critical step for ensuring data integrity. We will generate a local, authoritative list of official token contract addresses by cross-referencing Binance's spot market with CoinGecko's public data.

**Command:**
```bash
python -m src.scanner.token_address_builder
```

**What it does:**
1.  Fetches all active spot market symbols from Binance.
2.  For each symbol, queries the CoinGecko API to find its official contract addresses on various chains (e.g., Ethereum, Arbitrum, Base).
3.  Saves this verified data into `data/master_token_list.json`.

> **Note:** This script communicates with the public CoinGecko API, which has a rate limit. The script includes a delay, so the initial run may take 15-20 minutes. You only need to run this periodically (e.g., weekly) to update your list with new tokens.

### Step 2: Scan for Liquid DEX Pools

Next, we scan the DEX subgraphs to build a local database of all available and sufficiently liquid pools.

**Command:**
```bash
python src/scanner/dex_pool_scanner.py
```

**What it does:**
- Queries Uniswap v3-compatible subgraphs on supported chains (Ethereum, Arbitrum, Base).
- Fetches a comprehensive list of pools that meet a minimum liquidity threshold.
- Saves the raw pool data to `data/target_pools_Dex.json`.

### Step 3: Run the Auto-Discovery Engine

This is the main engine that finds arbitrage opportunities. It cross-references CEX volume spikes with our verified local DEX data.

**Command:**
```bash
python -m src.cli.main autodiscover
```

**What it does:**
1.  Scans Binance for assets with recent volume spikes.
2.  Loads the DEX pool data from `data/target_pools_Dex.json`.
3.  Loads the official address list from `data/master_token_list.json`.
4.  **Address Verification**: It filters the DEX pools, keeping only those whose token addresses match the official addresses in our master list. This is the key step where "scam" tokens are eliminated.
5.  For each valid CEX volume spike, it finds corresponding DEX pools (both direct and synthetic) and evaluates potential arbitrage opportunities.
6.  Saves any viable opportunities into `data/auto_discovery.json`.

---

## Getting Started

### 1. Installation

```bash
# Clone the repository
git clone <repository_url>
cd DexCex_bot

# Create and activate a virtual environment
python3 -m venv venv_uniswap_swapper
source venv_uniswap_swapper/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configuration

- Copy the `.env.example` file to `.env`:
  ```bash
  cp .env.example .env
  ```
- Edit the `.env` file and fill in your API keys and wallet private key:
  - `BINANCE_API_KEY`: Your Binance API key.
  - `BINANCE_API_SECRET`: Your Binance API secret.
  - `DEX_WALLET_PRIVATE_KEY`: The private key of the wallet you will use for DEX trades. **(CRITICAL: Handle with extreme care)**
  - RPC URLs for the chains you intend to use (e.g., `BASE_RPC_URL`).

- Review and adjust configuration files in the `config/` directory, especially:
  - `default.yaml`: Main application settings.
  - `pairs.yaml`: For statically configured trading pairs.
  - `tokens.yaml`: Pre-defined addresses and decimals for common tokens.

### 3. Running the Bot

First, follow the **Core Workflow** described above to generate the necessary data files (`master_token_list.json`, `target_pools_Dex.json`, `auto_discovery.json`).

Once the discovery process is complete, you can start the bot in paper trading mode to monitor its behavior.

**Run in Paper Trading Mode:**
```bash
python -m src.cli.main paper
```

**What it does:**
- Loads both static pairs from `config/pairs.yaml` and dynamically discovered pairs from `data/auto_discovery.json`.
- Connects to all exchanges and starts monitoring for the loaded opportunities.
- When an opportunity is found and passes all risk checks, it will log a simulated trade but **will not** execute any real orders.

**Run in Live Trading Mode:**
> **WARNING**: Live trading involves real funds. Ensure your configuration is thoroughly tested in paper mode before proceeding.

```bash
python -m src.cli.main run
```

## CLI Commands

The application provides several useful CLI commands:

- `paper`: Run the bot in paper trading mode.
- `run`: Run the bot in live trading mode.
- `autodiscover`: Run the full opportunity discovery and validation engine.
- `rebalance`: Perform a one-time check and rebalance of assets on the CEX.
- `check-dex-balance`: Show the balance of all configured tokens on the DEX wallet.

Use the `--help` flag for more information on any command, e.g., `python -m src.cli.main --help`.
