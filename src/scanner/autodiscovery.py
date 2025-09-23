import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Any

import aiohttp
from loguru import logger

# Import clients and config objects
from src.core.config import AppConfig, load_config
from src.core.types import MarketPair
from src.exchange.univ3 import UniV3DexClient

# --- Constants ---
TARGET_QUOTE_CURRENCIES = ["USDT", "USDC"]

# --- CEX Data Structures ---

@dataclass
class SymbolSnapshot:
    symbol: str
    base: str
    quote: str
    status: str
    permissions: List[str]

    @classmethod
    def from_exchange(cls, payload: Dict[str, Any]) -> "SymbolSnapshot":
        return cls(
            symbol=payload.get("symbol", ""),
            base=payload.get("baseAsset", ""),
            quote=payload.get("quoteAsset", ""),
            status=payload.get("status", ""),
            permissions=payload.get("permissions", []),
        )

@dataclass
class VolumeSpike:
    symbol: str
    base: str
    quote: str
    ratio: float

# --- DEX & Arbitrage Data Structures ---

@dataclass
class DexCandidate:
    chain: str
    protocol: str
    fee: int
    pool_address: str
    dex_price: float = 0.0
    raw_pool_data: Dict[str, Any] = field(default_factory=dict)

@dataclass
class ArbitrageOpportunity:
    symbol: str
    base: str
    quote: str
    ratio: float
    cex_price: float
    dex_candidates: List[DexCandidate] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base": self.base,
            "quote": self.quote,
            "ratio": self.ratio,
            "cex_price": self.cex_price,
            "dex_candidates": [
                {
                    "chain": cand.chain,
                    "protocol": cand.protocol,
                    "fee": cand.fee,
                    "pool": cand.pool_address,
                    "dex_price": cand.dex_price,
                    "raw_pool_data": cand.raw_pool_data,
                }
                for cand in self.dex_candidates
            ],
        }

# --- CEX Client ---

class BinancePublicClient:
    def __init__(self, base_url: str, session: aiohttp.ClientSession, concurrency: int = 20):
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.semaphore = asyncio.Semaphore(concurrency)

    async def fetch_exchange_info(self) -> List[SymbolSnapshot]:
        url = f"{self.base_url}/api/v3/exchangeInfo"
        try:
            async with self.session.get(url, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
            return [SymbolSnapshot.from_exchange(item) for item in data.get("symbols", [])]
        except Exception as e:
            logger.error(f"Failed to fetch exchange info: {e}")
            return []

    async def fetch_hourly_klines(self, symbol: str, limit: int = 2) -> List[List[Any]]:
        params = {"symbol": symbol, "interval": "1h", "limit": limit}
        try:
            async with self.semaphore:
                async with self.session.get(f"{self.base_url}/api/v3/klines", params=params, timeout=10) as response:
                    if response.status != 200:
                        logger.warning(f"Failed to get klines for {symbol} [{response.status}]")
                        return []
                    return await response.json()
        except Exception as e:
            logger.error(f"Error fetching klines for {symbol}: {e}")
            return []

    async def fetch_ticker_price(self, symbol: str) -> Optional[float]:
        params = {"symbol": symbol}
        try:
            async with self.session.get(f"{self.base_url}/api/v3/ticker/price", params=params, timeout=10) as response:
                if response.status != 200:
                    logger.warning(f"Failed to get ticker price for {symbol} [{response.status}]")
                    return None
                data = await response.json()
                return float(data.get("price"))
        except Exception as e:
            logger.error(f"Error fetching ticker price for {symbol}: {e}")
            return None

# --- Core Logic ---

def build_cex_to_dex_map(dex_pools: list) -> dict:
    """
    Builds a map from CEX symbols to a list of corresponding DEX pools.
    Handles both direct (TOKEN/USDT) and synthetic (TOKEN/WETH) pairs.
    """
    mapping = {}
    logger.info(f"建立 CEX-to-DEX map from {len(dex_pools)} DEX 池子...")
    for pool in dex_pools:
        if 'token0' not in pool or 'token1' not in pool:
            continue

        token0_sym = _normalize_dex_symbol(pool['token0']['symbol'])
        token1_sym = _normalize_dex_symbol(pool['token1']['symbol'])

        # --- Determine Pair Type ---
        alt_symbol = None
        quote_symbol = None
        intermediate_symbol = None

        is_t0_quote = token0_sym in TARGET_QUOTE_CURRENCIES
        is_t1_quote = token1_sym in TARGET_QUOTE_CURRENCIES
        is_t0_weth = token0_sym == 'ETH'
        is_t1_weth = token1_sym == 'ETH'

        if is_t0_quote and not (is_t1_quote or is_t1_weth):
            alt_symbol = token1_sym
            quote_symbol = token0_sym
        elif is_t1_quote and not (is_t0_quote or is_t0_weth):
            alt_symbol = token0_sym
            quote_symbol = token1_sym
        elif is_t0_weth and not (is_t1_quote or is_t1_weth):
            alt_symbol = token1_sym
            intermediate_symbol = 'ETH'
        elif is_t1_weth and not (is_t0_quote or is_t0_weth):
            alt_symbol = token0_sym
            intermediate_symbol = 'ETH'
        else:
            continue # Skip stable/stable, weth/stable, etc.

        if alt_symbol:
            pool_info = pool.copy() # Create a mutable copy
            
            if intermediate_symbol:
                # This is a synthetic pair (e.g., GNO/WETH)
                # We create mappings for both USDT and USDC
                pool_info['is_synthetic'] = True
                pool_info['intermediate_symbol'] = intermediate_symbol
                for q_currency in TARGET_QUOTE_CURRENCIES:
                    cex_symbol = f"{alt_symbol}{q_currency}"
                    if cex_symbol not in mapping:
                        mapping[cex_symbol] = []
                    mapping[cex_symbol].append(pool_info)
            else:
                # This is a direct pair (e.g., GNO/USDT)
                pool_info['is_synthetic'] = False
                cex_symbol = f"{alt_symbol}{quote_symbol}"
                if cex_symbol not in mapping:
                    mapping[cex_symbol] = []
                mapping[cex_symbol].append(pool_info)

    logger.info(f"Map built. Found mappings for {len(mapping)} unique CEX symbols.")
    return mapping

def _normalize_dex_symbol(symbol: str) -> str:
    normalized_symbol = symbol.upper()
    # Handle stablecoin variants like "USDC.e"
    if normalized_symbol.startswith("USDC"):
        return "USDC"
    if normalized_symbol.startswith("USDT"):
        return "USDT"
    
    normalized_symbol = normalized_symbol.replace('WETH', 'ETH')
    normalized_symbol = normalized_symbol.replace('WBTC', 'BTC')
    normalized_symbol = normalized_symbol.replace('WRAPPED ', '')
    normalized_symbol = normalized_symbol.replace('TOKEN', '')
    normalized_symbol = normalized_symbol.strip()
    return normalized_symbol

class AutoDiscoveryEngine:
    def __init__(self, config: AppConfig):
        self.config = config
        self.scanner_config = config.scanner.auto_discovery
        self._dex_pools: List[Dict] = []
        self._cex_to_dex_map: Dict[str, List[Dict]] = {}
        self.cex_client: Optional[BinancePublicClient] = None
        self.dex_client = UniV3DexClient(config.dex, config.network, config.secrets, config.tokens)

    def _load_dex_pools(self):
        dataset_path = Path(self.scanner_config.dex_pool_dataset)
        if not dataset_path.exists():
            logger.error(f"DEX pool dataset not found at: {dataset_path}")
            return
        with dataset_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            self._dex_pools = data.get("pools", [])
        logger.info(f"Loaded {len(self._dex_pools)} DEX pools from {dataset_path}")

    async def run(self):
        logger.info("開始執行 AutoDiscovery 引擎...")
        self._load_dex_pools()
        if not self._dex_pools:
            return
        self._cex_to_dex_map = build_cex_to_dex_map(self._dex_pools)
        logger.info(f"Sample CEX-to-DEX map keys (只顯示前五個作為範例): {list(self._cex_to_dex_map.keys())[:5]}")

        async with aiohttp.ClientSession() as session:
            self.cex_client = BinancePublicClient(self.config.cex.base_url, session, self.scanner_config.concurrency)
            
            spiked_symbols = await self._scan_cex_for_spikes()
            logger.info(f"異常的 CEX 交易對符號: {[s.symbol for s in spiked_symbols]}")
            opportunities = await self._evaluate_opportunities(spiked_symbols)
        self._save_opportunities(opportunities)
        logger.info("AutoDiscovery 引擎結束執行.")

    async def _scan_cex_for_spikes(self) -> List[VolumeSpike]:
        if not self.cex_client:
            logger.error("CEX client not initialized.")
            return []
        logger.info("掃描CEX以尋找交易量異常的交易對...")
        snapshots = await self.cex_client.fetch_exchange_info()
        
        # --- Comprehensive Debugging ---
        logger.debug(f"Fetched {len(snapshots)} total symbols from exchange info.")
        if snapshots:
            logger.debug(f"Sample snapshot data: {snapshots[0]}")
        # --- End Debugging ---

        # Broaden the filter to include all target quote currencies
        target_quotes = self.scanner_config.target_quote_currencies
        logger.debug(f"Filtering with target_quotes: {target_quotes}")

        # Corrected Filtering Logic: Exclude non-spot pairs instead of including spot pairs
        FORBIDDEN_PERMISSIONS = {"MARGIN", "LEVERAGED", "TRD_GRP_002"} # Add other groups if needed

        trading_symbols = [
            s for s in snapshots 
            if s.quote in target_quotes
            and s.status == "TRADING"
            and not any(p in FORBIDDEN_PERMISSIONS for p in s.permissions)
        ]
        logger.debug(f"Found {len(trading_symbols)} SPOT trading symbols with target quote assets.")
        
        tasks = [self._fetch_and_compute_spike(snap) for snap in trading_symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        spikes: List[VolumeSpike] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Error computing volume spike: {result}")
                continue
            if result:
                spikes.append(result)
        spikes.sort(key=lambda s: s.ratio, reverse=True)
        top_spikes = spikes[:self.scanner_config.max_candidates]
        logger.info(f"CEX掃描完成. 找到 {len(top_spikes)} 候選池子.")
        return top_spikes

    async def _fetch_and_compute_spike(self, snapshot: SymbolSnapshot) -> Optional[VolumeSpike]:
        if not self.cex_client:
            return None
        klines = await self.cex_client.fetch_hourly_klines(snapshot.symbol, limit=self.scanner_config.lookback_hours)
        if len(klines) < 2:
            return None
        try:
            current_volume = float(klines[-1][5])
            previous_volume = float(klines[-2][5])
        except (TypeError, ValueError, IndexError):
            return None
        if previous_volume <= 0:
            return None
        ratio = current_volume / previous_volume
        if ratio <= self.scanner_config.ratio_threshold:
            return None
        return VolumeSpike(symbol=snapshot.symbol, base=snapshot.base, quote=snapshot.quote, ratio=ratio)

    async def _evaluate_opportunities(self, spikes: List[VolumeSpike]) -> List[ArbitrageOpportunity]:
        if not self.cex_client:
            logger.error("CEX client not initialized for evaluation.")
            return []
        logger.info(f"評估 {len(spikes)} CEX異常交易對的套利機會...")

        opportunities: List[ArbitrageOpportunity] = []
        for spike in spikes:
            if spike.symbol not in self._cex_to_dex_map:
                logger.debug(f"DEX mapping 找不到 CEX 的交易對符號 {spike.symbol}")
                continue

            cex_price = await self.cex_client.fetch_ticker_price(spike.symbol)
            if not cex_price:
                continue

            logger.success(f"在 DEX map 中找到 {spike.symbol} ! CEX 價格: {cex_price}")
            opportunity = ArbitrageOpportunity(symbol=spike.symbol, base=spike.base, quote=spike.quote, ratio=spike.ratio, cex_price=cex_price)

            dex_candidates = self._cex_to_dex_map[spike.symbol]
            for dex_pool in dex_candidates:
                try:
                    dex_price = None
                    base_token_details = None
                    quote_token_details = None

                    token0 = dex_pool['token0']
                    token1 = dex_pool['token1']

                    if dex_pool.get('is_synthetic'):
                        # --- Handle Synthetic Pair (e.g., GNO/WETH) ---
                        intermediate_symbol = dex_pool['intermediate_symbol']
                        
                        # 1. Identify base and intermediate tokens
                        if _normalize_dex_symbol(token0['symbol']) == intermediate_symbol:
                            intermediate_token_details = token0
                            base_token_details = token1
                        else:
                            intermediate_token_details = token1
                            base_token_details = token0
                        
                        # 2. Get CEX price for the intermediate pair (e.g., ETH/USDT)
                        intermediate_cex_symbol = f"{intermediate_symbol}{spike.quote}"
                        intermediate_price = await self.cex_client.fetch_ticker_price(intermediate_cex_symbol)
                        if not intermediate_price:
                            logger.warning(f"Could not fetch CEX price for intermediate {intermediate_cex_symbol}")
                            continue
                        
                        # 3. Get DEX price for the base/intermediate pair (e.g., GNO/WETH)
                        pair_dex = MarketPair(
                            base=_normalize_dex_symbol(base_token_details['symbol']),
                            quote_cex=spike.quote, # Final quote is USDT/USDC
                            quote_dex=intermediate_symbol, # On-chain quote is WETH
                            cex_symbol=f"{base_token_details['symbol']}/{intermediate_symbol}",
                            dex_chain=dex_pool["chain"],
                            dex_pool_fee=dex_pool["feeTier"],
                            base_address=base_token_details['address'],
                            quote_address=intermediate_token_details['address'],
                            base_decimals=base_token_details.get('decimals', 18),
                            quote_decimals=intermediate_token_details.get('decimals', 18)
                        )
                        probe_size = Decimal(str(self.scanner_config.arbitrage.probe_size_base))
                        quote_dex = await self.dex_client.get_quote(pair_dex, probe_size, side="sell")
                        if not quote_dex or not quote_dex.price > 0:
                            continue
                        
                        # 4. Calculate final synthetic price
                        dex_price = float(quote_dex.price * Decimal(str(intermediate_price)))

                    else:
                        # --- Handle Direct Pair (e.g., GNO/USDT) ---
                        if _normalize_dex_symbol(token0['symbol']) in TARGET_QUOTE_CURRENCIES:
                            quote_token_details = token0
                            base_token_details = token1
                        else:
                            quote_token_details = token1
                            base_token_details = token0

                        pair_direct = MarketPair(
                            base=_normalize_dex_symbol(base_token_details['symbol']), 
                            quote_cex=_normalize_dex_symbol(quote_token_details['symbol']), 
                            quote_dex=_normalize_dex_symbol(quote_token_details['symbol']), # Direct pair, so they are the same
                            cex_symbol=f"{spike.base}/{spike.quote}", 
                            dex_chain=dex_pool["chain"], 
                            dex_pool_fee=dex_pool["feeTier"],
                            base_address=base_token_details['address'],
                            quote_address=quote_token_details['address'],
                            base_decimals=base_token_details.get('decimals', 18),
                            quote_decimals=quote_token_details.get('decimals', 18)
                        )
                        probe_size = Decimal(str(self.scanner_config.arbitrage.probe_size_base))
                        quote_direct = await self.dex_client.get_quote(pair_direct, probe_size, side="sell")
                        if not quote_direct or not quote_direct.price > 0:
                            continue
                        dex_price = float(quote_direct.price)

                    # --- Add candidate if price was found ---
                    if dex_price is not None:
                        candidate = DexCandidate(
                            chain=dex_pool["chain"],
                            protocol=dex_pool["protocol"],
                            fee=dex_pool["feeTier"],
                            pool_address=dex_pool["poolAddress"],
                            dex_price=dex_price,
                            raw_pool_data=dex_pool,
                        )
                        opportunity.dex_candidates.append(candidate)
                        logger.info(f"  -> DEX Candidate on {dex_pool['chain']} ({dex_pool['feeTier']}): Price = {dex_price:.4f} (Synthetic: {dex_pool.get('is_synthetic', False)})")

                except Exception as e:
                    logger.warning(f"Could not evaluate DEX pool {dex_pool.get('poolAddress')}: {e}", exc_info=True)
            
            if opportunity.dex_candidates:
                opportunities.append(opportunity)

        return opportunities

    def _save_opportunities(self, opportunities: List[ArbitrageOpportunity]):
        output_path = Path(self.scanner_config.persist_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_data = {
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
            "opportunity_count": len(opportunities),
            "opportunities": [opp.to_dict() for opp in opportunities],
        }
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(output_data, f, indent=2)
        logger.info(f"儲存 {len(opportunities)} 套利機會到 {output_path}")


async def main():
    """Main execution block for running the engine standalone."""
    import logging
    import sys
    # Add a sink for stdout to show loguru logs
    logger.remove()
    logger.add(sys.stdout, level="INFO")

    config_path = Path('config/default.yaml')
    if not config_path.exists():
        logger.error(f"Configuration file not found at {config_path}. Please ensure it exists.")
        return

    try:
        config = load_config(config_path)
        engine = AutoDiscoveryEngine(config)
        await engine.run()

    except Exception as e:
        logger.error(f"An error occurred during engine execution: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
