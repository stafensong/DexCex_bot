import asyncio
import uvloop
from typing import List

from .core.config import load_config, AppConfig
from .core.types import MarketPair
from .infra.logging import setup_logging
from .infra.metrics import setup_metrics
from .exchange.binance import BinanceCexClient
from .exchange.univ3 import UniV3DexClient
from .strategy.detector import OpportunityDetector
from .strategy.router import SimpleRouter
from .strategy.executor import TransactionExecutor, PaperExecutor
from .risk.limits import RiskManager

# 安裝 uvloop 以提升異步性能
uvloop.install()

class ArbiBotApp:
    def __init__(self, config: AppConfig, mode: str = 'live'):
        self.config = config
        self.logger = setup_logging(config.observability)
        self.metrics_server = setup_metrics(config.observability)
        self.running = False
        self.mode = mode

        # 初始化核心組件
        pairs = [MarketPair(
            base=p.base, 
            quote=p.quote, 
            symbol=p.cex_symbol, 
            dex_chain=p.dex_chain, 
            dex_pool_fee=p.dex_pool_fee
        ) for p in config.pairs]
        self.cex_client = BinanceCexClient(config.cex, config.secrets, pairs)
        self.dex_client = UniV3DexClient(config.dex, config.network, config.secrets, config.tokens)
        self.risk_manager = RiskManager(config.risk)
        self.detector = OpportunityDetector(self.cex_client, self.dex_client, config.pairs)
        self.router = SimpleRouter()
        
        # 根據模式選擇執行器
        if self.mode == 'paper':
            self.executor = PaperExecutor()
        else:
            self.executor = TransactionExecutor(self.cex_client, self.dex_client, self.risk_manager)
        self.logger.info(f"應用程式已在 {self.mode.upper()} 模式下初始化。")

    async def start(self):
        self.logger.info("機器人啟動中...")
        self.running = True
        await self.cex_client.connect()
        
        if self.config.risk.cancel_all_on_start:
            self.logger.info("正在取消所有現有 CEX 掛單 (功能待實現)...")
            # await self.cex_client.cancel_all_orders()
        
        self.logger.info("機器人已啟動，等待 CEX 訂單簿同步...")
        # 等待幾秒鐘讓 WebSocket 訂單簿初步同步
        await asyncio.sleep(5)
        self.logger.info("開始監控市場...")

        try:
            await self.main_loop()
        except asyncio.CancelledError:
            self.logger.info("主循環被取消。")
        finally:
            await self.shutdown()

    async def main_loop(self):
        while self.running:
            try:
                opportunities = await self.detector.detect()
                if not opportunities:
                    await asyncio.sleep(0.05) # 50ms, 避免空轉
                    continue

                for opp in opportunities:
                    self.logger.info(f"發現機會: {opp.direction} | 價差: {opp.edge_bps:.2f} bps | 預期 PnL: {opp.expected_pnl_quote:.4f}")

                    plan = self.router.plan(opp)
                    self.logger.debug(f"生成執行計畫: {plan}")

                    if not self.risk_manager.is_trade_allowed(plan):
                        self.logger.warning(f"交易被風險管理員阻止: {plan.pair.symbol}")
                        continue

                    exec_summary = await self.executor.run(plan)
                    self.logger.info(f"執行完畢: {exec_summary}")

                    self.risk_manager.update_state(exec_summary)

            except Exception as e:
                self.logger.error(f"主循環發生未預期錯誤: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def shutdown(self):
        self.logger.info("機器人關閉中...")
        self.running = False
        await self.cex_client.close()
        # if self.metrics_server: # Prometheus server is a daemon, no need to stop explicitly
        #     await self.metrics_server.stop()
        self.logger.info("機器人已安全關閉。")