# 即時監控儀表板

此目錄包含一個基於網頁的儀表板，用於即時監控套利機器人。

## 架構

- **後端**: Node.js 伺服器，訂閱機器人的 Redis 頻道 (`bot_dashboard_channel`)，並透過 WebSocket (`socket.io`) 將訊息轉發到前端。
- **前端**: React 應用程式 (使用 TypeScript 建構)，透過 WebSocket 接收即時數據，並使用 Chart.js 和日誌元件進行視覺化。

## 如何運行

要運行儀表板，您需要同時啟動後端伺服器和前端開發伺服器。建議在兩個獨立的終端機視窗中運行它們。

### 1. 運行後端伺服器

在您的第一個終端機中，導航到後端目錄並啟動伺服器：

```bash
cd dashboard/backend
node server.js
```

您應該會看到類似以下的輸出：
```
Connected to Redis successfully.
Subscribed to Redis channel: bot_dashboard_channel
Dashboard backend server listening on *:3001
```

### 2. 運行前端開發伺服器

在您的第二個終端機中，導航到前端目錄並啟動 React 應用程式：

```bash
cd dashboard/frontend
npm start
```

這將自動在您的網頁瀏覽器中打開一個新分頁，網址為 [http://localhost:3000](http://localhost:3000)。

### 3. 運行 Python 機器人

最後，在第三個終端機中，運行您的 Python 套利機器人（例如在紙上交易模式下）：

```bash
# 確保您的虛擬環境已激活
source venv_uniswap_swapper/bin/activate

# 運行機器人
python -m src.cli.main paper
```

## 使用方式

- 一旦所有三個元件（後端、前端、Python 機器人）都運行起來，您將在儀表板 [http://localhost:3000](http://localhost:3000) 上看到數據。
- 當機器人偵測到套利機會時，折線圖將會更新。
- 活動日誌將顯示機器人發布的所有訊息。
