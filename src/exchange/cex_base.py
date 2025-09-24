from typing import Protocol, Optional
from decimal import Decimal

from ..core.types import MarketPair, Quote, CexOrder, OrderUpdate


# --- Abstract Base Class / Protocol for CEX clients ---

class CexClient(Protocol):
    """
    定義了與中心化交易所 (CEX) 互動的標準介面。
    """

    async def connect(self) -> None:
        """連接到交易所 (例如，啟動 WebSocket)。"""
        ...

    async def close(self) -> None:
        """關閉與交易所的連線。"""
        ...

    async def get_quote(self, pair: MarketPair) -> Optional[Quote]:
        """
        獲取指定交易對的當前報價 (最佳買價/賣價)。
        在一個完整的實現中，這應該由 WebSocket 數據流驅動。
        """
        ...

    async def create_order(self, order: CexOrder) -> OrderUpdate:
        """
        創建一個新訂單。
        返回一個訂單更新，其中包含訂單 ID 和初始狀態。
        """
        ...

    async def cancel_order(self, order_id: str, pair: MarketPair) -> bool:
        """
        取消一個現有訂單。
        返回 True 表示取消成功。
        """
        ...

    async def get_balance(self, asset: str) -> Decimal:
        """查詢特定資產的餘額。"""
        ...
