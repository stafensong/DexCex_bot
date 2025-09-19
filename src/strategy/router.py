from loguru import logger

from ..core.types import Opportunity

class SimpleRouter:
    def __init__(self):
        logger.info("簡易路由器初始化。")

    def plan(self, opp: Opportunity) -> Opportunity:
        """
        在新的架構中，Router 的角色被簡化。
        它可以直接傳遞 Opportunity，或者在這裡添加額外的過濾/豐富邏輯。
        為了修正導入錯誤，我們暫時讓它直接返回機會物件。
        """
        # TODO: 可在此處添加更複雜的路由邏輯，例如選擇不同的 DEX 或 CEX。
        return opp
