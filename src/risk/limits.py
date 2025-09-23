import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from loguru import logger

from src.core.config import RiskConfig
from src.core.types import Opportunity, ExecutionSummary

STATE_FILE = Path("data/risk_state.json")

def get_current_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.positions: dict = {}
        self.daily_pnl: Decimal = Decimal("0")
        self._load_state()
        logger.info(f"風險管理器初始化，今日 PnL: {self.daily_pnl:.4f}")

    def _load_state(self):
        if not STATE_FILE.exists():
            logger.warning("找不到風險狀態檔案，將以全新狀態啟動。")
            return
        try:
            with STATE_FILE.open("r") as f:
                state = json.load(f)
            
            if state.get("date") == get_current_date_str():
                self.daily_pnl = Decimal(str(state.get("daily_pnl", "0")))
                logger.info(f"成功載入今日風險狀態。")
            else:
                logger.info("偵測到為新的一天，重置每日 PnL。")
                self.daily_pnl = Decimal("0")

            self.positions = state.get("positions", {})

        except (json.JSONDecodeError, TypeError) as e:
            logger.error(f"讀取風險狀態檔案時發生錯誤: {e}，將以全新狀態啟動。")
            self.daily_pnl = Decimal("0")
            self.positions = {}

    def _save_state(self):
        current_state = {
            "date": get_current_date_str(),
            "daily_pnl": float(self.daily_pnl),
            "positions": self.positions,
        }
        try:
            STATE_FILE.parent.mkdir(exist_ok=True)
            with STATE_FILE.open("w") as f:
                json.dump(current_state, f, indent=2)
        except Exception as e:
            logger.error(f"儲存風險狀態時發生錯誤: {e}")

    def is_trade_allowed(self, opp: Opportunity) -> bool:
        notional_value = opp.cex_price * opp.size
        if notional_value > self.config.max_notional_per_leg_quote:
            logger.warning(f"交易被拒: 超出單筆名義額度限制 ({notional_value:.2f} > {self.config.max_notional_per_leg_quote:.2f})。")
            return False
        return True

    def update_state(self, summary: ExecutionSummary):
        if not summary.legs:
            return

        if summary.pnl_quote is not None:
            self.daily_pnl += summary.pnl_quote
            logger.info(f"每日 PnL 更新: {self.daily_pnl:.4f}")
        
        self._save_state()