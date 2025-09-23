from typing import List, Optional, Tuple, Dict
from decimal import Decimal
import asyncio
import time

from loguru import logger

from src.core.types import MarketPair, Opportunity, CexQuote
from src.exchange.cex_base import CexClient
from src.exchange.dex_base import DexClient
from src.infra.metrics import opportunities_found

# --- Constants ---
TEN_THOUSAND = Decimal(10000)
ZERO = Decimal(0)
CEX_TAKER_FEE_BPS = Decimal(7.5) # Example: 0.075%
DEFAULT_TRADE_SIZE = Decimal("0.1") # Example trade size
DEFAULT_SAFETY_MULTIPLIER = Decimal("1.2")

class OpportunityDetector:
    def __init__(self, cex_client: CexClient, dex_client: DexClient, pairs: List[MarketPair]):
        self.cex_client = cex_client
        self.dex_client = dex_client
        self.pairs = pairs
        self._intermediate_price_cache: Dict[str, Tuple[Tuple[Decimal, Decimal], float]] = {}
        logger.info(f"機會偵測器初始化，監控 {len(pairs)} 個交易對。")

    async def detect(self) -> List[Opportunity]:
        """
        掃描所有設定的交易對，尋找套利機會。
        """
        tasks = [self.check_pair(p) for p in self.pairs]
        results = await asyncio.gather(*tasks)
        
        opportunities = [opp for opp in results if opp]
        return opportunities

    async def check_pair(self, pair: MarketPair) -> Optional[Opportunity]:
        """
        Checks a single pair for arbitrage opportunities, handling both direct and synthetic pairs.
        """
        if pair.is_synthetic:
            return await self._check_synthetic_pair(pair)
        else:
            return await self._check_direct_pair(pair)

    async def _check_direct_pair(self, pair: MarketPair) -> Optional[Opportunity]:
        """Handles direct TOKEN/QUOTE pairs."""
        cex_quote = await self.cex_client.get_quote(pair)
        if not (cex_quote and cex_quote.bid_price and cex_quote.ask_price):
            return None

        trade_size = DEFAULT_TRADE_SIZE
        dex_buy_task = self.dex_client.get_quote(pair, size=trade_size, side="buy")
        dex_sell_task = self.dex_client.get_quote(pair, size=trade_size, side="sell")
        dex_buy_quote, dex_sell_quote = await asyncio.gather(dex_buy_task, dex_sell_task)

        if not (dex_buy_quote and dex_sell_quote):
            return None

        return self._evaluate_prices(pair, cex_quote.ask_price, cex_quote.bid_price, dex_buy_quote.price, dex_sell_quote.price)

    async def _get_cached_intermediate_price(self, pair_config: MarketPair) -> Optional[Tuple[Decimal, Decimal]]:
        """Gets the intermediate pair's CEX price, with a 2-second cache."""
        now = time.time()
        record = self._intermediate_price_cache.get(pair_config.cex_symbol)
        if record:
            (cached_ask, cached_bid), ts = record
            if now - ts < 2:
                return (cached_ask, cached_bid)

        quote = await self.cex_client.get_quote(pair_config)
        if not (quote and quote.ask_price and quote.bid_price):
            return None

        prices = (quote.ask_price, quote.bid_price)
        self._intermediate_price_cache[pair_config.cex_symbol] = (prices, now)
        return prices

    async def _check_synthetic_pair(self, pair: MarketPair) -> Optional[Opportunity]:
        """Handles synthetic TOKEN/QUOTE pairs (via an intermediate like WETH)."""
        intermediate_symbol = pair.intermediate_symbol
        if not intermediate_symbol: return None

        intermediate_cex_pair_symbol = f"{intermediate_symbol}/{pair.quote}"
        intermediate_cex_pair = next((p for p in self.pairs if p.cex_symbol == intermediate_cex_pair_symbol), None)
        if not intermediate_cex_pair:
            logger.warning(f"Cannot check synthetic pair {pair.cex_symbol}: Intermediate pair {intermediate_cex_pair_symbol} not configured.")
            return None

        # --- Step 1: Fetch prices specific to the pair's chain ---
        tasks = {
            "cex_target": self.cex_client.get_quote(pair),
            "dex_sell": self.dex_client.get_quote(pair, size=DEFAULT_TRADE_SIZE, side="sell"),
            "dex_buy": self.dex_client.get_quote(pair, size=DEFAULT_TRADE_SIZE, side="buy"),
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        cex_target_q, dex_sell_q, dex_buy_q = results

        if any(isinstance(r, Exception) for r in results) or not all([cex_target_q, dex_sell_q, dex_buy_q]):
            logger.debug(f"Could not fetch chain-specific quotes for synthetic pair {pair.cex_symbol}")
            return None

        # --- Step 2: Fetch the shared intermediate CEX price (with caching) ---
        intermediate_prices = await self._get_cached_intermediate_price(intermediate_cex_pair)
        if not intermediate_prices:
            logger.warning(f"Could not fetch CEX price for intermediate pair {intermediate_cex_pair.cex_symbol}")
            return None
        cex_intermediate_ask, cex_intermediate_bid = intermediate_prices

        # --- Step 3: Sanity check prices and calculate synthetic prices ---
        if dex_sell_q.price <= 0 or dex_buy_q.price <= 0 or cex_intermediate_ask <= 0 or cex_intermediate_bid <= 0:
            logger.debug(f"Invalid quotes for synthetic pair {pair.cex_symbol}")
            return None

        synthetic_dex_sell_price = dex_sell_q.price * cex_intermediate_bid
        synthetic_dex_buy_price  = dex_buy_q.price  * cex_intermediate_ask

        # --- Step 4: Check for abnormal edge to prevent pollution ---
        def _bounded_bps(n: Decimal) -> bool:
            return abs(n) <= Decimal(100_000)  # 1000% edge limit

        edge_1 = (synthetic_dex_sell_price - cex_target_q.ask_price) / cex_target_q.ask_price * TEN_THOUSAND
        edge_2 = (cex_target_q.bid_price - synthetic_dex_buy_price) / synthetic_dex_buy_price * TEN_THOUSAND

        if not (_bounded_bps(edge_1) and _bounded_bps(edge_2)):
            logger.warning(f"Abnormal edge for {pair.cex_symbol}: edge1={edge_1:.2f}, edge2={edge_2:.2f}. Skip.")
            return None

        return self._evaluate_prices(
            pair, 
            cex_target_q.ask_price, cex_target_q.bid_price, 
            synthetic_dex_buy_price, synthetic_dex_buy_price
        )

    def _evaluate_prices(
        self, 
        pair: MarketPair, 
        cex_ask_price: Decimal, 
        cex_bid_price: Decimal, 
        dex_buy_price: Decimal, 
        dex_sell_price: Decimal
    ) -> Optional[Opportunity]:
        """Shared logic to evaluate prices and create an opportunity."""
        # Sanity check all incoming prices
        for v_name, v in [("cex_ask_price", cex_ask_price), ("cex_bid_price", cex_bid_price),
                          ("dex_buy_price", dex_buy_price), ("dex_sell_price", dex_sell_price)]:
            if v is None or v <= 0:
                logger.debug(f"Invalid {v_name} for {pair.cex_symbol} in evaluation")
                return None

        slippage_bps = Decimal(pair.max_slippage_bps or 10)
        safety_multiplier = pair.edge_safety_multiplier if pair.edge_safety_multiplier is not None else DEFAULT_SAFETY_MULTIPLIER
        static_threshold = Decimal(pair.min_edge_bps or 0)
        dynamic_min_edge = (CEX_TAKER_FEE_BPS + slippage_bps) * safety_multiplier
        effective_min_edge = max(static_threshold, dynamic_min_edge)

        # Direction 1: CEX_to_DEX
        edge_bps_1 = (dex_sell_price - cex_ask_price) / cex_ask_price * TEN_THOUSAND
        if edge_bps_1 >= effective_min_edge:
            opp = self._create_opportunity(
                pair=pair, direction="CEX_to_DEX", cex_price=cex_ask_price,
                dex_price=dex_sell_price, edge_bps=edge_bps_1,
                size=DEFAULT_TRADE_SIZE, slippage_bps=slippage_bps
            )
            opportunities_found.labels(pair=pair.cex_symbol, direction="CEX_to_DEX").inc()
            return opp

        # Direction 2: DEX_to_CEX
        edge_bps_2 = (cex_bid_price - dex_buy_price) / dex_buy_price * TEN_THOUSAND
        if edge_bps_2 >= effective_min_edge:
            opp = self._create_opportunity(
                pair=pair, direction="DEX_to_CEX", cex_price=cex_bid_price,
                dex_price=dex_buy_price, edge_bps=edge_bps_2,
                size=DEFAULT_TRADE_SIZE, slippage_bps=slippage_bps
            )
            opportunities_found.labels(pair=pair.cex_symbol, direction="DEX_to_CEX").inc()
            return opp
            
        return None

    def _create_opportunity(
        self,
        pair: MarketPair,
        direction: str,
        cex_price: Decimal,
        dex_price: Decimal,
        edge_bps: Decimal,
        size: Decimal,
        slippage_bps: Decimal,
    ) -> Opportunity:
        """輔助函數，用於創建 Opportunity 物件並計算預期 PnL。"""

        gas_cost = ZERO
        cex_fee_quote = (cex_price * size * CEX_TAKER_FEE_BPS) / TEN_THOUSAND

        if direction == "CEX_to_DEX":
            buy_price = cex_price
            sell_price = dex_price
        else:
            buy_price = dex_price
            sell_price = cex_price

        gross_pnl = (sell_price - buy_price) * size
        mid_price = (sell_price + buy_price) / Decimal(2)
        slippage_cost = (mid_price * size * slippage_bps) / TEN_THOUSAND
        net_pnl = gross_pnl - cex_fee_quote - slippage_cost - gas_cost

        return Opportunity(
            pair=pair,
            direction=direction,
            size=size,
            cex_price=cex_price,
            dex_price=dex_price,
            dex_chain=pair.dex_chain,
            dex_pool_fee=pair.dex_pool_fee,
            edge_bps=edge_bps,
            slippage_bps=slippage_bps,
            gas_cost_quote=gas_cost,
            cex_fee_quote=cex_fee_quote,
            expected_pnl_quote=net_pnl,
            valid_until=asyncio.get_running_loop().time() + 2 # 機會有效期限縮短為 2 秒
        )