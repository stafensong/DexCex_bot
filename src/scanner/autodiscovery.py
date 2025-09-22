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

    @classmethod
    def from_exchange(cls, payload: Dict[str, Any]) -> "SymbolSnapshot":
        return cls(
            symbol=payload.get("symbol", ""),
            base=payload.get("baseAsset", ""),
            quote=payload.get("quoteAsset", ""),
            status=payload.get("status", ""),
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
    """Builds a map from CEX symbols to a list of corresponding DEX pools."""
    mapping = {}
    logger.info(f"建立 CEX-to-DEX map from {len(dex_pools)} DEX 池子...")
    for pool in dex_pools:
        if 'token0' not in pool or 'token1' not in pool:
            continue
        token0_sym, token1_sym = pool['token0']['symbol'], pool['token1']['symbol']
        alt_symbol = None
        if token0_sym in TARGET_QUOTE_CURRENCIES:
            alt_symbol = token1_sym
        elif token1_sym in TARGET_QUOTE_CURRENCIES:
            alt_symbol = token0_sym
        elif 'WETH' in token0_sym.upper():
            alt_symbol = token1_sym
        elif 'WETH' in token1_sym.upper():
            alt_symbol = token0_sym
        if alt_symbol:
            # Normalize alt_symbol for CEX comparison
            normalized_alt_symbol = alt_symbol.upper() # Ensure uppercase
            normalized_alt_symbol = normalized_alt_symbol.replace('WETH', 'ETH')
            normalized_alt_symbol = normalized_alt_symbol.replace('WBTC', 'BTC')
            normalized_alt_symbol = normalized_alt_symbol.replace('WRAPPED ', '') # e.g., "WRAPPED BTC" -> "BTC"
            normalized_alt_symbol = normalized_alt_symbol.replace('TOKEN', '') # e.g., "ENS TOKEN" -> "ENS"
            normalized_alt_symbol = normalized_alt_symbol.strip() # Remove any leading/trailing spaces

            for q_currency in TARGET_QUOTE_CURRENCIES:
                cex_symbol_concat = f"{normalized_alt_symbol}{q_currency}"
                if cex_symbol_concat not in mapping:
                    mapping[cex_symbol_concat] = []
                mapping[cex_symbol_concat].append(pool)
    logger.info(f"Map built. Found mappings for {len(mapping)} unique CEX symbols.")
    return mapping

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
        logger.info(f"載入 {len(self._dex_pools)} DEX 池子 from {dataset_path}")

    async def run(self):
        logger.info("開始執行 AutoDiscovery 引擎...")
        self._load_dex_pools()
        if not self._dex_pools:
            return
        self._cex_to_dex_map = build_cex_to_dex_map(self._dex_pools)
        logger.info(f"Sample CEX-to-DEX map keys: {list(self._cex_to_dex_map.keys())[:5]}")

        async with aiohttp.ClientSession() as session:
            self.cex_client = BinancePublicClient(self.config.cex.base_url, session, self.scanner_config.concurrency)
            
            spiked_symbols = await self._scan_cex_for_spikes()
            logger.info(f"異常的 CEX 交易對符號: {[s.symbol for s in spiked_symbols]}")
            opportunities = await self._evaluate_opportunities(spiked_symbols)
        self._save_opportunities(opportunities)
        logger.info("AutoDiscovery Engine run finished.")

    async def _scan_cex_for_spikes(self) -> List[VolumeSpike]:
        if not self.cex_client:
            logger.error("CEX client not initialized.")
            return []
        logger.info("掃描CEX以尋找交易量異常的交易對...")
        snapshots = await self.cex_client.fetch_exchange_info()
        trading_symbols = [s for s in snapshots if s.quote == self.scanner_config.quote_asset and s.status == "TRADING"]
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

        # --- Debugging: Analyze Symbol Overlap ---
        self._analyze_symbol_overlap(spikes)
        # --- End Debugging ---

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
                    # Dynamically build token details from the pool data
                    token0 = dex_pool['token0']
                    token1 = dex_pool['token1']
                    
                    base_token_details = token0 if token0['symbol'] == spike.base else token1
                    quote_token_details = token1 if token0['symbol'] == spike.base else token0

                    pair = MarketPair(
                        base=spike.base, 
                        quote=spike.quote, 
                        symbol=f"{spike.base}/{spike.quote}", 
                        dex_chain=dex_pool["chain"], 
                        dex_pool_fee=dex_pool["feeTier"],
                        # Pass dynamic token info
                        base_address=base_token_details['address'],
                        quote_address=quote_token_details['address'],
                        base_decimals=base_token_details.get('decimals', 18), # Default to 18 if not present
                        quote_decimals=quote_token_details.get('decimals', 18)
                    )
                    probe_size = Decimal(str(self.scanner_config.arbitrage.probe_size_base))
                    
                    # Assume we sell base for quote on DEX to compare with CEX price
                    quote = await self.dex_client.get_quote(pair, probe_size, side="sell")
                    if not quote or not quote.price > 0:
                        continue

                    dex_price = float(quote.price)
                    candidate = DexCandidate(
                        chain=dex_pool["chain"],
                        protocol=dex_pool["protocol"],
                        fee=dex_pool["feeTier"],
                        pool_address=dex_pool["poolAddress"],
                        dex_price=dex_price
                    )
                    opportunity.dex_candidates.append(candidate)
                    logger.info(f"  -> DEX Candidate on {dex_pool['chain']} ({dex_pool['feeTier']}): Price = {dex_price:.4f}")
                except Exception as e:
                    logger.warning(f"Could not evaluate DEX pool {dex_pool.get('poolAddress')}: {e}", exc_info=True)
            
            if opportunity.dex_candidates:
                opportunities.append(opportunity)

        return opportunities

    def _normalize_dex_symbol(self, symbol: str) -> str:
        normalized_symbol = symbol.upper() # Ensure uppercase
        normalized_symbol = normalized_symbol.replace('WETH', 'ETH')
        normalized_symbol = normalized_symbol.replace('WBTC', 'BTC')
        normalized_symbol = normalized_symbol.replace('WRAPPED ', '') # e.g., "WRAPPED BTC" -> "BTC"
        normalized_symbol = normalized_symbol.replace('TOKEN', '') # e.g., "ENS TOKEN" -> "ENS"
        normalized_symbol = normalized_symbol.strip() # Remove any leading/trailing spaces
        return normalized_symbol

    def _analyze_symbol_overlap(self, spikes: List[VolumeSpike]):
        spiked_base_symbols = {spike.base for spike in spikes}
        logger.info(f"異常的 CEX 交易對 base symbols: {spiked_base_symbols}")

        dex_pool_base_symbols = set()
        for pool in self._dex_pools:
            if 'token0' in pool and 'symbol' in pool['token0']:
                dex_pool_base_symbols.add(self._normalize_dex_symbol(pool['token0']['symbol']))
            if 'token1' in pool and 'symbol' in pool['token1']:
                dex_pool_base_symbols.add(self._normalize_dex_symbol(pool['token1']['symbol']))
        
        logger.info(f"正規化 DEX 池子 token symbols (sample): {list(dex_pool_base_symbols)[:10]}")

        overlap = spiked_base_symbols.intersection(dex_pool_base_symbols)
        if overlap:
            logger.success(f"找到重疊的交易量異常CEX base symbols 與 DEX pool tokens: {overlap}")
        else:
            logger.warning("找不到重疊的交易量異常CEX base symbols 與 DEX pool tokens.")

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
        logger.info(f"Saved {len(opportunities)} opportunities to {output_path}")

async def main():
    """Main execution block for running the engine standalone."""
    import logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
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
