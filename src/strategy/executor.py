import time
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from typing import Dict, List, Optional, Tuple

from loguru import logger

from ..core.config import PairConfig
from ..core.types import Opportunity, ExecutionSummary, ExecutionLeg
from ..exchange.cex_base import CexClient
from ..exchange.dex_base import DexClient
from ..infra import metrics
from ..risk.limits import RiskManager


@dataclass(frozen=True)
class SanityThresholds:
    price_floor: Decimal
    price_ceiling: Decimal
    max_edge_bps: Decimal


DEFAULT_SANITY_THRESHOLDS = SanityThresholds(
    price_floor=Decimal("0"),
    price_ceiling=Decimal("1000000"),
    max_edge_bps=Decimal("50000"),
)
TEN_THOUSAND = Decimal("10000")
ZERO = Decimal("0")


def build_threshold_map(pair_configs: Optional[List[PairConfig]]) -> Dict[str, SanityThresholds]:
    if not pair_configs:
        return {}

    thresholds: Dict[str, SanityThresholds] = {}
    for cfg in pair_configs:
        price_floor = cfg.price_floor_quote if cfg.price_floor_quote is not None else DEFAULT_SANITY_THRESHOLDS.price_floor
        price_ceiling = cfg.price_ceiling_quote if cfg.price_ceiling_quote is not None else DEFAULT_SANITY_THRESHOLDS.price_ceiling
        max_edge = Decimal(cfg.max_edge_bps) if cfg.max_edge_bps is not None else DEFAULT_SANITY_THRESHOLDS.max_edge_bps
        thresholds[cfg.cex_symbol] = SanityThresholds(price_floor=price_floor, price_ceiling=price_ceiling, max_edge_bps=max_edge)
    return thresholds


def determine_leg_prices(opp: Opportunity) -> Tuple[Decimal, Decimal]:
    if opp.direction == "DEX_to_CEX":
        return opp.dex_price, opp.cex_price
    return opp.cex_price, opp.dex_price


def build_legs(opp: Opportunity) -> List[ExecutionLeg]:
    if opp.direction == "DEX_to_CEX":
        dex_leg = ExecutionLeg(
            venue="DEX",
            side="buy",
            price_quote=opp.dex_price,
            size=opp.size,
            fees_quote=ZERO,
        )
        cex_leg = ExecutionLeg(
            venue="CEX",
            side="sell",
            price_quote=opp.cex_price,
            size=opp.size,
            fees_quote=opp.cex_fee_quote,
        )
        return [dex_leg, cex_leg]

    cex_leg = ExecutionLeg(
        venue="CEX",
        side="buy",
        price_quote=opp.cex_price,
        size=opp.size,
        fees_quote=opp.cex_fee_quote,
    )
    dex_leg = ExecutionLeg(
        venue="DEX",
        side="sell",
        price_quote=opp.dex_price,
        size=opp.size,
        fees_quote=ZERO,
    )
    return [cex_leg, dex_leg]


def compute_edge(buy_price: Decimal, sell_price: Decimal) -> Decimal:
    if buy_price <= ZERO:
        raise DivisionByZero("買入價格必須為正數才能計算 edge。")
    return (sell_price / buy_price - Decimal(1)) * TEN_THOUSAND


def compute_pnl(opp: Opportunity, buy_price: Decimal, sell_price: Decimal) -> Decimal:
    gross = opp.size * (sell_price - buy_price)
    mid_price = (sell_price + buy_price) / Decimal(2)
    slippage_cost = (mid_price * opp.size * opp.slippage_bps) / TEN_THOUSAND
    return gross - opp.cex_fee_quote - slippage_cost - opp.gas_cost_quote


def evaluate_opportunity(
    opp: Opportunity,
    thresholds: SanityThresholds,
) -> Tuple[List[ExecutionLeg], Optional[Decimal], Optional[Decimal], Optional[str]]:
    buy_price, sell_price = determine_leg_prices(opp)

    if opp.cex_price <= ZERO:
        return [], None, None, "cex_price_non_positive"
    if opp.dex_price <= ZERO:
        return [], None, None, "dex_price_non_positive"

    if opp.cex_price < thresholds.price_floor or opp.cex_price > thresholds.price_ceiling:
        return [], None, None, "cex_price_out_of_range"
    if opp.dex_price < thresholds.price_floor or opp.dex_price > thresholds.price_ceiling:
        return [], None, None, "dex_price_out_of_range"

    if buy_price <= ZERO:
        return [], None, None, "buy_price_non_positive"
    if sell_price <= ZERO:
        return [], None, None, "sell_price_non_positive"

    try:
        edge_bps = compute_edge(buy_price, sell_price)
    except (DivisionByZero, InvalidOperation):
        return [], None, None, "edge_computation_error"

    if abs(edge_bps) > thresholds.max_edge_bps:
        return [], None, edge_bps, "edge_beyond_threshold"

    pnl_quote = compute_pnl(opp, buy_price, sell_price)
    legs = build_legs(opp)
    return legs, pnl_quote, edge_bps, None


class TransactionExecutor:
    """
    交易執行器會將 `Opportunity` 轉換成兩條標準化交易腿，並計算預期損益與價差。
    - `DEX_to_CEX`: 先在 DEX `buy` base，再在 CEX `sell` base。
    - `CEX_to_DEX`: 先在 CEX `buy` base，再在 DEX `sell` base。
    `ExecutionLeg.price_quote` 皆為「報價幣 / base」價格，`ExecutionLeg.size` 始終是 base 數量。
    PnL 計算: `size * (sell_px - buy_px) - cex_fee_quote - slippage_cost_quote - gas_cost_quote`。
    模組同時套用可配置的價格／edge sanity check，以避免異常資料污染計算。
    """

    def __init__(
        self,
        cex_client: Optional[CexClient],
        dex_client: Optional[DexClient],
        risk_manager: Optional[RiskManager],
        pair_configs: Optional[List[PairConfig]] = None,
    ):
        self.cex_client = cex_client
        self.dex_client = dex_client
        self.risk_manager = risk_manager
        self._thresholds = build_threshold_map(pair_configs)

    async def run(self, opp: Opportunity) -> ExecutionSummary:
        """根據輸入的 `Opportunity` 計算 legs、PnL 與 edge。"""
        start_ts = time.time()
        thresholds = self._thresholds.get(opp.pair.symbol, DEFAULT_SANITY_THRESHOLDS)

        legs, pnl_quote, edge_bps, error = evaluate_opportunity(opp, thresholds)
        if error:
            logger.warning(
                "機會未通過 sanity check: pair=%s direction=%s reason=%s",
                opp.pair.symbol,
                opp.direction,
                error,
            )
            metrics.trades_executed.labels(
                pair=opp.pair.symbol,
                direction=opp.direction,
                status="invalid",
            ).inc()
            return self._build_invalid_summary(opp, start_ts)

        summary = ExecutionSummary(
            pair=opp.pair,
            direction=opp.direction,
            size=opp.size,
            legs=legs,
            gas_quote=opp.gas_cost_quote,
            pnl_quote=pnl_quote,
            edge_bps=edge_bps,
            hedged=False,
            started_ts=start_ts,
            completed_ts=time.time(),
        )

        metrics.trades_executed.labels(
            pair=opp.pair.symbol,
            direction=opp.direction,
            status="success",
        ).inc()
        metrics.pnl_quote.labels(pair=opp.pair.symbol).inc(float(pnl_quote))
        return summary

    def _build_invalid_summary(self, opp: Opportunity, start_ts: float) -> ExecutionSummary:
        return ExecutionSummary(
            pair=opp.pair,
            direction=opp.direction,
            size=opp.size,
            legs=[],
            gas_quote=ZERO,
            pnl_quote=ZERO,
            edge_bps=ZERO,
            hedged=False,
            started_ts=start_ts,
            completed_ts=time.time(),
        )


class PaperExecutor:
    """
    一個僅記錄日誌而不執行真實交易的執行器。
    與 `TransactionExecutor` 相同地產生 legs 與 PnL，並套用 sanity check。
    """

    def __init__(self, pair_configs: Optional[List[PairConfig]] = None):
        logger.info("紙上交易執行器初始化。將只記錄機會，不執行交易。")
        self._thresholds = build_threshold_map(pair_configs)

    async def run(self, opp: Opportunity) -> ExecutionSummary:
        start_ts = time.time()
        thresholds = self._thresholds.get(opp.pair.symbol, DEFAULT_SANITY_THRESHOLDS)

        legs, pnl_quote, edge_bps, error = evaluate_opportunity(opp, thresholds)
        if error:
            logger.warning(
                "[PAPER MODE] 機會被拒絕: pair=%s direction=%s reason=%s",
                opp.pair.symbol,
                opp.direction,
                error,
            )
            return ExecutionSummary(
                pair=opp.pair,
                direction=opp.direction,
                size=opp.size,
                legs=[],
                gas_quote=ZERO,
                pnl_quote=ZERO,
                edge_bps=ZERO,
                hedged=False,
                started_ts=start_ts,
                completed_ts=time.time(),
            )

        logger.info(
            "[PAPER MODE] 偵測到機會: {} {} | CEX={:.4f} | DEX={:.4f} | PnL={:.4f}",
            opp.direction,
            opp.pair.symbol,
            float(opp.cex_price),
            float(opp.dex_price),
            float(pnl_quote),
        )
        return ExecutionSummary(
            pair=opp.pair,
            direction=opp.direction,
            size=opp.size,
            legs=legs,
            gas_quote=opp.gas_cost_quote,
            pnl_quote=pnl_quote,
            edge_bps=edge_bps,
            hedged=False,
            started_ts=start_ts,
            completed_ts=time.time(),
        )
