print("--- CLI Module Loaded ---")
import typer
import asyncio
import uvloop

import typer
import asyncio
import uvloop

from src.app import ArbiBotApp
from src.core.config import load_config
from src.core.types import MarketPair
from backtest.datasets import load_dataset
from backtest.simulator import Simulator
from src.strategy.rebalancer import Rebalancer
from src.exchange.binance import BinanceCexClient

app = typer.Typer()

@app.command()
def run():
    """
    啟動實盤交易機器人
    """
    typer.echo("正在啟動實盤交易模式...")
    uvloop.install()
    
    config = load_config()
    bot_app = ArbiBotApp(config)
    
    try:
        asyncio.run(bot_app.start())
    except KeyboardInterrupt:
        typer.echo("收到停止信號，正在優雅關閉...")
    finally:
        typer.echo("機器人已關閉。")


@app.command()
def paper():
    """
    啟動紙上交易模式 (不下單)
    """
    typer.echo("正在啟動紙上交易模式...")
    uvloop.install()
    
    config = load_config()
    # 以 paper 模式初始化 App
    bot_app = ArbiBotApp(config, mode='paper')
    
    try:
        asyncio.run(bot_app.start())
    except KeyboardInterrupt:
        typer.echo("收到停止信號，正在優雅關閉...")
    finally:
        typer.echo("機器人已關閉。")


@app.command()
def rebalance():
    """
    執行一次性的資產再平衡檢查與操作。
    """
    typer.echo("正在執行資產再平衡...")
    uvloop.install()
    config = load_config()
    
    async def main():
        # 為再平衡任務建立專用的 CEX 客戶端
        pairs = [MarketPair(
            base=p.base, 
            quote=p.quote, 
            symbol=p.cex_symbol, 
            dex_chain=p.dex_chain, 
            dex_pool_fee=p.dex_pool_fee
        ) for p in config.pairs]
        cex_client = BinanceCexClient(config.cex, config.secrets, pairs)
        await cex_client.connect()
        
        rebalancer = Rebalancer(config, cex_client)
        await rebalancer.run_rebalance_check()
        
        await cex_client.close()

    try:
        asyncio.run(main())
    except Exception as e:
        typer.echo(f"執行再平衡時發生錯誤: {e}", err=True)
    finally:
        typer.echo("再平衡任務結束。")


@app.command()
def backtest(dataset_path: str = typer.Option(..., "--dataset-path", help="歷史數據集的路徑 (.csv)")):
    """
    根據歷史數據進行回測
    """
    typer.echo(f"準備執行回測，使用數據集: {dataset_path}")
    uvloop.install()
    
    try:
        # 載入設定與數據
        config = load_config()
        dataset = load_dataset(dataset_path)

        # 初始化並執行模擬器
        simulator = Simulator(config, dataset)
        asyncio.run(simulator.run())

        # 產生報告
        simulator.report()

    except Exception as e:
        typer.echo(f"回測過程中發生錯誤: {e}", err=True)

if __name__ == "__main__":
    app()
