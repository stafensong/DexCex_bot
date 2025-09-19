# DEX-CEX 套利機器人 (arbi-bot)

這是一個基於 Python 的高頻套利機器人，用於在中心化交易所 (CEX) 和去中心化交易所 (DEX) 之間尋找並執行價差套利機會。

**免責聲明：此專案僅供學術研究與技術驗證。高頻交易與套利具有極高的風險，可能導致重大資金損失。您必須自行承擔所有風險，作者不對任何潛在的虧損負責。在投入真實資金前，請務必在測試網和模擬環境中進行充分測試。請遵守您所在地的法律規範與交易所政策。**

## 核心功能

- **多鏈與多協議支援**: 可設定在不同鏈 (如 Ethereum, Arbitrum, BSC) 和 DEX (如 Uniswap v3, PancakeSwap) 上運作。
- **低延遲架構**: 以 `asyncio` (uvloop) 為核心，CEX 行情採用 WebSocket 串流，DEX 採用高效的 RPC 查詢。
- **風險管理**:
    - **原子化執行與對沖**: 交易執行器確保在任一邊交易失敗時，能立即對沖已成功的另一邊，避免產生裸露風險部位。
    - **熔斷機制**: 內建多種熔斷條件 (如單筆超額虧損、庫存超限)，自動停止交易。
    - **額度限制**: 可設定單筆、總計的交易名義額度。
- **可觀測性**: 透過 Prometheus 導出詳細的性能指標，並以 JSON 格式記錄結構化日誌。
- **可回測性**: 提供回測框架，可載入歷史數據以評估策略表現。

## 安裝

1.  **前置要求**:
    *   Python 3.11+
    *   一個安全的 Ethereum RPC 節點 (例如 Alchemy, Infura)。
    *   Binance 交易所帳戶及 API 金鑰。

2.  **複製專案**:
    ```bash
    git clone <your-repo-url>
    cd arbi-bot
    ```

3.  **設定虛擬環境**:
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

4.  **安裝依賴**:
    ```bash
    pip install -r requirements.txt
    ```

## 設定

1.  **環境變數**:
    複製範例檔案並填入您的金鑰。**此檔案絕不應提交到版本控制中。**
    ```bash
    cp .env.example .env
    ```
    接著編輯 `.env` 檔案：
    ```dotenv
    # Ethereum RPC 節點 URL
    ETH_RPC_URL="https://eth-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY"
    ARBITRUM_RPC_URL="https://arb-mainnet.g.alchemy.com/v2/YOUR_ALCHEMY_KEY"
    BSC_RPC_URL="https://bsc-dataseed.binance.org/"

    # CEX API 金鑰 (僅需現貨交易權限，關閉提幣權限)
    BINANCE_API_KEY="YOUR_BINANCE_API_KEY"
    BINANCE_API_SECRET="YOUR_BINANCE_API_SECRET"

    # 用於 DEX 交易的錢包私鑰 (建議使用僅為此機器人創建的專用熱錢包)
    DEX_WALLET_PRIVATE_KEY="0x..."

    # (可選) 用於發送告警的 Webhook URL
    ALERT_WEBHOOK_URL="https://hooks.slack.com/services/..."
    ```

2.  **策略設定**:
    *   `config/default.yaml`: 全局設定，如網路、DEX/CEX 端點、風險參數等。
    *   `config/pairs.yaml`: 您想要監控的交易對列表及相關參數。

## 執行

本專案提供一個命令列介面 (CLI) 來啟動不同模式。

1.  **實盤交易 (Live Trading)**:
    ```bash
    python -m src.cli.main run
    ```

2.  **紙上交易 (Paper Trading)**:
    此模式會偵測機會並記錄，但不會實際執行任何交易。
    ```bash
    python -m src.cli.main paper
    ```

3.  **執行資產再平衡**:
    根據設定的目標比例，在 CEX 上調整資產庫存。
    ```bash
    python -m src.cli.main rebalance
    ```

4.  **回測 (Backtesting)**:
    ```bash
    python -m src.cli.main backtest --dataset-path /path/to/your/data.csv
    ```

## 風險與安全

請務必閱讀 [SECURITY.md](SECURITY.md) 以了解詳細的安全設置指南，包括金鑰管理、API 權限和日誌脫敏。

## 維運

請參考 [OPERATIONS.md](OPERATIONS.md) 了解部署、監控和故障排查的相關資訊。