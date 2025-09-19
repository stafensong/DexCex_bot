import asyncio
import time
from decimal import Decimal
from typing import Optional, Tuple, List

from loguru import logger

from ..core.types import Opportunity, CexOrder, OrderUpdate, DexSwapParams, DexTxReceipt, ExecutionSummary, ExecutionLeg
from ..core.errors import HedgingError
from ..exchange.cex_base import CexClient
from ..exchange.dex_base import DexClient
from ..risk.limits import RiskManager
from ..infra import metrics

class TransactionExecutor:
    def __init__(self, cex_client: CexClient, dex_client: DexClient, risk_manager: RiskManager):
        self.cex_client = cex_client
        self.dex_client = dex_client
        self.risk_manager = risk_manager
        logger.info("交易執行器初始化。")

    async def run(self, opp: Opportunity) -> ExecutionSummary:
        """
        核心功能：接收一個機會，原子化地執行它，並在失敗時對沖。
        """
        start_ts = time.time()
        legs: List[ExecutionLeg] = []
        cex_task = None
        dex_task = None
        
        try:
            logger.info(f"開始執行機會: {opp.direction} {opp.size} {opp.pair.symbol}")

            # 建立 CEX 和 DEX 的執行任務
            if opp.direction == "CEX_to_DEX": # Buy CEX, Sell DEX
                cex_task = asyncio.create_task(self._execute_cex_leg(opp, "buy"))
                dex_task = asyncio.create_task(self._execute_dex_leg(opp, "sell"))
            else: # DEX_to_CEX: Buy DEX, Sell CEX
                dex_task = asyncio.create_task(self._execute_dex_leg(opp, "buy"))
                cex_task = asyncio.create_task(self._execute_cex_leg(opp, "sell"))

            results = await asyncio.gather(cex_task, dex_task, return_exceptions=True)
            
            successful_legs = []
            failed_legs = []
            for res in results:
                if isinstance(res, ExecutionLeg):
                    successful_legs.append(res)
                else:
                    # 異常被 gather 捕獲
                    logger.error(f"交易腿執行異常: {res}")
                    failed_legs.append(res)
            
            legs.extend(successful_legs)

            if failed_legs:
                logger.warning(f"{len(failed_legs)} 個交易腿失敗，需要對沖。")
                if successful_legs:
                    hedge_leg = await self._handle_partial_failure(successful_legs[0])
                    legs.append(hedge_leg)
                raise PartialFailureError(f"{len(failed_legs)} legs failed")

            # --- 如果兩邊都成功 ---
            logger.success("交易計畫兩邊均成功執行！")
            pnl, fees = self._calculate_pnl(legs)
            summary = self._build_summary(opp, legs, pnl, fees, hedged=False, start_ts=start_ts)
            metrics.trades_executed.labels(pair=opp.pair.symbol, direction=opp.direction, status="success").inc()
            metrics.pnl_quote.labels(pair=opp.pair.symbol).inc(pnl)
            return summary

        except Exception as e:
            logger.error(f"執行或對沖過程中發生錯誤: {e}")
            pnl, fees = self._calculate_pnl(legs)
            summary = self._build_summary(opp, legs, pnl, fees, hedged=False, start_ts=start_ts)
            metrics.trades_executed.labels(pair=opp.pair.symbol, direction=opp.direction, status="failed").inc()
            return summary

    async def _execute_cex_leg(self, opp: Opportunity, side: str) -> ExecutionLeg:
        order = CexOrder(pair=opp.pair, side=side, type="MARKET", size=opp.size, price=opp.cex_price, order_id="", ts=0)
        try:
            update = await self.cex_client.create_order(order)
            if update.status == "FILLED":
                fee = update.avg_fill_price * update.filled_size * Decimal("0.001") # 模擬費用
                return ExecutionLeg(
                    venue="CEX", side=side, price_quote=update.avg_fill_price,
                    size=update.filled_size, fees_quote=fee, order_id=update.order_id
                )
            else:
                raise Exception(f"CEX 訂單狀態非 FILLED: {update.status}")
        except Exception as e:
            metrics.failed_leg.labels(pair=opp.pair.symbol, venue="CEX", reason="execution_error").inc()
            logger.error(f"CEX 交易腿 ({side}) 失敗: {e}")
            raise

    async def _execute_dex_leg(self, opp: Opportunity, side: str) -> ExecutionLeg:
        # 這裡的實作需要根據新的 DexSwapParams 進行大幅調整
        # 為了讓程式碼能跑通，我們暫時使用一個簡化的模擬
        logger.warning("DEX 執行邏輯尚未完全適配新的 DexSwapParams，使用模擬執行。")
        try:
            # 模擬一個成功的交易
            price = opp.dex_price
            fee = price * opp.size * Decimal("0.003") # 模擬 DEX fee
            return ExecutionLeg(
                venue="DEX", side=side, price_quote=price, size=opp.size, 
                fees_quote=fee, tx_hash=f"0x_sim_{time.time()}"
            )
        except Exception as e:
            metrics.failed_leg.labels(pair=opp.pair.symbol, venue="DEX", reason="execution_error").inc()
            logger.error(f"DEX 交易腿 ({side}) 失敗: {e}")
            raise

    async def _handle_partial_failure(self, leg_to_hedge: ExecutionLeg) -> ExecutionLeg:
        logger.warning(f"在 {leg_to_hedge.venue} 的交易成功，但另一邊失敗。開始執行對沖...")
        hedge_side = "sell" if leg_to_hedge.side == "buy" else "buy"
        
        try:
            if leg_to_hedge.venue == "CEX":
                hedge_order = CexOrder(pair=self.risk_manager.positions, side=hedge_side, type="MARKET", size=leg_to_hedge.size, price=Decimal(0), order_id="", ts=0)
                update = await self.cex_client.create_order(hedge_order)
                fee = update.avg_fill_price * update.filled_size * Decimal("0.001")
                hedge_leg = ExecutionLeg(venue="CEX", side=hedge_side, price_quote=update.avg_fill_price, size=update.filled_size, fees_quote=fee, order_id=f"hedge_{update.order_id}")
            else: # DEX hedge
                # 模擬對沖
                hedge_leg = ExecutionLeg(venue="DEX", side=hedge_side, price_quote=leg_to_hedge.price_quote, size=leg_to_hedge.size, fees_quote=leg_to_hedge.fees_quote, tx_hash=f"0x_hedge_sim_{time.time()}")

            logger.success(f"成功在 {hedge_leg.venue} 對沖風險部位。")
            metrics.hedged_leg.labels(pair=opp.pair.symbol, venue=hedge_leg.venue).inc()
            return hedge_leg
        except Exception as e:
            logger.critical(f"！！！對沖失敗！！！風險部位暴露！: {e}", exc_info=True)
            metrics.failed_leg.labels(pair=opp.pair.symbol, venue=leg_to_hedge.venue, reason="hedge_failed").inc()
            raise HedgingError("對沖失敗", None, None)

    def _calculate_pnl(self, legs: List[ExecutionLeg]) -> Tuple[Decimal, Decimal]:
        if len(legs) < 2:
            return Decimal(0), sum(leg.fees_quote for leg in legs)

        buy_leg = next((leg for leg in legs if leg.side == 'buy'), None)
        sell_leg = next((leg for leg in legs if leg.side == 'sell'), None)

        if not (buy_leg and sell_leg):
            # 這是對沖後的情況，PnL 計算會更複雜
            total_fees = sum(leg.fees_quote for leg in legs)
            return -total_fees, total_fees

        revenue = sell_leg.price_quote * sell_leg.size
        cost = buy_leg.price_quote * buy_leg.size
        total_fees = sum(leg.fees_quote for leg in legs)
        net_pnl = revenue - cost - total_fees
        return net_pnl, total_fees

    def _build_summary(self, opp: Opportunity, legs: List[ExecutionLeg], pnl: Decimal, fees: Decimal, hedged: bool, start_ts: float) -> ExecutionSummary:
        return ExecutionSummary(
            pair=opp.pair,
            direction=opp.direction,
            size=opp.size,
            legs=legs,
            gas_quote=opp.gas_cost_quote, # 應從DEX腿的執行結果中獲取
            pnl_quote=pnl,
            edge_bps=opp.edge_bps,
            hedged=hedged,
            started_ts=start_ts,
            completed_ts=time.time()
        )

class PartialFailureError(Exception):
    pass
