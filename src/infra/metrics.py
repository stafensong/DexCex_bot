from prometheus_client import start_http_server, Counter, Gauge, Histogram
from loguru import logger

from ..core.config import ObservabilityConfig

# --- 定義指標 ---

# 機會與交易
opportunities_found = Counter(
    "arb_opportunities_found_total",
    "發現的套利機會總數",
    ["pair", "direction"]
)
trades_executed = Counter(
    "arb_trades_executed_total",
    "執行的套利交易總數",
    ["pair", "direction", "status"] # status: success, failed, hedged
)

# 損益 (PnL)
pnl_quote = Counter(
    "arb_pnl_quote_total",
    "以報價貨幣計的累計 PnL",
    ["pair"]
)

# 失敗與對沖
failed_leg = Counter(
    "arb_failed_leg_total",
    "交易腿失敗計數",
    ["pair", "venue", "reason"] # venue: CEX/DEX, reason: timeout, reverted, etc.
)
hedged_leg = Counter(
    "arb_hedged_leg_total",
    "成功對沖的交易腿計數",
    ["pair", "venue"]
)

# 延遲
latency_ms = Histogram(
    "latency_ms",
    "各階段的處理延遲 (毫秒)",
    ["stage"], # stage: detection, execution, hedge
    buckets=[10, 25, 50, 75, 100, 150, 200, 300, 500, 1000]
)

# 庫存與餘額
asset_balance = Gauge(
    "asset_balance",
    "錢包或交易所中的資產餘額",
    ["asset", "venue"] # venue: CEX/DEX
)

# 風險
circuit_breaker_triggered = Counter(
    "risk_circuit_breaker_triggered_total",
    "熔斷機制觸發總次數",
    ["reason"]
)


def setup_metrics(config: ObservabilityConfig):
    """
    啟動 Prometheus 指標服務。
    """
    try:
        port = config.metrics_port
        start_http_server(port)
        logger.info(f"Prometheus 指標服務已在端口 {port} 啟動。")
        return True
    except Exception as e:
        logger.error(f"啟動 Prometheus 指標服務失敗: {e}")
        return None
