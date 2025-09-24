# 維運指南 (Operations Guide)

本文件提供部署、監控和維護此套利機器人的指導。

## 1. 部署 (Deployment)

### 虛擬環境 (venv) - 建議開發使用
此為最直接的部署方式。

1.  確保伺服器已安裝 Python 3.11+。
2.  將專案複製到伺服器。
3.  建立並激活虛擬環境。
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
4.  安裝依賴。
    ```bash
    pip install -r requirements.txt
    ```
5.  創建 `.env` 檔案並填入生產環境的變數。
6.  使用 `systemd` 或 `supervisor` 來管理機器人行程，確保其能開機自啟並在崩潰後自動重啟。

**Systemd 服務範例 (`arbi-bot.service`):**
```ini
[Unit]
Description=DEX-CEX Arbitrage Bot
After=network.target

[Service]
User=your_user
Group=your_group
WorkingDirectory=/path/to/your/arbi-bot
ExecStart=/path/to/your/arbi-bot/venv/bin/python -m src.cli.main run
Restart=always
RestartSec=10
StandardOutput=append:/var/log/arbi-bot/output.log
StandardError=append:/var/log/arbi-bot/error.log

[Install]
WantedBy=multi-user.target
```

### Docker - 建議生產使用
使用 Docker 可以提供一個更一致、隔離的執行環境。

1.  **建立 `Dockerfile`**:
    ```dockerfile
    FROM python:3.11-slim

    WORKDIR /app

    COPY requirements.txt .
    RUN pip install --no-cache-dir -r requirements.txt

    COPY . .

    # 將 src 目錄添加到 PYTHONPATH
    ENV PYTHONPATH="${PYTHONPATH}:/app/src"

    # 設定環境變數 (建議使用 docker-compose 或 k8s secrets)
    # ENV ETH_RPC_URL=...

    CMD ["python", "-m", "src.cli.main", "run"]
    ```

2.  **建立映像**:
    ```bash
    docker build -t arbi-bot:latest .
    ```

3.  **執行容器**:
    最簡單的方式是使用 `--env-file` 來傳遞金鑰。
    ```bash
    docker run -d --name arbi-bot --env-file ./.env arbi-bot:latest
    ```
    在生產中，建議使用 Docker Secrets 或其他更安全的密鑰注入方式。

## 2. 監控 (Monitoring)

### Prometheus 指標
機器人會透過 `/metrics` 端點暴露 Prometheus 指標。
- **預設端口**: `9000` (可在 `config/default.yaml` 中修改)。
- **設定 Prometheus**: 在您的 `prometheus.yml` 中添加一個抓取目標。
  ```yaml
  scrape_configs:
    - job_name: 'arbi-bot'
      static_configs:
        - targets: ['<your-bot-ip>:9000']
  ```
- **關鍵指標**:
    - `arb_opportunities_found_total`: 發現的套利機會總數。
    - `arb_trades_executed_total`: 成功執行的套利交易總數。
    - `arb_pnl_quote_total`: 累計的 PnL (以報價貨幣計)。
    - `arb_failed_leg_total{leg="CEX|DEX", reason="..."}`: 交易腿失敗的計數，可用於診斷問題。
    - `arb_hedged_leg_total`: 成功對沖的次數。
    - `latency_ms_bucket{stage="..."}`: 各個階段 (偵測、下單) 的延遲分佈。

### 結構化日誌 (Structured Logging)
- **格式**: 日誌以 JSON 格式輸出，方便日誌聚合系統 (如 ELK Stack, Loki, Datadog) 進行解析和查詢。
- **位置**: 若使用 `systemd`，日誌會導向指定的檔案。若使用 Docker，日誌會輸出到 `stdout`/`stderr`，可由 Docker 的日誌驅動收集。
- **查詢範例**: 在 Grafana Loki 或 Kibana 中，您可以輕易地查詢特定交易對或特定錯誤。
  `{job="arbi-bot"} | json | level="error" and pair="ETH/USDT"`

## 3. 告警 (Alerting)

- **設定 Alertmanager**: 使用 Prometheus 的 Alertmanager，根據指標設定告警規則。
- **關鍵告警規則**:
    - **機器人離線**: `up{job="arbi-bot"} == 0`
    - **大量交易失敗**: `rate(arb_failed_leg_total[5m]) > 5`
    - **觸發熔斷**: `increase(risk_circuit_breaker_triggered_total[1m]) > 0`
    - **持續負 PnL**: `rate(arb_pnl_quote_total[15m]) < 0`
    - **錢包餘額過低**: `asset_balance{asset="ETH"} < 0.1`

## 4. 常見故障排查 (Troubleshooting)

- **時鐘同步**: 確保您的伺服器時間與全球標準時間 (NTP) 同步。與 CEX 伺服器的時間差可能導致 API 請求失敗 (特別是 Binance 的 `recvWindow` 錯誤)。
- **Nonce 衝突**: 如果您在同一個錢包上手動執行其他交易，可能導致機器人計算的 nonce 過期。機器人內建了基本的 nonce 管理，但在外部干擾下仍可能出錯。日誌中若出現 `nonce too low` 錯誤，請檢查是否有其他程序在使用該錢包。
- **API 限速**: CEX 和 RPC 節點都有請求頻率限制。如果日誌中出現 `RateLimitExceeded` 錯誤，請考慮：
    - 增加 `config/default.yaml` 中的輪詢間隔。
    - 升級您的 RPC 節點方案。
- **DEX 交易失敗**:
    - `transaction failed`: 可能是滑點過高或 Gas 不足。
    - `transaction pending for too long`: 網路擁堵，Gas Price 給得太低。機器人應有自動 bump fee 的機制，但若持續發生，需檢查 Gas 策略。
