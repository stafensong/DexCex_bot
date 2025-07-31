import ccxt.async_support as ccxt
import os
from dotenv import load_dotenv

class CexTrader:
    """
    A class to interact with Binance for trading, designed for the arbitrage bot.
    It uses the async version of ccxt.
    """
    def __init__(self, api_key: str, api_secret: str, sandbox_mode: bool = False):
        """
        Initializes the CexTrader.

        Args:
            api_key (str): Binance API key.
            api_secret (str): Binance API secret.
            sandbox_mode (bool): Whether to use the sandbox (testnet) environment.
        """
        if not api_key or not api_secret:
            raise ValueError("Binance API Key or Secret not provided.")

        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
            },
        })
        
        # Set sandbox mode if needed
        self.exchange.set_sandbox_mode(sandbox_mode)
        print(f"[Info] CexTrader initialized. Sandbox mode: {sandbox_mode}")

    async def get_market_info(self, symbol: str):
        """
        Fetches market information for a given symbol to get precision details.
        
        Args:
            symbol (str): The trading symbol (e.g., 'ETH/USDC').

        Returns:
            dict: A dictionary containing market data, or None if not found.
        """
        try:
            # The markets should be pre-loaded by the main script, but we can check.
            if not self.exchange.markets:
                await self.exchange.load_markets()
            return self.exchange.market(symbol)
        except Exception as e:
            print(f"[Error] Could not fetch market info for {symbol}: {e}")
            return None

    async def execute_order(self, symbol: str, side: str, amount: float):
        """
        Places a market order on Binance.

        Args:
            symbol (str): The trading symbol (e.g., 'ETH/USDC').
            side (str): The order side, 'buy' or 'sell'.
            amount (float): The quantity of the asset to trade.

        Returns:
            dict: The order details from ccxt if successful, otherwise None.
        """
        print(f"Attempting to place a {side} order for {amount} {symbol.split('/')[0]} on Binance...")
        try:
            order = await self.exchange.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount
            )
            print(f"✅ Successfully placed market order on Binance.")
            print(f"   - Order ID: {order.get('id')}, Status: {order.get('status')}")
            return order
        except ccxt.NetworkError as e:
            print(f"❌ Binance Network Error: {e}")
            return None
        except ccxt.ExchangeError as e:
            print(f"❌ Binance Exchange Error: {e}")
            return None
        except Exception as e:
            print(f"❌ An unexpected error occurred during order execution: {e}")
            return None

    async def close(self):
        """Closes the exchange connection."""
        print("[Info] Closing CexTrader connection.")
        await self.exchange.close()

# --- Example Usage ---
async def main_example():
    load_dotenv()
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")

    # Initialize with sandbox mode for safety
    trader = CexTrader(api_key=api_key, api_secret=api_secret, sandbox_mode=True)

    # --- Get Market Info ---
    symbol = 'ETH/USDC'
    market = await trader.get_market_info(symbol)
    if market:
        print(f"\nMarket Info for {symbol}:")
        print(f"Amount Precision: {market['precision']['amount']}")
        print(f"Price Precision: {market['precision']['price']}\n")

    # --- Place a market buy order ---
    # Note: Testnet may not have ETH/USDC, use a common pair like BTC/USDT
    symbol_to_trade = 'BTC/USDT'
    amount_to_buy = 0.001
    buy_order = await trader.execute_order(symbol=symbol_to_trade, side='buy', amount=amount_to_buy)
    if buy_order:
        print(f"Buy order details: {buy_order}\n")
    else:
        print("Failed to place buy order.\n")

    # --- Close connection ---
    await trader.close()
    print("--- Script Finished ---")

if __name__ == '__main__':
    import asyncio
    asyncio.run(main_example())