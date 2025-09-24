from abc import ABC, abstractmethod
from typing import Protocol, Optional
from decimal import Decimal

from ..core.types import MarketPair, Quote, DexSwapParams, DexTxReceipt

# --- Abstract Base Class / Protocol for DEX clients ---

class DexClient(Protocol):
    """
    定義了與去中心化交易所 (DEX) 互動的標準介面。
    """

    async def get_quote(self, pair: MarketPair, size: Decimal, side: str) -> Optional[Quote]:
        """
        獲取在特定池子中交易一定數量的代幣可以得到的報價。
        這通常會呼叫 DEX 的 Quoter 合約。
        """
        ...

    async def execute_swap(self, params: DexSwapParams) -> DexTxReceipt:
        """
        執行一個交換操作。
        這會構建、簽署並發送一個交易到區塊鏈。
        """
        ...

    async def estimate_gas(self, params: DexSwapParams) -> int:
        """
        估算執行一個交換操作所需的 Gas 成本。
        """
        ...

    async def get_balance(self, asset: str, chain: str) -> Decimal:
        """查詢特定代幣在特定鏈上的錢包餘額。"""
        ...
