import asyncio
import pytest
from decimal import Decimal

from src.core.config import PairConfig
from src.core.types import MarketPair, Opportunity
from src.strategy.executor import TransactionExecutor


@pytest.fixture
def pair_config() -> PairConfig:
    return PairConfig(
        base="WETH",
        quote="USDT",
        cex_symbol="ETH/USDT",
        min_edge_bps=10,
        max_slippage_bps=5,
        max_size_quote=5000,
        dex_chain="ethereum",
        dex_pool_fee=500,
        price_floor_quote=Decimal("100"),
        price_ceiling_quote=Decimal("10000"),
        max_edge_bps=50000,
    )


@pytest.fixture
def market_pair(pair_config: PairConfig) -> MarketPair:
    return MarketPair(
        base=pair_config.base,
        quote=pair_config.quote,
        symbol=pair_config.cex_symbol,
        dex_chain=pair_config.dex_chain,
        dex_pool_fee=pair_config.dex_pool_fee,
    )


def make_opportunity(
    market_pair: MarketPair,
    direction: str,
    cex_price: Decimal,
    dex_price: Decimal,
    size: Decimal = Decimal("0.1"),
    gas_cost: Decimal = Decimal("10"),
    cex_fee: Decimal = Decimal("5"),
) -> Opportunity:
    return Opportunity(
        pair=market_pair,
        direction=direction,
        size=size,
        cex_price=cex_price,
        dex_price=dex_price,
        dex_chain=market_pair.dex_chain,
        dex_pool_fee=market_pair.dex_pool_fee,
        edge_bps=Decimal("0"),
        slippage_bps=Decimal("5"),
        gas_cost_quote=gas_cost,
        cex_fee_quote=cex_fee,
        expected_pnl_quote=Decimal("0"),
        valid_until=0.0,
    )


def test_dex_to_cex_legs(pair_config: PairConfig, market_pair: MarketPair):
    executor = TransactionExecutor(None, None, None, [pair_config])
    opp = make_opportunity(
        market_pair,
        direction="DEX_to_CEX",
        cex_price=Decimal("4500"),
        dex_price=Decimal("4000"),
    )

    summary = asyncio.run(executor.run(opp))

    assert [leg.venue for leg in summary.legs] == ["DEX", "CEX"]
    assert [leg.side for leg in summary.legs] == ["buy", "sell"]
    assert summary.pnl_quote == Decimal("35")
    assert summary.edge_bps == Decimal("1250")
    assert summary.edge_bps > 0


def test_cex_to_dex_legs(pair_config: PairConfig, market_pair: MarketPair):
    executor = TransactionExecutor(None, None, None, [pair_config])
    opp = make_opportunity(
        market_pair,
        direction="CEX_to_DEX",
        cex_price=Decimal("4000"),
        dex_price=Decimal("4500"),
    )

    summary = asyncio.run(executor.run(opp))

    assert [leg.venue for leg in summary.legs] == ["CEX", "DEX"]
    assert [leg.side for leg in summary.legs] == ["buy", "sell"]
    assert summary.pnl_quote == Decimal("35")
    assert summary.edge_bps == Decimal("1250")
    assert summary.edge_bps > 0


def test_invalid_price_rejected(pair_config: PairConfig, market_pair: MarketPair):
    executor = TransactionExecutor(None, None, None, [pair_config])
    opp = make_opportunity(
        market_pair,
        direction="DEX_to_CEX",
        cex_price=Decimal("4500"),
        dex_price=Decimal("0.0002"),
    )

    summary = asyncio.run(executor.run(opp))

    assert summary.legs == []
    assert summary.pnl_quote == Decimal("0")
    assert summary.edge_bps == Decimal("0")
