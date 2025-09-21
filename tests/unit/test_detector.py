import asyncio
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock

from src.core.types import MarketPair, Quote
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
        dex_pool_fee=500,
        edge_safety_multiplier=Decimal("1.2"),
    )

# 模擬的 MarketPair
@pytest.fixture
def market_pair() -> MarketPair:
    return MarketPair(
        base="WETH",
        quote="USDT",
        symbol="ETH/USDT",
        dex_chain="ethereum",
        dex_pool_fee=500,
    )

# 模擬的 CEX 和 DEX 客戶端
@pytest.fixture
def mock_clients():
    cex_client = AsyncMock()
    dex_client = AsyncMock()
    return cex_client, dex_client

def test_no_opportunity(mock_clients, mock_pair_config, market_pair):
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
    opportunities = asyncio.run(detector.detect())

    assert len(opportunities) == 0

def test_cex_to_dex_opportunity(mock_clients, mock_pair_config, market_pair):
    """測試 CEX 買 -> DEX 賣的機會"""
    cex_client, dex_client = mock_clients

    # CEX 價格便宜，DEX 價格貴
    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3000"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("2999.5"), ask_price=Decimal("3000.5")
    )
    async def dex_quote_side_effect(pair, size, side):
        if side == "buy":
            return Quote(pair=pair, price=Decimal("3007"), size=size, venue="DEX", timestamp=0)
        else:
            return Quote(pair=pair, price=Decimal("3008"), size=size, venue="DEX", timestamp=0)
    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = asyncio.run(detector.detect())

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.direction == "CEX_to_DEX"
    assert opp.cex_price == Decimal("3000.5") # CEX ask price
    assert opp.dex_price == Decimal("3008")   # DEX bid price
    assert opp.edge_bps > Decimal(mock_pair_config.min_edge_bps)
    assert opp.cex_fee_quote == Decimal("0.2250375")
    assert opp.expected_pnl_quote == Decimal("0.37475")

def test_dex_to_cex_opportunity(mock_clients, mock_pair_config, market_pair):
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
    opportunities = asyncio.run(detector.detect())

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.direction == "DEX_to_CEX"
    assert opp.cex_price == Decimal("3009.5") # CEX bid price
    assert opp.dex_price == Decimal("3001")   # DEX ask price
    assert opp.edge_bps > Decimal(mock_pair_config.min_edge_bps)
    assert opp.cex_fee_quote == Decimal("0.2257125")
    assert opp.expected_pnl_quote == Decimal("0.474025")

def test_opportunity_below_threshold(mock_clients, mock_pair_config, market_pair):
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
    opportunities = asyncio.run(detector.detect())

    assert len(opportunities) == 0


def test_opportunity_filtered_by_dynamic_threshold(mock_clients, mock_pair_config, market_pair):
    """edge 介於靜態與動態門檻之間時應被過濾"""
    cex_client, dex_client = mock_clients

    cex_client.get_quote.return_value = Quote(
        pair=market_pair, price=Decimal("3000"), size=Decimal(10),
        venue="CEX", timestamp=0, bid_price=Decimal("2999.5"), ask_price=Decimal("3000.5")
    )

    async def dex_quote_side_effect(pair, size, side):
        if side == "buy":
            return Quote(pair=pair, price=Decimal("3001.0"), size=size, venue="DEX", timestamp=0)
        else:
            return Quote(pair=pair, price=Decimal("3003.1"), size=size, venue="DEX", timestamp=0)

    dex_client.get_quote.side_effect = dex_quote_side_effect

    detector = OpportunityDetector(cex_client, dex_client, [mock_pair_config])
    opportunities = asyncio.run(detector.detect())

    # edge 約 13 bps，高於靜態門檻 (10 bps) 但低於動態門檻 (~15 bps)
    assert len(opportunities) == 0
