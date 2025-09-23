
import asyncio
import json
from pathlib import Path
from typing import Dict, List, Any
import aiohttp
from loguru import logger

# --- Constants ---
BINANCE_API_URL = "https://api.binance.com/api/v3/exchangeInfo"
CG_API_BASE_URL = "https://api.coingecko.com/api/v3"
MASTER_LIST_PATH = Path("data/master_token_list.json")
TARGET_QUOTE_ASSETS = {"USDT", "USDC", "BUSD", "TUSD"}

# --- Main Builder Class ---

class MasterTokenListBuilder:
    def __init__(self):
        self.session: aiohttp.ClientSession
        self.binance_symbols: set[str] = set()
        self.cg_coin_list: List[Dict[str, str]] = []
        self.master_token_list: Dict[str, Any] = {}

    async def run(self):
        logger.info("開始建立權威代幣地址清單...")
        MASTER_LIST_PATH.parent.mkdir(exist_ok=True)

        async with aiohttp.ClientSession() as self.session:
            await self._get_binance_spot_symbols()
            await self._get_coingecko_coin_list()
            await self._build_master_list()
        
        self._save_master_list()
        logger.success(f"權威代幣清單建立完畢，已儲存至 {MASTER_LIST_PATH}")

    async def _get_binance_spot_symbols(self):
        logger.info("從幣安獲取所有現貨交易的基礎代幣...")
        try:
            response = await self.session.get(BINANCE_API_URL)
            response.raise_for_status()
            data = await response.json()
            
            FORBIDDEN_PERMISSIONS = {"MARGIN", "LEVERAGED", "TRD_GRP_002", "TRD_GRP_003"} # Add more if needed

            for symbol_data in data.get("symbols", []):
                permissions = set(symbol_data.get("permissions", []))
                is_trading = symbol_data.get("status") == "TRADING"
                is_target_quote = symbol_data.get("quoteAsset") in TARGET_QUOTE_ASSETS

                # A symbol is considered a valid spot symbol if it's trading and has no forbidden permissions.
                if is_trading and is_target_quote and not permissions.intersection(FORBIDDEN_PERMISSIONS):
                    self.binance_symbols.add(symbol_data.get("baseAsset"))

            logger.info(f"從幣安找到 {len(self.binance_symbols)} 個獨立的基礎代幣。")

        except Exception as e:
            logger.error(f"從幣安獲取交易對資訊時發生錯誤: {e}")

    async def _get_coingecko_coin_list(self):
        logger.info("從 CoinGecko 獲取完整的代幣列表...")
        try:
            response = await self.session.get(f"{CG_API_BASE_URL}/coins/list")
            response.raise_for_status()
            self.cg_coin_list = await response.json()
            logger.info(f"從 CoinGecko 找到 {len(self.cg_coin_list)} 個代幣。")
        except Exception as e:
            logger.error(f"從 CoinGecko 獲取代幣列表時發生錯誤: {e}")

    async def _build_master_list(self):
        logger.info("開始遍歷幣安代幣，並從 CoinGecko 獲取詳細地址...")
        tasks = []
        for symbol_str in self.binance_symbols:
            if not symbol_str: continue
            
            # Find the corresponding CoinGecko ID
            cg_id = None
            for coin in self.cg_coin_list:
                if coin.get("symbol", "").upper() == symbol_str.upper():
                    cg_id = coin.get("id")
                    break
            
            if cg_id:
                tasks.append(self._get_coin_details(cg_id, symbol_str))
            else:
                logger.warning(f"在 CoinGecko 列表中找不到代幣 {symbol_str} 的 ID。")

        results = await asyncio.gather(*tasks)
        for result in results:
            if result:
                symbol, data = result
                self.master_token_list[symbol] = data

    async def _get_coin_details(self, cg_id: str, symbol: str) -> tuple[str, Dict] | None:
        logger.debug(f"正在獲取 {symbol} (ID: {cg_id}) 的詳細資訊...")
        url = f"{CG_API_BASE_URL}/coins/{cg_id}"
        try:
            # Add a small delay to avoid hitting rate limits
            await asyncio.sleep(5) # CoinGecko has a rate limit of 30 calls/minute
            
            response = await self.session.get(url)
            if response.status == 429:
                logger.error("達到 CoinGecko API 速率限制，請稍後再試。")
                return None
            response.raise_for_status()
            data = await response.json()

            addresses = {}
            if "platforms" in data:
                for platform, address in data["platforms"].items():
                    if address: # Ensure address is not an empty string
                        addresses[platform] = address
            
            if not addresses:
                logger.debug(f"代幣 {symbol} 沒有找到任何鏈上地址。")
                return None

            logger.success(f"成功獲取 {symbol} 的地址資訊。")
            return symbol, {
                "coingecko_id": cg_id,
                "name": data.get("name"),
                "addresses": addresses
            }
        except Exception as e:
            logger.error(f"獲取 {symbol} (ID: {cg_id}) 的詳細資訊時發生錯誤: {e}")
            return None

    def _save_master_list(self):
        with open(MASTER_LIST_PATH, "w", encoding="utf-8") as f:
            json.dump(self.master_token_list, f, indent=2)

# --- Main Execution ---

async def main():
    builder = MasterTokenListBuilder()
    await builder.run()

if __name__ == "__main__":
    # Setup logger for standalone execution
    import sys
    logger.remove()
    logger.add(sys.stdout, level="INFO")
    
    asyncio.run(main())
