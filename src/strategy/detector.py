from typing import List, Optional
from decimal import Decimal
import asyncio

from loguru import logger

from ..core.config import PairConfig
from ..core.types import Opportunity, MarketPair, Quote
from ..exchange.cex_base import CexClient
from ..exchange.dex_base import DexClient
from ..infra import metrics

class OpportunityDetector:
    def __init__(self, cex_client: CexClient, dex_client: DexClient, pairs: List[PairConfig]):
        self.cex_client = cex_client
        self.dex_client = dex_client
        self.pairs = pairs
        logger.info(f"機會偵測器初始化，監控 {len(pairs)} 個交易對。")

    async def detect(self) -> List[Opportunity]:
        """
        掃描所有設定的交易對，尋找套利機會。
        """
        tasks = [self.check_pair(p) for p in self.pairs]
        results = await asyncio.gather(*tasks)
        
        opportunities = [opp for opp in results if opp]
        return opportunities

    async def check_pair(self, pair_config: PairConfig) -> Optional[Opportunity]:
        """
        檢查單一交易對的 CEX 和 DEX 價格，計算潛在機會。
        """
        pair = MarketPair(
            base=pair_config.base, 
            quote=pair_config.quote, 
            symbol=pair_config.cex_symbol,
            dex_chain=pair_config.dex_chain,
            dex_pool_fee=pair_config.dex_pool_fee
        )
        
        # 從 CEX 本地訂單簿獲取報價
        cex_quote = await self.cex_client.get_quote(pair)
        if not (cex_quote and cex_quote.bid_price and cex_quote.ask_price):
            return None

        # 假設我們想交易 1 個 base asset (此處應有更智能的數量決定邏輯)
        trade_size = Decimal("0.1") 
        
        # 併發獲取 DEX 報價
        dex_buy_task = self.dex_client.get_quote(pair, size=trade_size, side="buy")
        dex_sell_task = self.dex_client.get_quote(pair, size=trade_size, side="sell")
        dex_buy_quote, dex_sell_quote = await asyncio.gather(dex_buy_task, dex_sell_task)

        if not (dex_buy_quote and dex_sell_quote):
            return None

        cex_bid_price = cex_quote.bid_price
        cex_ask_price = cex_quote.ask_price
        dex_buy_price = dex_buy_quote.price
        dex_sell_price = dex_sell_quote.price

        # --- 計算兩個方向的機會 ---

        # 方向 1: 在 CEX 買, 在 DEX 賣 (CEX_to_DEX)
        edge_bps_1 = (dex_sell_price - cex_ask_price) / cex_ask_price * 10000
        
        if edge_bps_1 > pair_config.min_edge_bps:
            opp = self._create_opportunity(
                pair=pair, direction="CEX_to_DEX", cex_price=cex_ask_price,
                dex_price=dex_sell_price, edge_bps=edge_bps_1, pair_config=pair_config, size=trade_size
            )
            metrics.opportunities_found.labels(pair=pair.symbol, direction="CEX_to_DEX").inc()
            return opp

        # 方向 2: 在 DEX 買, 在 CEX 賣 (DEX_to_CEX)
        edge_bps_2 = (cex_bid_price - dex_buy_price) / dex_buy_price * 10000

        if edge_bps_2 > pair_config.min_edge_bps:
            opp = self._create_opportunity(
                pair=pair, direction="DEX_to_CEX", cex_price=cex_bid_price,
                dex_price=dex_buy_price, edge_bps=edge_bps_2, pair_config=pair_config, size=trade_size
            )
            metrics.opportunities_found.labels(pair=pair.symbol, direction="DEX_to_CEX").inc()
            return opp
            
        return None

    def _create_opportunity(self, pair: MarketPair, direction: str, cex_price: Decimal, dex_price: Decimal, edge_bps: Decimal, pair_config: PairConfig, size: Decimal) -> Opportunity:
        """輔助函數，用於創建 Opportunity 物件並計算預期 PnL。"""
        # TODO: 實現更精確的費用和 PnL 計算
        gas_cost = Decimal("5.0") # 模擬 Gas 費用
        cex_fee = cex_price * size * Decimal("0.001") # 模擬 CEX 手續費
        
        if direction == "CEX_to_DEX":
            gross_pnl = (dex_price - cex_price) * size
        else:
            gross_pnl = (cex_price - dex_price) * size
            
        net_pnl = gross_pnl - gas_cost - cex_fee

        return Opportunity(
            pair=pair,
            direction=direction,
            size=size,
            cex_price=cex_price,
            dex_price=dex_price,
            dex_chain=pair_config.dex_chain,
            dex_pool_fee=pair_config.dex_pool_fee,
            edge_bps=edge_bps,
            slippage_bps=Decimal(pair_config.max_slippage_bps),
            gas_cost_quote=gas_cost,
            cex_fee_quote=cex_fee,
            expected_pnl_quote=net_pnl,
            valid_until=asyncio.get_running_loop().time() + 2 # 機會有效期限縮短為 2 秒
        )