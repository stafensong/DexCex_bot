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
- **動態交易對發現**: 內建 Volume Scanner，能自動偵測 CEX 1 小時交易量異常的交易對、驗證 Uniswap V3 Pool 並將結果寫入 `data/discovered_pairs.yaml`，下次啟動時即自動納入監控。
- **Volume Spike 監控**: 每 30 分鐘掃描 Binance 所有 USDT 現貨交易對，鎖定成交量激增的標的並立即比對 DEX 價格，輸出可套利訊號。
- **可觀測性**: 透過 Prometheus 導出詳細的性能指標 (延遲、PnL、錯誤計數等)，並以 JSON 格式記錄結構化日誌，便於監控與告警。

## 系統架構

專案採用了清晰的模組化分層架構，實現了邏輯與基礎設施的分離。

- **`config/`**: 存放所有使用者可配置的 YAML 檔案。
- **`data/`**: 用於存放數據檔案，如回測數據集 (`.csv`) 和機器人狀態 (`state.json`)。
- **`src/core/`**: 定義了專案的核心數據結構 (`types.py`) 和設定模型 (`config.py`)。
- **`src/infra/`**: 基礎設施層，負責日誌、指標、儲存和外部通訊 (HTTP/WS)。
- **`src/exchange/`**: 交易所客戶端實現，分為 `cex_base.py` 和 `dex_base.py` 協議，以及 `binance.py` 和 `univ3.py` 的具體實現。
- **`src/strategy/`**: 策略層，是機器人的「大腦」，包含機會偵測 (`detector.py`)、路由 (`router.py`)、執行 (`executor.py`) 和再平衡 (`rebalancer.py`)。
- **`src/risk/`**: 風險管理模組，負責交易前的檢查和交易後的状态更新。
- **`src/cli/`**: 命令列介面 (`main.py`)，是整個應用程式的入口。
- **`backtest/`**: 回測框架，包含數據集處理 (`datasets.py`) 和模擬器 (`simulator.py`)。

### 數據流

1.  **行情輸入**: `BinanceCexClient` 透過 WebSocket 維護訂單簿；`UniV3DexClient` 透過 RPC 查詢鏈上報價。
2.  **機會偵測**: `OpportunityDetector` 不斷比對 CEX 和 DEX 的價格。
3.  **產生機會**: 當價差超過 `min_edge_bps` 時，創建一個 `Opportunity` 物件。
4.  **執行交易**: `TransactionExecutor` 接收 `Opportunity`，產生標準化的 CEX/DEX legs 並即時計算 PnL 與 sanity check。
5.  **狀態更新**: `ExecutionSummary` 被發送到 `RiskManager`，後者更新倉位和 PnL，並透過 `storage.py` 將狀態持久化。
6.  **成交回補**: `BinanceCexClient` 訂閱 User Data Stream，取得即時 `executionReport` 成交事件，並將資訊推送至 Redis/dashboard，供真實損益與監控使用。

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
- **`config/pairs.yaml`**: 設定您想要監控的交易對。每個交易對都需要指定 `base` (基礎貨幣), `quote` (報價貨幣), `dex_chain` (該交易對應的 DEX 所在鏈) 等。
- **`config/scanner.yaml`**: 管理 Volume Scanner 的觸發條件（如最小 1 小時交易量、異常倍率）以及動態交易對的預設參數與儲存位置。
  > **注意**：Volume Scanner 仍需在 `config/tokens.yaml` 中事先定義欲查詢代幣於各鏈的合約地址與小數位。若掃描時出現「tokens.yaml 缺少 XXX/鏈 的地址設定」，請先補齊代幣資訊後再重跑。

#### 第三步：新增鏈或代幣 (config/tokens.yaml)

所有代幣在不同鏈上的地址都在此檔案中統一管理。

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

### 紙上交易 (Paper Trading)

連接真實行情數據進行模擬，驗證數據準確性，但**不執行**任何真實交易。這是上線前最重要的驗證步驟。
```bash
python -m src.cli.main paper
```

1. 查詢 DEX 錢包餘額：
```bash
  python -m src.cli.main check-dex-balance
```

2. 檢查 CEX 資產並模擬再平衡（無實際交易）：
```bash
 python -m src.cli.main rebalance --paper-run
```

執行後預測日誌：顯示再平衡機器人的模擬計畫，它告訴您如果真的執行再平衡會發生什麼事，但並沒有實際執行。
   1. 資產: WETH, 當前比例: 100.00%,目標比例: 50.00%
       * 解釋：機器人檢查了您在 CEX (中心化交易所) 上的資產。在它監控的資產對 (WETH 和 USDT) 中，它發現您資產的總價值
         100% 都集中在 WETH 上，而您的目標是讓 WETH 和 USDT 的價值各佔 50%。這意味著您 CEX 帳戶中的 USDT
         餘額可能趨近於零。

   2. 資產比例偏離過大，觸發再平衡！
       * 解釋：因為 100% 的現況和 50% 的目標差距太大，超過了您設定的觸發門檻，所以機器人決定需要進行一次「再平衡」。

   3. 計畫在 CEX 以市價單 sell 0.110465 WETH
       * 解釋：這是機器人計算出來的行動計畫。為了讓資產比例回到 50/50，它需要賣掉一部分的 WETH 來換成
         USDT。它算出來需要賣掉的數量是 0.110465 WETH。
如果機器人長時間沒有打印 [PAPER MODE] 的機會日誌，這可能意味著：
   * 當前市場上沒有符合您 config/pairs.yaml 中 min_edge_bps 設定的套利機會。您可以嘗試將 min_edge_bps
     設定為一個非常小的值（例如 0 或 1），以便在測試階段更容易偵測到機會。

### 自動發現交易對 (Volume Scanner)

自動從 CEX 偵測 1 小時交易量異常的交易對，並確認 Uniswap V3 是否存在對應池子。符合條件者會寫入
`data/discovered_pairs.yaml`，並在下次啟動時自動加入監控名單。您可以使用底下指令手動觸發：
```bash
python -m src.cli.main discover_pairs  # 或使用 discover-pairs
```

Volume Scanner 的參數位於 `config/scanner.yaml`，可依需求調整引用的報價資產、異常倍率、最小成交量、偏好的鏈別與池子費率階層，以及寫入預設的交易對風險參數。

若需要手動驗證池子是否存在，可使用 CLI 指令查詢：
```bash
python -m src.cli.main lookup_pool REZ USDC --chain ethereum --fee 10000 \
  --base-address 0x... --base-decimals 18
```
未在 `tokens.yaml` 設定的代幣可以透過 `--base-address`／`--quote-address` 臨時指定地址與小數位，快速確認 Uniswap Factory 的回應。

### Volume Spike 監控與套利評估

此模組會每次執行時：

1. 透過 `get_klines()` 抓取所有 USDT 現貨交易對最近兩小時的成交量。
2. 若「當前小時成交量 > 前一小時 × 1.5」，視為 spike；依變化倍數排序並寫入 `data/volume_spikes.json`（僅保留前 10 名）。
3. 針對 JSON 中的 symbol 取得 Binance 最新價格與 Uniswap V3 報價，計算扣除手續費與 gas buffer 後的價差，超過 `config/scanner.yaml` → `spike.arbitrage.edge_threshold_bps` 即輸出套利訊號。

執行指令：
```bash
python -m src.cli.main spike_run
```
- `--no-evaluate`：僅更新 JSON，不計算價差。
- `--use-existing`：跳過 Binance 掃描，直接使用現有 JSON 評估（適用於短時間重複檢查）。

排程建議（例如每 30 分鐘一次）：
```cron
*/30 * * * * /bin/bash -lc 'cd /Users/<user>/DexCex_bot && source venv/bin/activate && python -m src.cli.main spike_run'
```

Volume Spike 模組的參數（掃描頻率、倍率門檻、檔案路徑、套利閾值等）皆在 `config/scanner.yaml` 中可自訂。

#### 排程自動更新 CEX 交易量清單

建議每小時或自訂週期執行一次 Volume Scanner，以便自動補齊新的潛在交易對。以下提供常見排程方式：

1. **crontab (Linux/macOS)**：
   ```bash
   crontab -e
   ```
   加入類似條目（以每小時執行為例）：
   ```cron
   0 * * * * /bin/bash -lc 'cd /Users/<你的使用者名稱>/DexCex_bot && source venv/bin/activate && python -m src.cli.main discover_pairs'
   ```
   - 請把路徑替換成實際的專案位置與虛擬環境。
   - 若使用 `venv_uniswap_swapper`，請將 `source` 指向對應的虛擬環境。

2. **systemd (Linux 伺服器)**：建立 `discover-pairs.service` 與 `discover-pairs.timer`，指定執行指令 `python -m src.cli.main discover_pairs`，並啟用 `systemctl enable --now discover-pairs.timer`。

3. **macOS launchd**：建立 `~/Library/LaunchAgents/com.arbibot.discoverpairs.plist`，在 `ProgramArguments` 中填入虛擬環境 Python 路徑與 `-m src.cli.main discover_pairs`，設定 `StartInterval` 3600 以每小時執行一次，最後用 `launchctl load` 載入。

排程完成後，可檢查 `data/discovered_pairs.yaml` 是否有新的交易對紀錄，並透過日誌確認執行情況。
```

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

## 其他文件

- **安全指南**: [SECURITY.md](SECURITY.md)
- **維運指南**: [OPERATIONS.md](OPERATIONS.md)
