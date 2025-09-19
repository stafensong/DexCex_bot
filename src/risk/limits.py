from loguru import logger
from ..core.config import RiskConfig
from ..core.types import Opportunity, ExecutionSummary

class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        # 在此處初始化部位、PnL 等狀態
        self.positions = {} # e.g., {"ETH": 0.5}
        self.daily_pnl = 0.0

    def is_trade_allowed(self, opp: Opportunity) -> bool:
        """
        在執行前檢查交易是否符合風險限制。
        """
        # 1. 檢查單筆名義額度
        # 使用機會中的價格和大小來估算名義價值
        notional_value = opp.cex_price * opp.size
        if notional_value > self.config.max_notional_per_leg_quote:
            logger.warning(f"交易被拒: 超出單筆名義額度限制 ({notional_value:.2f} > {self.config.max_notional_per_leg_quote:.2f})。")
            return False

        # 2. 檢查執行後的預計部位
        base_asset = opp.pair.base
        current_position = self.positions.get(base_asset, 0)
        
        # 這裡是一個簡化的部位檢查，完整的系統需要區分 CEX 和 DEX 的庫存
        # 假設套利後總部位不變，所以暫不檢查
        # if abs(current_position) > self.config.max_position_per_asset:
        #      logger.warning(f"交易被拒: 超出最大部位限制。")
        #      return False

        return True

    def update_state(self, summary: ExecutionSummary):
        """
        根據交易執行結果更新風險狀態。
        """
        # 使用新的 ExecutionSummary 中的 pnl_quote 欄位
        if summary.pnl_quote is not None:
            self.daily_pnl += summary.pnl_quote
            logger.info(f"每日 PnL 更新: {self.daily_pnl:.4f}")

        # TODO: 根據 summary.legs 中的具體成交情況，更新詳細的 CEX/DEX 部位
