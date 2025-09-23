# DexCex：高頻套利機器人

這是一個精密的高頻交易機器人，專為在中心化交易所（CEX，如幣安）與去中心化交易所（DEX，如 Uniswap v3）之間執行套利策略而設計。

此機器人的架構設計旨在實現模組化、可擴展性與高效能，並採用了現代 Python 的特性與穩健的函式庫。

## 核心功能

- **CEX 與 DEX 整合**：連接到幣安以獲取中心化的訂單簿數據與進行交易，並連接到兼容 Uniswap v3 的去中心化交易所（跨越多條鏈）以進行去中心化交易。
- **進階機會偵測**：實現了直接套利（例如 CEX 上的 `TOKEN/USDT` vs. DEX 上的 `TOKEN/USDT`）與三角/合成套利（例如 CEX 上的 `TOKEN/USDT` vs. DEX 上的 `TOKEN/WETH`）的邏輯。
- **自動發現引擎**：一個強大的三步驟工作流程，可自動發現、驗證並交易新的套利機會，無需手動設定。
- **風險管理**：包含可配置的風險控制機制，例如每日虧損上限與單筆交易的最大名目金額。
- **狀態持久化**：維持每日損益（PnL）與部位的狀態，確保在重啟後仍能保持韌性。
- **高效能**：基於 `asyncio` 與 `uvloop` 建立，以實現高吞吐量與低延遲的操作。
- **可觀測性**：整合了 Prometheus 以進行指標監控，並提供結構化、易於閱讀的日誌記錄。

---

## 核心工作流程：自動化機會發現

尋找並執行交易的主要方法是自動化發現工作流程。此流程確保所有機會都基於經過官方驗證的代幣合約，從而消除了來自仿盤幣或「詐騙」代幣的風險。

這是一個三步驟的過程。

### 第一步：建立權威代幣清單（一次性設定）

這是確保數據完整性最關鍵的一步。我們將透過交叉比對幣安的現貨市場與 CoinGecko 的公開數據，來生成一份本地的、權威的官方代幣合約地址清單。

**指令：**
```bash
python -m src.scanner.token_address_builder
```

**其作用：**
1.  從幣安獲取所有活躍的現貨市場交易對符號。
2.  對於每個符號，查詢 CoinGecko API 以找到其在各個鏈上（如 Ethereum、Arbitrum、Base 等）的官方合約地址。
3.  將這些經過驗證的數據儲存到 `data/master_token_list.json` 中。

> **注意：** 此腳本會與公開的 CoinGecko API 通訊，該 API 有速率限制。腳本中已加入延遲，因此初次執行可能需要 15-20 分鐘。您只需定期（例如每週）運行此腳本，以更新您的清單並加入新的代幣。運作非常嚴格，如果檔案中只有20筆資料，那就只會處理那 20 個在 `master_token_list.json` 中能找到官方地址的代幣，所有其他的都會被過濾掉。

> **結論：**
   * **不會有風險**：系統不會允許任何未經驗證的代幣進入套利監控。這確保了我們不會再遇到「仿盤幣」的問題。
   * **機會會減少**：由於您的 `master_token_list.json` 目前只包含了 20 個代幣，autodiscover 引擎只會在這 20 個代幣以及
     USDT/USDC/WETH 之間尋找套利機會。即使幣安上有 664 個交易對出現了交易量突增，但只要那個代幣（例如 XYZ）不在您的
     `master_token_list.json` 中，所有與 XYZ 相關的 DEX 池子都會在第一時間被過濾掉。

### 第二步：掃描具流動性的 DEX 池

接下來，我們掃描 DEX 的 subgraphs，以建立一個包含所有可用且具備足夠流動性池子的本地資料庫。

**指令：**
```bash
python src/scanner/dex_pool_scanner.py
```

**其作用：**
- 查詢支援鏈上（Ethereum、Arbitrum、Base）的 Uniswap v3 相容 subgraph。
- 獲取一份符合最低流動性門檻的完整池子列表。
- 將原始的池子數據儲存到 `data/target_pools_Dex.json`。

### 第三步：運行自動發現引擎

這是尋找套利機會的主引擎。它會將 CEX 的交易量突增情況與我們已驗證的本地 DEX 數據進行交叉比對。

**指令：**
```bash
python -m src.cli.main autodiscover
```

**其作用：**
1.  掃描幣安以尋找近期有交易量突增的資產。
2.  從 `data/target_pools_Dex.json` 載入 DEX 池子數據。
3.  從 `data/master_token_list.json` 載入官方地址清單。
4.  **地址驗證**：過濾 DEX 池子，僅保留那些代幣地址與我們權威清單中官方地址相符的池子。這一步是消除「詐騙」代幣的關鍵。
5.  對於每個有效的 CEX 交易量突增，它會尋找對應的 DEX 池子（包括直接和合成對），並評估潛在的套利機會。
6.  將任何可行的機會儲存到 `data/auto_discovery.json` 中。

---

## 快速入門

### 1. 安裝

```bash
# 複製儲存庫
git clone <repository_url>
cd DexCex_bot

# 建立並啟用虛擬環境
python3 -m venv venv_uniswap_swapper
source venv_uniswap_swapper/bin/activate

# 安裝依賴套件
pip install -r requirements.txt
```

### 2. 設定

- 將 `.env.example` 檔案複製為 `.env`：
  ```bash
  cp .env.example .env
  ```
- 編輯 `.env` 檔案並填入您的 API 金鑰與錢包私鑰：
  - `BINANCE_API_KEY`：您的幣安 API 金鑰。
  - `BINANCE_API_SECRET`：您的幣安 API 密鑰。
  - `DEX_WALLET_PRIVATE_KEY`：您將用於 DEX 交易的錢包私鑰。 **（極度重要：請謹慎保管）**
  - 您打算使用的鏈的 RPC URL（例如 `BASE_RPC_URL`）。

- 檢視並調整 `config/` 目錄中的設定檔，特別是：
  - `default.yaml`：主應用程式設定。
  - `pairs.yaml`：用於靜態設定的交易對。
  - `tokens.yaml`：常用代幣的預定義地址和精度。

### 3. 運行機器人

首先，請遵循上述的「**核心工作流程**」來生成必要的數據檔案（`master_token_list.json`, `target_pools_Dex.json`, `auto_discovery.json`）。

在發現過程完成後，您可以先以紙上交易模式啟動機器人，以監控其行為。

**以紙上交易模式運行：**
```bash
python -m src.cli.main paper
```

**其作用：**
- 同時載入 `config/pairs.yaml` 中的靜態交易對與 `data/auto_discovery.json` 中的動態發現交易對。
- 連接到所有交易所，並開始監控已載入的機會。
- 當發現一個機會且通過所有風險檢查時，它會記錄一筆模擬交易，但**不會**執行任何真實訂單。

**以實盤交易模式運行：**
> **警告**：實盤交易涉及真實資金。在繼續之前，請確保您的設定已在紙上交易模式下經過充分測試。

```bash
python -m src.cli.main run
```

## CLI 指令

本應用程式提供了數個實用的 CLI 指令：

- `paper`：以紙上交易模式運行機器人。
- `run`：以實盤交易模式運行機器人。
- `autodiscover`：運行完整的機會發現與驗證引擎。
- `rebalance`：在 CEX 上執行一次性的資產檢查與再平衡。
- `check-dex-balance`：顯示 DEX 錢包中所有已設定代幣的餘額。

對於任何指令，您都可以使用 `--help` 旗標來獲取更多資訊，例如：`python -m src.cli.main --help`。