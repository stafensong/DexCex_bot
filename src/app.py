import asyncio
import json
import uvloop
from typing import List
from pathlib import Path

from .core.config import load_config, AppConfig
from .core.types import MarketPair
from .infra.logging import setup_logging
from .infra.metrics import setup_metrics
from .infra.dashboard import DashboardPublisher
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
        self.dashboard_publisher = None

        # 1. Load static pairs from config
        static_pairs = [MarketPair(
            base=p.base,
            quote=p.quote,
            cex_symbol=p.cex_symbol,
            dex_chain=p.dex_chain,
            dex_pool_fee=p.dex_pool_fee,
            base_precision=p.base_precision if p.base_precision is not None else 8,
            quote_precision=p.quote_precision if p.quote_precision is not None else 8,
            # Copy strategy params from static config
            min_edge_bps=p.min_edge_bps,
            max_slippage_bps=p.max_slippage_bps,
            max_size_quote=p.max_size_quote,
            price_floor_quote=p.price_floor_quote,
            price_ceiling_quote=p.price_ceiling_quote,
            max_edge_bps=p.max_edge_bps,
            edge_safety_multiplier=p.edge_safety_multiplier,
        ) for p in config.pairs]
        
        # 2. Load dynamic pairs from auto-discovery JSON
        dynamic_pairs = self._load_discovered_pairs(config)

        # 3. Combine and de-duplicate pairs
        final_pairs = {p.cex_symbol: p for p in static_pairs}
        for p in dynamic_pairs:
            if p.cex_symbol not in final_pairs:
                final_pairs[p.cex_symbol] = p
        
        pairs = list(final_pairs.values())
        self.logger.info(f"監控交易對總數: {len(pairs)}. (Static: {len(static_pairs)}, Dynamic: {len(dynamic_pairs)})")
        if not pairs:
            self.logger.warning("沒有設定任何交易對，機器人將不會執行任何交易。")

        if config.dashboard.enabled:
            self.dashboard_publisher = DashboardPublisher(
                config.dashboard.redis_url,
                config.dashboard.channel,
            )
        self.cex_client = BinanceCexClient(config.cex, config.secrets, pairs, dashboard_publisher=self.dashboard_publisher)
        self.dex_client = UniV3DexClient(config.dex, config.network, config.secrets, config.tokens)
        self.risk_manager = RiskManager(config.risk)
        self.detector = OpportunityDetector(self.cex_client, self.dex_client, pairs)
        self.router = SimpleRouter()
        
        # 根據模式選擇執行器
        if self.mode == 'paper':
            self.executor = PaperExecutor(pairs)
        else:
            self.executor = TransactionExecutor(self.cex_client, self.dex_client, self.risk_manager, pairs)
        self.logger.info(f"應用程式已在 {self.mode.upper()} 模式下初始化。")

    def _load_discovered_pairs(self, config: AppConfig) -> List[MarketPair]:
        """從 auto_discovery.json 檔案載入動態發現的交易對。"""
        if not config.scanner or not config.scanner.auto_discovery:
            return []
            
        discovery_path = Path(config.scanner.auto_discovery.persist_path)
        if not discovery_path.exists():
            self.logger.info("未找到 auto_discovery.json，跳過載入動態交易對。")
            return []

        try:
            with discovery_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            
            discovered_pairs = []
            opportunities = data.get("opportunities", [])
            self.logger.info(f"從 {discovery_path} 載入 {len(opportunities)} 個動態機會。")

            for opp in opportunities:
                if not opp.get("dex_candidates"):
                    continue
                
                candidate = opp["dex_candidates"][0]
                raw_pool = candidate.get("raw_pool_data")

                if not raw_pool:
                    self.logger.warning(f"Skipping opportunity {opp['symbol']} because raw_pool_data is missing.")
                    continue

                # --- Correctly identify token details from raw_pool_data ---
                token0 = raw_pool['token0']
                token1 = raw_pool['token1']
                
                # Normalize symbols to correctly identify base/quote
                token0_norm_sym = token0['symbol'].upper().replace('WETH', 'ETH').split('.')[0]
                token1_norm_sym = token1['symbol'].upper().replace('WETH', 'ETH').split('.')[0]
                
                base_details_raw, quote_details_raw = (token0, token1) if token0_norm_sym == opp["base"] else (token1, token0)

                # --- Authoritative Decimal Lookup ---
                is_synthetic = raw_pool.get('is_synthetic', False)
                intermediate_symbol = raw_pool.get('intermediate_symbol') if is_synthetic else None

                # For synthetic pairs, decimals MUST come from the raw pool data to match the on-chain addresses.
                if is_synthetic:
                    base_decimals = base_details_raw.get('decimals', 18)
                    quote_decimals = quote_details_raw.get('decimals', 18) # This is the intermediate token's decimals
                else:
                    # For direct pairs, we can use the authoritative lookup.
                    base_token_config = config.tokens.get(opp["base"], {}).get(candidate["chain"])
                    quote_token_config = config.tokens.get(opp["quote"], {}).get(candidate["chain"])
                    base_decimals = base_token_config.decimals if base_token_config else base_details_raw.get('decimals', 18)
                    quote_decimals = quote_token_config.decimals if quote_token_config else quote_details_raw.get('decimals', 18)

                # Load default strategy params from config for dynamic pairs
                default_params = config.scanner.volume.default_pair_params
                pair = MarketPair(
                    base=opp["base"],
                    quote=opp["quote"],
                    cex_symbol=opp["symbol"],
                    dex_chain=candidate["chain"],
                    dex_pool_fee=candidate["fee"],
                    base_address=base_details_raw['address'],
                    quote_address=quote_details_raw['address'],
                    base_decimals=base_decimals,
                    quote_decimals=quote_decimals,
                    is_synthetic=is_synthetic,
                    intermediate_symbol=intermediate_symbol,
                    base_precision=8, 
                    quote_precision=8,
                    min_edge_bps=default_params.get("min_edge_bps"),
                    max_slippage_bps=default_params.get("max_slippage_bps"),
                    max_size_quote=default_params.get("max_size_quote"),
                    price_floor_quote=default_params.get("price_floor_quote"),
                    price_ceiling_quote=default_params.get("price_ceiling_quote"),
                    max_edge_bps=default_params.get("max_edge_bps"),
                    edge_safety_multiplier=default_params.get("edge_safety_multiplier"),
                )
                discovered_pairs.append(pair)
            return discovered_pairs
        except Exception as e:
            self.logger.error(f"讀取或解析 {discovery_path} 時發生錯誤: {e}", exc_info=True)
            return []

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
                    await asyncio.sleep(0.2) # 0.1=100ms, 避免空轉
                    continue

                for opp in opportunities:
                    self.logger.info(f"發現機會: {opp.direction} | 價差: {opp.edge_bps:.2f} bps | 預期 PnL: {opp.expected_pnl_quote:.4f}")

                    plan = self.router.plan(opp)
                    self.logger.debug(f"生成執行計畫: {plan}")

                    if not self.risk_manager.is_trade_allowed(plan):
                        self.logger.warning(f"交易被風險管理員阻止: {plan.pair.cex_symbol}")
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
        if self.dashboard_publisher:
            await self.dashboard_publisher.close()
        self.logger.info("機器人已安全關閉。")