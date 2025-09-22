from typing import List, Optional
from decimal import Decimal
import asyncio

from loguru import logger

from ..core.config import PairConfig
from ..core.types import Opportunity, MarketPair
from ..exchange.cex_base import CexClient
from ..exchange.dex_base import DexClient
from ..infra import metrics

CEX_TAKER_FEE_BPS = Decimal("7.5")
DEFAULT_SAFETY_MULTIPLIER = Decimal("1.2")
DEFAULT_TRADE_SIZE = Decimal("0.1")
TEN_THOUSAND = Decimal("10000")
ZERO = Decimal("0")

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
            cex_symbol=pair_config.cex_symbol,
            dex_chain=pair_config.dex_chain,
            dex_pool_fee=pair_config.dex_pool_fee,
            base_precision=pair_config.base_precision,
            quote_precision=pair_config.quote_precision,
        )
        
        # 從 CEX 本地訂單簿獲取報價
        cex_quote = await self.cex_client.get_quote(pair)
        if not (cex_quote and cex_quote.bid_price and cex_quote.ask_price):
            return None

        # 假設我們想交易 1 個 base asset (此處應有更智能的數量決定邏輯)
        trade_size = DEFAULT_TRADE_SIZE
        
        # 併發獲取 DEX 報價
        dex_buy_task = self.dex_client.get_quote(pair, size=trade_size, side="buy")
        dex_sell_task = self.dex_client.get_quote(pair, size=trade_size, side="sell")
        dex_buy_quote, dex_sell_quote = await asyncio.gather(dex_buy_task, dex_sell_task)

        if not (dex_buy_quote and dex_sell_quote):
            return None

        cex_bid_price = Decimal(cex_quote.bid_price)
        cex_ask_price = Decimal(cex_quote.ask_price)
        dex_buy_price = Decimal(dex_buy_quote.price)
        dex_sell_price = Decimal(dex_sell_quote.price)

        slippage_bps = Decimal(pair_config.max_slippage_bps)
        safety_multiplier = Decimal(pair_config.edge_safety_multiplier) if pair_config.edge_safety_multiplier is not None else DEFAULT_SAFETY_MULTIPLIER
        static_threshold = Decimal(pair_config.min_edge_bps)
        dynamic_min_edge = (CEX_TAKER_FEE_BPS + slippage_bps) * safety_multiplier
        effective_min_edge = max(static_threshold, dynamic_min_edge)

        # --- 計算兩個方向的機會 ---

        # 方向 1: 在 CEX 買, 在 DEX 賣 (CEX_to_DEX)
        edge_bps_1 = (dex_sell_price - cex_ask_price) / cex_ask_price * TEN_THOUSAND

        if edge_bps_1 >= effective_min_edge:
            opp = self._create_opportunity(
                pair=pair, direction="CEX_to_DEX", cex_price=cex_ask_price,
                dex_price=dex_sell_price, edge_bps=edge_bps_1, pair_config=pair_config,
                size=trade_size, slippage_bps=slippage_bps
            )
            metrics.opportunities_found.labels(pair=pair.cex_symbol, direction="CEX_to_DEX").inc()
            return opp

        # 方向 2: 在 DEX 買, 在 CEX 賣 (DEX_to_CEX)
        edge_bps_2 = (cex_bid_price - dex_buy_price) / dex_buy_price * TEN_THOUSAND

        if edge_bps_2 >= effective_min_edge:
            opp = self._create_opportunity(
                pair=pair, direction="DEX_to_CEX", cex_price=cex_bid_price,
                dex_price=dex_buy_price, edge_bps=edge_bps_2, pair_config=pair_config,
                size=trade_size, slippage_bps=slippage_bps
            )
            metrics.opportunities_found.labels(pair=pair.cex_symbol, direction="DEX_to_CEX").inc()
            return opp

        return None

    def _create_opportunity(
        self,
        pair: MarketPair,
        direction: str,
        cex_price: Decimal,
        dex_price: Decimal,
        edge_bps: Decimal,
        pair_config: PairConfig,
        size: Decimal,
        slippage_bps: Decimal,
    ) -> Opportunity:
        """輔助函數，用於創建 Opportunity 物件並計算預期 PnL。"""

        gas_cost = ZERO
        cex_fee_quote = (cex_price * size * CEX_TAKER_FEE_BPS) / TEN_THOUSAND

        if direction == "CEX_to_DEX":
            buy_price = cex_price
            sell_price = dex_price
        else:
            buy_price = dex_price
            sell_price = cex_price

        gross_pnl = (sell_price - buy_price) * size
        mid_price = (sell_price + buy_price) / Decimal(2)
        slippage_cost = (mid_price * size * slippage_bps) / TEN_THOUSAND
        net_pnl = gross_pnl - cex_fee_quote - slippage_cost - gas_cost

        return Opportunity(
            pair=pair,
            direction=direction,
            size=size,
            cex_price=cex_price,
            dex_price=dex_price,
            dex_chain=pair.dex_chain,
            dex_pool_fee=pair.dex_pool_fee,
            edge_bps=edge_bps,
            slippage_bps=slippage_bps,
            gas_cost_quote=gas_cost,
            cex_fee_quote=cex_fee_quote,
            expected_pnl_quote=net_pnl,
            valid_until=asyncio.get_running_loop().time() + 2 # 機會有效期限縮短為 2 秒
        )
