import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from src.core.types import MarketPair, Quote, Opportunity
from src.core.config import PairConfig
from src.strategy.detector import OpportunityDetector

# 模擬的交易對設定
@pytest.fixture
def mock_pair_config() -> PairConfig:
    return PairConfig(
        base="WETH",
        quote="USDT",
        cex_symbol="ETH/USDT",
        min_edge_bps=10,
        max_slippage_bps=5,
        max_size_quote=5000,
        dex_chain="ethereum",
        dex_pool_fee=500
    )

# 模擬的 MarketPair
@pytest.fixture
def market_pair() -> MarketPair:
    return MarketPair(base="WETH", quote="USDT", symbol="ETH/USDT")

# 模擬的 CEX 和 DEX 客戶端
@pytest.fixture
def mock_clients():
    cex_client = AsyncMock()
    dex_client = AsyncMock()
    return cex_client, dex_client

@pytest.mark.asyncio
async def test_no_opportunity(mock_clients, mock_pair_config, market_pair):
    """測試沒有套利機會的情況"""
    cex_client, dex_client = mock_clients

    # 設定 CEX 報價
    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3000"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("2999.5"), ask_price=Decimal("3000.5")
    )
    # 設定 DEX 報價
    async def dex_quote_side_effect(pair, size, side):
        if side == "buy": # DEX 買價 (ask)
            return Quote(pair=pair, price=Decimal("3001"), size=size, venue="DEX", timestamp=0)
        else: # DEX 賣價 (bid)
            return Quote(pair=pair, price=Decimal("2999"), size=size, venue="DEX", timestamp=0)
    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = await detector.detect()

    assert len(opportunities) == 0

@pytest.mark.asyncio
async def test_cex_to_dex_opportunity(mock_clients, mock_pair_config, market_pair):
    """測試 CEX 買 -> DEX 賣的機會"""
    cex_client, dex_client = mock_clients

    # CEX 價格便宜，DEX 價格貴
    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3000"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("2999.5"), ask_price=Decimal("3000.5")
    )
    async def dex_quote_side_effect(pair, size, side):
        if side == "buy":
            return Quote(pair=pair, price=Decimal("3005"), size=size, venue="DEX", timestamp=0)
        else:
            return Quote(pair=pair, price=Decimal("3004"), size=size, venue="DEX", timestamp=0)
    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = await detector.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.direction == "CEX_to_DEX"
    assert opp.cex_price == Decimal("3000.5") # CEX ask price
    assert opp.dex_price == Decimal("3004")   # DEX bid price
    assert opp.edge_bps > mock_pair_config.min_edge_bps

async def test_dex_to_cex_opportunity(mock_clients, mock_pair_config, market_pair):
    """測試 DEX 買 -> CEX 賣的機會"""
    cex_client, dex_client = mock_clients

    # DEX 價格便宜，CEX 價格貴
    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3010"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("3009.5"), ask_price=Decimal("3010.5")
    )
    async def dex_quote_side_effect(pair, size, side):
        if side == "buy":
            return Quote(pair=pair, price=Decimal("3001"), size=size, venue="DEX", timestamp=0)
        else:
            return Quote(pair=pair, price=Decimal("3000"), size=size, venue="DEX", timestamp=0)
    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = await detector.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.direction == "DEX_to_CEX"
    assert opp.cex_price == Decimal("3009.5") # CEX bid price
    assert opp.dex_price == Decimal("3001")   # DEX ask price
    assert opp.edge_bps > mock_pair_config.min_edge_bps

async def test_opportunity_below_threshold(mock_clients, mock_pair_config, market_pair):
    """測試價差小於最小門檻的情況"""
    cex_client, dex_client = mock_clients

    # 價差存在，但很小
    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3000"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("2999.9"), ask_price=Decimal("3000.1")
    )
    async def dex_quote_side_effect(pair, size, side):
        if side == "buy":
            return Quote(pair=pair, price=Decimal("3000.2"), size=size, venue="DEX", timestamp=0)
        else:
            return Quote(pair=pair, price=Decimal("3000"), size=size, venue="DEX", timestamp=0)
    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = await detector.detect()

    assert len(opportunities) == 0
