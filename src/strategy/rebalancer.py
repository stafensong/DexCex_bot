import asyncio
from decimal import Decimal
from loguru import logger

from src.core.config import AppConfig
from src.core.types import CexOrder, MarketPair
from src.exchange.cex_base import CexClient

class Rebalancer:
    def __init__(self, config: AppConfig, cex_client: CexClient):
        self.config = config.inventory.rebalance
        self.cex_client = cex_client
        self.pairs_config = {p.cex_symbol: p for p in config.pairs}
        logger.info("資產再平衡器初始化。")

    async def run_rebalance_check(self, paper_run: bool = False):
        if not self.config.enable:
            logger.info("資產再平衡功能未啟用。")
            return

        logger.info("開始檢查 CEX 資產庫存比例...")
        
        if not self.pairs_config:
            logger.warning("沒有設定任何交易對，無法執行再平衡。")
            return
        
        pair_config = list(self.pairs_config.values())[0]
        base_asset = pair_config.base
        quote_asset = pair_config.quote_cex # Use the CEX quote symbol
        market_pair = MarketPair(
            base=base_asset,
            quote_cex=quote_asset,
            quote_dex=quote_asset, # Rebalancing happens on CEX, so they are the same
            cex_symbol=pair_config.cex_symbol,
            dex_chain=pair_config.dex_chain,
            dex_pool_fee=pair_config.dex_pool_fee,
            base_precision=pair_config.base_precision if pair_config.base_precision is not None else 8,
            quote_precision=pair_config.quote_precision if pair_config.quote_precision is not None else 8,
        )

        # 1. 獲取餘額和價格
        base_balance, quote_balance, quote = await asyncio.gather(
            self.cex_client.get_balance(base_asset),
            self.cex_client.get_balance(quote_asset),
            self.cex_client.get_quote(market_pair)
        )

        if base_balance < 0 or quote_balance < 0 or not quote:
            logger.error("無法獲取完整的餘額或價格資訊，跳過此次再平衡。")
            return

        # 2. 計算當前比例
        base_value = base_balance * quote.price
        total_value = base_value + quote_balance
        if total_value == 0:
            logger.warning("總資產價值為零，無法計算比例。")
            return
        
        current_ratio = base_value / total_value
        target_ratio = Decimal(str(self.config.target_ratio))
        trigger_threshold = Decimal(str(self.config.trigger_bps)) / 10000

        logger.info(f"資產: {base_asset}, 當前比例: {current_ratio:.2%},目標比例: {target_ratio:.2%}")

        # 3. 判斷是否需要再平衡
        if abs(current_ratio - target_ratio) > trigger_threshold:
            logger.warning(f"資產比例偏離過大，觸發再平衡！")
            # 4. 計算需要交易的數量
            target_base_value = total_value * target_ratio
            value_to_trade = target_base_value - base_value
            amount_to_trade = abs(value_to_trade) / quote.price
            side = "buy" if value_to_trade > 0 else "sell"

            logger.info(f"計畫在 CEX 以市價單 {side} {amount_to_trade:.6f} {base_asset}")

            if paper_run:
                logger.warning("Paper Run 模式啟用，跳過實際交易。")
                return

            # 5. 執行交易
            try:
                order = CexOrder(pair=market_pair, side=side, type="MARKET", size=amount_to_trade, price=Decimal(0), order_id="", ts=0)
                update = await self.cex_client.create_order(order)
                if update.status == "FILLED":
                    logger.success("再平衡交易成功。")
                else:
                    logger.error(f"再平衡交易失敗，訂單狀態: {update.status}")
            except Exception as e:
                logger.error(f"執行再平衡交易時發生錯誤: {e}")
        else:
            logger.info("資產比例在正常範圍內，無需再平衡。")
