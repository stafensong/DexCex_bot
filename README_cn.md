# CEX-DEX Triangular Arbitrage Bot

這是一個用於掃描中心化交易所 (CEX) 和去中心化交易所 (DEX) 之間套利機會的自動化機器人系統。系統採用多鏈 (Base, Arbitrum, BSC)、多協議 (Uniswap, PancakeSwap) 設計，並整合了兩種核心套利策略：直接套利和三角套利。

此外，系統包含一個即時交易量監控器和一個網頁儀表板，以實現對市場機會的高效發現和監控。

## 系統架構

本系統由多個獨立但相互協作的腳本組成，透過 JSON 檔案和 Redis 進行數據交換，實現了模組化和高效率的運作模式。

```
+--------------------------+      +---------------------------+
|   pool_Dex_scanner.py    |----->|   target_pools_Dex.json   |
+--------------------------+      +---------------------------+
                                             |
                                             | (讀取)
+--------------------------+      +---------------------------+      +---------------------------+
| volume_scanner_async.py  |----->|      hot_pools.json       |----->|         main.py           |
+--------------------------+      +---------------------------+      |   (核心套利 & 交易機器人)   |
                                                                     +---------------------------+
                                                                                 |
                                                                                 | (執行交易)
                                             +-----------------------------------+-----------------------------------+
                                             |                                   |                                   |
                                  +--------------------------+      +--------------------------+      +--------------------------+
                                  |  cex_binance_trader.py   |      |  dex_uniswap_trader.py   |      |   dashboard_publisher.py |
                                  +--------------------------+      +--------------------------+      +--------------------------+
                                                                                                                   |
                                                                                                                   | (發布訊息)
                                                                                                        +--------------------------+
                                                                                                        |          Redis           |
                                                                                                        +--------------------------+
                                                                                                                   |
                                                                                                                   | (監聽訊息)
+--------------------------+      +--------------------------+                                          +--------------------------+
|      dashboard.html      |<---- |  dashboard_server.py     |------------------------------------------|
|      (前端儀表板)        |      |      (WebSocket)         |
+--------------------------+      +--------------------------+

```

**數據流說明:**
1.  **`pool_Dex_scanner.py`**: 掃描各大 DEX 的流動性池，篩選出符合 TVL (總鎖定價值) 和交易對類型標準的池子，並將結果寫入 `target_pools_Dex.json`。這是機會發現的第一步。
2.  **`volume_scanner_async.py`**: 讀取 `target_pools_Dex.json` 中的池子，並持續監控對應的 CEX (Binance) 交易對。當發現交易量異常放大時，它會將這些 "熱點池" 寫入 `hot_pools.json`。
3.  **`main.py`**: 這是主機器人程序。它會讀取 `target_pools_Dex.json` 和 `hot_pools.json`，優先掃描熱點池中的套利機會。一旦發現潛在利潤，它會記錄日誌，並可選擇性地透過 `cex_binance_trader.py` 和 `dex_uniswap_trader.py` 執行真實交易。
4.  **儀表板系統**: 所有核心腳本 (掃描器、主機器人) 都會透過 `dashboard_publisher.py` 將狀態、日誌和發現的機會發布到 Redis。`dashboard_server.py` 則監聽 Redis，並透過 WebSocket 將這些即時訊息廣播給前端 `dashboard.html`，讓使用者可以即時監控系統狀態。

## 主要功能

- **多鏈/多協議支持**: 可在 Base, Arbitrum, BSC 等鏈上，對 Uniswap V3, PancakeSwap V3 等協議進行掃描。
- **混合套利策略**:
  - **直接套利 (ALT/Stable)**: 在穩定幣和非原生代幣之間尋找 DEX 和 CEX 的價差。
  - **三角套利 (ALT/WETH)**: 透過 CEX 上的 `ALT/USDT` 和 `ETH/USDT` 推算出 `ALT/ETH` 的隱含價格，並與 DEX 上的 `ALT/WETH` 池進行比較。
- **交易量突增檢測**: 自動將資源集中在高交易量、高波動性的 "熱點池" 上，提高發現機會的效率。
- **即時網頁儀表板**: 提供一個簡單的網頁介面，用於即時監控日誌、發現的機會和系統錯誤。
- **模組化設計**: 掃描、策略、交易執行和監控等功能被拆分成獨立模組，易於維護和擴展。

## 環境要求

- Python 3.10 或更高版本
- 一個正在運行的 Redis 伺服器
- 區塊鏈節點提供商 (例如 Infura, Alchemy) 的帳戶以獲取 RPC URL
- Binance (幣安) 帳戶及 API 金鑰
- 一個用於 DEX 交易的錢包私鑰

## 安裝與設定

1.  **克隆儲存庫**
    ```bash
    git clone <repository-url>
    cd <repository-directory>
    ```

2.  **安裝 Redis**
    請確保您的系統上已安裝並正在運行 Redis。您可以使用 Docker 或根據您的操作系統進行安裝。

3.  **設定環境變數**
    複製 `.env.example` 檔案並將其重新命名為 `.env`。
    ```bash
    cp .env.example .env
    ```
    然後，編輯 `.env` 檔案並填入所有必要的金鑰和 URL：
    ```env
    # Binance API Keys (CEX)
    BINANCE_API_KEY="YOUR_BINANCE_API_KEY"
    BINANCE_API_SECRET="YOUR_BINANCE_API_SECRET"

    # Wallet Private Key (DEX) - EXTREMELY SENSITIVE
    PRIVATE_KEY="YOUR_WALLET_PRIVATE_KEY_WITHOUT_0x"

    # Blockchain RPC URLs
    BASE_RPC_URL="your RPC URL"
    ARBITRUM_RPC_URL="your RPC URL"
    BSC_RPC_URL="https://bsc-dataseed.binance.org/"
    ```

4.  **安裝 Python 依賴**
    ```bash
    pip install -r requirements.txt
    ```

## 如何運行系統

為了讓系統正確運作，請按照以下順序在**不同的終端機視窗**中啟動各個腳本。

**步驟 1: 產生初始目標池列表**
首先，運行 DEX 掃描器來建立一份潛在的套利目標清單。這個過程可能需要幾分鐘。
```bash
python pool_Dex_scanner.py
```
成功後，您會看到一個 `target_pools_Dex.json` 檔案被建立。

**步驟 2: 啟動儀表板後端**
這個服務負責從 Redis 接收數據並透過 WebSocket 傳送給前端。
```bash
python dashboard_server.py
```
啟動後，您可以在瀏覽器中打開 `dashboard.html` 來查看儀表板。

**步驟 3: 啟動交易量掃描器**
這個腳本會**持續**監控 CEX 上的交易量，並將熱點池更新到 `hot_pools.json`。
```bash
python volume_scanner_async.py
```

**步驟 4: 啟動主套利機器人**
這是系統的核心，它會開始掃描 `target_pools_Dex.json` 和 `hot_pools.json` 中的機會。
- **僅監控模式 (推薦)**:
  如果只想掃描和記錄機會而不執行真實交易，請運行 `CexDex_tri_bot.py`。
  ```bash
  python CexDex_tri_bot.py
  ```
- **交易執行模式 (高風險!)**:
  如果想在發現機會時自動執行交易，請運行 `main.py`。
  ```bash
  python main.py
  ```

## 腳本說明

- `main.py`: **主執行程序**。整合了套利策略和交易執行模組，能夠在發現機會時自動下單。
- `CexDex_tri_bot.py`: **核心套利邏輯**。包含套利策略的實現和主掃描循環，但本身不執行交易，僅用於發現和記錄機會。
- `pool_Dex_scanner.py`: 從 The Graph 的子圖中獲取並篩選符合條件的 DEX 流動性池。
- `volume_scanner_async.py`: 監控 Binance 交易對的交易量，以識別市場熱點。
- `cex_binance_trader.py`: 負責與 Binance API 互動，執行 CEX 端的買賣訂單。
- `dex_uniswap_trader.py`: 負責與區塊鏈互動，執行 DEX 端的 Swap 交易。
- `dashboard_server.py`: FastAPI 後端，提供 WebSocket 服務。
- `dashboard_publisher.py`: 提供一個簡單的函數，讓其他腳本可以向 Redis 發布訊息。
- `dashboard.html`: 前端頁面，用於展示來自後端的即時訊息。
- `requirements.txt`: 專案所需的 Python 函式庫。
- `.env`: 存儲所有敏感的 API 金鑰和私鑰。

## 風險提示與免責聲明

**警告：此軟體僅供學術研究和技術驗證使用。在真實市場中使用自動化交易機器人具有極高的金融風險。**

- **資金安全**: 自動交易可能因程式碼錯誤、API 延遲、市場劇烈波動等原因導致嚴重虧損。**切勿在未經充分測試的情況下使用真實資金。**
- **私鑰安全**: 您的錢包私鑰存儲在 `.env` 檔案中。請確保您的操作環境絕對安全，防止私鑰被盜。
- **無任何保證**: 本軟體按 "原樣" 提供，不附帶任何明示或暗示的保證。對於因使用本軟體而造成的任何直接或間接損失，作者概不負責。

**強烈建議您先在測試網或使用極小金額進行大量測試，完全理解程式碼的每一個細節後，再考慮在真實環境中使用。**
