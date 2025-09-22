# DEX-CEX 套利機器人 (ArbiBot)

這是一個基於 Python 的高頻套利機器人，旨在偵測並執行中心化交易所 (CEX) 與去中心化交易所 (DEX) 之間的價差套利機會。專案經過了完整的架構審視與重構，專注於低延遲、安全性、可靠性與可擴展性。

**免責聲明：此專案僅供學術研究與技術驗證。高頻交易與套利具有極高的金融與技術風險，可能導致重大資金損失。您必須自行承擔所有風險，作者不對任何潛在的虧損負責。在投入真實資金前，請務必在測試網和模擬環境中進行充分測試。請嚴格遵守您所在地的法律規範與交易所政策。**

---

## 核心功能

- **低延遲架構**: 以 `asyncio` 和 `uvloop` 為核心，CEX 行情採用 WebSocket 串流即時維護本地訂單簿，DEX 採用高效的 RPC 查詢，確保毫秒級的機會偵測能力。
- **設定檔驅動**: 所有策略、交易對、地址和風險參數均在 `config/` 目錄下的 YAML 檔案中定義，無需修改程式碼即可調整策略。
- **多鏈/多協議支援**: 架構設計上可輕鬆擴展至不同的鏈 (如 Ethereum, Arbitrum, BSC) 和 DEX 協議。
- **風險管理與安全**: 
    - **原子化執行與對沖**: 核心交易執行器 (`Executor`) 具備單邊交易失敗後的自動對沖邏輯，最大限度地避免產生風險敞口。
    - **金鑰安全**: 使用 `.env` 和 Pydantic 的 `SecretStr` 來管理金鑰，避免在日誌或程式碼中意外洩漏。
    - **風險參數**: 可自訂單筆名義額度、總倉位上限、熔斷閾值等關鍵風控參數。
- **多種運行模式**:
    - **紙上交易 (Paper Trading)**: 連接真實行情進行模擬，驗證數據準確性與策略有效性，但不執行真實交易。
    - **回測框架 (Backtesting)**: 可載入歷史數據 (CSV)，對策略進行績效評估與參數調優。
    - **實盤交易 (Live Trading)**: 執行真實交易。
- **狀態持久化**: 可將倉位、PnL 等狀態保存至檔案，在機器人重啟後能恢復先前狀態，實現不間斷管理。
- **資產再平衡**: 提供獨立的再平衡指令，可根據設定的目標比例，自動在 CEX 調整資產庫存。
- **自動機會發現 (AutoDiscovery Engine)**: 內建全新的自動發現引擎，取代了舊有的掃描器。它透過一個兩階段流程來發現套利機會：
    1. **DEX 數據集建立**: 執行一次性的 `dex_pool_scanner.py` 腳本，從多個鏈的 Uniswap Subgraph 中獲取數千個流動性池的數據，並過濾出符合 TVL 和交易量標準的潛力池，儲存為 `data/target_pools_Dex.json`。
    2. **CEX-DEX 匹配與評估**: `autodiscover` 指令會掃描幣安上交易量異常飆升的交易對，然後利用第一步建立的數據集，快速匹配出這些交易對在 DEX 上的對應流動性池，獲取即時價格，並將最終的套利機會寫入 `data/auto_discovery.json`。
- **可觀測性**: 透過 Prometheus 導出詳細的性能指標 (延遲、PnL、錯誤計數等)，並以 JSON 格式記錄結構化日誌，便於監控與告警。

## 系統架構

專案採用了清晰的模組化分層架構，實現了邏輯與基礎設施的分離。

- **`config/`**: 存放所有使用者可配置的 YAML 檔案。
- **`data/`**: 用於存放數據檔案，如回測數據集 (`.csv`)、機器人狀態 (`state.json`) 和自動發現的機會 (`auto_discovery.json`)。
- **`src/core/`**: 定義了專案的核心數據結構 (`types.py`) 和設定模型 (`config.py`)。
- **`src/infra/`**: 基礎設施層，負責日誌、指標、儲存和外部通訊 (HTTP/WS)。
- **`src/exchange/`**: 交易所客戶端實現，分為 `cex_base.py` 和 `dex_base.py` 協議，以及 `binance.py` 和 `univ3.py` 的具體實現。
- **`src/scanner/`**: 包含新的 `autodiscovery.py` 引擎和 `dex_pool_scanner.py` 腳本。
- **`src/strategy/`**: 策略層，是機器人的「大腦」，包含機會偵測 (`detector.py`)、路由 (`router.py`)、執行 (`executor.py`) 和再平衡 (`rebalancer.py`)。
- **`src/risk/`**: 風險管理模組，負責交易前的檢查和交易後的状态更新。
- **`src/cli/`**: 命令列介面 (`main.py`)，是整個應用程式的入口。
- **`backtest/`**: 回測框架，包含數據集處理 (`datasets.py`) 和模擬器 (`simulator.py`)。

### 數據流

1.  **機會發現 (手動觸發)**:
    - **`python src/scanner/dex_pool_scanner.py`**: 執行此腳本以更新本地的 DEX 流動性池數據集。
    - **`python -m src.cli.main autodiscover`**: 執行此指令，掃描 CEX 交易量，與本地 DEX 數據集進行匹配，並將發現的機會寫入 `data/auto_discovery.json`。
2.  **啟動監控 (主程式)**:
    - **`python -m src.cli.main paper`**: 啟動機器人。
    - `ArbiBotApp` 在初始化時，會自動載入 `config/pairs.yaml` 中的靜態交易對和 `data/auto_discovery.json` 中的動態交易對。
3.  **行情輸入**: `BinanceCexClient` 透過 WebSocket 維護訂單簿；`UniV3DexClient` 透過 RPC 查詢鏈上報價。
4.  **機會偵測**: `OpportunityDetector` 不斷比對 CEX 和 DEX 的價格。
5.  **產生機會**: 當價差超過 `min_edge_bps` 時，創建一個 `Opportunity` 物件。
6.  **執行交易**: `TransactionExecutor` 接收 `Opportunity`，產生標準化的 CEX/DEX legs 並即時計算 PnL 與 sanity check。
7.  **狀態更新**: `ExecutionSummary` 被發送到 `RiskManager`，後者更新倉位和 PnL，並透過 `storage.py` 將狀態持久化。
8.  **成交回補**: `BinanceCexClient` 訂閱 User Data Stream，取得即時 `executionReport` 成交事件，並將資訊推送至 Redis/dashboard，供真實損益與監控使用。

#### 交易腿與損益計算

- `DEX_to_CEX`：先在 DEX 以 `buy` 取得 base，再到 CEX 以 `sell` 釋出 base。
- `CEX_to_DEX`：先在 CEX `buy` base，再在 DEX `sell` base。
- 每條 `ExecutionLeg.price_quote` 都是「報價貨幣 / base」的價格，`ExecutionLeg.size` 始終是 base 數量，可直接與 CEX 報價比較。
- 損益計算公式：`PnL = size * (sell_px - buy_px) - cex_fee_quote - slippage_cost_quote - gas_cost_quote`，其中 CEX taker fee 固定 7.5 bps、slippage 來自 pair 設定；若使用限價單，可直接套用偵測得到的價格並依交易對精度下單。
- Executor 內建價格與 edge 的 sanity check，並會依據 taker/slippage 動態調整 `min_edge_bps` 門檻；相關閾值可在 `config/pairs.yaml` 的 `price_floor_quote`、`price_ceiling_quote`、`max_edge_bps` 與 `edge_safety_multiplier` 調整，以避免異常行情污染統計。
- Binance 端提供 User Data Stream 成交更新，可取得實際成交量與價格；如需對沖或更精細的 PnL，建議在主網以限價單為主，並在 `config/pairs.yaml` 中設定正確的 `base_precision`/`quote_precision`（對應 Binance 的 `stepSize` / `tickSize`）。

## 安裝與設定

### 1. 安裝

```bash
# 複製專案
git clone <your-repo-url>
cd <your-repo-name>

# 建立並啟用虛擬環境
python3 -m venv venv
source venv/bin/activate

# 安裝依賴
pip install -r requirements.txt
```

### 2. 設定

設定分為三個部分，請依序完成：

#### 第一步：環境變數 (.env)

這是存放最敏感金鑰的地方。請複製範例檔案並填寫您自己的金鑰。

```bash
cp .env.example .env
```

接著，編輯 `.env` 檔案：

```dotenv
# 至少提供一個您想交易的鏈的 RPC URL
ETH_RPC_URL="https://your_ethereum_rpc_url"
ARBITRUM_RPC_URL="https://your_arbitrum_rpc_url"
BSC_RPC_URL="https://your_bsc_rpc_url"

# CEX API 金鑰 (請務必只授予現貨交易權限，關閉提幣！)
BINANCE_API_KEY="YOUR_BINANCE_API_KEY"
BINANCE_API_SECRET="YOUR_BINANCE_API_SECRET"

# 用於 DEX 交易的錢包私鑰 (強烈建議使用一個全新的、僅為此機器人設立的專用熱錢包)
DEX_WALLET_PRIVATE_KEY="0x..."
```

#### 第二步：通用與交易對設定 (config/default.yaml, config/pairs.yaml, config/scanner.yaml)

- **`config/default.yaml`**: 設定全局參數，如風險限制、CEX mainnet/testnet、再平衡策略、DEX 的通用合約地址 (Router/Quoter/Factory) 等。
- **`config/pairs.yaml`**: 設定您想要**靜態**監控的核心交易對。
- **`config/scanner.yaml`**: 管理 `AutoDiscoveryEngine` 的所有參數，例如 CEX 掃描的條件（回溯時長、交易量倍率）、候選池數量、以及動態交易對的預設策略參數。

#### 第三步：新增鏈或代幣 (config/tokens.yaml)

所有代幣在不同鏈上的地址都在此檔案中統一管理。**注意：`AutoDiscoveryEngine` 不需要此檔案**，但對於 `pairs.yaml` 中靜態定義的交易對，以及 `rebalance`、`check-dex-balance` 等輔助指令，此檔案仍然是必需的。

- **若要新增一個代幣 (例如 MATIC on Polygon)**:
  在 `tokens.yaml` 中新增一個 `MATIC` 區塊，並提供它在 `polygon` 鏈上的地址和精度。
  ```yaml
  MATIC:
    polygon:
      address: "0x..."
      decimals: 18
  ```

- **若要為現有代幣新增一條鏈的支援**:
  在該代幣（如 `USDC`）下，新增鏈的名稱和對應的地址/精度即可。

## 如何執行

本專案提供一個功能豐富的命令列介面 (CLI)。

### 工作流程：從發現到監控

新的標準工作流程分為兩步：

#### 第一步：執行自動機會發現

首先，執行 `autodiscover` 指令來掃描市場並更新機會列表。您可以根據市場變化，定期（例如每小時）執行此指令。

```bash
python -m src.cli.main autodiscover
```
此指令會執行 `AutoDiscoveryEngine`，掃描 CEX 交易量，匹配 DEX 池，並將發現的潛在機會覆寫到 `data/auto_discovery.json`。

#### 第二步：啟動機器人進行監控

執行 `paper`（紙上交易）或 `run`（實盤交易）指令來啟動機器人。機器人現在會自動載入 `pairs.yaml` 和 `data/auto_discovery.json` 中的所有交易對進行監控。

```bash
python -m src.cli.main paper
```

如果機器人長時間沒有打印 `[PAPER MODE]` 的機會日誌，這可能意味著：
   * 當前市場上沒有符合您 `min_edge_bps` 設定的套利機會。您可以嘗試將靜態交易對的 `min_edge_bps` 設定為一個非常小的值（例如 0 或 1），以便在測試階段更容易偵測到機會。

### 輔助指令

#### 更新 DEX 數據集

在初次運行或您想更新 DEX 流動性池數據時，請手動執行 `dex_pool_scanner.py` 腳本。
目的是直接去 Subgraph，把所有存在的、有流動性的池子的完整清單（地圖）拿回來。

```bash
python src/scanner/dex_pool_scanner.py
```

#### 查詢 DEX 錢包餘額
```bash
python -m src.cli.main check-dex-balance
```

#### 檢查 CEX 資產並模擬再平衡
```bash
python -m src.cli.main rebalance --paper-run
```

#### 手動驗證池子是否存在
若需要手動驗證池子是否存在，可使用 CLI 指令查詢：
```bash
python -m src.cli.main lookup_pool REZ USDC --chain ethereum --fee 10000 \
  --base-address 0x... --base-decimals 18
```
未在 `tokens.yaml` 設定的代幣可以透過 `--base-address`／`--quote-address` 臨時指定地址與小數位，快速確認 Uniswap Factory 的回應。

### 回測 (Backtesting)

根據本地的歷史數據 (CSV) 評估策略表現。
```bash
# 您需要先準備一個符合格式的 a.csv 檔案
python -m src.cli.main backtest --dataset-path data/your_data.csv
```

### 資產再平衡 (Rebalancing)

手動觸發一次 CEX 資產庫存檢查與調整。
```bash
python -m src.cli.main rebalance
```

### 實盤交易 (Live Trading)

**風險極高！** 請確保您已完成所有測試，並完全理解程式碼的行為。
```bash
python -m src.cli.main run
```

## 其他文件/資訊

- **安全指南**: [SECURITY.md](SECURITY.md)
- **維運指南**: [OPERATIONS.md](OPERATIONS.md)
- **請求速率**: 在 src/app.py 的 main_loop 中有寫明， await asyncio.sleep(0.05) # 50ms, 避免空轉。
               在這個頻率之下，3 (個交易對) × 2 (個請求/交易對) × 20 (次循環/秒) = 120 RPS (每秒請求數)
               Quicknode費率：$212/month (yearly billed) for 125 RPS
               預算關係目前將sleep時間延長到0.2秒，即30 RPS。
- **偵測器邏輯**: 
  - 如果 `is_synthetic` 是 `False`：它會執行我們現有的、用於直接套利的雙邊價格比較邏輯。
  - 如果 `is_synthetic` 是 `True`：它將執行一個全新的三角套利驗證邏輯。它會併發地獲取三個價格：DEX 上的
         TOKEN/WETH 價格、CEX 上的 ETH/USDT 價格、以及 CEX 上的 TOKEN/USDT