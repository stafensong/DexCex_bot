# CEX-DEX Triangular Arbitrage Bot

This is an automated bot system for scanning arbitrage opportunities between Centralized Exchanges (CEX) and Decentralized Exchanges (DEX). The system features a multi-chain (Base, Arbitrum, BSC) and multi-protocol (Uniswap, PancakeSwap) design, integrating two core arbitrage strategies: direct arbitrage and triangular arbitrage.

Additionally, the system includes a real-time volume monitor and a web dashboard to enable efficient discovery and monitoring of market opportunities.

## System Architecture

The system consists of multiple independent but collaborative scripts that exchange data through JSON files and Redis, achieving a modular and efficient operational model.

```
+--------------------------+      +---------------------------+
|   pool_Dex_scanner.py    |----->|   target_pools_Dex.json   |
+--------------------------+      +---------------------------+
                                             |
                                             | (Read)
+--------------------------+      +---------------------------+      +---------------------------+
| volume_scanner_async.py  |----->|      hot_pools.json       |----->|         main.py           |
+--------------------------+      +---------------------------+      | (Core Arbitrage & Trading Bot) |
                                                                     +---------------------------+
                                                                                 |
                                                                                 | (Execute Trades)
                                             +-----------------------------------+-----------------------------------+
                                             |                                   |                                   |
                                  +--------------------------+      +--------------------------+      +--------------------------+
                                  |  cex_binance_trader.py   |      |  dex_uniswap_trader.py   |      |   dashboard_publisher.py |
                                  +--------------------------+      +--------------------------+      +--------------------------+
                                                                                                                   |
                                                                                                                   | (Publish Messages)
                                                                                                        +--------------------------+
                                                                                                        |          Redis           |
                                                                                                        +--------------------------+
                                                                                                                   |
                                                                                                                   | (Listen for Messages)
+--------------------------+      +--------------------------+                                          +--------------------------+
|      dashboard.html      |<---- |  dashboard_server.py     |------------------------------------------|
|      (Frontend)          |      |      (WebSocket)         |
+--------------------------+      +--------------------------+

```

**Data Flow Description:**
1.  **`pool_Dex_scanner.py`**: Scans liquidity pools on major DEXs, filtering them based on TVL (Total Value Locked) and pair type criteria. The results are written to `target_pools_Dex.json`. This is the first step in opportunity discovery.
2.  **`volume_scanner_async.py`**: Reads the pools from `target_pools_Dex.json` and continuously monitors the corresponding CEX (Binance) trading pairs. When an unusual surge in volume is detected, it writes these "hot pools" to `hot_pools.json`.
3.  **`main.py`**: This is the main bot program. It reads `target_pools_Dex.json` and `hot_pools.json`, prioritizing scans for arbitrage opportunities in the hot pools. Upon finding a potential profit, it logs the event and can optionally execute real trades via `cex_binance_trader.py` and `dex_uniswap_trader.py`.
4.  **Dashboard System**: All core scripts (scanners, main bot) publish their status, logs, and discovered opportunities to Redis via `dashboard_publisher.py`. The `dashboard_server.py` listens to Redis and broadcasts these real-time messages via WebSocket to the `dashboard.html` frontend, allowing users to monitor the system status in real-time.

## Main Features

- **Multi-Chain/Multi-Protocol Support**: Scans on chains like Base, Arbitrum, and BSC for protocols like Uniswap V3 and PancakeSwap V3.
- **Hybrid Arbitrage Strategies**:
  - **Direct Arbitrage (ALT/Stable)**: Finds price discrepancies between stablecoins and non-native tokens on DEXs and CEXs.
  - **Triangular Arbitrage (ALT/WETH)**: Calculates an implied `ALT/ETH` price from CEX pairs like `ALT/USDT` and `ETH/USDT` and compares it with the `ALT/WETH` pool on a DEX.
- **Volume Surge Detection**: Automatically focuses resources on "hot pools" with high trading volume and volatility, increasing the efficiency of opportunity discovery.
- **Real-Time Web Dashboard**: Provides a simple web interface for real-time monitoring of logs, discovered opportunities, and system errors.
- **Modular Design**: Functions like scanning, strategy, trade execution, and monitoring are split into independent modules, making them easy to maintain and extend.

## System Requirements

- Python 3.10 or higher
- A running Redis server
- An account with a blockchain node provider (e.g., Infura, Alchemy) to obtain RPC URLs
- A Binance account and API keys
- A wallet private key for DEX trading

## Installation and Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/ericeric0101/DexCex_bot.git
    cd DexCex_bot
    ```

2.  **Install Redis**
    Ensure that Redis is installed and running on your system. You can use Docker or install it based on your operating system.

3.  **Set Up Environment Variables**
    Copy the `.env.example` file and rename it to `.env`.
    ```bash
    cp .env.example .env
    ```
    Then, edit the `.env` file and fill in all the necessary keys and URLs:
    ```env
    # Binance API Keys (CEX)
    BINANCE_API_KEY="YOUR_BINANCE_API_KEY"
    BINANCE_API_SECRET="YOUR_BINANCE_API_SECRET"

    # Wallet Private Key (DEX) - EXTREMELY SENSITIVE
    PRIVATE_KEY="YOUR_WALLET_PRIVATE_KEY_WITHOUT_0x"

    # Blockchain RPC URLs
    BASE_RPC_URL="https://base-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY"
    ARBITRUM_RPC_URL="https://arb-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY"
    BSC_RPC_URL="https://bsc-dataseed.binance.org/"
    ```

4.  **Install Python Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

## How to Run the System

To operate the system correctly, please start the scripts in the following order in **separate terminal windows**.

**Step 1: Generate the Initial Target Pool List**
First, run the DEX scanner to create a list of potential arbitrage targets. This process may take a few minutes.
```bash
python pool_Dex_scanner.py
```
Upon success, you will see a `target_pools_Dex.json` file created.

**Step 2: Start the Dashboard Backend**
This service is responsible for receiving data from Redis and sending it to the frontend via WebSocket.
```bash
python dashboard_server.py
```
After starting, you can open `dashboard.html` in your browser to view the dashboard.

**Step 3: Start the Volume Scanner**
This script continuously monitors CEX volume and updates `hot_pools.json` with any hot pools.
```bash
python volume_scanner_async.py
```

**Step 4: Start the Main Arbitrage Bot**
This is the core of the system, which will begin scanning for opportunities in `target_pools_Dex.json` and `hot_pools.json`.
- **Monitoring-Only Mode (Recommended)**:
  If you only want to scan and log opportunities without executing real trades, run `CexDex_tri_bot.py`.
  ```bash
  python CexDex_tri_bot.py
  ```
- **Trade Execution Mode (High Risk!)**:
  If you want to automatically execute trades when an opportunity is found, run `main.py`.
  ```bash
  python main.py
  ```

## Script Descriptions

- `main.py`: **Main Execution Program**. Integrates arbitrage strategies and trade execution modules to automatically place orders when opportunities are found.
- `CexDex_tri_bot.py`: **Core Arbitrage Logic**. Contains the implementation of the arbitrage strategies and the main scanning loop but does not execute trades itself. It is used only for discovering and logging opportunities.
- `pool_Dex_scanner.py`: Fetches and filters eligible DEX liquidity pools from The Graph's subgraphs.
- `volume_scanner_async.py`: Monitors the trading volume of Binance pairs to identify market hotspots.
- `cex_binance_trader.py`: Handles interaction with the Binance API to execute CEX-side buy/sell orders.
- `dex_uniswap_trader.py`: Handles interaction with the blockchain to execute DEX-side swap transactions.
- `dashboard_server.py`: A FastAPI backend that provides the WebSocket service.
- `dashboard_publisher.py`: Provides a simple function for other scripts to publish messages to Redis.
- `dashboard.html`: The frontend page for displaying real-time messages from the backend.
- `requirements.txt`: The required Python libraries for the project.
- `.env`: Stores all sensitive API keys and private keys.

## Risk Warning and Disclaimer

**WARNING: This software is for academic research and technical validation purposes only. Using automated trading bots in live markets carries extremely high financial risk.**

- **Fund Security**: Automated trading can lead to significant losses due to code errors, API latency, extreme market volatility, and other factors. **Do not use real funds without extensive testing.**
- **Private Key Security**: Your wallet's private key is stored in the `.env` file. Ensure your operating environment is absolutely secure to prevent theft of the private key.
- **No Warranty**: This software is provided "as is" without any express or implied warranty. The author is not responsible for any direct or indirect losses resulting from the use of this software.

**It is strongly recommended that you first conduct extensive testing on a testnet or with a very small amount of funds and fully understand every detail of the code before considering its use in a live environment.**
