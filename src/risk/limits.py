from loguru import logger
from ..core.config import RiskConfig
from ..core.types import Opportunity, ExecutionSummary
from ..infra.storage import load_state, save_state

class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        # 從檔案載入初始狀態
        initial_state = load_state()
        self.positions = initial_state.get("positions", {}) # e.g., {"ETH": 0.5}
        self.daily_pnl = initial_state.get("daily_pnl", 0.0)
        logger.info(f"風險管理器初始化，當前部位: {self.positions}, 今日 PnL: {self.daily_pnl}")

    def is_trade_allowed(self, opp: Opportunity) -> bool:
        """
        在執行前檢查交易是否符合風險限制。
        """
        # 1. 檢查單筆名義額度
        notional_value = opp.cex_price * opp.size
        if notional_value > self.config.max_notional_per_leg_quote:
            logger.warning(f"交易被拒: 超出單筆名義額度限制 ({notional_value:.2f} > {self.config.max_notional_per_leg_quote:.2f})。")
            return False

        # TODO: 增加更詳細的部位檢查
        return True

    def update_state(self, summary: ExecutionSummary):
        """
        根據交易執行結果更新風險狀態，並將其持久化。
        """
        if summary.pnl_quote is not None:
            self.daily_pnl += summary.pnl_quote
            logger.info(f"每日 PnL 更新: {self.daily_pnl:.4f}")

        # TODO: 根據 summary.legs 中的具體成交情況，更新詳細的 CEX/DEX 部位
        
        # 將更新後的狀態保存到檔案
        current_state = {
            "positions": self.positions,
            "daily_pnl": self.daily_pnl
        }
        save_state(current_state)
